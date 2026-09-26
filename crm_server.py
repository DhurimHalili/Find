#!/usr/bin/env python3
"""
Roblox Acquisition CRM -- localhost web console for the scraper's pipeline.

- Serves the CRM UI (crm.html) and a JSON API on http://127.0.0.1:8780 ONLY.
- Syncs games from results_history.csv / curated_list.csv (written by the watcher's
  shell redirects). New scan passes appear here automatically on Sync.
- CRM data persists in crm_data.json (fixed literal path inside this folder).
- Deleted games enter a cooldown (default 7 days): sync skips them until it expires,
  then they are re-admitted automatically if the watcher finds them again.
- Outbound requests (per-game Check / Add by URL) go through roblox_finder's enrichment
  with a host allowlist guard: https only, no localhost/loopback/private/reserved hosts.

Run:  python crm_server.py        (then open http://127.0.0.1:8780)
"""

import csv
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import winreg  # Windows only: reads ROBLOSECURITY from HKCU\Environment
except ImportError:  # Linux/cloud: cookie comes from the ROBLOSECURITY env var
    winreg = None

import roblox_finder as rf

HOST, PORT = "127.0.0.1", 8780
START_TS = time.time()
GOLDEN_WINDOWS = rf.parse_windows("12-16,20-2")   # mirrors the watcher's default schedule

STATUSES = ["new", "contacted", "awaiting", "negotiating", "delayed", "acquired", "rejected", "done"]
INT_FIELDS = {"priority_score", "active", "peak_active_seen", "visits", "favorites", "likes",
              "dislikes", "like_ratio", "heat_active_per_1k_visits", "age_days",
              "discord_members", "discord_online", "group_members", "max_players"}

# ---------------------------------------------------------------- security guard
ALLOWED_HOSTS = {"roblox.com", "www.roblox.com", "apis.roblox.com", "games.roblox.com",
                 "groups.roblox.com", "users.roblox.com", "discord.com"}


def guard(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("https",):
        raise ValueError(f"rejected scheme: {p.scheme}")
    if p.username or p.password:
        raise ValueError("rejected URL with credentials")
    if (p.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError(f"rejected host: {p.hostname}")
    return url


# ---------------------------------------------------------------- data store
_lock = threading.RLock()
DATA = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_data():
    global DATA
    if os.path.exists("crm_data.json"):
        with open("crm_data.json", encoding="utf-8") as f:
            DATA = json.load(f)
    else:
        DATA = {}
    DATA.setdefault("games", {})
    DATA.setdefault("deleted", {})
    DATA.setdefault("radar", {})
    DATA.setdefault("nodiscord", {})
    DATA.setdefault("radar_rejected", {})
    DATA.setdefault("activity", [])
    DATA.setdefault("settings", {"cooldown_days": 3})
    DATA.setdefault("meta", {"created": utc_now(), "last_sync": None})


def save_data():
    """Persist to the fixed data file (literal path, low-level write)."""
    with _lock:
        blob = json.dumps(DATA, ensure_ascii=False, indent=1).encode("utf-8")
        fd = os.open("crm_data.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)


def log_act(t, msg):
    DATA["activity"].insert(0, {"ts": utc_now(), "type": t, "msg": msg})
    del DATA["activity"][500:]


# ---- permanent rejection (never searched, never re-imported) -----------------
FOREVER = "9999-12-31T00:00:00+00:00"


def mark_permanent_skip(uid):
    """Write a never-expiring entry into the watcher's scan ledger so the game
    is skipped in discovery forever."""
    try:
        with open("seen_ledger.json", encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        ledger = {}
    ledger[str(uid)] = FOREVER
    blob = json.dumps(ledger, ensure_ascii=False).encode("utf-8")
    fd = os.open("seen_ledger.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, blob)
    finally:
        os.close(fd)


def clear_permanent_skip(uid):
    try:
        with open("seen_ledger.json", encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        return
    if str(uid) in ledger and ledger[str(uid)] == FOREVER:
        del ledger[str(uid)]
        blob = json.dumps(ledger, ensure_ascii=False).encode("utf-8")
        fd = os.open("seen_ledger.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)


# ---------------------------------------------------------------- guarded client
class GuardedClient(rf.Client):
    def get(self, url, params=None, retries=6, key=None):
        guard(url)
        return super().get(url, params=params, retries=retries, key=key)


def guard_discord_session():
    orig = rf._discord_session.get

    def wrapped(url, *a, **kw):
        return orig(guard(url), *a, **kw)
    rf._discord_session.get = wrapped


def make_client():
    # Env var first (Linux/cloud + explicit overrides), Windows registry second.
    cookie = os.environ.get("ROBLOSECURITY")
    if not cookie and winreg is not None:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
            cookie, _ = winreg.QueryValueEx(k, "ROBLOSECURITY")
        except OSError:
            cookie = None
    client = GuardedClient(cookie=cookie, delay=0.4)
    orig = client.s.get
    client.s.get = lambda url, *a, **kw: orig(guard(url), *a, **kw)
    return client


def enrich_one(client, uid):
    """Full enrichment for one universe id, reusing the scraper's exact logic."""
    uid = int(uid)
    det = rf.fetch_details(client, [uid]).get(uid)
    if not det:
        return None
    visits, active = det.get("visits") or 0, det.get("playing") or 0
    votes = rf.fetch_votes(client, [uid])
    up, down = votes.get(uid, (0, 0))
    # Same ownership-gated evidence as the watcher: fan/member groups are
    # never scanned, invites verify in trust order with provenance recorded.
    ev = rf.collect_social_evidence(client, uid, det)
    ctype, cid, cname = ev["ctype"], ev["cid"], ev["cname"]
    creator_url, group_name = ev["creator_url"], ev["group_name"]
    group_members = ev["group_members"]
    owner_name, owner_id, owner_url = ev["owner_name"], ev["owner_id"], ev["owner_url"]
    owner_roblox_signal = ev["owner_roblox_signal"]
    discord_name_signal = ev["discord_name_signal"]

    socials, prov = rf.extract_socials(ev["labeled_texts"], ev["labeled_links"])
    discord_info = {"valid": False, "name": "", "members": "", "online": ""}
    discord_url = ""
    discord_via = ""
    for code in rf.rank_discord_codes(socials["discord"], prov):
        info = rf.verify_discord(code)
        if info["valid"]:
            discord_info, discord_url = info, f"https://discord.gg/{code}"
            discord_via = prov.get(code, "")
            break
    if not discord_url and socials["discord"]:
        first = rf.rank_discord_codes(socials["discord"], prov)[0]
        discord_url = f"https://discord.gg/{first} (UNVERIFIED/expired)"
        discord_via = prov.get(first, "")

    created, updated = rf.parse_dt(det.get("created")), rf.parse_dt(det.get("updated"))
    age_days = (datetime.now(timezone.utc) - created).days if created else ""
    tier = rf.classify(visits, active)
    heat = round(active / visits * 1000, 2) if visits else 0
    has_discord = bool(discord_info["valid"])
    score = 0 if not tier else (rf.TIER_WEIGHT[tier] + (200 if has_discord else 0)
                                + min(heat * 5, 200)
                                + (100 if isinstance(age_days, int) and age_days <= 30 else 0)
                                + (min(int(discord_info["members"] or 0) / 50, 100) if has_discord else 0))
    return {
        "priority_score": round(score), "tier": tier,
        "title": det.get("name", ""), "description": (det.get("description") or "")[:2000],
        "game_url": f"https://www.roblox.com/games/{det.get('rootPlaceId')}/",
        "has_discord": "YES" if has_discord else "",
        "discord_url": discord_url, "discord_server": discord_info["name"],
        "discord_members": discord_info["members"], "discord_online": discord_info["online"],
        "discord_via": discord_via,
        "active": active, "peak_active_seen": max(active, 0), "visits": visits,
        "favorites": det.get("favoritedCount") or 0, "likes": up, "dislikes": down,
        "like_ratio": round(up / (up + down) * 100, 1) if (up + down) else "",
        "heat_active_per_1k_visits": heat, "age_days": age_days,
        "created": created.strftime("%Y-%m-%d") if created else "",
        "last_updated": updated.strftime("%Y-%m-%d") if updated else "",
        "creator_type": ctype, "creator_name": cname, "creator_url": creator_url,
        "group_members": group_members, "owner_name": owner_name, "owner_url": owner_url,
        "owner_roblox_signal": owner_roblox_signal,
        "discord_name_signal": discord_name_signal,
        "youtube": " | ".join(socials["youtube"]), "tiktok": " | ".join(socials["tiktok"]),
        "twitter_x": " | ".join(socials["twitter"]), "twitch": " | ".join(socials["twitch"]),
        "instagram": " | ".join(socials["instagram"]), "other_links": " | ".join(socials["other"]),
        "genre": det.get("genre", ""), "max_players": det.get("maxPlayers", ""),
        "universe_id": str(uid), "place_id": det.get("rootPlaceId"),
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }


# ---------------------------------------------------------------- CSV sync
def parse_multiblock_csv(name):
    """results_history.csv accumulates one CSV block per pass (repeated headers).
    csv.reader handles quoted multiline fields; header rows reset the column map.
    Only literal, allowlisted files inside this folder are ever opened.
    Missing files (fresh install, watcher not run yet) yield zero rows."""
    if name == "results_history.csv":
        path = "results_history.csv"
    elif name == "curated_list.csv":
        path = "curated_list.csv"
    elif name == "nodiscord_history.csv":
        path = "nodiscord_history.csv"
    else:
        raise ValueError(f"file not allowlisted: {name}")
    try:
        fh = open(path, encoding="utf-8-sig", newline="")
    except FileNotFoundError:
        return []
    rows, header = [], None
    with fh:
        for rec in csv.reader(fh):
            if not rec or not rec[0].strip():
                continue
            # lstrip BOM: every process restart re-writes it mid-file (first_pass)
            if rec[0].lstrip("\ufeff").strip().lower() == "priority_score":
                header = [c.lstrip("\ufeff") for c in rec]
                continue
            if header and len(rec) == len(header):
                rows.append(dict(zip(header, rec)))
    return rows


def to_i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def cooldown_end(deleted_at_iso):
    days = DATA["settings"].get("cooldown_days", 7)
    try:
        d = datetime.fromisoformat(deleted_at_iso)
    except (TypeError, ValueError):
        d = datetime.now(timezone.utc)
    return d + timedelta(days=days)


def merge_source_rows():
    """Merge all CSV blocks from all sources; newest checked_at wins per universe."""
    best, excluded = {}, 0
    for name in ("results_history.csv", "curated_list.csv"):
        try:
            rows = parse_multiblock_csv(name)
        except (OSError, ValueError):
            continue
        for r in rows:
            uid = to_i(r.get("universe_id"))
            if not uid:
                continue
            if rf.is_excluded(r.get("title", ""), r.get("description", "")):
                excluded += 1
                continue
            old = best.get(uid)
            if not old or (r.get("checked_at_utc", "") > old.get("checked_at_utc", "")):
                best[uid] = r
    return best, excluded


def parse_radar_csv():
    """Near-miss feed written by the watcher (--watchlist-file). Same multiblock
    scheme as the match files: repeated 'title,...' headers reset the map.
    Missing file (watcher flag not enabled yet) yields zero rows."""
    try:
        fh = open("watchlist_history.csv", encoding="utf-8-sig", newline="")
    except FileNotFoundError:
        return []
    rows, header = [], None
    with fh:
        for rec in csv.reader(fh):
            if not rec or not rec[0].strip():
                continue
            if rec[0].lstrip("\ufeff").strip().lower() == "title":
                header = [c.lstrip("\ufeff") for c in rec]
                continue
            if header and len(rec) == len(header):
                rows.append(dict(zip(header, rec)))
    return rows


def do_radar_sync(now):
    """Fold the near-miss feed into DATA['radar']: one entry per universe with
    first/last CCU and peak, so risers ('we never know') are visible. Games
    already in the pipeline, rejected-frozen, or on delete-cooldown stay out."""
    try:
        rows = parse_radar_csv()
    except (OSError, ValueError):
        rows = []
    touched = 0
    for r in rows:
        uid = str(to_i(r.get("universe_id")))
        if uid == "0" or uid in DATA["games"]:
            continue
        if uid in DATA.get("radar_rejected", {}):
            continue   # permanently rejected: never scanned, never re-imported
        if uid in DATA["deleted"] and now < cooldown_end(DATA["deleted"][uid]["deleted_at"]):
            continue
        ts = r.get("checked_at_utc", "")
        active, visits = to_i(r.get("active")), to_i(r.get("visits"))
        e = DATA["radar"].get(uid)
        if e is None:
            DATA["radar"][uid] = {"universe_id": uid, "title": r.get("title", uid),
                                  "game_url": r.get("game_url", ""),
                                  "creator": r.get("creator", ""),
                                  "active": active, "first_active": active,
                                  "peak_active": active, "visits": visits,
                                  "sightings": 1, "first_seen": ts, "last_seen": ts}
            touched += 1
        elif ts >= e.get("last_seen", ""):
            e.update({"title": r.get("title", "") or e["title"],
                      "game_url": r.get("game_url", "") or e["game_url"],
                      "creator": r.get("creator", "") or e["creator"],
                      "active": active, "visits": visits,
                      "peak_active": max(e.get("peak_active", 0), active),
                      "sightings": e.get("sightings", 1) + 1, "last_seen": ts})
            touched += 1
    # promoted to the pipeline -> leave the radar
    for uid in [u for u in DATA["radar"] if u in DATA["games"]]:
        del DATA["radar"][uid]
    return touched


def do_nodiscord_sync(now):
    """Fold the qualified-but-no-presence feed into DATA['nodiscord']: newest
    row wins per universe. Same exclusions as the radar (pipeline, rejected,
    cooldown, do-not-buy). Games that later gain a presence graduate via the
    normal match feed; direct Track adds promote them immediately."""
    try:
        rows = parse_multiblock_csv("nodiscord_history.csv")
    except (OSError, ValueError):
        rows = []
    best = {}
    for r in rows:
        uid = str(to_i(r.get("universe_id")))
        if uid == "0":
            continue
        old = best.get(uid)
        if not old or (r.get("checked_at_utc", "") > old.get("checked_at_utc", "")):
            best[uid] = r
    touched = 0
    for uid, r in best.items():
        if uid in DATA["games"]:
            continue
        if uid in DATA.get("radar_rejected", {}):
            continue
        if uid in DATA["deleted"] and now < cooldown_end(DATA["deleted"][uid]["deleted_at"]):
            continue
        if rf.is_excluded(r.get("title", ""), r.get("description", "")):
            continue
        ts = r.get("checked_at_utc", "")
        e = DATA["nodiscord"].get(uid)
        if e is None:
            DATA["nodiscord"][uid] = {"universe_id": uid, "title": r.get("title", uid),
                                      "game_url": r.get("game_url", ""),
                                      "creator": r.get("creator_name", ""),
                                      "tier": r.get("tier", ""),
                                      "active": to_i(r.get("active")),
                                      "visits": to_i(r.get("visits")),
                                      "age_days": to_i(r.get("age_days")),
                                      "sightings": 1,
                                      "first_seen": ts, "last_seen": ts}
            touched += 1
        elif ts >= e.get("last_seen", ""):
            e.update({"title": r.get("title", "") or e["title"],
                      "game_url": r.get("game_url", "") or e["game_url"],
                      "creator": r.get("creator_name", "") or e["creator"],
                      "tier": r.get("tier", "") or e["tier"],
                      "active": to_i(r.get("active")), "visits": to_i(r.get("visits")),
                      "age_days": to_i(r.get("age_days")),
                      "sightings": e.get("sightings", 1) + 1, "last_seen": ts})
            touched += 1
    for uid in [u for u in DATA["nodiscord"] if u in DATA["games"]]:
        del DATA["nodiscord"][uid]
    return touched


def do_sync():
    with _lock:
        best, excluded = merge_source_rows()
        now = datetime.now(timezone.utc)
        imported = updated = cooling = frozen = kept = 0
        for uid, r in best.items():
            uid = str(uid)   # all lookups/keys are string universe ids
            if uid in DATA["deleted"]:
                if now < cooldown_end(DATA["deleted"][uid]["deleted_at"]):
                    cooling += 1
                    continue
                title = DATA["deleted"][uid].get("title", uid)
                del DATA["deleted"][uid]
                log_act("sync", f"'{title}' cooldown expired -- re-admitted to pipeline")
            if uid in DATA["games"] and DATA["games"][uid].get("status") in ("rejected", "done"):
                frozen += 1   # rejected/done games are frozen: sync never touches them again
                continue
            if uid in DATA.get("radar_rejected", {}):
                frozen += 1   # radar-rejected: same promise, never imported either
                continue
            crm_fields = {"status": "new", "notes": "", "added_at": utc_now(),
                          "contacted_at": None, "status_history": []}
            if uid in DATA["games"]:
                g = DATA["games"][uid]
                crm_fields = {k: g.get(k) for k in crm_fields}
                # NEWEST-WINS: a manual Check / Refresh-all newer than this CSV
                # row must never be clobbered by stale watcher data. Keep the
                # fresh stats, just fold in the best peak ever seen.
                if (g.get("checked_at_utc") or "") >= (r.get("checked_at_utc") or "") \
                        and g.get("checked_at_utc"):
                    kept += 1
                    g["peak_active_seen"] = max(to_i(g.get("peak_active_seen")),
                                                to_i(r.get("peak_active_seen")),
                                                to_i(r.get("active")))
                    g["tier"] = rf.classify(g.get("visits") or 0, g.get("active") or 0)
                    DATA["games"][uid] = g
                    continue
                updated += 1
            else:
                imported += 1
                log_act("sync", f"new: '{r.get('title', uid)}' ({r.get('tier', '?')})")
            row = dict(r)
            row["universe_id"] = uid
            for f in INT_FIELDS:
                row[f] = to_i(r.get(f))
            # peak must never go backwards on sync: keep the best CCU ever seen
            # across all passes (old stored peak vs incoming row).
            prev_peak = to_i(DATA["games"].get(uid, {}).get("peak_active_seen"))
            row["peak_active_seen"] = max(row.get("peak_active_seen") or 0,
                                          row.get("active") or 0, prev_peak)
            crm_fields["status_history"] = crm_fields.get("status_history") or []
            row.update(crm_fields)
            # keep the tier label honest against the row's own numbers: games that
            # grew out of their tier show as unranked until a fresh Check re-qualifies them
            row["tier"] = rf.classify(row.get("visits") or 0, row.get("active") or 0)
            DATA["games"][uid] = row
        radar_touched = do_radar_sync(now)
        nodiscord_touched = do_nodiscord_sync(now)
        # Self-heal: re-assert permanent skips for everything rejected, so a
        # clobbered or older ledger (e.g. the cloud overwrote it before your
        # push went up) can never resurrect a banished game at watcher level.
        healed = 0
        try:
            with open("seen_ledger.json", encoding="utf-8") as f:
                ledger = json.load(f)
        except (OSError, ValueError):
            ledger = {}
        for u, g in DATA["games"].items():
            if (g.get("status") or "new") in ("rejected", "done") and ledger.get(str(u)) != FOREVER:
                ledger[str(u)] = FOREVER
                healed += 1
        for u in DATA.get("radar_rejected", {}):
            if ledger.get(str(u)) != FOREVER:
                ledger[str(u)] = FOREVER
                healed += 1
        if healed:
            blob = json.dumps(ledger, ensure_ascii=False).encode("utf-8")
            fd = os.open("seen_ledger.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
            log_act("sync", f"re-sealed {healed} permanent skip(s) in the scan ledger")
        DATA["meta"]["last_sync"] = utc_now()
        log_act("sync", f"sync done: +{imported} new, {updated} updated, {kept} kept fresh, "
                        f"{cooling} skipped (cooldown), {frozen} rejected-frozen, "
                        f"{excluded} do-not-buy rows ignored, radar +{radar_touched}, "
                        f"no-discord +{nodiscord_touched}")
        save_data()
        return {"imported": imported, "updated": updated, "cooling": cooling,
                "frozen": frozen, "excluded": excluded, "radar": radar_touched,
                "nodiscord": nodiscord_touched,
                "kept": kept, "total": len(DATA["games"])}


# ---------------------------------------------------------------- API handlers
def watcher_status():
    """Live watcher telemetry from finder.log -- what is it doing right now?"""
    info = {"last_line": "", "log_age_s": None, "csv_age_s": None}
    try:
        info["log_age_s"] = int(time.time() - os.path.getmtime("finder.log"))
        with open("finder.log", encoding="utf-8", errors="replace") as f:
            lines = [l.strip() for l in f.read()[-4000:].splitlines() if l.strip()]
        info["last_line"] = lines[-1][:200] if lines else ""
    except OSError:
        pass
    try:
        info["csv_age_s"] = int(time.time() - os.path.getmtime("results_history.csv"))
    except OSError:
        pass
    return info


def api_state():
    with _lock:
        now = datetime.now()
        nxt = rf.next_window_start(GOLDEN_WINDOWS, now) if not rf.in_window(GOLDEN_WINDOWS, now) else None
        return {"games": list(DATA["games"].values()), "deleted": [
            {"universe_id": to_i(uid), **d, "eligible_at": cooldown_end(d["deleted_at"]).isoformat()}
            for uid, d in DATA["deleted"].items()],
            "radar": sorted(DATA.get("radar", {}).values(),
                            key=lambda e: (-(e.get("active") or 0), -(e.get("peak_active") or 0))),
            "nodiscord": sorted(DATA.get("nodiscord", {}).values(),
                                key=lambda e: (-(e.get("active") or 0), -(e.get("visits") or 0))),
            "radar_rejected": sorted(DATA.get("radar_rejected", {}).items()),
            "settings": DATA["settings"], "activity": DATA["activity"][:200],
            "meta": DATA["meta"], "statuses": STATUSES,
            "runtime": {
                "crm_uptime_s": int(time.time() - START_TS),
                "golden_windows": "12-16, 20-02",
                "in_window": rf.in_window(GOLDEN_WINDOWS, now),
                "next_window": nxt.isoformat() if nxt else None,
                "watcher": watcher_status(),
            }}


def api_status(body):
    uid = str(to_i(body.get("universe_id")))
    status = body.get("status")
    if status not in STATUSES or uid not in DATA["games"]:
        return {"error": "bad status or unknown game"}
    with _lock:
        g = DATA["games"][uid]
        old = g.get("status", "new")
        if old != status:
            g["status"] = status
            g.setdefault("status_history", []).append(
                {"ts": utc_now(), "from": old, "to": status})
            if status == "contacted" and not g.get("contacted_at"):
                g["contacted_at"] = utc_now()
            elif status == "new":
                # reset to untouched: a misclick-undo (or a fresh approach)
                # leaves no stale "contacted ..." trace behind
                g["contacted_at"] = None
            if status in ("rejected", "done"):
                mark_permanent_skip(uid)
                how = ("REJECTED -- permanently excluded from scanning" if status == "rejected"
                       else "marked DONE -- archived, never scanned again")
                log_act("status", f"'{g.get('title', uid)}' {how}")
            else:
                if old in ("rejected", "done"):
                    clear_permanent_skip(uid)
                log_act("status", f"'{g.get('title', uid)}': {old} -> {status}")
            save_data()
        return {"ok": True, "status": status}


def api_notes(body):
    uid = str(to_i(body.get("universe_id")))
    if uid not in DATA["games"]:
        return {"error": "unknown game"}
    with _lock:
        g = DATA["games"][uid]
        g["notes"] = (body.get("notes") or "")[:5000]
        save_data()
        return {"ok": True}


def api_delete(body):
    uid = str(to_i(body.get("universe_id")))
    if uid not in DATA["games"]:
        return {"error": "unknown game"}
    with _lock:
        g = DATA["games"][uid]
        if (g.get("status") or "new") in ("rejected", "done"):
            return {"error": "permanent (rejected/done) -- change its status first if you really want it back in rotation"}
        g = DATA["games"].pop(uid)
        title = g.get("title", uid)
        DATA["deleted"][uid] = {"title": title, "deleted_at": utc_now(), "row": g}
        log_act("delete", f"deleted '{title}' -- can return after "
                          f"{DATA['settings'].get('cooldown_days', 7)} days")
        save_data()
        return {"ok": True}


def api_delete_all(body):
    """Bulk delete: moves every listed game to cooldown-deleted. The client
    sends exactly the universe_ids it shows (filtered view), so the user
    deletes precisely what they see. Rejected/done games are permanent, not
    cooldown deletes, so they are left untouched."""
    ids = body.get("universe_ids") or []
    if not isinstance(ids, list):
        return {"error": "universe_ids must be a list"}
    with _lock:
        n = 0
        for raw in ids[:5000]:
            uid = str(to_i(raw))
            g = DATA["games"].get(uid)
            if not g or (g.get("status") or "new") in ("rejected", "done"):
                continue
            DATA["games"].pop(uid)
            DATA["deleted"][uid] = {"title": g.get("title", uid),
                                    "deleted_at": utc_now(), "row": g}
            n += 1
        if n:
            log_act("delete", f"bulk delete: {n} game(s) removed -- can return after "
                              f"{DATA['settings'].get('cooldown_days', 7)} days")
            save_data()
        return {"ok": True, "deleted": n}


def api_radar_clear(body):
    """Empty the Early Radar feed (it refills on the next watcher pass)."""
    with _lock:
        n = len(DATA.get("radar", {}))
        DATA["radar"] = {}
        if n:
            log_act("purge", f"cleared early radar ({n} entries)")
            save_data()
        return {"ok": True, "cleared": n}


def api_radar_reject(body):
    """Permanently reject a radar OR no-discord game: leaves its list, can
    never be re-scanned (FOREVER ledger entry) nor re-imported by sync."""
    uid = str(to_i(body.get("universe_id")))
    with _lock:
        e = DATA.get("radar", {}).pop(uid, None)
        if e is None:
            e = DATA.get("nodiscord", {}).pop(uid, None)
        if e is None:
            return {"error": "not on the radar"}
        DATA.setdefault("radar_rejected", {})[uid] = {
            "title": e.get("title", uid), "rejected_at": utc_now(),
            "row": e}
        mark_permanent_skip(uid)
        log_act("status", f"'{e.get('title', uid)}' REJECTED -- permanently excluded from scanning")
        save_data()
        return {"ok": True}


def api_radar_unreject(body):
    uid = str(to_i(body.get("universe_id")))
    with _lock:
        if uid not in DATA.get("radar_rejected", {}):
            return {"error": "not rejected"}
        title = DATA["radar_rejected"].pop(uid).get("title", uid)
        clear_permanent_skip(uid)
        log_act("status", f"radar '{title}' un-rejected -- eligible for scanning again")
        save_data()
        return {"ok": True}


def api_restore(body):
    uid = str(to_i(body.get("universe_id")))
    if uid not in DATA["deleted"]:
        return {"error": "not in deleted"}
    with _lock:
        d = DATA["deleted"].pop(uid)
        row = d.get("row") or {"title": d.get("title", uid), "universe_id": to_i(uid)}
        # keep notes + status history; just re-enter the pipeline as fresh
        row.update({"status": "new", "added_at": utc_now(), "contacted_at": None})
        DATA["games"][uid] = row
        log_act("restore", f"restored '{row.get('title', uid)}'")
        save_data()
        return {"ok": True}


def api_purge(body):
    uid = str(to_i(body.get("universe_id")))
    force = bool(body.get("force"))
    with _lock:
        if uid not in DATA["deleted"]:
            return {"error": "not in deleted"}
        if not force and datetime.now(timezone.utc) < cooldown_end(DATA["deleted"][uid]["deleted_at"]):
            return {"error": "cooldown still active (purge unlocks when it expires)"}
        title = DATA["deleted"][uid].get("title", uid)
        del DATA["deleted"][uid]
        log_act("purge", f"permanently removed '{title}'")
        save_data()
        return {"ok": True}


def api_purge_expired():
    with _lock:
        now = datetime.now(timezone.utc)
        expired = [uid for uid, d in DATA["deleted"].items() if now >= cooldown_end(d["deleted_at"])]
        for uid in expired:
            title = DATA["deleted"][uid].get("title", uid)
            del DATA["deleted"][uid]
            log_act("purge", f"purged expired delete record '{title}'")
        if expired:
            save_data()
        return {"ok": True, "purged": len(expired)}


def api_add(body):
    url = (body.get("url") or "").strip()
    m = re.search(r"/games/(\d+)", url) or re.fullmatch(r"\d+", url)
    if not url or not m:
        return {"error": "paste a roblox.com/games/<id> link or a place id"}
    client = make_client()
    pid = m.group(1)
    res = client.get(f"https://apis.roblox.com/universes/v1/places/{pid}/universe")
    if not res or not res.get("universeId"):
        return {"error": "could not resolve that place to a game (check the link)"}
    uid = str(res["universeId"])
    with _lock:
        if uid in DATA["games"]:
            return {"error": f"already tracked: '{DATA['games'][uid].get('title')}'"}
        if uid in DATA["deleted"]:
            if datetime.now(timezone.utc) < cooldown_end(DATA["deleted"][uid]["deleted_at"]):
                return {"error": "this game was deleted and is still on cooldown"}
            del DATA["deleted"][uid]
    row = enrich_one(client, to_i(uid))
    if not row:
        return {"error": "Roblox returned no details for that game"}
    reason = rf.is_excluded(row.get("title", ""), row.get("description", ""))
    if reason:
        return {"error": f"blocked by do-not-buy rules ({reason})"}
    with _lock:
        row.update({"status": "new", "notes": "", "added_at": utc_now(),
                    "contacted_at": None, "status_history": [], "found_via": "manual"})
        DATA["games"][uid] = row
        DATA.get("radar", {}).pop(uid, None)   # promoted from Early Radar -> pipeline
        DATA.get("nodiscord", {}).pop(uid, None)   # promoted from No Discord -> pipeline
        log_act("add", f"added manually: '{row.get('title')}' ({row.get('tier') or 'unranked'})")
        save_data()
        return {"ok": True, "universe_id": uid, "title": row.get("title"),
                "tier": row.get("tier")}


def api_check(body):
    uid = str(to_i(body.get("universe_id")))
    if uid == "0" or uid not in DATA["games"]:
        return {"error": "unknown game"}
    client = make_client()
    row = enrich_one(client, to_i(uid))
    if not row:
        return {"error": "Roblox returned no details"}
    with _lock:
        g = DATA["games"][uid]
        crm = {k: g.get(k) for k in ("status", "notes", "added_at", "contacted_at",
                                     "status_history", "found_via")}
        row.update(crm)
        # never lose the best CCU: a refresh during a trough must not erase peak
        row["peak_active_seen"] = max(to_i(row.get("peak_active_seen")),
                                      to_i(row.get("active")),
                                      to_i(g.get("peak_active_seen")))
        DATA["games"][uid] = row
        log_act("check", f"refreshed '{row.get('title', uid)}': "
                         f"{row.get('active')} active / {row.get('visits')} visits")
        save_data()
        return {"ok": True}


# ---------------------------------------------------------------- bulk refresh
# "Refresh all": re-enriches every tracked game with live Roblox data in a
# background thread (full enrich_one quality: stats, owner, Discord verify).
# Stalest-first so an interruption still fixes the worst rows. Progress is
# polled via /api/refresh_status; only one job runs at a time.
_REFRESH = {"running": False, "total": 0, "done": 0, "current": "", "errors": 0,
            "started": None, "finished": None}
_REFRESH_LOCK = threading.Lock()


def _refresh_snapshot():
    with _REFRESH_LOCK:
        return dict(_REFRESH)


def api_refresh_all(body):
    with _REFRESH_LOCK:
        if _REFRESH["running"]:
            return {"ok": True, "already": True, **dict(_REFRESH)}
    ids = body.get("universe_ids")
    with _lock:
        if isinstance(ids, list) and ids:
            wanted = {str(to_i(x)) for x in ids}
            uids = [u for u in DATA["games"] if u in wanted]
        else:
            uids = list(DATA["games"].keys())
        uids = [u for u in uids if (DATA["games"][u].get("status") or "new") not in ("rejected", "done")]
        uids.sort(key=lambda u: DATA["games"][u].get("checked_at_utc", "") or "")
    with _REFRESH_LOCK:
        _REFRESH.update({"running": True, "total": len(uids), "done": 0,
                         "current": "", "errors": 0,
                         "started": utc_now(), "finished": None})
    threading.Thread(target=_refresh_worker, args=(uids,), daemon=True).start()
    return {"ok": True, "total": len(uids)}


def _refresh_worker(uids):
    client = make_client()
    for uid in uids:
        with _lock:
            g = DATA["games"].get(uid)
            title = (g or {}).get("title", uid)
        with _REFRESH_LOCK:
            _REFRESH["current"] = title
        try:
            row = enrich_one(client, to_i(uid))
        except Exception:
            row = None
        with _lock:
            g = DATA["games"].get(uid)
            if g and row:
                crm = {k: g.get(k) for k in ("status", "notes", "added_at",
                                             "contacted_at", "status_history",
                                             "found_via")}
                row.update(crm)
                row["peak_active_seen"] = max(to_i(row.get("peak_active_seen")),
                                              to_i(row.get("active")),
                                              to_i(g.get("peak_active_seen")))
                DATA["games"][uid] = row
                save_data()
            with _REFRESH_LOCK:
                _REFRESH["done"] += 1
                if not row:
                    _REFRESH["errors"] += 1
    with _lock:
        log_act("refresh", f"bulk refresh finished: {_REFRESH['done']} games, "
                           f"{_REFRESH['errors']} failed")
        save_data()
    with _REFRESH_LOCK:
        _REFRESH.update({"running": False, "current": "", "finished": utc_now()})


def api_refresh_status(body):
    return {"ok": True, **_refresh_snapshot()}


# ---------------------------------------------------------------- discord scan
# "Scan discords": background full enrichment of radar / no-discord entries
# (stats + owner + live Discord verify). Entries with an already-live Discord
# are skipped; everything saves per entry, so stopping the CRM mid-run loses
# nothing -- pressing the button again resumes with the remaining stale ones.
_SCAN = {"running": False, "total": 0, "done": 0, "current": "",
         "errors": 0, "found": 0, "started": None, "finished": None, "scope": ""}
_SCAN_LOCK = threading.Lock()


def _scan_snapshot():
    with _SCAN_LOCK:
        return dict(_SCAN)


def api_radar_scan(body):
    scope = (body.get("scope") or "radar").strip().lower()
    if scope not in ("radar", "nodiscord", "all"):
        return {"error": "scope must be radar, nodiscord or all"}
    with _SCAN_LOCK:
        if _SCAN["running"]:
            return {"ok": True, "already": True, **dict(_SCAN)}
    ids = body.get("universe_ids")
    with _lock:
        pools = []
        if scope in ("radar", "all"):
            pools.append("radar")
        if scope in ("nodiscord", "all"):
            pools.append("nodiscord")
        jobs = []
        for pool in pools:
            for uid, e in DATA.get(pool, {}).items():
                if isinstance(ids, list) and ids and str(to_i(uid)) not in \
                        {str(to_i(x)) for x in ids}:
                    continue
                if (e.get("has_discord") == "YES" and e.get("discord_url")
                        and "UNVERIFIED" not in str(e.get("discord_url"))):
                    continue   # live discord already known; nothing to gain
                jobs.append((pool, str(uid)))
        # stalest first so interruptions still fix the worst rows
        jobs.sort(key=lambda j: DATA.get(j[0], {}).get(j[1], {}).get("last_seen", "") or "")
    with _SCAN_LOCK:
        _SCAN.update({"running": True, "total": len(jobs), "done": 0,
                      "current": "", "errors": 0, "found": 0,
                      "started": utc_now(), "finished": None, "scope": scope})
    threading.Thread(target=_scan_worker, args=(jobs,), daemon=True).start()
    return {"ok": True, "total": len(jobs), "scope": scope}


def _scan_worker(jobs):
    client = make_client()
    try:
        client.delay = 0.2
        client.limiter = rf.RateLimiter(1.0 / 0.2)
    except Exception:
        pass
    for pool, uid in jobs:
        with _lock:
            e = DATA.get(pool, {}).get(uid)
            title = (e or {}).get("title", uid)
        with _SCAN_LOCK:
            _SCAN["current"] = title
        try:
            row = enrich_one(client, to_i(uid))
        except Exception:
            row = None
        with _lock:
            e = DATA.get(pool, {}).get(uid)
            if e and row:
                e.update({"title": row.get("title") or e.get("title"),
                          "game_url": row.get("game_url") or e.get("game_url"),
                          "creator": row.get("creator_name") or e.get("creator"),
                          "active": row.get("active", e.get("active")),
                          "visits": row.get("visits", e.get("visits")),
                          "peak_active": max(to_i(e.get("peak_active")),
                                             to_i(row.get("active"))),
                          "has_discord": "YES" if row.get("has_discord") == "YES" else "",
                          "discord_url": row.get("discord_url", ""),
                          "discord_server": row.get("discord_server", ""),
                          "discord_members": row.get("discord_members", ""),
                          "discord_online": row.get("discord_online", ""),
                          "discord_via": row.get("discord_via", ""),
                          "sightings": e.get("sightings", 1) + 1,
                          "last_seen": row.get("checked_at_utc") or e.get("last_seen")})
                if e["has_discord"]:
                    with _SCAN_LOCK:
                        _SCAN["found"] += 1
                save_data()
            with _SCAN_LOCK:
                _SCAN["done"] += 1
                if not row:
                    _SCAN["errors"] += 1
    with _lock:
        log_act("scan", f"discord scan finished ({_SCAN['scope']}): {_SCAN['done']} "
                        f"checked, {_SCAN['found']} with live Discord, {_SCAN['errors']} failed")
        save_data()
    with _SCAN_LOCK:
        _SCAN.update({"running": False, "current": "", "finished": utc_now()})


def api_scan_status(body):
    return {"ok": True, **_scan_snapshot()}


DATA_FEED_FILES = ["results_history.csv", "watchlist_history.csv",
                   "nodiscord_history.csv", "seen_ledger.json", "keyword_cursor.json"]


def _union_data_text(fname, old_text, cur_text):
    """Union-merge one data file (old = pre-pull local, cur = fresh tip).
    Same semantics as ci_merge.py: CSVs dedupe by universe+timestamp, ledger
    keeps max expiry (FOREVER wins), cursor keeps the fresh tip."""
    import ci_merge
    if fname == "seen_ledger.json":
        return (ci_merge.union_ledger(old_text, cur_text) if old_text.strip()
                else cur_text)
    if fname == "keyword_cursor.json":
        return cur_text or old_text
    header = ("priority_score" if ("results" in fname or "nodiscord" in fname)
              else "title")
    return (ci_merge.union_csv(old_text, cur_text, header) if old_text.strip()
            else cur_text)


def api_git_pull(body):
    """One-click cloud fetch that tolerates locally-modified data files. Your
    own rejects rewrite seen_ledger.json constantly, so a strict pull would
    refuse almost every time -- instead: set untracked feed files aside, stash
    tracked data dirt, fast-forward, fold everything back in with union
    semantics (never a text merge on appends), then sync. Local CODE edits
    still refuse loudly (resolve in a terminal)."""
    import subprocess

    def run(*a):
        try:
            p = subprocess.run(list(a), capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            return 127, "git is not installed or not on PATH"
        except subprocess.TimeoutExpired:
            return 124, "git command timed out (network?) -- try again"
        return p.returncode, (p.stdout + p.stderr).strip()

    def restore_backup(aside_dir):
        for f in DATA_FEED_FILES:
            src = os.path.join(aside_dir, f)
            if os.path.exists(src):
                shutil.move(src, f)

    rc, ls = run("git", "ls-files")
    tracked = set(ls.split()) if rc == 0 else set()
    aside_dir = tempfile.mkdtemp(prefix="crm-pull-")
    aside_untracked = []
    try:
        for f in DATA_FEED_FILES:   # untracked feed files would block the pull too
            if os.path.exists(f) and f not in tracked:
                shutil.move(f, os.path.join(aside_dir, f))
                aside_untracked.append(f)
        rc, out = run("git", "stash", "push", "-m", "crm-autopull", "--",
                      *[f for f in DATA_FEED_FILES if f in tracked])
        if rc not in (0, 1):
            restore_backup(aside_dir)
            return {"error": f"could not stash local data ({out[:200]}) -- report this."}
        rc, lst = run("git", "stash", "list")
        stashed = "crm-autopull" in lst
        rc, out = run("git", "pull", "--ff-only")
        if rc != 0:
            if stashed:
                run("git", "stash", "pop")   # pull touched nothing, pop restores exactly
            restore_backup(aside_dir)
            hint = ("Local CODE files changed -- resolve it in a terminal, then Sync."
                    if "would be overwritten" in out
                    else "Resolve it in a terminal, then Sync.")
            return {"error": f"pull refused (no merge attempted, nothing changed): {out[:300]} {hint}"}
        if stashed:
            for f in DATA_FEED_FILES:
                p = subprocess.run(["git", "show", "stash@{0}:" + f], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=30)
                old = p.stdout if p.returncode == 0 else ""
                if not old.strip():
                    continue
                try:
                    with open(f, encoding="utf-8-sig") as fh:
                        cur = fh.read()
                except OSError:
                    cur = ""
                merged = _union_data_text(f, old, cur)
                if merged.strip():
                    with open(f, "w", encoding="utf-8", newline="") as fh:
                        fh.write(merged if merged.endswith("\n") else merged + "\n")
            run("git", "stash", "drop")
        for f in aside_untracked:   # origin may track them now; union either way
            src = os.path.join(aside_dir, f)
            try:
                with open(src, encoding="utf-8-sig") as fh:
                    old = fh.read()
            except OSError:
                continue
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    cur = fh.read()
            except OSError:
                cur = ""
            merged = _union_data_text(f, old, cur)
            if merged.strip():
                with open(f, "w", encoding="utf-8", newline="") as fh:
                    fh.write(merged if merged.endswith("\n") else merged + "\n")
            try:
                os.remove(src)
            except OSError:
                pass
    finally:
        try:
            os.rmdir(aside_dir)
        except OSError:
            pass
    sync_res = do_sync()
    first = next((l for l in out.splitlines() if l.strip()), "already up to date")
    sync_res.update({"ok": True, "pull": first[:160]})
    return sync_res


def api_settings(body):
    with _lock:
        if "cooldown_days" in body:
            DATA["settings"]["cooldown_days"] = max(0, min(365, to_i(body.get("cooldown_days"))))
            log_act("edit", f"cooldown set to {DATA['settings']['cooldown_days']} days")
            save_data()
        return {"ok": True, "settings": DATA["settings"]}


def games_csv():
    buf = io.StringIO()
    if DATA["games"]:
        fields = list(next(iter(DATA["games"].values())).keys())
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for g in DATA["games"].values():
            w.writerow(g)
    return buf.getvalue()


# ---------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/crm.html"):
            with open("crm.html", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(api_state())
        elif path == "/api/export":
            body = games_csv().encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=crm_export.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)
        routes = {
            "/api/sync": lambda: do_sync(),
            "/api/status": lambda: api_status(body),
            "/api/notes": lambda: api_notes(body),
            "/api/delete": lambda: api_delete(body),
            "/api/delete_all": lambda: api_delete_all(body),
            "/api/radar_clear": lambda: api_radar_clear(body),
            "/api/radar_reject": lambda: api_radar_reject(body),
            "/api/radar_unreject": lambda: api_radar_unreject(body),
            "/api/restore": lambda: api_restore(body),
            "/api/purge": lambda: api_purge(body),
            "/api/purge_expired": lambda: api_purge_expired(),
            "/api/add": lambda: api_add(body),
            "/api/check": lambda: api_check(body),
            "/api/refresh_all": lambda: api_refresh_all(body),
            "/api/refresh_status": lambda: api_refresh_status(body),
            "/api/radar_scan": lambda: api_radar_scan(body),
            "/api/scan_status": lambda: api_scan_status(body),
            "/api/pull": lambda: api_git_pull(body),
            "/api/settings": lambda: api_settings(body),
        }
        fn = routes.get(path)
        if not fn:
            return self._json({"error": "not found"}, 404)
        try:
            self._json(fn())
        except Exception as e:  # keep the server alive no matter what a handler hits
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def _auto_sync_loop():
    """Server-side sync every 10 min: new eligible games flow into crm_data.json
    even when no browser tab is open."""
    import time
    while True:
        time.sleep(600)
        try:
            do_sync()
        except Exception:
            pass


def main():
    load_data()
    guard_discord_session()

    def _boot_sync():
        import time
        time.sleep(0.5)
        try:
            do_sync()
        except Exception as e:
            print("initial sync failed:", e)
    threading.Thread(target=_boot_sync, daemon=True).start()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    print(f"Roblox Acquisition CRM running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
