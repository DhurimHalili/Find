#!/usr/bin/env python3
"""
Roblox Acquisition Finder v2
Finds low-visit / high-concurrency Roblox games for acquisition outreach.

TIERS (checked in order):
  BEST (MUST BUY):  visits < 40K   and active >= 100
  GREAT:            visits < 100K  and active >= 600
  GOOD:             visits < 200K  and active >= 800
  MID:              visits < 500K  and active >= 800

Discovery: Discover sorts (every pass) + rotating keyword slices + recommendation
  snowball (3 hops) + creator expansion (newest games first).
Enrichment: exact stats, votes, creator/owner, Discord + socials (verified), growth & peak tracking.

SPEED MODEL -- RoTrend-style coverage without 2-hour passes:
  sorts + snowball run fully every pass (~5 min, highest yield); keyword search
  covers a rotating slice (--keyword-limit 250, cursor in keyword_cursor.json),
  so each pass finishes in ~15 min but the full sweep completes over ~5 passes.
  Creator game lists use newest-first order so fresh low-visit candidates surface
  immediately instead of paging through years of catalogue.
  All network I/O runs in a thread pool (--workers, default 8) behind a token
  bucket (--delay sets the average rate, same politeness as before): same
  endpoints, same data, same validation -- only the waiting is parallel. The
  45s verification wait hides inside the snowball work instead of idling.

OUTPUT MODEL -- this script writes NO files. All data goes to stdout, diagnostics to stderr:
    python roblox_finder.py --csv        > results.csv      (matches, Excel-ready UTF-8 BOM)
    python roblox_finder.py --watchlist  > watchlist.csv    (near misses, 50-99 concurrent)
    python roblox_finder.py                                 (human-readable report)
    --loop N re-runs every N minutes; peak/growth history is kept in memory for the
    life of the process. Save CSVs by redirecting stdout. Cookie via ROBLOSECURITY
    env var only (setx) -- never stored in a file by this script.

SOCIAL FILTER: only games with an online presence are reported: a Discord or any other
    social link (YouTube/TikTok/Twitter/Twitch/Instagram/Facebook/other), found on the game page,
    group page (description + shout + official links), group shout, owner profile page/bio,
    the owner's OWNED groups (role rank 255 only -- fan/member groups are never
    scanned, so someone else's Discord can never attach to the game), or a creator/group/owner NAME
    that advertises a Discord community (discord_name_signal).
    If nothing is found, the owner's profile and bio are the last check before dropping; an owner
    bio mentioning Roblox counts as a presence signal. Zero presence -> removed.
    Disable with --no-social-filter.

EXCLUSIONS: modded (admin panels, x999, ...), reuploaded/uncopylocked/leaked, NSFW
    (condo-type), and non-English games are never reported -- checked on title and
    description before tiering, so they never seed the snowball or waste API calls.
    Disable with --no-exclusions.

GOLDEN HOURS + SCAN MEMORY: --golden '12-16,20-2' (your PC's local time) runs passes
    only inside high-CCU windows -- Asia peak, EU evening, US peak. Scan memory: every
    evaluated game waits --seen-days (3, matches) or 4h (near-misses) before re-check,
    so low-CCU troughs get re-sampled at peak and known games are never re-searched
    early. Ledger persists in seen_ledger.json.

Local build notes (2026-09-05, verified against live APIs):
  - Stats/votes batches capped at 50 universe IDs (Roblox returns HTTP 400 "Too many
    universe IDs" above 50 -- probed and confirmed).
  - omni-search keyword search works WITHOUT a cookie (probed, 200 OK).
  - Official game social-links endpoint needs a logged-in cookie (401 without);
    description/group/profile regex scanning still finds Discord invites without one.
  - Recommendations endpoint takes universeId (probed: 200 by universeId, 404 by placeId).
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# =============================================================================
# TIERS
# =============================================================================
TIERS = [  # (label, max_visits (exclusive), min_active (inclusive))
    ("1-BEST (MUST BUY)", 40_000, 100),
    ("2-GREAT", 100_000, 600),
    ("3-GOOD", 200_000, 800),
    ("4-MID", 500_000, 800),
]
TIER_WEIGHT = {"1-BEST (MUST BUY)": 1000, "2-GREAT": 750, "3-GOOD": 500, "4-MID": 250}
ABS_MIN_ACTIVE = min(t[2] for t in TIERS)     # 100
ABS_MAX_VISITS = max(t[1] for t in TIERS)     # 500K
NEAR_MISS_MIN_ACTIVE = 50                     # goes to watchlist + used as snowball seeds

# =============================================================================
# DO-NOT-BUY EXCLUSIONS (modded / reuploaded / NSFW / non-English)
# =============================================================================
EXCL_MODDED_RE = re.compile(
    r"\b(modded|mods?|modmenu|admin ?panels?|admn|owner ?panels?|adminz|free ?admin|infs?|"
    r"uncopylocked|full ?source|place ?file)\b|\b[x\u00d7]\d{3,}|\b\d{3,}[x\u00d7]\b|\+\d{4,}\b", re.I)
# Description-level modded tells. Deliberately NARROWER than the title rule:
# bare "admin" is NOT here (legit games write "contact an admin"), only the
# panel/menu/free-admin formats plus the x999/+9999 multipliers.
EXCL_MODDED_DESC_RE = re.compile(
    r"\b(admin ?panels?|owner ?panels?|modmenus?|free ?admin)\b"
    r"|\b[x\u00d7]\d{3,}|\b\d{3,}[x\u00d7]\b|\+\d{4,}\b", re.I)
EXCL_REUPLOAD_RE = re.compile(
    r"\b(re-?uploads?|uncopylocked|leaked|copys?|copied|stolen|free ?model|not ?my ?game)\b", re.I)
EXCL_REUPLOAD_DESC_RE = re.compile(
    r"\b(uncopylocked|re-?upload(ed|ing)?|leaked|full ?source|free ?model|not ?my ?game)\b", re.I)
EXCL_NSFW_RE = re.compile(
    r"\b(nsfw|18\+|17\+|condos?|sexy|hentai|porn|sex|nudes?|naked|r ?63|erp|goon\w*)\b", re.I)
EXCL_FOREIGN_SCRIPT_RE = re.compile(
    "[\u0400-\u04FF\u0370-\u03FF\u0600-\u06FF\u0590-\u05FF\u0900-\u097F"
    "\u0E00-\u0E7F\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")
EXCL_FOREIGN_WORDS_RE = re.compile(
    r"\b(espa\u00f1ol|espanol|espanhol|portugues|portugu\u00eas|fran\u00e7ais|deutsch|"
    r"juego|juegos|jogos|simulador|oyun)\b", re.I)
# Owner's call: mount games are never acquisition targets. Title-only on
# purpose ("mount up your dragon" in a description must not kill a good game).
EXCL_MOUNT_RE = re.compile(r"\bmount\b", re.I)


def _has_admin_abuse(text):
    """'Admin abuse' as a modded-genre signal -- unless the text explicitly
    disavows it ('no admin abuse', 'without admin abuse'). Legit games
    protesting fairness must never trip the filter."""
    t = text or ""
    if not re.search(r"admin ?abuse", t, re.I):
        return False
    return not re.search(r"\b(no|without|against|anti|zero)\s+admin\s+abuse", t, re.I)


def is_excluded(title, description=""):
    """Returns a rejection reason string, or None if the game passes the do-not-buy rules."""
    t, d = title or "", description or ""
    if EXCL_MODDED_RE.search(t) or _has_admin_abuse(t):
        return "modded"
    if EXCL_NSFW_RE.search(t):
        return "nsfw"
    if EXCL_MOUNT_RE.search(t):
        return "mount"
    if EXCL_REUPLOAD_RE.search(t):
        return "reuploaded"
    if EXCL_FOREIGN_SCRIPT_RE.search(t) or EXCL_FOREIGN_WORDS_RE.search(t):
        return "non-english"
    if EXCL_MODDED_DESC_RE.search(d) or _has_admin_abuse(d):
        return "modded (description)"
    if EXCL_MOUNT_RE.search(d):
        return "mount (description)"
    if EXCL_REUPLOAD_DESC_RE.search(d):
        return "reuploaded (description)"
    return None

# Peak/growth history, kept in memory for the life of the process (--loop mode).
# universe_id(str) -> {peak_active, first_seen, first_visits, last_visits, last_ts, name}
_STATE = {}

# Scan memory: each game carries an "eligible again at" timestamp in seen_ledger.json
# (literal path, low-level write). Matches wait --seen-days (3); near-misses only
# NEAR_MISS_SEEN_HOURS so they get re-sampled at a different, usually higher-CCU hour.
SEEN_DAYS_DEFAULT = 3
NEAR_MISS_SEEN_HOURS = 4
_SEEN = {}          # uid(str) -> eligible-again ISO timestamp
_SEEN_DAYS = SEEN_DAYS_DEFAULT


def load_seen():
    global _SEEN
    try:
        with open("seen_ledger.json", encoding="utf-8") as f:
            _SEEN = json.load(f)
    except (OSError, ValueError):
        _SEEN = {}


def mark_seen(ids, hours=None):
    if _SEEN_DAYS <= 0:
        return   # scan memory disabled (--no-seen)
    hours = _SEEN_DAYS * 24 if hours is None else hours
    eligible = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    for u in ids:
        _SEEN[str(u)] = eligible


def recently_seen(uid):
    ts = _SEEN.get(str(uid))
    if not ts:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(ts)
    except ValueError:
        return False


def save_seen():
    # merge entries written by the CRM (permanent rejects use far-future expiry)
    # so a save from this process never drops them
    try:
        with open("seen_ledger.json", encoding="utf-8") as f:
            disk = json.load(f)
        for k, v in disk.items():
            if v > (_SEEN.get(k) or ""):
                _SEEN[k] = v
    except (OSError, ValueError):
        pass
    now = datetime.now(timezone.utc).isoformat()
    for k in [k for k, v in _SEEN.items() if v <= now]:
        del _SEEN[k]   # expired = eligible again; no need to keep the entry
    try:
        blob = json.dumps(_SEEN, ensure_ascii=False).encode("utf-8")
        fd = os.open("seen_ledger.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
    except OSError:
        pass


# ---- golden-hours scheduling (local time) -----------------------------------
def parse_windows(spec):
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a, b = part.split("-")
            out.append((int(a) % 24, int(b) % 24))
        except ValueError:
            raise SystemExit(f"bad --golden window: {part!r} (use e.g. '12-16,20-2')")
    return out


def in_window(windows, now=None):
    now = now or datetime.now()
    h = now.hour
    for a, b in windows:
        if a < b:
            if a <= h < b:
                return True
        elif a > b:      # wraps midnight, e.g. 20-2
            if h >= a or h < b:
                return True
        else:
            return True  # a == b -> always
    return False


def next_window_start(windows, now=None):
    now = now or datetime.now()
    best = None
    for a, _ in windows:
        cand = now.replace(hour=a % 24, minute=0, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best:
            best = cand
    return best or (now + timedelta(days=1))

# =============================================================================
# KEYWORDS
# =============================================================================
KW_GENRES = [
    "simulator", "tycoon", "obby", "rng", "clicker", "incremental", "idle", "tower defense", "td",
    "battlegrounds", "fighting", "pvp", "fps", "shooter", "gun game", "horror", "survival", "roleplay",
    "rp", "racing", "driving", "parkour", "puzzle", "escape", "escape room", "mining", "farming",
    "fishing", "hatching", "pet", "pets", "eggs", "rpg", "mmorpg", "dungeon", "dungeon crawler",
    "roguelike", "raid", "boss rush", "wave survival", "zombie", "backrooms", "liminal", "scary",
    "hide and seek", "tag", "murder", "mystery", "detective", "story", "story game", "adventure",
    "open world", "sandbox", "build", "building", "craft", "crafting", "block", "voxel", "restaurant",
    "cafe", "hotel", "school", "hospital", "prison", "prison escape", "military", "war", "army",
    "plane", "flight", "flight sim", "boat", "ship", "train", "subway", "tower", "climb", "climbing",
    "fall", "jump", "run", "runner", "speed", "sprint", "sports", "football", "soccer", "basketball",
    "baseball", "hockey", "boxing", "dodgeball", "volleyball", "tennis", "golf", "bowling", "kart",
    "drift", "bike", "skate", "ski", "snowboard", "surf", "swim", "dive", "space", "planet", "galaxy",
    "moon", "mars", "sky", "island", "beach", "desert", "jungle", "forest", "cave", "underground",
    "city", "town", "village", "kingdom", "empire", "castle", "medieval", "fantasy", "sci fi",
    "cyberpunk", "steampunk", "apocalypse", "wasteland", "arena", "battle royale", "br", "deathmatch",
    "team", "capture the flag", "ctf", "bedwars", "skywars", "eggwars", "minigames", "party games",
    "trivia", "quiz", "guess", "would you rather", "hangout", "chill", "vibe", "social", "chat",
    "dress up", "fashion", "makeover", "salon", "roleplay school", "family", "baby", "daycare",
    "life", "life sim", "job", "jobs", "work", "career", "business", "company", "factory", "industry",
    "shop", "store", "mall", "supermarket", "delivery", "pizza", "burger", "bakery", "ice cream",
    "candy", "cooking", "chef", "kitchen", "farm", "ranch", "zoo", "aquarium", "safari", "hunting",
    "hunt", "fish", "bee", "ant", "bug", "insect", "animal", "animals", "dog", "cat", "horse", "bird",
    "shark", "dinosaur", "dino", "dragon", "monster", "monsters", "creature", "creatures", "beast",
    "mutant", "alien", "robot", "mech", "titan", "giant", "slime", "ghost", "demon", "angel",
    "vampire", "werewolf", "witch", "wizard", "mage", "magic", "spell", "elemental", "element",
    "fire", "ice", "water", "lightning", "wind", "earth", "shadow", "light", "dark", "void", "chaos",
    "sword", "katana", "blade", "knife", "axe", "bow", "spear", "hammer", "staff", "shield", "gun",
    "guns", "sniper", "rifle", "pistol", "bomb", "nuke", "rocket", "missile", "tank", "jet",
    "helicopter", "car", "cars", "truck", "bus", "motorcycle", "vehicle", "vehicles", "mecha",
]

KW_MECHANICS = [
    "steal", "grow", "plant", "garden", "merge", "evolve", "evolution", "infinite", "aura", "luck",
    "lucky", "roll", "rolling", "spin", "gacha", "case", "cases", "unbox", "unboxing", "trade",
    "trading", "dig", "digging", "deep", "drill", "collect", "collector", "collecting", "rebirth",
    "prestige", "ascend", "upgrade", "level up", "grind", "quest", "quests", "mission", "boss",
    "bosses", "wave", "waves", "defense", "defend", "protect", "tower defence", "summon", "summoning",
    "fuse", "fusion", "mutate", "mutation", "train", "training", "punch", "punching", "slap", "kick",
    "throw", "explode", "destroy", "destruction", "demolish", "crush", "smash", "break", "cut",
    "slice", "chop", "carry", "lift", "lifting", "push", "pull", "drag", "cart", "cart ride", "ride",
    "elevator", "rocket ride", "slide", "waterslide", "launch", "fling", "bounce", "float", "fly",
    "flying", "glide", "swing", "grapple", "dash", "sneak", "stealth", "hide", "seek", "chase",
    "escape the", "survive the", "find the", "eat", "eating", "grow big", "get big", "size", "scale",
    "shrink", "expand", "become", "be a", "be the", "turn into", "transform", "morph", "spawn",
    "hatch", "raise", "breed", "tame", "capture", "catch", "adopt", "care", "feed", "sell", "buy",
    "earn", "money", "cash", "coins", "gems", "rich", "richest", "billionaire", "millionaire",
    "trillionaire", "bank", "heist", "rob", "robbery", "steal a", "grab", "loot", "treasure", "gold",
    "diamond", "diamonds", "crystal", "ore", "ores", "gem", "rock", "rocks", "sand", "snow", "lava",
    "obstacle", "obstacle course", "stages", "checkpoint", "checkpoints", "difficulty", "difficulty chart",
    "impossible", "easy", "hard", "extreme", "insane", "long", "longest", "biggest", "tallest",
    "fastest", "strongest", "weakest", "noob", "pro", "hacker", "god", "op", "overpowered", "op weapons",
    "admin", "admin commands", "vip", "free admin", "free", "free ugc", "ugc", "limited", "event",
    "the hunt", "egg hunt", "spleef", "sumo", "king of the hill", "koth", "last man standing", "juggernaut",
    "infection", "tag arena", "freeze tag", "manhunt", "hunter", "prop hunt", "sharks and minnows",
    "red light green light", "glass bridge", "tug of war", "musical chairs", "floor is lava", "doors",
    "rooms", "levels", "floors", "backroom", "anomaly", "scp", "containment", "breach", "facility",
    "lab", "laboratory", "experiment", "test", "testing", "simulation", "typing", "type", "math",
    "spelling", "drawing", "draw", "paint", "art", "music", "rhythm", "piano", "guitar", "dance",
    "singing", "karaoke", "movie", "cinema", "theater", "youtube", "streamer", "tiktok", "influencer",
]

KW_THEMES_MEMES = [
    "brainrot", "italian brainrot", "tung tung", "tralalero", "bombardiro", "sigma", "skibidi",
    "skibidi toilet", "toilet", "cameraman", "meme", "memes", "troll", "trolling", "rizz", "ohio",
    "gyatt", "aura farming", "mewing", "goofy", "silly", "cursed", "funny", "chaos mode", "random",
    "randomizer", "weird", "sus", "impostor", "imposter", "crewmate", "backrooms level", "nextbot",
    "nextbots", "obunga", "sanic", "shrek", "spongebob", "peppa", "grimace", "mrbeast", "beast",
    "sprunki", "incredibox", "fnf", "friday night funkin", "poppy playtime", "huggy", "banban",
    "garten of banban", "rainbow friends", "fnaf", "five nights", "freddy", "springtrap",
    "baldi", "granny", "slender", "slenderman", "siren head", "cartoon cat", "mimic", "apeirophobia",
    "rainbow friends", "piggy", "flee the facility", "murder mystery 2", "mm2", "arsenal", "phantom forces",
    "rivals fps", "frontlines", "bad business", "jailbreak", "mad city", "emergency hamburg", "erlc",
    "greenville", "southwest florida", "car dealership tycoon", "driving empire", "vehicle legends",
    "car crushers", "ultimate driving", "ptfs", "pilot training", "cabin crew simulator", "airline manager",
    "restaurant tycoon", "my restaurant", "work at a pizza place", "retail tycoon", "theme park tycoon",
    "lumber tycoon", "miners haven", "mining simulator", "bee swarm simulator", "pet simulator 99",
    "pet sim", "pets go", "bubble gum simulator", "bubble gum simulator infinity", "ninja legends",
    "clicker simulator", "race clicker", "speed run", "speedrun", "tower of hell", "toh", "jtoh",
    "tower of misery", "difficulty chart obby", "mega fun obby", "escape obby", "barry's prison run",
    "prison run", "spider train", "train escape", "escape tsunami", "tsunami", "natural disaster",
    "natural disaster survival", "build a boat", "build a boat for treasure", "babft", "build to survive",
    "survive the killer", "survive the night", "survive and kill", "kill the noob", "noob army tycoon",
    "war tycoon", "military tycoon", "tank tycoon", "airport tycoon", "zoo tycoon", "farm tycoon",
    "restaurant simulator", "supermarket simulator", "shop simulator", "pizza tycoon", "cafe simulator",
    "hospital tycoon", "school tycoon", "mansion tycoon", "house tycoon", "city tycoon", "life tycoon",
    "youtuber tycoon", "streamer tycoon", "crypto tycoon", "bitcoin miner", "bitcoin", "crypto",
    "pls donate", "please donate", "donate", "donation", "starving artists", "art game", "free draw",
    "obby creator", "obby maker", "game creator", "world creator", "tower defense simulator", "tds",
    "toilet tower defense", "ttd", "anime defenders", "anime vanguards", "anime adventures",
    "all star tower defense", "astd", "anime last stand", "als", "anime reborn", "anime rangers",
    "anime royale", "anime card", "anime crossover", "anime fighters", "anime fighting simulator",
    "anime punching simulator", "anime dimensions", "anime souls", "anime world", "anime realms",
    "arise crossover", "fruit battlegrounds", "king legacy", "grand piece online", "gpo", "a one piece game",
    "aopg", "haze piece", "sea piece", "legacy piece", "pirate piece", "one fruit", "fruit sim",
    "jujutsu infinite", "jujutsu chronicles", "jujutsu legacy", "cursed arena", "sorcerer battlegrounds",
    "demon slayer rpg", "project slayers", "slayers unleashed", "demonfall", "wisteria", "slayer legacy",
    "shindo life", "naruto rpg", "naruto war tycoon", "shinobi battlegrounds", "ninja battlegrounds",
    "saiyan battlegrounds", "dragon ball battlegrounds", "z battlegrounds", "dragon soul", "dragon blox",
    "dbz final stand", "final stand", "a universal time", "your bizarre adventure", "stand upright",
    "stand awakening", "project jojo", "reaper 2", "project mugetsu", "pm", "hollow battlegrounds",
    "soul reaper battlegrounds", "attack on titan revolution", "aot revolution", "aotr", "titan warfare",
    "untitled attack on titan", "uaot", "heroes online", "boku no roblox", "my hero mania", "quirk battlegrounds",
    "solo leveling battlegrounds", "sung battlegrounds", "monarch battlegrounds", "the strongest",
    "strongest battlegrounds clone", "ability battlegrounds", "power battlegrounds", "z battle",
    "punch battlegrounds", "fist battlegrounds", "sword battlegrounds", "weapon battlegrounds",
    "kaiju universe", "kaiju paradise", "creatures of sonaria", "dragon adventures", "wild horse islands",
    "horse life", "horse valley", "warrior cats", "warrior cats ultimate edition", "cat game", "dog game",
    "animal simulator", "animal battlegrounds", "feather family", "bird game", "shark bite", "sharkbite",
    "fish game", "fisch clone", "fishing frontier", "fishing simulator", "hunting simulator", "wild hunt",
    "bee game", "ant colony", "ant simulator", "bug simulator", "creature simulator", "prehistoric",
    "dinosaur simulator", "dino game", "primal", "primordial", "isle", "the isle", "path of titans",
    "cenozoic", "mesozoic", "era of", "age of", "rise of", "legend of", "legends", "chronicles",
    "saga", "origins", "reborn", "remastered", "remake", "revamped", "reloaded", "revolution",
    "evolution 2", "2", "3", "4", "ii", "iii", "x", "z", "plus", "deluxe", "ultimate", "infinity",
    "online", "world", "universe", "realm", "realms", "islands", "sea", "ocean", "seas", "piece",
    "legacy", "unleashed", "awakening", "shenanigans", "battlegrounds 2", "rng 2", "simulator 2",
    "tycoon 2", "obby 2", "story 2", "chapter 2", "chapter", "episode", "season", "part 2", "part",
]

KW_ANIME_IP = [
    "anime", "anime game", "one piece", "blox fruits", "fruits", "devil fruit", "naruto", "shinobi life",
    "shindo", "dragon ball", "dbz", "saiyan", "jujutsu", "jujutsu kaisen", "jjk", "cursed technique",
    "domain expansion", "demon slayer", "slayer", "breathing", "bleach", "shinigami", "hollow",
    "soul reaper", "attack on titan", "aot", "titan shifting", "my hero academia", "mha", "quirk",
    "quirks", "hunter x hunter", "nen", "black clover", "grimoire", "chainsaw man", "chainsaw",
    "solo leveling", "arise", "shadow monarch", "hunters", "sung jin woo", "jojo", "stand", "stands",
    "bizarre adventure", "yba", "aut", "universal time", "fairy tail", "seven deadly sins", "sao",
    "sword art online", "overlord", "re zero", "konosuba", "mob psycho", "one punch man", "opm",
    "saitama", "tokyo ghoul", "ghoul", "kagune", "tokyo revengers", "haikyuu", "blue lock", "rivals",
    "kuroko", "slam dunk", "captain tsubasa", "pokemon anime", "digimon world", "beyblade burst",
    "yugioh duel", "dandadan", "sakamoto days", "kaiju no 8", "wind breaker", "mashle", "undead unluck",
    "spy x family", "frieren", "dungeon meshi", "vinland saga", "berserk", "hellsing", "death note",
    "code geass", "cowboy bebop", "evangelion unit", "gundam battle", "dragon quest", "final fantasy",
    "kingdom hearts", "persona", "genshin", "genshin impact", "honkai", "star rail", "wuthering waves",
    "zenless", "arknights", "fate", "fate grand order", "touhou", "vocaloid", "hatsune miku", "miku",
    "hololive", "vtuber", "isekai", "shonen", "shounen", "manga", "manhwa", "webtoon", "tower of god",
    "god of highschool", "noblesse", "lookism", "eleceed", "omniscient reader", "the beginning after the end",
    "martial peak", "cultivation", "cultivator", "xianxia", "wuxia", "murim", "sect", "immortal",
    "immortality", "reincarnated", "reincarnation", "slime isekai", "tensura", "goblin slayer",
    "chuunibyou", "kawaii", "cute", "chibi", "waifu", "husbando", "maid", "maid cafe", "cat girl",
    "neko", "fox", "kitsune", "oni", "yokai", "samurai anime", "shrine", "japan", "japanese", "tokyo",
    "korea", "korean", "seoul", "china", "chinese", "beijing", "kung fu", "karate", "taekwondo",
    "martial arts", "martial artist", "mma", "ufc", "wrestling", "wwe", "sumo wrestling",
]

# Current hits and their naming conventions - clones/variants of these appear daily. UPDATE WEEKLY.
KW_TRENDING_HITS = [
    "steal a brainrot", "steal a", "grow a garden", "grow a", "build a", "make a", "raise a", "adopt a",
    "escape a", "eat a", "catch a", "become a", "find a", "hatch a", "craft a", "cook a", "sell a",
    "dead rails", "forsaken", "99 nights in the forest", "dress to impress", "dti", "fisch", "fishing sim",
    "blue lock rivals", "basketball zero", "volleyball legends", "haikyuu legends", "blade ball", "ball",
    "the strongest battlegrounds", "tsb", "heroes battlegrounds", "jujutsu shenanigans", "jjs",
    "untitled boxing game", "ubg", "sols rng", "sol's rng", "rng game", "rng simulator", "aura rng",
    "pressure", "doors", "dandy's world", "dandys world", "regretevator", "evade", "item asylum",
    "combat warriors", "slap battles", "ability wars", "elemental battlegrounds", "type soul",
    "peroxide", "deepwoken", "rogue lineage", "arcane odyssey", "pilgrammed", "mimic", "apeirophobia",
    "rainbow friends", "piggy", "flee the facility", "murder mystery 2", "mm2", "arsenal", "phantom forces",
    "rivals fps", "frontlines", "bad business", "jailbreak", "mad city", "emergency hamburg", "erlc",
    "greenville", "southwest florida", "car dealership tycoon", "driving empire", "vehicle legends",
    "car crushers", "ultimate driving", "ptfs", "pilot training", "cabin crew simulator", "airline manager",
    "restaurant tycoon", "my restaurant", "work at a pizza place", "retail tycoon", "theme park tycoon",
    "lumber tycoon", "miners haven", "mining simulator", "bee swarm simulator", "pet simulator 99",
    "pet sim", "pets go", "bubble gum simulator", "bubble gum simulator infinity", "ninja legends",
    "clicker simulator", "race clicker", "speed run", "speedrun", "tower of hell", "toh", "jtoh",
    "tower of misery", "difficulty chart obby", "mega fun obby", "escape obby", "barry's prison run",
    "prison run", "spider train", "train escape", "escape tsunami", "tsunami", "natural disaster",
    "natural disaster survival", "build a boat", "build a boat for treasure", "babft", "build to survive",
    "survive the killer", "survive the night", "survive and kill", "kill the noob", "noob army tycoon",
    "war tycoon", "military tycoon", "tank tycoon", "airport tycoon", "zoo tycoon", "farm tycoon",
    "restaurant simulator", "supermarket simulator", "shop simulator", "pizza tycoon", "cafe simulator",
    "hospital tycoon", "school tycoon", "mansion tycoon", "house tycoon", "city tycoon", "life tycoon",
    "youtuber tycoon", "streamer tycoon", "crypto tycoon", "bitcoin miner", "bitcoin", "crypto",
    "pls donate", "please donate", "donate", "donation", "starving artists", "art game", "free draw",
    "obby creator", "obby maker", "game creator", "world creator", "tower defense simulator", "tds",
    "toilet tower defense", "ttd", "anime defenders", "anime vanguards", "anime adventures",
    "all star tower defense", "astd", "anime last stand", "als", "anime reborn", "anime rangers",
    "anime royale", "anime card", "anime crossover", "anime fighters", "anime fighting simulator",
    "anime punching simulator", "anime dimensions", "anime souls", "anime world", "anime realms",
    "arise crossover", "fruit battlegrounds", "king legacy", "grand piece online", "gpo", "a one piece game",
    "aopg", "haze piece", "sea piece", "legacy piece", "pirate piece", "one fruit", "fruit sim",
    "jujutsu infinite", "jujutsu chronicles", "jujutsu legacy", "cursed arena", "sorcerer battlegrounds",
    "demon slayer rpg", "project slayers", "slayers unleashed", "demonfall", "wisteria", "slayer legacy",
    "shindo life", "naruto rpg", "naruto war tycoon", "shinobi battlegrounds", "ninja battlegrounds",
    "saiyan battlegrounds", "dragon ball battlegrounds", "z battlegrounds", "dragon soul", "dragon blox",
    "dbz final stand", "final stand", "a universal time", "your bizarre adventure", "stand upright",
    "stand awakening", "project jojo", "reaper 2", "project mugetsu", "pm", "hollow battlegrounds",
    "soul reaper battlegrounds", "attack on titan revolution", "aot revolution", "aotr", "titan warfare",
    "untitled attack on titan", "uaot", "heroes online", "boku no roblox", "my hero mania", "quirk battlegrounds",
    "solo leveling battlegrounds", "sung battlegrounds", "monarch battlegrounds", "the strongest",
    "strongest battlegrounds clone", "ability battlegrounds", "power battlegrounds", "z battle",
    "punch battlegrounds", "fist battlegrounds", "sword battlegrounds", "weapon battlegrounds",
    "kaiju universe", "kaiju paradise", "creatures of sonaria", "dragon adventures", "wild horse islands",
    "horse life", "horse valley", "warrior cats", "warrior cats ultimate edition", "cat game", "dog game",
    "animal simulator", "animal battlegrounds", "feather family", "bird game", "shark bite", "sharkbite",
    "fish game", "fisch clone", "fishing frontier", "fishing simulator", "hunting simulator", "wild hunt",
    "bee game", "ant colony", "ant simulator", "bug simulator", "creature simulator", "prehistoric",
    "dinosaur simulator", "dino game", "primal", "primordial", "isle", "the isle", "path of titans",
    "cenozoic", "mesozoic", "era of", "age of", "rise of", "legend of", "legends", "chronicles",
    "saga", "origins", "reborn", "remastered", "remake", "revamped", "reloaded", "revolution",
    "evolution 2", "2", "3", "4", "ii", "iii", "x", "z", "plus", "deluxe", "ultimate", "infinity",
    "online", "world", "universe", "realm", "realms", "islands", "sea", "ocean", "seas", "piece",
    "legacy", "unleashed", "awakening", "shenanigans", "battlegrounds 2", "rng 2", "simulator 2",
    "tycoon 2", "obby 2", "story 2", "chapter 2", "chapter", "episode", "season", "part 2", "part",
]

# Tags/emojis that brand-new games put in titles - very effective for catching fresh releases
KW_TITLE_TAGS = [
    "[NEW]", "[UPD]", "[UPDATE]", "[RELEASE]", "[RELEASED]", "[BETA]", "[ALPHA]", "[PRE-ALPHA]",
    "[EARLY ACCESS]", "[EA]", "[DEMO]", "[FREE]", "[EVENT]", "[FIXED]", "[FIX]", "[2X]", "[X2]",
    "[OPEN]", "[TESTING]", "[TEST]", "[WIP]", "[SOON]", "[OUT NOW]", "[LIVE]", "[NEW MAP]", "[NEW CODE]",
    "[CODES]", "[CODE]", "[UGC]", "[FREE UGC]", "[LIMITED]", "[HALLOWEEN]", "[CHRISTMAS]", "[WINTER]",
    "[SUMMER]", "[EASTER]", "[VALENTINES]", "[ANNIVERSARY]", "[ADMIN]", "[VC]", "[VOICE CHAT]",
    "new", "update", "upd", "release", "beta", "alpha", "early access", "demo", "codes", "code",
    "voice chat", "vc", "🔥", "🎉", "✨", "💀", "🌟", "⚡", "👑", "🎃", "🎄", "❄️", "🌊", "🍀", "💎",
    "🐟", "🧠", "🍌", "🥶", "😈", "🩸", "🗡️", "🔫", "🚗", "🏠", "🌱", "🥚", "🐾", "🎣", "⛏️", "🪙",
    "💰", "🏆", "🆕", "🚨", "‼️", "❗", "⭐", "🌈", "🍕", "🍔", "🐉", "🦖", "🦈", "🕷️", "👻", "🤖",
]

# Very broad single tokens - shallow pages of these return a lot of different games
KW_BROAD = list("abcdefghijklmnopqrstuvwxyz") + [str(i) for i in range(10)] + [
    "the", "a", "of", "in", "my", "your", "our", "me", "you", "we", "it", "is", "and", "or", "to",
    "go", "no", "yes", "up", "down", "out", "on", "off", "big", "small", "little", "mini", "mega",
    "super", "ultra", "hyper", "max", "pro", "plus", "x", "z", "v2", "v3", "2025", "2026", "fun",
    "cool", "epic", "best", "new game", "game", "games", "play", "playing", "player", "players",
]

# Combinatorial pieces used in --keyword-mode max
COMBO_PREFIXES = [
    "steal a", "grow a", "build a", "make a", "raise a", "escape", "escape the", "survive the",
    "find the", "become a", "eat the", "catch a", "hatch a", "craft a", "cook a", "sell a", "destroy the",
    "protect the", "defend the", "fight the", "kill the", "save the", "hide from", "run from",
]
COMBO_NOUNS = [
    "brainrot", "garden", "pet", "car", "boat", "plane", "house", "mansion", "castle", "city", "island",
    "planet", "dragon", "monster", "zombie", "titan", "noob", "boss", "animal", "dog", "cat", "fish",
    "shark", "dinosaur", "robot", "slime", "ghost", "demon", "hero", "villain", "sword", "gun", "tower",
    "kingdom", "empire", "farm", "shop", "restaurant", "school", "prison", "hospital", "office",
    "world", "universe", "galaxy", "meme", "toilet", "egg", "tree", "plant", "bee", "ant", "block",
]
COMBO_SUFFIXES = ["simulator", "tycoon", "rng", "obby", "battlegrounds", "tower defense", "clicker",
                  "incremental", "rp", "story", "legacy", "online", "legends", "game"]

# =============================================================================
# EXPANSION WAVE -- high-signal clone bait, hot formats, title tags & nouns.
# Same bar as the base lists (tiers + exclusions clean everything); these only
# widen the top of the funnel. Ordered signal-first, combos last.
# =============================================================================
KW_TRENDING_X = [
    "tung tung tung sahur", "tralalero tralala", "bombardiro crocodilo",
    "lirili larila", "cappuccino assassino", "chimpanzini bananini",
    "trippi troppi", "la vaca saturno", "los tralaleritos",
    "steal brainrot", "brainrot stealer", "brainrot tycoon", "brainrot simulator",
    "brainrot rng", "brainrot tower defense", "brainrot battlegrounds", "brainrot obby",
    "brainrot clicker", "brainrot merge", "brainrot hunter", "collect brainrots",
    "merge brainrots", "find the brainrots", "escape the brainrot", "brainrot boss",
    "brainrot shop", "brainrot trading", "trade brainrots",
    "grow garden", "garden simulator", "garden tycoon", "garden rng", "plant simulator",
    "plant tycoon", "grow a plant", "grow plants", "grow a tree", "grow a flower",
    "seed shop", "sell plants", "candy garden", "grow a zoo",
    "eat the world", "eat to grow", "eat simulator", "eat players", "devour",
    "escape school", "escape daycare", "escape hospital", "escape mall", "escape airport",
    "escape hotel", "escape carnival", "escape lava", "escape flood", "escape volcano",
    "escape area 51", "escape lab", "escape sewer", "escape basement",
    "run from", "chase simulator", "tag simulator", "tornado survival", "meteor survival",
    "lava run", "shark run", "murder run",
    "tap simulator", "afk grind", "auto click", "idle tycoon", "idle miner",
    "idle restaurant", "click to win",
    "hatch a pet", "hatch eggs", "egg simulator", "open eggs", "dice simulator",
    "roll for pets", "pet catcher", "pet collector", "pet rescue", "pet race",
    "pet battle", "pet fight", "pet merge", "golden pet", "huge pet", "shiny pets",
    "rainbow pets",
    "obby parkour", "obby race", "obby tycoon", "tower obby",
    "1000 stages", "500 stages", "100 stages", "stage obby", "checkpoint obby",
    "lava obby", "candy obby", "rainbow obby", "reach the top", "only up",
    "rage obby", "no jumping", "speed obby",
    "toilet tycoon", "gas station tycoon", "car wash tycoon", "laundromat tycoon",
    "gym tycoon", "music tycoon", "pet shop tycoon", "toy tycoon", "museum tycoon",
    "bank tycoon", "island tycoon", "volcano tycoon", "space tycoon", "moon tycoon",
    "underwater tycoon", "pirate tycoon", "ninja tycoon", "samurai tycoon",
    "wizard tycoon", "dragon tycoon", "dino tycoon", "slime tycoon", "ghost tycoon",
    "superhero tycoon", "villain tycoon", "spy tycoon", "oil tycoon", "gold tycoon",
    "diamond tycoon", "candy tycoon", "donut tycoon", "sushi tycoon", "taco tycoon",
    "car factory tycoon", "robot factory tycoon", "clone tycoon", "army tycoon",
    "castle tycoon", "kingdom tycoon", "empire tycoon",
    "sword fighting", "sword simulator", "katana simulator", "blade simulator",
    "boxing simulator", "wrestling simulator", "karate simulator", "ninja simulator",
    "knight simulator", "gladiator simulator", "arena simulator", "1v1 simulator",
    "duel simulator", "fight simulator", "battle simulator", "war simulator",
    "stickman battlegrounds", "block battlegrounds", "noob battlegrounds",
    "roll simulator", "spin simulator", "gacha simulator", "luck simulator",
    "aura simulator", "roll for auras", "wheel simulator", "case opening",
    "case simulator", "unbox simulator", "mystery box simulator",
    "scary obby", "horror obby", "horror simulator", "haunted house", "haunted school",
    "analog horror", "liminal horror", "poolrooms", "level 0",
    "piggy chapter", "granny escape", "fnaf clone", "night shift simulator",
    "night guard simulator", "midnight horror", "scary elevator", "horror elevator",
    "killer simulator", "survivor simulator",
    "soccer simulator", "football simulator", "basketball simulator",
    "volleyball simulator", "boxing league", "race simulator", "parkour simulator",
    "skate simulator", "bike simulator", "drift simulator", "drag race", "street race",
    "boat race", "climb race",
    "brookhaven clone", "family simulator", "baby simulator", "daycare simulator",
    "high school simulator", "teacher simulator", "doctor simulator", "dentist simulator",
    "vet simulator", "cashier simulator", "delivery simulator", "taxi simulator",
    "truck simulator", "pilot simulator", "farmer simulator", "miner simulator",
    "lumberjack simulator", "chef simulator", "baker simulator", "barista simulator",
    "pizza delivery simulator", "burger simulator",
    "donate simulator", "money simulator", "cash simulator", "bank simulator",
    "billionaire simulator", "trillionaire simulator", "rich simulator",
    "squid game simulator", "red light green light clone", "who is the murderer",
    "impostor simulator", "sus simulator", "stumble simulator", "party simulator",
    "talent show simulator", "runway simulator",
    "zombie defense", "castle defense", "tower battles", "defense simulator",
    "dti clone", "fashion simulator", "makeup simulator",
    "blade ball clone", "parry simulator", "rivals clone", "fps clone",
    "blue lock clone", "soccer anime", "train simulator", "train survival",
    "99 nights clone", "forest survival", "fisch clone", "best fishing game",
    "fishing tycoon", "aquarium simulator", "ocean simulator",
    "forsaken clone", "slasher simulator", "c00lkidd", "guest 666", "two time",
    "disco bee", "queen bee garden", "red fox garden", "mimic octopus",
    "candy blossom simulator", "moon mango simulator",
    "zoonomaly", "zoochosis", "indigo park", "mascot horror",
    "hello neighbor clone", "poppy playtime clone", "rainbow friends clone",
    "baldi clone", "granny clone", "doors clone", "mimic clone", "piggy clone",
]

KW_TITLE_TAGS_X = [
    "[UPD 2]", "[UPD 3]", "[SEASON 2]", "[CHAPTER 2]", "[PART 2]", "[ACT 2]",
    "[DOUBLE XP]", "[DOUBLE LUCK]", "[X2 LUCK]", "[X3 LUCK]", "[FREE GAMEPASS]",
    "[FREE VIP]", "[FREE PET]", "[FREE LEGENDARY]", "[FREE MYTHIC]", "[SECRET]",
    "[MYTHIC]", "[LEGENDARY]", "[SHINY]", "[GOLDEN]", "[RAINBOW]", "[VOID]",
    "[CELESTIAL]", "[DIVINE]", "[PRISMATIC]", "[FROZEN]", "[NEON]", "[GALAXY]",
    "[COSMIC]", "[SHADOW]", "[BLOOD]", "[TOXIC]", "[CRYSTAL]", "[DIAMOND]",
    "[BOSS]", "[RAID]", "[DUNGEON]", "[PVP]", "[CO-OP]", "[SOLO]", "[DUO]",
    "[SQUAD]", "[CLAN]", "[GUILD]", "[TRADING]", "[AFK]", "[IDLE]", "[AUTO]",
    "[NOOB TO PRO]", "[NOOB VS PRO]", "[1 TO 100]", "[100 DAYS]", "[HARDCORE]",
    "[HARD MODE]", "[INSANE MODE]", "[NIGHTMARE]", "[ENDLESS]", "[INFINITE]",
    "[1V1]", "[2V2]", "[BATTLE ROYALE]", "[LUCKY BLOCK]", "[ONE BLOCK]",
    "[SKYBLOCK]", "[TOWER DEFENSE]", "[OBBY]", "[TYCOON]", "[SIMULATOR]",
    "secret", "mythic", "legendary", "shiny", "rainbow", "boss fight", "new boss",
    "limited pet", "free legendary", "double xp", "double luck", "1v1", "hardcore",
]

KW_GENRES_X = [
    "lawn mowing simulator", "pressure washing simulator", "window cleaning simulator",
    "pool cleaning simulator", "car detailing simulator", "dog grooming simulator",
    "babysitting simulator", "lemonade stand simulator", "food truck simulator",
    "coffee shop simulator", "sneaker store simulator", "barber shop simulator",
    "tattoo shop simulator", "dojo simulator", "arcade simulator", "bowling alley simulator",
    "laser tag simulator", "paintball simulator", "archery simulator", "kayak simulator",
    "jet ski simulator", "yacht simulator", "cruise ship simulator", "oil rig simulator",
    "space station simulator", "mars colony simulator", "underwater base simulator",
    "bunker simulator", "doomsday bunker simulator", "courtroom simulator",
    "lawyer simulator", "judge simulator", "court simulator", "detective agency simulator",
    "ghost hunter simulator", "ufo hunting simulator", "cryptid hunting simulator",
    "storm chasing simulator", "volcano explorer simulator", "cave explorer simulator",
    "deep sea explorer simulator", "safari simulator", "photo safari simulator",
    "bird watching simulator", "fishing tournament simulator", "ice fishing simulator",
    "pearl diving simulator", "treasure diving simulator", "lighthouse keeper simulator",
    "survivors clone", "deck builder simulator", "auto battler simulator",
    "chess simulator", "wordle clone simulator", "word game simulator",
    "type racer simulator", "piano simulator", "guitar simulator", "drum simulator",
    "dj simulator", "concert simulator", "music studio simulator", "record label simulator",
    "movie studio simulator", "actor simulator", "streaming simulator",
    "podcast simulator", "auction simulator", "flea market simulator",
    "garage simulator", "mechanic simulator", "house flipper simulator",
    "interior design simulator", "construction simulator", "demolition simulator",
    "bulldozer simulator", "excavator simulator", "crane simulator", "forklift simulator",
    "tractor simulator",
]

KW_MECHANICS_X = [
    "ascension simulator", "enchant simulator", "alchemy simulator", "potion simulator",
    "potion brewer simulator", "wand simulator", "dragon tamer simulator",
    "pet tamer simulator", "beast tamer simulator", "monster tamer simulator",
    "fossil simulator", "cloning simulator", "time travel simulator", "time machine simulator",
    "portal simulator", "multiverse simulator", "offline earnings simulator",
    "idle earnings simulator", "auto farm simulator", "auto hatch simulator",
    "combo simulator", "damage simulator", "sniper simulator", "assassin simulator",
    "thief simulator", "lockpicking simulator", "safe cracking simulator",
    "jewel heist simulator", "museum heist simulator", "casino heist simulator",
    "bounty hunter simulator", "hitman simulator", "bodyguard simulator",
    "last to leave simulator", "dont move simulator", "try not to laugh simulator",
    "impossible quiz simulator", "quiz simulator", "spelling bee simulator",
    "riddle simulator", "mystery mansion simulator", "haunted mansion simulator",
    "abandoned mall simulator", "liminal mall simulator", "dreamcore simulator",
    "nostalgia simulator", "retro simulator", "arm wrestling simulator",
    "pushup simulator", "muscle simulator", "workout simulator", "gym simulator",
    "eat simulator", "sleep simulator", "dream simulator", "toilet simulator",
    "1v1 simulator", "sword simulator", "axe simulator", "kung fu simulator",
]

KW_THEMES_X = [
    "ohio simulator", "ohio boss simulator", "rizz simulator", "rizz up simulator",
    "gyatt simulator", "skibidi defense simulator", "skibidi invasion simulator",
    "titan cameraman simulator", "titan speakerman simulator", "titan tv man simulator",
    "astro toilet simulator", "gman toilet simulator", "sigma simulator", "sigma boss simulator",
    "alpha simulator", "mewing simulator", "mewing streak simulator", "aura farming simulator",
    "aura simulator", "kissy missy simulator", "mommy long legs simulator",
    "jumbo josh simulator", "rainbow friends chapter simulator", "blue rainbow friend simulator",
    "seek doors simulator", "figure doors simulator", "rush doors simulator",
    "piggy book simulator", "zizzy piggy simulator", "glamrock freddy simulator",
    "monty gator simulator", "circus baby simulator", "purple guy simulator",
    "glitchtrap simulator", "granny horror simulator", "slendrina simulator",
    "playtime baldi simulator", "siren head horror simulator", "cartoon dog simulator",
    "shrek swamp simulator", "shrek horror simulator", "shrek obby simulator",
    "spongebob horror simulator", "spongebob simulator", "krusty krab simulator",
    "peppa pig horror simulator", "peppa pig simulator", "grimace shake simulator",
    "grimace horror simulator", "mrbeast challenge simulator", "feastables simulator",
    "sprunki horror simulator", "sprunki simulator", "wenda sprunki simulator",
    "fnf simulator", "whitty fnf simulator", "tricky fnf simulator",
    "impostor horror simulator", "impostor simulator", "crewmate simulator",
    "bendy horror simulator", "cuphead boss simulator",
    "dti clone", "dress to impress clone", "modeling simulator",
    "blade ball clone", "parry simulator", "rivals clone",
    "dead rails clone", "99 nights clone", "cultist raid simulator",
    "fishing update simulator", "slasher simulator", "chance forsaken simulator",
]

KW_ANIME_X = [
    "anime infinite", "infinite anime", "anime tower", "anime siege", "anime war",
    "anime war tycoon", "anime empire", "anime kingdom", "anime capital",
    "anime story", "anime tales", "anime odyssey", "anime journey", "anime quest",
    "anime heroes", "anime legends", "anime spirits", "anime blades", "anime brawl",
    "anime arena", "anime clash", "anime strike",
    "gachiakuta", "dragon ball daima", "daima simulator", "one piece elbaf",
    "demon slayer infinity castle", "bleach tybw", "jjk interactions simulator",
    "type soul clone", "sparking zero simulator",
]

KW_BROAD_X = [
    "of the", "in the", "to the", "and the", "for the", "on the", "from the",
    "with the", "without the", "into the", "over the", "under the",
    "the best", "best ever", "brand new", "just released", "official game",
    "part one", "part two", "episode 1", "season 1", "chapter 1", "level 1",
    "world 1", "day one",
]

COMBO_PREFIXES_X = [
    "steal the", "collect the", "unlock the", "upgrade the", "merge the",
    "catch the", "defeat the", "race the", "chase the", "guard the",
    "build the", "grow the", "raise the", "train the", "tame the",
    "adopt the", "feed the", "conquer the", "rule the", "own the",
    "buy the", "trade the", "clean the", "fix the", "drive the",
]

COMBO_NOUNS_X = [
    "penguin", "panda", "fox", "wolf", "bear", "tiger", "lion", "elephant",
    "monkey", "giraffe", "zebra", "crocodile", "snake", "spider", "crab",
    "octopus", "whale", "dolphin", "owl", "eagle", "parrot", "chicken",
    "duck", "pig", "cow", "sheep", "goat", "horse", "donkey", "llama",
    "camel", "kangaroo", "koala", "sloth", "raccoon", "squirrel", "hamster",
    "frog", "turtle", "unicorn", "phoenix", "mermaid", "fairy", "goblin",
    "orc", "troll", "dwarf", "elf", "vampire", "mummy", "skeleton", "pirate",
    "ninja", "samurai", "knight", "viking", "wizard", "clown", "astronaut",
    "cowboy", "chef", "doctor", "teacher", "student", "baby", "grandma",
    "king", "queen", "prince", "princess", "santa",
    "pumpkin", "mushroom", "cactus", "sunflower", "carrot", "apple", "banana",
    "watermelon", "strawberry", "pineapple", "coconut", "donut", "pizza",
    "burger", "taco", "sushi", "cake", "cookie", "candy", "chocolate", "popcorn",
    "train", "submarine", "rocket", "ufo", "helicopter", "bike", "skateboard",
    "volcano", "cave", "mine", "lighthouse", "igloo", "cabin", "treehouse",
    "mall", "airport", "harbor", "park", "zoo", "circus", "museum", "bank",
    "jail", "flower", "rose",
]

COMBO_SUFFIXES_X = [
    "adventure", "journey", "quest", "tales", "odyssey", "empire", "kingdom",
    "life", "challenge", "race", "arena", "defense", "escape", "party",
]


def build_keywords(mode):
    if mode == "fast":
        kws = (KW_TRENDING_HITS + KW_TRENDING_X + KW_TITLE_TAGS + KW_TITLE_TAGS_X
               + KW_GENRES[:80] + KW_GENRES_X[:40])
    elif mode == "full":
        kws = (KW_TRENDING_HITS + KW_TRENDING_X + KW_TITLE_TAGS + KW_TITLE_TAGS_X
               + KW_GENRES + KW_GENRES_X + KW_MECHANICS + KW_MECHANICS_X
               + KW_THEMES_MEMES + KW_THEMES_X + KW_ANIME_IP + KW_ANIME_X
               + KW_BROAD + KW_BROAD_X)
    else:  # max
        kws = (KW_TRENDING_HITS + KW_TRENDING_X + KW_TITLE_TAGS + KW_TITLE_TAGS_X
               + KW_GENRES + KW_GENRES_X + KW_MECHANICS + KW_MECHANICS_X
               + KW_THEMES_MEMES + KW_THEMES_X + KW_ANIME_IP + KW_ANIME_X
               + KW_BROAD + KW_BROAD_X)
        prefixes = COMBO_PREFIXES + COMBO_PREFIXES_X
        nouns = COMBO_NOUNS + COMBO_NOUNS_X
        suffixes = COMBO_SUFFIXES + COMBO_SUFFIXES_X
        kws += [f"{p} {n}" for p in prefixes for n in nouns]
        kws += [f"{n} {s}" for n in nouns for s in suffixes]
    return list(dict.fromkeys(k.strip() for k in kws if k.strip()))

# =============================================================================
# ENDPOINTS
# =============================================================================
GAMES_API = "https://games.roblox.com/v1/games"
VOTES_API = "https://games.roblox.com/v1/games/votes"
GAME_SOCIAL_API = "https://games.roblox.com/v1/games/{}/social-links/list"
RECS_API = "https://games.roblox.com/v1/games/recommendations/game/{}"
USER_GAMES_API = "https://games.roblox.com/v2/users/{}/games"
GROUP_GAMES_API = "https://games.roblox.com/v2/groups/{}/games"
GROUP_API = "https://groups.roblox.com/v1/groups/{}"
GROUP_SOCIAL_API = "https://groups.roblox.com/v1/groups/{}/social-links"
USER_API = "https://users.roblox.com/v1/users/{}"
OMNI_API = "https://apis.roblox.com/search-api/omni-search"
SORTS_API = "https://apis.roblox.com/explore-api/v1/get-sorts"
SORT_CONTENT_API = "https://apis.roblox.com/explore-api/v1/get-sort-content"
DISCORD_INVITE_API = "https://discord.com/api/v10/invites/{}"

DISCORD_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite|discordapp\.com/invite|dsc\.gg|dc\.gg)/([A-Za-z0-9\-_]{2,32})",
    re.I)
# Owner / group / creator names that advertise a Discord community even when no
# invite code is pasted anywhere ("JoinMyDiscord", "XYZ Community Server", ...).
# Checked against creator name, group name and owner username -- counts as a
# presence signal so the game is kept for manual outreach.
DISCORD_NAME_RE = re.compile(
    r"discord|dsc\.gg|dc\.gg|community server|support server|\bdc\b.{0,12}server|join.{0,20}(server|community)",
    re.I)
# Extra invite shorteners + Facebook LIABILITY: folded into "other" so no CSV
# schema change is needed -- the social filter already keeps other_links rows.
FACEBOOK_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|web\.)?facebook\.com/[\w\.\-/]+", re.I)
SOCIAL_RES = {
    "youtube": re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:@|c/|channel/|user/)[\w\-\.]+|youtu\.be/[\w\-]+)", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w\.\-]+", re.I),
    "twitter": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/[\w]{1,15}\b", re.I),
    "twitch": re.compile(r"(?:https?://)?(?:www\.)?twitch\.tv/[\w]{3,25}\b", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/[\w\.]{1,30}\b", re.I),
}

# Roblox's own site-wide social links (page footers) -- never a developer's social presence.
GENERIC_SOCIAL_RE = re.compile(
    r"(?:twitter|x)\.com/roblox(?:/|$)|youtube\.com/user/roblox(?:/|$)|twitch\.tv/roblox(?:/|$)"
    r"|instagram\.com/roblox(?:/|$)"
    r"|x\.com/(?:users?|js|www|home|explore|notifications|settings|messages|compose|intent|"
    r"search|hashtag|i|transactions|groups|userhub|spotlight|pe|de|es|fr|id|it|ja|ko|pl|pt|th|tr|vi|ar|hi)\b", re.I)

# =============================================================================
# CONCURRENCY -- thread-safe HTTP + ordered parallel map
# The workload is ~95% waiting on network, so --workers threads hide latency
# while a token bucket keeps the average request rate at 1/delay (the same
# politeness the sequential version had). Same endpoints, same data, same
# validation -- only the waiting is parallel.
# =============================================================================
_PRINT_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


def tsay(msg):
    """say() from worker threads without interleaved stderr lines."""
    with _PRINT_LOCK:
        say(msg)


class RateLimiter:
    """Token bucket: sustains `rate_per_sec` with a small burst allowance.
    Threads block in acquire(); the bucket refills with wall-clock time, so
    short parallel bursts are absorbed but the long-run average never exceeds
    the configured rate (existing 429 backoff in Client.get still applies)."""

    def __init__(self, rate_per_sec, burst=None):
        self.rate = max(float(rate_per_sec), 0.05)
        # Small bursts: Roblox tolerates the sustained rate fine but answers
        # parallel bursts with flaky 500s (observed live). Keep bursts tiny.
        self.capacity = burst if burst else 2
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._cond = threading.Condition(threading.Lock())

    def acquire(self):
        with self._cond:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                self._cond.wait(timeout=(1.0 - self._tokens) / self.rate)


def _pmap(fn, items, workers):
    """Order-preserving parallel map. workers<=1 runs sequentially (same code
    path, deterministic -- used for debugging and equivalence tests)."""
    items = list(items)
    if not items:
        return []
    if workers <= 1:
        return [fn(x) for x in items]
    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futs = {ex.submit(fn, x): i for i, x in enumerate(items)}
        for fut in as_completed(futs):
            out[futs[fut]] = fut.result()
    return out
# =============================================================================
# HTTP
# =============================================================================
class Client:
    def __init__(self, cookie=None, delay=0.35, verbose=False):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
        })
        self.has_cookie = bool(cookie)
        if cookie:
            self.s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        self.delay = delay
        self.verbose = verbose
        self.session_id = str(uuid.uuid4())
        self.calls = 0
        self.dead_endpoints = set()   # endpoints that returned 401/403 -> don't hammer them
        self._lock = threading.Lock()
        # Average request rate stays at the old 1/delay politeness; threads
        # only hide latency. delay<=0 keeps the legacy no-wait behavior.
        self.limiter = RateLimiter(1.0 / delay if delay and delay > 0 else 30.0)

    def get(self, url, params=None, retries=6, key=None):
        if key:
            with self._lock:
                if key in self.dead_endpoints:
                    return None
        backoff = 5
        for _ in range(retries):
            try:
                self.limiter.acquire()
                r = self.s.get(url, params=params, timeout=30)
                with self._lock:
                    self.calls += 1
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    if self.verbose:
                        tsay(f"   [429] sleeping {backoff}s")
                    time.sleep(backoff); backoff = min(backoff * 2, 120); continue
                if r.status_code in (401, 403, 404):
                    if key:
                        with self._lock:
                            self.dead_endpoints.add(key)
                        tsay(f"   [!] {key} returned {r.status_code} - skipping this source for the rest of the run.")
                    return None
                if r.status_code >= 500:
                    # Always logged (not just verbose): server-side flakiness is
                    # the signal for tuning --workers/--delay down.
                    tsay(f"   [{r.status_code}] retrying after {backoff}s: {url[:90]}")
                    time.sleep(backoff); continue
                if self.verbose:
                    tsay(f"   [{r.status_code}] {url}")
                return None
            except (requests.RequestException, ValueError) as e:
                # Always logged: connection blips under parallel load are the
                # signal for tuning --workers/--delay (each costs a backoff sleep).
                tsay(f"   [retry in {backoff}s] {type(e).__name__}: {str(e)[:100]}")
                time.sleep(backoff)
        return None


def say(msg):
    """Progress/diagnostics go to stderr so stdout stays clean for redirected CSVs."""
    print(msg, file=sys.stderr)


_discord_session = requests.Session()
_discord_session.headers.update({"User-Agent": "Mozilla/5.0 (RobloxFinder/2.0)"})
_discord_cache = {}
# Discord's invite endpoint is a different host with its own budget: 2 req/s
# shared across enrichment threads (same long-run average as the old 0.6s
# sequential sleep, but parallel-safe).
_DISCORD_LIMITER = RateLimiter(2.0, burst=4)

def verify_discord(code):
    """Returns dict(valid, name, members, online) using Discord's public invite endpoint."""
    code = code.strip()
    with _CACHE_LOCK:
        if code in _discord_cache:
            return _discord_cache[code]
    result = {"valid": False, "name": "", "members": "", "online": ""}
    for _ in range(4):
        try:
            _DISCORD_LIMITER.acquire()
            r = _discord_session.get(DISCORD_INVITE_API.format(code),
                                     params={"with_counts": "true", "with_expiration": "true"}, timeout=20)
            if r.status_code == 200:
                d = r.json()
                g = d.get("guild") or {}
                result = {"valid": True, "name": g.get("name", ""),
                          "members": d.get("approximate_member_count", ""),
                          "online": d.get("approximate_presence_count", "")}
                break
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", r.json().get("retry_after", 5))) + 1)
                continue
            break  # 404 = dead invite, others = give up
        except (requests.RequestException, ValueError):
            time.sleep(3)
    with _CACHE_LOCK:
        _discord_cache.setdefault(code, result)
        return _discord_cache[code]

# =============================================================================
# DISCOVERY
# =============================================================================
def discover_search(client, keyword, max_pages, prefilter):
    """One keyword's pages -> {universe_id: source}. Pure worker: no shared
    state touched (seen/dup filtering happens at merge time in run())."""
    token, pages, found = "", 0, {}
    while pages < max_pages:
        data = client.get(OMNI_API, {"searchQuery": keyword, "pageToken": token,
                                     "sessionId": client.session_id, "pageType": "all"}, key="omni-search")
        if not data:
            break
        pages += 1
        for group in data.get("searchResults", []):
            for item in group.get("contents", []):
                uid = item.get("universeId")
                if not uid or uid in found:
                    continue
                pc = item.get("playerCount")
                if prefilter and isinstance(pc, int) and pc < NEAR_MISS_MIN_ACTIVE:
                    continue
                found[uid] = f"search:{keyword}"
        token = data.get("nextPageToken")
        if not token:
            break
    return found


def _fetch_sort_pages(client, sort_id, name, games, page_token, max_pages, prefilter):
    """One sort's full page chain -> {universe_id: source}. Pure worker."""
    found, pages = {}, 0
    while True:
        for g in games:
            uid = g.get("universeId")
            if not uid or uid in found:
                continue
            pc = g.get("playerCount")
            if prefilter and isinstance(pc, int) and pc < NEAR_MISS_MIN_ACTIVE:
                continue
            found[uid] = f"sort:{name}"
        pages += 1
        if not page_token or pages >= max_pages:
            break
        cont = client.get(SORT_CONTENT_API, {"sessionId": client.session_id, "sortId": sort_id,
                                             "pageToken": page_token, "device": "computer",
                                             "country": "all"}, key="explore-sort-content")
        if not cont:
            break
        games, page_token = cont.get("games", []), cont.get("nextPageToken")
    return found


def discover_sorts(client, max_pages, prefilter, workers):
    """All Discover sorts -> {universe_id: source}. Sort-list walk stays
    sequential (cheap); each sort's page chain runs in the pool."""
    sorts_token, seen_sorts, jobs = None, set(), []
    while True:
        params = {"sessionId": client.session_id, "device": "computer", "country": "all"}
        if sorts_token:
            params["sortsPageToken"] = sorts_token
        data = client.get(SORTS_API, params, key="explore-sorts")
        if not data:
            break
        for sort in data.get("sorts", []):
            sort_id = sort.get("sortId")
            name = sort.get("sortDisplayName", sort_id)
            if not sort_id or sort_id in seen_sorts:
                continue
            seen_sorts.add(sort_id)
            jobs.append((sort_id, name, sort.get("games", []), sort.get("nextPageToken")))
        sorts_token = data.get("nextSortsPageToken")
        if not sorts_token:
            break

    def _one(job):
        sort_id, name, games, page_token = job
        got = _fetch_sort_pages(client, sort_id, name, games, page_token, max_pages, prefilter)
        return name, got

    merged = {}
    for name, got in _pmap(_one, jobs, workers):
        tsay(f"   sort '{name}': +{len(got)}")
        for uid, src in got.items():
            merged.setdefault(uid, src)
    return merged


def discover_recommendations(client, universe_id):
    """One game's recommendations -> {universe_id: source}. Pure worker."""
    data = client.get(RECS_API.format(universe_id), {"maxRows": 25}, key="recommendations")
    found = {}
    if data:
        for g in data.get("games", []):
            uid = g.get("universeId")
            if uid and uid not in found:
                found[uid] = f"rec:{universe_id}"
    return found


def discover_creator_games(client, ctype, cid):
    """One creator's catalogue (newest first) -> {universe_id: source}. Pure worker."""
    url = (GROUP_GAMES_API if ctype == "Group" else USER_GAMES_API).format(cid)
    cursor, found, pages = "", {}, 0
    while pages < 3:
        # Desc = newest games first: fresh low-visit / high-CCU candidates surface
        # immediately instead of paging through years of old catalogue first.
        data = client.get(url, {"accessFilter": 2, "limit": 50, "sortOrder": "Desc", "cursor": cursor},
                          key="creator-games")
        if not data:
            break
        pages += 1
        for g in data.get("data", []):
            uid = g.get("id")
            if uid and uid not in found:
                found[uid] = f"creator:{ctype}:{cid}"
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
    return found

# =============================================================================
# STATS & ENRICHMENT
# =============================================================================
def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch_details(client, ids, workers=1):
    batches = list(chunks(list(ids), 50))  # 50, not 100 -- Roblox 400s ("Too many universe IDs") above 50

    def _one(batch):
        # one batch = one API call; callers merge the pity-free dicts
        data = client.get(GAMES_API, {"universeIds": ",".join(map(str, batch))})
        out = {}
        if data:
            for g in data.get("data", []):
                out[g["id"]] = g
        return out

    merged = {}
    for part in _pmap(_one, batches, workers):
        merged.update(part)
    return merged


def fetch_votes(client, ids, workers=1):
    batches = list(chunks(list(ids), 50))  # same 50-ID limit as the stats endpoint

    def _one(batch):
        data = client.get(VOTES_API, {"universeIds": ",".join(map(str, batch))})
        out = {}
        if data:
            for v in data.get("data", []):
                out[v["id"]] = (v.get("upVotes", 0), v.get("downVotes", 0))
        return out

    merged = {}
    for part in _pmap(_one, batches, workers):
        merged.update(part)
    return merged


_group_cache, _user_cache = {}, {}

def fetch_group(client, gid):
    with _CACHE_LOCK:
        if gid in _group_cache:
            return _group_cache[gid]
    grp = client.get(GROUP_API.format(gid)) or {}
    with _CACHE_LOCK:
        return _group_cache.setdefault(gid, grp)


def fetch_user(client, uid):
    with _CACHE_LOCK:
        if uid in _user_cache:
            return _user_cache[uid]
    user = client.get(USER_API.format(uid)) or {}
    with _CACHE_LOCK:
        return _user_cache.setdefault(uid, user)


_profile_cache = {}

def fetch_owner_profile_texts(client, user_id):
    """Owner profile page HTML -- no public JSON endpoint exposes a user's social links,
    so the rendered page is scanned for Discord/social URLs. Cached per owner."""
    with _CACHE_LOCK:
        if user_id in _profile_cache:
            return _profile_cache[user_id]
    html = ""
    try:
        with client._lock:
            client.calls += 1
        client.limiter.acquire()
        r = client.s.get(f"https://www.roblox.com/users/{user_id}/profile", timeout=30)
        if r.status_code == 200:
            html = r.text
    except requests.RequestException:
        pass
    with _CACHE_LOCK:
        _profile_cache[user_id] = html
    return html


_eco_cache = {}

def _role_is_owner(role):
    """True only for rank 255 (Owner). Ranks like 243/253 are senior members,
    NOT owners -- their groups' Discords must never attach to someone's game."""
    try:
        return int((role or {}).get("rank") or 0) == 255
    except (TypeError, ValueError):
        return False


def fetch_creator_ecosystem_texts(client, ctype, cid, owner_id):
    """Developer-ecosystem scan with an ownership gate: the creator's OWNED
    groups (role rank 255 -- social links + description + shout) and, for user
    creators, the descriptions of their other games. Fan/member groups the dev
    merely joined are NEVER scanned: their Discords belong to other people's
    communities and must not attach to this game.
    Returns (labeled_texts, labeled_links): [(source, text)] and
    [(type, url, title, source)]. Cached per creator."""
    key = (ctype, cid, owner_id)
    with _CACHE_LOCK:
        if key in _eco_cache:
            return _eco_cache[key]
    texts, links = [], []
    try:
        user_ids = []
        if ctype == "User" and cid:
            user_ids.append(cid)
        if owner_id and owner_id != cid:
            user_ids.append(owner_id)
        seen_groups = set()
        for u in user_ids[:2]:
            gr = client.get(f"https://groups.roblox.com/v1/users/{u}/groups/roles", key="user-groups")
            owned, skipped = 0, 0
            for g in (gr or {}).get("data", []):
                if not _role_is_owner(g.get("role")):
                    skipped += 1
                    continue
                if owned >= 8:
                    break
                gid = (g.get("group") or {}).get("id")
                if not gid or gid in seen_groups:
                    continue
                seen_groups.add(gid)
                owned += 1
                gd = client.get(GROUP_API.format(gid))
                gname = (gd or {}).get("name", "") or f"group {gid}"
                if gd:
                    if gd.get("description"):
                        texts.append((f"owned group description (same owner, other community): {gname}", gd["description"]))
                    shout = group_shout_text(gd)
                    if shout:
                        texts.append((f"owned group shout (same owner, other community): {gname}", shout))
                sl = client.get(GROUP_SOCIAL_API.format(gid), key="group-social-links")
                for l in (sl or {}).get("data", []):
                    links.append((l.get("type", ""), l.get("url", ""), l.get("title", ""),
                                  f"owned group socials (same owner, other community): {gname}"))
            if owned or skipped:
                tsay(f"   ecosystem: {owned} owned groups scanned, {skipped} member groups ignored")
        if ctype == "User" and cid:
            ug = client.get(USER_GAMES_API.format(cid),
                            {"accessFilter": 2, "limit": 50, "sortOrder": "Desc"}, key="creator-games")
            other = [g["id"] for g in (ug or {}).get("data", []) if g.get("id")][:10]
            if other:
                for g in fetch_details(client, other).values():
                    if g.get("description"):
                        texts.append((f"creator's other game (same dev): {g.get('name', g.get('id'))}",
                                      g["description"]))
    except Exception:
        pass   # ecosystem scan is best-effort enrichment -- never break the pass
    with _CACHE_LOCK:
        _eco_cache[key] = (texts, links)
    return texts, links


def fetch_social_links(client, universe_id, ctype, cid):
    """Official social links from game page + own group page.
    Returns [(type, url, title, source)] with provenance labels."""
    links = []
    data = client.get(GAME_SOCIAL_API.format(universe_id), key="game-social-links")
    if data:
        for l in data.get("data", []):
            links.append((l.get("type", ""), l.get("url", ""), l.get("title", ""),
                          "official game link"))
    if ctype == "Group":
        gname = ""
        try:
            gname = (fetch_group(client, cid) or {}).get("name", "")
        except Exception:
            pass
        src = f"official group link: {gname}" if gname else "official group link"
        data = client.get(GROUP_SOCIAL_API.format(cid), key="group-social-links")
        if data:
            for l in data.get("data", []):
                links.append((l.get("type", ""), l.get("url", ""), l.get("title", ""), src))
    return links


def extract_socials(labeled_texts, labeled_links):
    """Merge official links + regex-scanned texts. Inputs carry provenance:
    labeled_texts = [(source, text)], labeled_links = [(type, url, title, source)].
    Returns (socials, prov): socials is the usual dict of lists (discovery
    order, deduped, generic Roblox links removed); prov maps each found token
    to the label of where it was FIRST seen (most authoritative source first,
    so callers must pass game-page evidence before ecosystem evidence)."""
    out = {"discord": [], "youtube": [], "tiktok": [], "twitter": [], "twitch": [], "instagram": [], "other": []}
    prov = {}

    def note(token, source):
        if token and token not in prov:
            prov[token] = source

    for t, url, title, source in labeled_links:
        tl, ul = (t or "").lower(), (url or "")
        if "discord" in tl or DISCORD_RE.search(ul):
            m = DISCORD_RE.search(ul)
            code = m.group(1) if m else ul
            out["discord"].append(code)
            note(code, source)
        elif "youtube" in tl:
            out["youtube"].append(ul); note(ul, source)
        elif "tiktok" in tl:
            out["tiktok"].append(ul); note(ul, source)
        elif "twitter" in tl or tl == "x":
            out["twitter"].append(ul); note(ul, source)
        elif "twitch" in tl:
            out["twitch"].append(ul); note(ul, source)
        elif "instagram" in tl:
            out["instagram"].append(ul); note(ul, source)
        elif ul:
            out["other"].append(f"{t}:{ul}"); note(f"{t}:{ul}", source)
    for source, text in labeled_texts:
        if not text:
            continue
        for m in DISCORD_RE.finditer(text):
            out["discord"].append(m.group(1))
            note(m.group(1), source)
        for m in FACEBOOK_RE.finditer(text):
            # Facebook has no dedicated column -- presence counts via other_links.
            out["other"].append(m.group(0))
            note(m.group(0), source)
        for k, rx in SOCIAL_RES.items():
            for m in rx.finditer(text):
                out[k].append(m.group(0))
                note(m.group(0), source)
    for k in out:
        out[k] = [u for u in dict.fromkeys(out[k]) if not GENERIC_SOCIAL_RE.search(u)]
    return out, prov


def source_trust(source):
    """Lower = more authoritative. A valid invite from the game page always
    outranks one from the wider ecosystem, so a stray-but-live invite found
    far from the game can never shadow the real one."""
    s = (source or "").lower()
    if s.startswith("official game link"):
        return 0
    if s.startswith("game description"):
        return 1
    if s.startswith("official group link"):
        return 2
    if s.startswith("group description") or s.startswith("group shout"):
        return 3
    if s.startswith("owner bio") or s.startswith("owner profile"):
        return 4
    if "owned group" in s or "other game" in s:
        return 5
    return 6


def collect_social_evidence(client, uid, det):
    """Authoritative, ownership-gated evidence bundle for one universe.
    Order is trust order: game page -> own group -> owner -> owned ecosystem.
    Fan/member groups are never scanned (see fetch_creator_ecosystem_texts).
    Returns a dict with creator/owner fields plus labeled_texts/labeled_links
    ready for extract_socials()."""
    creator = det.get("creator") or {}
    ctype, cid, cname = creator.get("type", ""), creator.get("id"), creator.get("name", "")
    labeled_texts = [("game description", det.get("description", ""))]
    group_name = ""
    if ctype == "Group":
        grp = fetch_group(client, cid)
        group_name = grp.get("name", "")
        creator_url = f"https://www.roblox.com/groups/{cid}"
        labeled_texts.append(("group description", grp.get("description", "")))
        labeled_texts.append(("group shout", group_shout_text(grp)))
        labeled_texts.append(("group name", group_name))
        owner = grp.get("owner") or {}
        owner_name, owner_id = owner.get("username", ""), owner.get("userId")
        group_members = grp.get("memberCount", "")
    else:
        creator_url = f"https://www.roblox.com/users/{cid}/profile"
        owner_name, owner_id, group_members = cname, cid, ""
    owner_url = f"https://www.roblox.com/users/{owner_id}/profile" if owner_id else ""
    owner_desc = ""
    if owner_id:
        owner_desc = (fetch_user(client, owner_id) or {}).get("description", "")
        labeled_texts.append(("owner bio", owner_desc))
        labeled_texts.append(("owner profile", fetch_owner_profile_texts(client, owner_id)))
    owner_roblox_signal = "YES" if re.search(r"\broblox\b", owner_desc, re.I) else ""
    discord_name_signal = ("YES" if has_discord_name_signal(cname, group_name, owner_name)
                           else "")

    labeled_links = fetch_social_links(client, uid, ctype, cid)
    eco_texts, eco_links = fetch_creator_ecosystem_texts(client, ctype, cid, owner_id)
    labeled_texts.extend(eco_texts)
    labeled_links.extend(eco_links)
    return {
        "ctype": ctype, "cid": cid, "cname": cname,
        "creator_url": creator_url, "group_name": group_name,
        "group_members": group_members, "owner_name": owner_name,
        "owner_id": owner_id, "owner_url": owner_url, "owner_desc": owner_desc,
        "owner_roblox_signal": owner_roblox_signal,
        "discord_name_signal": discord_name_signal,
        "labeled_texts": labeled_texts, "labeled_links": labeled_links,
    }


def rank_discord_codes(codes, prov):
    """Deduped invite codes, most-authoritative source first (stable for ties)."""
    return sorted(dict.fromkeys(codes), key=lambda c: source_trust(prov.get(c, "")))


def has_discord_name_signal(*names):
    """True if any creator/group/owner name advertises a Discord community."""
    return any(n and DISCORD_NAME_RE.search(str(n)) for n in names)


def group_shout_text(grp):
    """Group shout body -- discords are often pinned there, not in the description."""
    try:
        return ((grp or {}).get("shout") or {}).get("body") or ""
    except AttributeError:
        return ""


def classify(visits, active):
    for label, max_v, min_a in TIERS:
        if visits < max_v and active >= min_a:
            return label
    return None


def parse_dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None

# =============================================================================
# MAIN
# =============================================================================
def main():
    global _SEEN_DAYS
    # Windows consoles/pipes can be non-UTF-8; game titles contain emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="Find under-the-radar high-concurrency Roblox games.")
    ap.add_argument("--keyword-mode", choices=["fast", "full", "max"], default="full",
                    help="keyword universe size: fast (~900) | full (~1900) | max (~15000 combos). "
                         "Per-pass cost is capped by --keyword-limit regardless of mode")
    ap.add_argument("--extra-keywords", nargs="*", default=[], help="keywords to add")
    ap.add_argument("--search-pages", type=int, default=5)
    ap.add_argument("--sort-pages", type=int, default=25)
    ap.add_argument("--no-sorts", action="store_true")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--no-snowball", action="store_true", help="skip recommendation + creator expansion")
    ap.add_argument("--snowball-hops", type=int, default=2)
    ap.add_argument("--no-discord", action="store_true", help="skip social/discord enrichment")
    ap.add_argument("--no-social-filter", action="store_true",
                    help="keep matches with no social links (default: socials required, Discord required for group games)")
    ap.add_argument("--no-exclusions", action="store_true",
                    help="report modded/reuploaded/NSFW/non-English games too (default: excluded)")
    ap.add_argument("--seeds", nargs="*", default=[], help="extra universe IDs to check")
    ap.add_argument("--seed-file", action="append", default=[],
                    help="CSV file(s) with a universe_id column to re-check every pass "
                         "(e.g. watchlist_history.csv = yesterday's near-misses get "
                         "fresh stats until they graduate or cool; repeatable)")
    ap.add_argument("--seed-max", type=int, default=300,
                    help="max recent IDs taken per seed file (newest rows first)")
    ap.add_argument("--no-prefilter", action="store_true", help="don't skip <50-player search results")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="average seconds between requests per host (token bucket rate); "
                         "lower to 0.2 for more speed if the log shows no [429]s")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel request threads (hides network latency; the request "
                         "rate still respects --delay). 1 = sequential, deterministic")
    ap.add_argument("--loop", type=int, default=0, help="repeat every N minutes (0 = once)")
    ap.add_argument("--seen-days", type=int, default=3,
                    help="skip games already evaluated within this many days (scan memory)")
    ap.add_argument("--no-seen", action="store_true", help="disable the seen-ledger skip")
    ap.add_argument("--no-verify", action="store_true", help="skip second-snapshot match verification")
    ap.add_argument("--verify-wait", type=int, default=45, help="seconds before the verification snapshot")
    ap.add_argument("--golden", default="12-16,20-2",
                    help="local-time windows to run in, e.g. '12-16,20-2' (start-end hours; "
                         "start>end wraps midnight). Covers Asia peak + EU evening + US peak")
    ap.add_argument("--no-golden", action="store_true", help="disable golden-hours scheduling (run 24/7)")
    ap.add_argument("--window-gap", type=int, default=10, help="minutes between passes inside a golden window")
    ap.add_argument("--keyword-limit", type=int, default=0,
                    help="max keywords per pass; rotates through the full list via "
                         "keyword_cursor.json (0 = all keywords every pass). Rolling windows "
                         "keep each pass to ~15 min while covering everything over N passes.")
    ap.add_argument("--cursor-file", default="keyword_cursor.json",
                    help="file holding the rolling keyword offset (literal path)")
    ap.add_argument("--csv", action="store_true", help="print matches CSV to stdout (redirect to save)")
    ap.add_argument("--watchlist", action="store_true", help="print near-miss watchlist CSV to stdout")
    ap.add_argument("--watchlist-file", default="",
                    help="append near-miss watchlist rows to this CSV file each pass "
                         "(feeds the CRM Early Radar view; e.g. watchlist_history.csv)")
    ap.add_argument("--nodiscord-file", default="",
                    help="append qualified-but-no-presence match rows to this CSV file "
                         "each pass (feeds the CRM No Discord tab; e.g. nodiscord_history.csv)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cookie = os.environ.get("ROBLOSECURITY")
    if cookie:
        say("Cookie loaded from ROBLOSECURITY env var.")
    client = Client(cookie=cookie, delay=args.delay, verbose=args.verbose)

    all_keywords = build_keywords(args.keyword_mode)
    all_keywords = list(dict.fromkeys(all_keywords + args.extra_keywords))
    # ---- rolling keyword window: each pass covers a rotating slice so a pass
    # finishes in ~15 min but the full sweep is covered over consecutive passes.
    # Sorts + snowball still run fully every pass (fast + highest yield).
    if args.keyword_limit and args.keyword_limit > 0 and len(all_keywords) > args.keyword_limit:
        cursor = 0
        try:
            with open(args.cursor_file, encoding="utf-8") as f:
                cursor = int(json.load(f).get("offset", 0))
        except (OSError, ValueError, AttributeError):
            cursor = 0
        cursor %= len(all_keywords)
        wrapped = all_keywords + all_keywords[:args.keyword_limit]
        keywords = wrapped[cursor:cursor + args.keyword_limit]
        say(f"Rolling keywords: {len(keywords)}/{len(all_keywords)} this pass "
            f"(offset {cursor}, advances {args.keyword_limit}/pass).")
        try:
            blob = json.dumps({"offset": (cursor + args.keyword_limit) % len(all_keywords)}).encode("utf-8")
            fd = os.open(args.cursor_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
        except OSError:
            pass
    else:
        keywords = all_keywords
    _SEEN_DAYS = 0 if args.no_seen else max(0, args.seen_days)
    load_seen()
    if _SEEN_DAYS:
        say(f"Scan memory: {len(_SEEN)} games on file -- matches re-check after {_SEEN_DAYS} day(s), "
            f"near-misses after {NEAR_MISS_SEEN_HOURS}h (catches CCU growth at a different hour).")

    windows = [] if args.no_golden else parse_windows(args.golden)
    if windows:
        say(f"Golden hours (your PC's local time): {args.golden} -- passes run only inside these windows.")

    first_pass = True
    while True:
        if args.loop and windows and not in_window(windows):
            nxt = next_window_start(windows)
            mins = int((nxt - datetime.now()).total_seconds() // 60) + 1
            say(f"\nOutside golden hours -- sleeping until {nxt:%H:%M} local ({mins} min). Next pass starts then.")
            while datetime.now() < nxt:
                time.sleep(min(300, max(1, (nxt - datetime.now()).total_seconds())))
        run(client, keywords, args, first_pass)
        first_pass = False
        if not args.loop:
            break
        if windows and in_window(windows):
            say(f"\nStill inside golden hours -- next pass in {args.window_gap} min.\n")
            time.sleep(args.window_gap * 60)
        elif not windows:
            say(f"\nSleeping {args.loop} min before next pass...\n")
            time.sleep(args.loop * 60)
        # golden + outside window: the top of the loop sleeps until the next window opens


def load_seed_ids(path, max_n):
    """Recent universe IDs from a CSV feed (watchlist / results history) so
    near-misses get fresh stats every pass until they graduate or cool.
    Missing/unreadable files yield nothing (never break a pass)."""
    try:
        fh = open(path, encoding="utf-8-sig", newline="")
    except OSError:
        return []
    ids = []
    try:
        with fh:
            for rec in csv.DictReader(fh):
                try:
                    uid = int((rec.get("universe_id") or "").strip())
                except (ValueError, AttributeError):
                    continue
                if uid:
                    ids.append(uid)
    except (csv.Error, UnicodeDecodeError):
        return ids[-max_n:] if max_n else ids
    ids = list(dict.fromkeys(ids))   # dedupe, oldest first
    return ids[-max_n:] if max_n and len(ids) > max_n else ids


def is_permanent_skip(uid):
    """A never-expiring ledger entry (CRM rejection). Only these survive
    seed-file re-checks -- everything else gets fresh stats every pass."""
    ts = _SEEN.get(str(uid))
    return bool(ts) and ts.startswith("9999")


def run(client, keywords, args, first_pass=True):
    t0 = time.time()
    run.calls_before = client.calls
    now = datetime.now(timezone.utc)
    say(f"=== Roblox Acquisition Finder v2  {now:%Y-%m-%d %H:%M UTC} ===")
    if not client.has_cookie:
        say("(!) No ROBLOSECURITY cookie set. Stats + keyword search work, but official game "
            "Discord/social links will be skipped. Description-based Discord detection still runs.")
    prefilter = not args.no_prefilter
    load_seen()   # pick up CRM-written permanent skips + changes made since boot

    # ---------------- discovery (keywords + sorts run in the pool; merges
    # apply scan-memory + dup filtering in the main thread)
    found = {}
    for s in args.seeds:
        try: found[int(s)] = "seed"
        except ValueError: pass
    for path in getattr(args, "seed_file", []) or []:
        ids = load_seed_ids(path, args.seed_max)
        n = 0
        for uid in ids:
            if uid not in found and not is_permanent_skip(uid):
                found[uid] = f"seedfile:{os.path.basename(path)}"
                n += 1
        if ids:
            say(f"   reseeds from {os.path.basename(path)}: {n} queued ({len(ids)} recent)")
    workers = max(1, args.workers)

    def absorb(new_ids):
        added = 0
        for uid, src in new_ids.items():
            if uid not in found and not recently_seen(uid):
                found[uid] = src
                added += 1
        return added

    if not args.no_sorts:
        say(f"\n[1/5] Scanning Discover sorts ({workers} workers)...")
        absorb(discover_sorts(client, args.sort_pages, prefilter, workers))

    if not args.no_search:
        say(f"\n[2/5] Searching {len(keywords)} keywords x {args.search_pages} pages "
            f"({workers} workers)...")
        done = 0

        def _kw(kw):
            return kw, discover_search(client, kw, args.search_pages, prefilter)

        if workers <= 1:
            for kw in keywords:
                done += 1
                new = absorb(_kw(kw)[1])
                say(f"   ({done}/{len(keywords)}) '{kw}': +{new}   total {len(found)}")
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(keywords))) as ex:
                futs = {ex.submit(_kw, kw): kw for kw in keywords}
                for fut in as_completed(futs):
                    kw, got = fut.result()
                    done += 1
                    new = absorb(got)
                    tsay(f"   ({done}/{len(keywords)}) '{kw}': +{new}   total {len(found)}")

    say(f"\n[3/5] Pulling exact stats for {len(found)} games...")
    details = fetch_details(client, list(found.keys()), workers)
    t_stats_done = time.time()  # anchor for the verification gap (see below)

    def candidates_from(detail_map):
        m, nm, excl = [], [], {}
        for uid, g in detail_map.items():
            v, a = g.get("visits") or 0, g.get("playing") or 0
            reason = None if args.no_exclusions else is_excluded(g.get("name", ""), g.get("description", ""))
            if reason:
                excl[reason] = excl.get(reason, 0) + 1
                continue
            tier = classify(v, a)
            if tier: m.append(uid)
            elif v < ABS_MAX_VISITS and a >= NEAR_MISS_MIN_ACTIVE: nm.append(uid)
        if excl:
            say("   excluded do-not-buy: " + ", ".join(f"{k} x{v}" for k, v in sorted(excl.items())))
        return m, nm

    matches, near = candidates_from(details)
    say(f"   {len(matches)} matches, {len(near)} near-misses")
    mark_seen(details.keys())                    # everything evaluated: full span
    mark_seen(near, hours=NEAR_MISS_SEEN_HOURS)  # near-misses: re-check at another hour soon

    # ---------------- snowball (recs + creator catalogues in the pool;
    # merges apply scan-memory + dup filtering in the main thread)
    if not args.no_snowball:
        frontier = matches + near
        for hop in range(1, args.snowball_hops + 1):
            say(f"\n[4/5] Snowball hop {hop}: recommendations + creator games for {len(frontier)} seeds...")
            before = set(found.keys())
            for got in _pmap(lambda u: discover_recommendations(client, u), frontier, workers):
                absorb(got)
            creators = []
            seen_ck = set()
            for uid in frontier:
                c = (details.get(uid, {}) or {}).get("creator") or {}
                ck = (c.get("type"), c.get("id"))
                if c.get("id") and ck not in seen_ck:
                    seen_ck.add(ck)
                    creators.append(ck)
            for got in _pmap(lambda ck: discover_creator_games(client, ck[0], ck[1]), creators, workers):
                absorb(got)
            new_ids = [u for u in found if u not in before]
            say(f"   +{len(new_ids)} new games from snowball")
            if not new_ids:
                break
            new_details = fetch_details(client, new_ids, workers)
            details.update(new_details)
            nm, nn = candidates_from(new_details)
            mark_seen(new_details.keys())
            mark_seen(nn, hours=NEAR_MISS_SEEN_HOURS)
            say(f"   -> {len(nm)} new matches, {len(nn)} new near-misses")
            matches += nm; near += nn
            frontier = nm + nn
    else:
        say("\n[4/5] Snowball skipped")

    # ---------------- match verification (second snapshot: kills one-off spikes / API flake)
    # OVERLAP: the wait is measured from the first stats pull, so it hides
    # inside the snowball work above (usually the longest phase). Only if the
    # snowball was skipped or unusually fast do we sleep the remainder.
    if not args.no_verify and matches:
        uniq = list(dict.fromkeys(matches))
        rest = args.verify_wait - (time.time() - t_stats_done)
        if rest > 0:
            say(f"\n[4.6/5] Verifying {len(uniq)} matches with a second stats snapshot ({rest:.0f}s)...")
            time.sleep(rest)
        else:
            say(f"\n[4.6/5] Verifying {len(uniq)} matches with a second stats snapshot "
                f"(snowball covered the {args.verify_wait}s gap)...")
        fresh = fetch_details(client, uniq, workers)
        kept = []
        for uid in uniq:
            g2 = fresh.get(uid)
            if not g2:
                kept.append(uid)   # refetch failed -- keep the original rather than drop blindly
                continue
            mark_seen([uid])
            if classify(g2.get("visits") or 0, g2.get("playing") or 0):
                details[uid] = g2
                kept.append(uid)
            else:
                say(f"   verified out: '{details.get(uid, {}).get('name', uid)}' no longer qualified on re-check")
        say(f"   {len(kept)}/{len(uniq)} matches confirmed")
        matches = kept

    # ---------------- enrichment (one worker per match; _STATE + progress
    # under locks; result order matches the sequential version)
    say(f"\n[5/5] Enriching {len(matches)} matches (votes, owners, Discord/socials)...")
    votes = fetch_votes(client, matches, workers)

    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    done_count = [0]

    def _enrich(uid):
        g = details[uid]
        visits, active, favs = g.get("visits") or 0, g.get("playing") or 0, g.get("favoritedCount") or 0
        tier = classify(visits, active)
        up, down = votes.get(uid, (0, 0))
        # Authoritative, ownership-gated evidence (game page -> own group ->
        # owner -> OWNED ecosystem). Fan/member groups are never scanned, so a
        # stray-but-live invite from someone else's community can neither
        # attach here nor outrank the real one (trust-ordered verification).
        ev = collect_social_evidence(client, uid, g)
        ctype, cid, cname = ev["ctype"], ev["cid"], ev["cname"]
        creator_url, group_name = ev["creator_url"], ev["group_name"]
        group_members = ev["group_members"]
        owner_name, owner_id, owner_url = ev["owner_name"], ev["owner_id"], ev["owner_url"]
        owner_roblox_signal = ev["owner_roblox_signal"]
        discord_name_signal = ev["discord_name_signal"]

        socials = {"discord": [], "youtube": [], "tiktok": [], "twitter": [], "twitch": [], "instagram": [], "other": []}
        discord_info = {"valid": False, "name": "", "members": "", "online": ""}
        discord_url = ""
        discord_via = ""
        if not args.no_discord:
            socials, prov = extract_socials(ev["labeled_texts"], ev["labeled_links"])
            for code in rank_discord_codes(socials["discord"], prov):
                info = verify_discord(code)
                if info["valid"]:
                    discord_info, discord_url = info, f"https://discord.gg/{code}"
                    discord_via = prov.get(code, "")
                    break
            if not discord_url and socials["discord"]:
                first = rank_discord_codes(socials["discord"], prov)[0]
                discord_url = f"https://discord.gg/{first} (UNVERIFIED/expired)"
                discord_via = prov.get(first, "")

        created, updated = parse_dt(g.get("created")), parse_dt(g.get("updated"))
        age_days = (datetime.now(timezone.utc) - created).days if created else ""

        # ---- state: peak + growth (in-memory, lives as long as this process)
        key = str(uid)
        with _STATE_LOCK:
            prev = _STATE.get(key, {})
            peak = max(active, prev.get("peak_active", 0))
            growth_per_day = ""
            if prev.get("last_ts") and prev.get("last_visits") is not None:
                hrs = (datetime.now(timezone.utc) - datetime.fromisoformat(prev["last_ts"])).total_seconds() / 3600
                if hrs >= 1:
                    growth_per_day = round((visits - prev["last_visits"]) / hrs * 24)
            is_new = key not in _STATE
            _STATE[key] = {"peak_active": peak, "first_seen": prev.get("first_seen", now_iso),
                           "first_visits": prev.get("first_visits", visits),
                           "last_visits": visits, "last_ts": now_iso, "name": g.get("name")}

        heat = round(active / visits * 1000, 2) if visits else 0
        has_discord = bool(discord_info["valid"])
        score = (TIER_WEIGHT[tier] + (200 if has_discord else 0) + min(heat * 5, 200)
                 + (100 if isinstance(age_days, int) and age_days <= 30 else 0)
                 + (min(int(discord_info["members"] or 0) / 50, 100) if has_discord else 0))

        row = {
            "priority_score": round(score),
            "tier": tier,
            "new_this_run": "NEW" if is_new else "",
            "title": g.get("name", ""),
            "game_url": f"https://www.roblox.com/games/{g.get('rootPlaceId')}/",
            "has_discord": "YES" if has_discord else "",
            "discord_url": discord_url,
            "discord_server": discord_info["name"],
            "discord_members": discord_info["members"],
            "discord_online": discord_info["online"],
            "discord_via": discord_via,
            "active": active,
            "peak_active_seen": peak,
            "visits": visits,
            "visits_growth_per_day": growth_per_day,
            "favorites": favs,
            "likes": up, "dislikes": down,
            "like_ratio": round(up / (up + down) * 100, 1) if (up + down) else "",
            "heat_active_per_1k_visits": heat,
            "age_days": age_days,
            "created": created.strftime("%Y-%m-%d") if created else "",
            "last_updated": updated.strftime("%Y-%m-%d") if updated else "",
            "creator_type": ctype, "creator_name": cname, "creator_url": creator_url,
            "group_members": group_members,
            "owner_name": owner_name, "owner_url": owner_url,
            "owner_roblox_signal": owner_roblox_signal,
            "discord_name_signal": discord_name_signal,
            "youtube": " | ".join(socials["youtube"]),
            "tiktok": " | ".join(socials["tiktok"]),
            "twitter_x": " | ".join(socials["twitter"]),
            "twitch": " | ".join(socials["twitch"]),
            "instagram": " | ".join(socials["instagram"]),
            "other_links": " | ".join(socials["other"]),
            "genre": g.get("genre", ""), "max_players": g.get("maxPlayers", ""),
            "universe_id": uid, "place_id": g.get("rootPlaceId"),
            "found_via": found.get(uid, ""),
            "checked_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        }
        with _STATE_LOCK:
            done_count[0] += 1
            if done_count[0] % 10 == 0:
                say(f"   enriched {done_count[0]}/{len(matches)}")
        return row

    for row in _pmap(_enrich, matches, workers):
        rows.append(row)

    rows.sort(key=lambda r: (r["tier"], r["has_discord"] != "YES", -r["priority_score"]))

    # ---------------- social presence filter (keep only games with an online presence)
    # Games that qualify on numbers but have zero presence are NOT thrown away:
    # they go to the no-Discord feed (CRM "No Discord" tab) for manual review.
    dropped_rows = []
    if not args.no_social_filter and not args.no_discord:
        kept, dropped = [], 0
        for r in rows:
            has_any = any(r[c] for c in ("discord_url", "youtube", "tiktok", "twitter_x",
                                         "twitch", "instagram", "other_links"))
            if has_any or r.get("owner_roblox_signal") == "YES" or r.get("discord_name_signal") == "YES":
                kept.append(r)
            else:
                dropped += 1
                dropped_rows.append(r)
        say(f"Social filter: kept {len(kept)}/{len(rows)} matches "
            f"(dropped {dropped} with no social presence at all)")
        rows = kept
    if getattr(args, "nodiscord_file", None) and dropped_rows:
        # Append-only feed of qualified-but-uncontactable games. Same schema as
        # the match CSV; header written once (CRM parser tolerates repeats).
        try:
            need_header = (not os.path.exists(args.nodiscord_file)
                           or os.path.getsize(args.nodiscord_file) == 0)
            with open(args.nodiscord_file, "a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(dropped_rows[0].keys()))
                if need_header:
                    w.writeheader()
                w.writerows(dropped_rows)
        except OSError as e:
            say(f"   [!] could not write no-discord file: {e}")

    # ---------------- watchlist (near misses: 50+ CCU, not yet in a tier --
    # tomorrow's matches; a rising game shows up here before it qualifies)
    watch = []
    watch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for uid in near:
        g = details[uid]
        watch.append({"title": g.get("name", ""), "game_url": f"https://www.roblox.com/games/{g.get('rootPlaceId')}/",
                      "active": g.get("playing") or 0, "visits": g.get("visits") or 0,
                      "creator": (g.get("creator") or {}).get("name", ""), "universe_id": uid,
                      "checked_at_utc": watch_ts})
    watch.sort(key=lambda r: -r["active"])
    if getattr(args, "watchlist_file", None) and watch:
        # Append-only radar feed for the CRM's Early Radar view; header is
        # written once (repeated headers across passes are tolerated by the CRM
        # parser, same multiblock scheme as results_history.csv).
        try:
            need_header = (not os.path.exists(args.watchlist_file)
                           or os.path.getsize(args.watchlist_file) == 0)
            with open(args.watchlist_file, "a", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(watch[0].keys()))
                if need_header:
                    w.writeheader()
                w.writerows(watch)
        except OSError as e:
            say(f"   [!] could not write watchlist file: {e}")

    # ---------------- stdout data blocks (script writes no files)
    wrote_stdout = False
    if args.csv and rows:
        if first_pass:
            sys.stdout.write("\ufeff")   # BOM so Excel opens UTF-8 correctly
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
        wrote_stdout = True
    if args.watchlist and watch:
        if args.csv and rows:
            print("==== WATCHLIST (near misses: 50+ concurrent, not yet in a tier) ====")
        w = csv.DictWriter(sys.stdout, fieldnames=list(watch[0].keys()))
        w.writeheader(); w.writerows(watch)
        wrote_stdout = True
    if wrote_stdout:
        sys.stdout.flush()

    # ---------------- summary (stderr)
    pass_calls = client.calls - run.calls_before if hasattr(run, "calls_before") else client.calls
    say(f"\nDone in {(time.time() - t0) / 60:.1f} min, {pass_calls} API calls this pass, "
        f"{len(details)} games checked, {len(rows)} matches ({len(dropped_rows)} no-discord), "
        f"{len(watch)} on watchlist.\n")
    counts = {}
    for r in rows:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    for label, _, _ in TIERS:
        d = sum(1 for r in rows if r["tier"] == label and r["has_discord"] == "YES")
        say(f"  {label:<20} {counts.get(label, 0):>4}   (with Discord: {d})")
    say("")
    for r in rows[:40]:
        dc = f" DISCORD {r['discord_members']}m" if r["has_discord"] else ""
        say(f"[{r['tier']}]{dc} {r['title'][:36]:<36} active={r['active']:<6} visits={r['visits']:<8} "
            f"age={r['age_days']}d  {r['creator_name']} ({r['creator_type']})")
        say(f"      {r['game_url']}")
    if not rows:
        say("No matches this pass. Run during peak hours (see docs), or try --no-prefilter / --keyword-mode max.")
    save_seen()


if __name__ == "__main__":
    main()
