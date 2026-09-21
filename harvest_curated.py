#!/usr/bin/env python3
"""One-off harvest: re-check every known match from results_history.csv with full
enrichment (cookie, group links, owner profile page, owner bio) and the presence filter.
Writes the curated list CSV to stdout; diagnostics to stderr. Safe to re-run."""
import csv
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

try:
    import winreg  # Windows only
except ImportError:  # Linux/cloud: cookie comes from the ROBLOSECURITY env var
    winreg = None

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import roblox_finder as rf

# --- security guard: https to allowlisted public hosts only -------------------
ALLOWED = {"roblox.com", "www.roblox.com", "apis.roblox.com", "games.roblox.com",
           "groups.roblox.com", "users.roblox.com", "discord.com"}


def guard(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("https",):
        raise ValueError(f"rejected scheme: {p.scheme}")
    if (p.hostname or "").lower() not in ALLOWED:
        raise ValueError(f"rejected host: {p.hostname}")
    return url


class GuardedClient(rf.Client):
    def get(self, url, params=None, retries=6, key=None):
        guard(url)
        return super().get(url, params=params, retries=retries, key=key)


cookie = os.environ.get("ROBLOSECURITY")
if not cookie and winreg is not None:
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        cookie, _ = winreg.QueryValueEx(k, "ROBLOSECURITY")
    except OSError:
        cookie = None
if not cookie:
    sys.exit("Set the ROBLOSECURITY env var first (Windows: setx ROBLOSECURITY \"...\" / Linux: export ROBLOSECURITY=\"...\").")
client = GuardedClient(cookie=cookie, delay=0.5)
_orig_sess_get = client.s.get
client.s.get = lambda url, *a, **kw: _orig_sess_get(guard(url), *a, **kw)
_orig_dg = rf._discord_session.get
rf._discord_session.get = lambda url, *a, **kw: _orig_dg(guard(url), *a, **kw)

# ---- load all known matches from previous runs -------------------------------
rows_in = list(csv.DictReader(open("results_history.csv", encoding="utf-8-sig")))
found_via = {}
for r in rows_in:
    try:
        found_via[int(r["universe_id"])] = r.get("found_via", "")
    except (KeyError, ValueError):
        pass
uids = list(found_via.keys())
rf.say(f"harvest: re-checking {len(uids)} known matches with full enrichment")

details = rf.fetch_details(client, uids)

matches, dropped = [], {}
for uid, g in details.items():
    reason = rf.is_excluded(g.get("name", ""), g.get("description", ""))
    if reason:
        dropped[reason] = dropped.get(reason, 0) + 1
        continue
    tier = rf.classify(g.get("visits") or 0, g.get("playing") or 0)
    if tier:
        matches.append((uid, g, tier))
    else:
        dropped["out-of-tier since last run"] = dropped.get("out-of-tier since last run", 0) + 1
rf.say("after exclusions + tier check: " + str(len(matches)) + " matches | dropped: "
       + ", ".join(f"{k} x{v}" for k, v in sorted(dropped.items())))

votes = rf.fetch_votes(client, [u for u, _, _ in matches])

now_iso = datetime.now(timezone.utc).isoformat()
rows = []
for idx, (uid, g, tier) in enumerate(matches, 1):
    visits, active, favs = g.get("visits") or 0, g.get("playing") or 0, g.get("favoritedCount") or 0
    up, down = votes.get(uid, (0, 0))
    # Same ownership-gated evidence as the watcher/CRM: fan/member groups are
    # never scanned, invites verify in trust order with provenance recorded.
    ev = rf.collect_social_evidence(client, uid, g)
    ctype, cid, cname = ev["ctype"], ev["cid"], ev["cname"]
    creator_url, group_name = ev["creator_url"], ev["group_name"]
    group_members = ev["group_members"]
    owner_name, owner_id, owner_url = ev["owner_name"], ev["owner_id"], ev["owner_url"]
    owner_roblox_signal = ev["owner_roblox_signal"]
    discord_name_signal = ev["discord_name_signal"]

    socials = {"discord": [], "youtube": [], "tiktok": [], "twitter": [],
               "twitch": [], "instagram": [], "other": []}
    discord_info = {"valid": False, "name": "", "members": "", "online": ""}
    discord_url = ""
    discord_via = ""
    socials, prov = rf.extract_socials(ev["labeled_texts"], ev["labeled_links"])
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

    created, updated = rf.parse_dt(g.get("created")), rf.parse_dt(g.get("updated"))
    age_days = (datetime.now(timezone.utc) - created).days if created else ""
    key = str(uid)
    prev = rf._STATE.get(key, {})
    peak = max(active, prev.get("peak_active", 0))
    rf._STATE[key] = {"peak_active": peak, "first_seen": prev.get("first_seen", now_iso),
                      "first_visits": prev.get("first_visits", visits),
                      "last_visits": visits, "last_ts": now_iso, "name": g.get("name")}

    heat = round(active / visits * 1000, 2) if visits else 0
    has_discord = bool(discord_info["valid"])
    score = (rf.TIER_WEIGHT[tier] + (200 if has_discord else 0) + min(heat * 5, 200)
             + (100 if isinstance(age_days, int) and age_days <= 30 else 0)
             + (min(int(discord_info["members"] or 0) / 50, 100) if has_discord else 0))

    rows.append({
        "priority_score": round(score), "tier": tier,
        "title": g.get("name", ""),
        "game_url": f"https://www.roblox.com/games/{g.get('rootPlaceId')}/",
        "has_discord": "YES" if has_discord else "",
        "discord_url": discord_url, "discord_server": discord_info["name"],
        "discord_members": discord_info["members"], "discord_online": discord_info["online"],
        "discord_via": discord_via,
        "active": active, "peak_active_seen": peak, "visits": visits,
        "favorites": favs, "likes": up, "dislikes": down,
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
        "genre": g.get("genre", ""), "max_players": g.get("maxPlayers", ""),
        "universe_id": uid, "place_id": g.get("rootPlaceId"),
        "found_via": found_via.get(uid, "rerun"),
        "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    })
    if idx % 10 == 0:
        rf.say(f"   enriched {idx}/{len(matches)}")

rows.sort(key=lambda r: (r["tier"], r["has_discord"] != "YES", -r["priority_score"]))

kept, no_presence = [], 0
for r in rows:
    has_any = any(r[c] for c in ("discord_url", "youtube", "tiktok", "twitter_x",
                                 "twitch", "instagram", "other_links"))
    if has_any or r["owner_roblox_signal"] == "YES" or r.get("discord_name_signal") == "YES":
        kept.append(r)
    else:
        no_presence += 1
rf.say(f"social filter: kept {len(kept)}/{len(rows)} "
       f"(dropped {no_presence} with no social presence at all)")

if kept:
    sys.stdout.write("\ufeff")
    w = csv.DictWriter(sys.stdout, fieldnames=list(kept[0].keys()))
    w.writeheader()
    w.writerows(kept)

rf.say("")
dc_yes = sum(1 for r in kept if r["has_discord"] == "YES")
rf.say(f"FINAL: {len(kept)} games with a social presence ({dc_yes} with live Discord)")
for r in kept:
    if r["has_discord"] == "YES":
        tag = f"DISCORD: {r['discord_server']} ({r['discord_members']}m/{r['discord_online']}on)"
    elif r["owner_roblox_signal"] == "YES":
        tag = "owner bio mentions Roblox"
    else:
        tag = "other socials"
    socials_txt = ", ".join(f"{c}:{r[c]}" for c in ("youtube", "tiktok", "twitter_x", "twitch", "instagram") if r[c]) or "-"
    rf.say(f"[{r['tier']}] {r['title'][:38]:<38} active={r['active']:<6} {tag} | {socials_txt}")
rf.say(f"done, {client.calls} API calls")
