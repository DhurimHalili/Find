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
    the creator's ecosystem (their other groups incl. shouts + their other games), or a
    creator/group/owner NAME that advertises a Discord community (discord_name_signal).
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
import time
import uuid
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
    r"\b(modded|mods?|modmenu|admin ?panels?|owner ?panels?|adminz|free ?admin|infs?|"
    r"uncopylocked|full ?source|place ?file)\b|\b[x\u00d7]\d{3,}|\b\d{3,}[x\u00d7]\b|\+\d{4,}\b", re.I)
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


def is_excluded(title, description=""):
    """Returns a rejection reason string, or None if the game passes the do-not-buy rules."""
    t = title or ""
    if EXCL_MODDED_RE.search(t):
        return "modded"
    if EXCL_NSFW_RE.search(t):
        return "nsfw"
    if EXCL_REUPLOAD_RE.search(t):
        return "reuploaded"
    if EXCL_FOREIGN_SCRIPT_RE.search(t) or EXCL_FOREIGN_WORDS_RE.search(t):
        return "non-english"
    if EXCL_REUPLOAD_DESC_RE.search(description or ""):
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


def build_keywords(mode):
    if mode == "fast":
        kws = KW_TRENDING_HITS + KW_TITLE_TAGS + KW_GENRES[:80]
    elif mode == "full":
        kws = (KW_TRENDING_HITS + KW_TITLE_TAGS + KW_GENRES + KW_MECHANICS + KW_THEMES_MEMES
               + KW_ANIME_IP + KW_BROAD)
    else:  # max
        kws = (KW_TRENDING_HITS + KW_TITLE_TAGS + KW_GENRES + KW_MECHANICS + KW_THEMES_MEMES
               + KW_ANIME_IP + KW_BROAD)
        kws += [f"{p} {n}" for p in COMBO_PREFIXES for n in COMBO_NOUNS]
        kws += [f"{n} {s}" for n in COMBO_NOUNS for s in COMBO_SUFFIXES]
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

    def get(self, url, params=None, retries=6, key=None):
        if key and key in self.dead_endpoints:
            return None
        backoff = 5
        for _ in range(retries):
            try:
                time.sleep(self.delay)
                r = self.s.get(url, params=params, timeout=30)
                self.calls += 1
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    if self.verbose:
                        print(f"   [429] sleeping {backoff}s", file=sys.stderr)
                    time.sleep(backoff); backoff = min(backoff * 2, 120); continue
                if r.status_code in (401, 403, 404):
                    if key:
                        self.dead_endpoints.add(key)
                        say(f"   [!] {key} returned {r.status_code} - skipping this source for the rest of the run.")
                    return None
                if r.status_code >= 500:
                    time.sleep(backoff); continue
                if self.verbose:
                    say(f"   [{r.status_code}] {url}")
                return None
            except (requests.RequestException, ValueError) as e:
                if self.verbose:
                    say(f"   [error] {e}")
                time.sleep(backoff)
        return None


def say(msg):
    """Progress/diagnostics go to stderr so stdout stays clean for redirected CSVs."""
    print(msg, file=sys.stderr)


_discord_session = requests.Session()
_discord_session.headers.update({"User-Agent": "Mozilla/5.0 (RobloxFinder/2.0)"})
_discord_cache = {}

def verify_discord(code):
    """Returns dict(valid, name, members, online) using Discord's public invite endpoint."""
    code = code.strip()
    if code in _discord_cache:
        return _discord_cache[code]
    result = {"valid": False, "name": "", "members": "", "online": ""}
    for _ in range(4):
        try:
            time.sleep(0.6)
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
    _discord_cache[code] = result
    return result

# =============================================================================
# DISCOVERY
# =============================================================================
def discover_search(client, keyword, max_pages, found, prefilter):
    token, pages, new = "", 0, 0
    while pages < max_pages:
        data = client.get(OMNI_API, {"searchQuery": keyword, "pageToken": token,
                                     "sessionId": client.session_id, "pageType": "all"}, key="omni-search")
        if not data:
            break
        pages += 1
        for group in data.get("searchResults", []):
            for item in group.get("contents", []):
                uid = item.get("universeId")
                if not uid:
                    continue
                pc = item.get("playerCount")
                if prefilter and isinstance(pc, int) and pc < NEAR_MISS_MIN_ACTIVE:
                    continue
                if uid not in found and not recently_seen(uid):
                    found[uid] = f"search:{keyword}"; new += 1
        token = data.get("nextPageToken")
        if not token:
            break
    return new


def discover_sorts(client, max_pages, found, prefilter):
    sorts_token, seen_sorts, total_new = None, set(), 0
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
            games, page_token, pages, new = sort.get("games", []), sort.get("nextPageToken"), 0, 0
            while True:
                for g in games:
                    uid = g.get("universeId")
                    if not uid:
                        continue
                    pc = g.get("playerCount")
                    if prefilter and isinstance(pc, int) and pc < NEAR_MISS_MIN_ACTIVE:
                        continue
                    if uid not in found and not recently_seen(uid):
                        found[uid] = f"sort:{name}"; new += 1
                pages += 1
                if not page_token or pages >= max_pages:
                    break
                cont = client.get(SORT_CONTENT_API, {"sessionId": client.session_id, "sortId": sort_id,
                                                     "pageToken": page_token, "device": "computer",
                                                     "country": "all"}, key="explore-sort-content")
                if not cont:
                    break
                games, page_token = cont.get("games", []), cont.get("nextPageToken")
            say(f"   sort '{name}': +{new}")
            total_new += new
        sorts_token = data.get("nextSortsPageToken")
        if not sorts_token:
            break
    return total_new


def discover_recommendations(client, universe_id, found):
    data = client.get(RECS_API.format(universe_id), {"maxRows": 12}, key="recommendations")
    new = 0
    if not data:
        return 0
    for g in data.get("games", []):
        uid = g.get("universeId")
        if uid and uid not in found and not recently_seen(uid):
            found[uid] = f"rec:{universe_id}"; new += 1
    return new


def discover_creator_games(client, ctype, cid, found):
    url = (GROUP_GAMES_API if ctype == "Group" else USER_GAMES_API).format(cid)
    cursor, new, pages = "", 0, 0
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
            if uid and uid not in found and not recently_seen(uid):
                found[uid] = f"creator:{ctype}:{cid}"; new += 1
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
    return new

# =============================================================================
# STATS & ENRICHMENT
# =============================================================================
def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch_details(client, ids):
    out = {}
    for batch in chunks(ids, 50):   # 50, not 100 -- Roblox 400s ("Too many universe IDs") above 50
        data = client.get(GAMES_API, {"universeIds": ",".join(map(str, batch))})
        if data:
            for g in data.get("data", []):
                out[g["id"]] = g
    return out


def fetch_votes(client, ids):
    out = {}
    for batch in chunks(ids, 50):   # same 50-ID limit as the stats endpoint
        data = client.get(VOTES_API, {"universeIds": ",".join(map(str, batch))})
        if data:
            for v in data.get("data", []):
                out[v["id"]] = (v.get("upVotes", 0), v.get("downVotes", 0))
    return out


_group_cache, _user_cache = {}, {}

def fetch_group(client, gid):
    if gid not in _group_cache:
        _group_cache[gid] = client.get(GROUP_API.format(gid)) or {}
    return _group_cache[gid]


def fetch_user(client, uid):
    if uid not in _user_cache:
        _user_cache[uid] = client.get(USER_API.format(uid)) or {}
    return _user_cache[uid]


_profile_cache = {}

def fetch_owner_profile_texts(client, user_id):
    """Owner profile page HTML -- no public JSON endpoint exposes a user's social links,
    so the rendered page is scanned for Discord/social URLs. Cached per owner."""
    if user_id in _profile_cache:
        return _profile_cache[user_id]
    html = ""
    try:
        client.calls += 1
        time.sleep(client.delay)
        r = client.s.get(f"https://www.roblox.com/users/{user_id}/profile", timeout=30)
        if r.status_code == 200:
            html = r.text
    except requests.RequestException:
        pass
    _profile_cache[user_id] = html
    return html


_eco_cache = {}

def fetch_creator_ecosystem_texts(client, ctype, cid, owner_id):
    """Developer-ecosystem scan: the creator's groups (social links + description) and,
    for user creators, the descriptions of their other games. Catches Discords that are
    not on the game page itself -- the dev's community is the same across their games.
    Returns (extra_texts, extra_official_links). Cached per creator."""
    key = (ctype, cid, owner_id)
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
            for g in (gr or {}).get("data", [])[:8]:
                gid = (g.get("group") or {}).get("id")
                if not gid or gid in seen_groups:
                    continue
                seen_groups.add(gid)
                gd = client.get(GROUP_API.format(gid))
                if gd:
                    if gd.get("description"):
                        texts.append(gd["description"])
                    shout = group_shout_text(gd)
                    if shout:
                        texts.append(shout)
                    if gd.get("name"):
                        texts.append(gd["name"])
                sl = client.get(GROUP_SOCIAL_API.format(gid), key="group-social-links")
                for l in (sl or {}).get("data", []):
                    links.append((l.get("type", ""), l.get("url", ""), l.get("title", "")))
        if ctype == "User" and cid:
            ug = client.get(USER_GAMES_API.format(cid),
                            {"accessFilter": 2, "limit": 50, "sortOrder": "Desc"}, key="creator-games")
            other = [g["id"] for g in (ug or {}).get("data", []) if g.get("id")][:10]
            if other:
                for g in fetch_details(client, other).values():
                    if g.get("description"):
                        texts.append(g["description"])
    except Exception:
        pass   # ecosystem scan is best-effort enrichment -- never break the pass
    _eco_cache[key] = (texts, links)
    return texts, links


def fetch_social_links(client, universe_id, ctype, cid):
    """Official social links from game page + group page. Returns list of (type, url, title)."""
    links = []
    data = client.get(GAME_SOCIAL_API.format(universe_id), key="game-social-links")
    if data:
        for l in data.get("data", []):
            links.append((l.get("type", ""), l.get("url", ""), l.get("title", "")))
    if ctype == "Group":
        data = client.get(GROUP_SOCIAL_API.format(cid), key="group-social-links")
        if data:
            for l in data.get("data", []):
                links.append((l.get("type", ""), l.get("url", ""), l.get("title", "")))
    return links


def extract_socials(texts, official_links):
    """Merge official links + regex-scanned descriptions. Returns dict of lists."""
    out = {"discord": [], "youtube": [], "tiktok": [], "twitter": [], "twitch": [], "instagram": [], "other": []}
    for t, url, title in official_links:
        tl, ul = (t or "").lower(), (url or "")
        if "discord" in tl or DISCORD_RE.search(ul):
            m = DISCORD_RE.search(ul)
            out["discord"].append(m.group(1) if m else ul)
        elif "youtube" in tl: out["youtube"].append(ul)
        elif "tiktok" in tl: out["tiktok"].append(ul)
        elif "twitter" in tl or tl == "x": out["twitter"].append(ul)
        elif "twitch" in tl: out["twitch"].append(ul)
        elif "instagram" in tl: out["instagram"].append(ul)
        elif ul: out["other"].append(f"{t}:{ul}")
    for text in texts:
        if not text:
            continue
        for m in DISCORD_RE.finditer(text):
            out["discord"].append(m.group(1))
        for m in FACEBOOK_RE.finditer(text):
            # Facebook has no dedicated column -- presence counts via other_links.
            out["other"].append(m.group(0))
        for k, rx in SOCIAL_RES.items():
            for m in rx.finditer(text):
                out[k].append(m.group(0))
    for k in out:
        out[k] = [u for u in dict.fromkeys(out[k]) if not GENERIC_SOCIAL_RE.search(u)]
    return out


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
                    help="fast (~300 kw, ~20 min) | full (~900 kw, ~60 min) | max (~2000 kw, ~2.5 h)")
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
    ap.add_argument("--no-prefilter", action="store_true", help="don't skip <50-player search results")
    ap.add_argument("--delay", type=float, default=0.35)
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

    # ---------------- discovery
    found = {}
    for s in args.seeds:
        try: found[int(s)] = "seed"
        except ValueError: pass

    if not args.no_sorts:
        say(f"\n[1/5] Scanning Discover sorts...")
        discover_sorts(client, args.sort_pages, found, prefilter)

    if not args.no_search:
        say(f"\n[2/5] Searching {len(keywords)} keywords x {args.search_pages} pages "
            f"(~{len(keywords) * args.search_pages * (args.delay + 0.3) / 60:.0f} min)...")
        for i, kw in enumerate(keywords, 1):
            new = discover_search(client, kw, args.search_pages, found, prefilter)
            say(f"   ({i}/{len(keywords)}) '{kw}': +{new}   total {len(found)}")

    say(f"\n[3/5] Pulling exact stats for {len(found)} games...")
    details = fetch_details(client, list(found.keys()))

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

    # ---------------- snowball
    if not args.no_snowball:
        frontier = matches + near
        for hop in range(1, args.snowball_hops + 1):
            say(f"\n[4/5] Snowball hop {hop}: recommendations + creator games for {len(frontier)} seeds...")
            before = set(found.keys())
            creators_done = set()
            for uid in frontier:
                discover_recommendations(client, uid, found)
                g = details.get(uid, {})
                c = g.get("creator") or {}
                ck = (c.get("type"), c.get("id"))
                if c.get("id") and ck not in creators_done:
                    creators_done.add(ck)
                    discover_creator_games(client, c["type"], c["id"], found)
            new_ids = [u for u in found if u not in before]
            say(f"   +{len(new_ids)} new games from snowball")
            if not new_ids:
                break
            new_details = fetch_details(client, new_ids)
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
    if not args.no_verify and matches:
        uniq = list(dict.fromkeys(matches))
        say(f"\n[4.6/5] Verifying {len(uniq)} matches with a second stats snapshot ({args.verify_wait}s)...")
        time.sleep(args.verify_wait)
        fresh = fetch_details(client, uniq)
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

    # ---------------- enrichment
    say(f"\n[5/5] Enriching {len(matches)} matches (votes, owners, Discord/socials)...")
    votes = fetch_votes(client, matches)

    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for idx, uid in enumerate(matches, 1):
        g = details[uid]
        visits, active, favs = g.get("visits") or 0, g.get("playing") or 0, g.get("favoritedCount") or 0
        tier = classify(visits, active)
        up, down = votes.get(uid, (0, 0))
        creator = g.get("creator") or {}
        ctype, cid, cname = creator.get("type", ""), creator.get("id"), creator.get("name", "")
        texts = [g.get("description", "")]

        if ctype == "Group":
            grp = fetch_group(client, cid)
            creator_url = f"https://www.roblox.com/groups/{cid}"
            texts.append(grp.get("description", ""))
            texts.append(group_shout_text(grp))
            texts.append(grp.get("name", ""))
            owner = grp.get("owner") or {}
            owner_name, owner_id = owner.get("username", ""), owner.get("userId")
            group_members = grp.get("memberCount", "")
            group_name = grp.get("name", "")
        else:
            creator_url = f"https://www.roblox.com/users/{cid}/profile"
            owner_name, owner_id, group_members = cname, cid, ""
            group_name = ""
        owner_url = f"https://www.roblox.com/users/{owner_id}/profile" if owner_id else ""
        owner_desc = ""
        if owner_id:
            owner_desc = (fetch_user(client, owner_id) or {}).get("description", "")
            texts.append(owner_desc)
            texts.append(fetch_owner_profile_texts(client, owner_id))
        owner_roblox_signal = "YES" if re.search(r"\broblox\b", owner_desc, re.I) else ""
        discord_name_signal = ("YES" if has_discord_name_signal(cname, group_name, owner_name)
                               else "")

        socials = {"discord": [], "youtube": [], "tiktok": [], "twitter": [], "twitch": [], "instagram": [], "other": []}
        discord_info = {"valid": False, "name": "", "members": "", "online": ""}
        discord_url = ""
        discord_via = ""
        if not args.no_discord:
            official = fetch_social_links(client, uid, ctype, cid)
            base = extract_socials(texts, official)
            eco_texts, eco_links = fetch_creator_ecosystem_texts(client, ctype, cid, owner_id)
            texts.extend(eco_texts)
            official.extend(eco_links)
            socials = extract_socials(texts, official)
            for code in socials["discord"]:
                info = verify_discord(code)
                if info["valid"]:
                    discord_info, discord_url = info, f"https://discord.gg/{code}"
                    discord_via = ("game/group page" if any(code in str(x) for x in base["discord"])
                                   else "creator ecosystem")
                    break
            if not discord_url and socials["discord"]:
                discord_url = f"https://discord.gg/{socials['discord'][0]} (UNVERIFIED/expired)"
                discord_via = "unverified mention"

        created, updated = parse_dt(g.get("created")), parse_dt(g.get("updated"))
        age_days = (datetime.now(timezone.utc) - created).days if created else ""

        # ---- state: peak + growth (in-memory, lives as long as this process)
        key = str(uid)
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

        rows.append({
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
        })
        if idx % 10 == 0:
            say(f"   enriched {idx}/{len(matches)}")

    rows.sort(key=lambda r: (r["tier"], r["has_discord"] != "YES", -r["priority_score"]))

    # ---------------- social presence filter (keep only games with an online presence)
    if not args.no_social_filter and not args.no_discord:
        kept, dropped = [], 0
        for r in rows:
            has_any = any(r[c] for c in ("discord_url", "youtube", "tiktok", "twitter_x",
                                         "twitch", "instagram", "other_links"))
            if has_any or r.get("owner_roblox_signal") == "YES" or r.get("discord_name_signal") == "YES":
                kept.append(r)
            else:
                dropped += 1
        say(f"Social filter: kept {len(kept)}/{len(rows)} matches "
            f"(dropped {dropped} with no social presence at all)")
        rows = kept

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
        f"{len(details)} games checked, {len(rows)} matches, {len(watch)} on watchlist.\n")
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
