# Roblox Acquisition Finder v2 — setup & operations

Scouts Roblox for low-visit / high-concurrency games to acquire, tiered:

| Tier | Lifetime visits | Live players (CCU) |
|---|---|---|
| 1-BEST (MUST BUY) | < 40,000 | ≥ 100 |
| 2-GREAT | < 100,000 | ≥ 600 |
| 3-GOOD | < 200,000 | ≥ 800 |
| 4-MID | < 500,000 | ≥ 800 |

Ranking: tier first, then **games with a verified Discord always rank above games without one**,
then priority score (tier weight + Discord bonus + member count + heat + games ≤30 days old).
Twitter/YouTube/TikTok/Twitch/Instagram links are collected in their own columns too.

## What it does each pass

1. **Discover sorts** — walks every chart sort (Up-and-Coming, Trending, …).
2. **Keyword search** — sweeps ~900 (full) / ~2,000 (max) keywords across Roblox site search.
   **Scan memory:** every game already evaluated in the last 3 days (`--seen-days`) is skipped,
   so each pass spends its time on genuinely new games. Games become eligible for re-checking
   after the cooldown — if they're still on the charts they get re-reported with fresh CCU.
3. **Stats** — exact visits / CCU / favorites from the public games API, batches of 50.
4. **Snowball** — for every match & near-miss: pulls Roblox's "recommended games" + every other
   game by the same creator/group, 2 hops deep.
5. **Verification** — every match is re-checked against a second stats snapshot ~45s later;
   games that no longer qualify are dropped ("verified out" in the log). This is what keeps
   the list accurate instead of one-off CCU spikes.
6. **Enrichment** — votes, owner (group owner for group games), Discord + socials:
   official social-links API, regex scan of game/group/owner descriptions, the owner's
   profile page, **and the creator's ecosystem** (their groups' socials + descriptions
   and their other games' descriptions). Each Discord invite is **verified live** via
   Discord's public API (server name, members, online). Every row carries `discord_via`
   -- where the Discord was found (game/group page vs creator ecosystem).

The scan memory lives in `seen_ledger.json` (auto-managed, entries expire after 30 days).
Delete it to force a full re-scan of everything.

## Run it

```bat
:: human-readable report (top 40, tier counts)
python roblox_finder.py --no-search

:: full scan, save Excel-ready CSV (~60-75 min)
python roblox_finder.py --csv > results.csv

:: near-miss watchlist (50-99 concurrent, check again at peak)
python roblox_finder.py --watchlist > watchlist.csv

:: both in one file
python roblox_finder.py --csv --watchlist > everything.txt

:: narrow / fast test
python roblox_finder.py --keyword-mode fast --search-pages 2 --csv > quick.csv
```

Useful flags: `--keyword-mode fast|full|max`, `--search-pages N`, `--sort-pages N`,
`--no-sorts`, `--no-search`, `--no-snowball`, `--no-discord`, `--seeds <universeId...>`,
`--extra-keywords "new hit game" ...`, `--delay 0.8` (if you see lots of 429s), `--loop N`.

## Continuous mode (already wired up)

`start_finder.bat` runs a pass every 90 minutes forever:
- match CSVs append to **`results_history.csv`** (every row is timestamped in `checked_at_utc`,
  so filter/sort in Excel by that column to see today's rows),
- progress + per-pass summary go to **`finder.log`**.

A copy sits in your Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\robloxfinder_start.bat`),
so it starts whenever you log in. **To stop it:** close its console window or delete that Startup file.
**Do not run two copies at once** — Roblox rate-limits per IP.

Note: peak/growth history lives in the running process's memory — a PC restart resets it;
it rebuilds over the following passes.

## The cookie (the one thing worth adding)

Without a cookie everything works **except** the official per-game social-links endpoint
(description-based Discord detection still works). To unlock it:

1. Use a **throwaway alt account** (never your main), birthday set to 18+.
2. Log in on a browser → F12 → Application → Cookies → `https://www.roblox.com`
   → copy the **`.ROBLOSECURITY`** value.
3. Set it once, permanently (then open a NEW terminal):
   ```bat
   setx ROBLOSECURITY "_|WARNING:-DO-NOT-SHARE-THIS....your cookie value"
   ```
   Never paste the cookie into chat, files, or screenshots. It's your account login.

The Startup task picks it up automatically on its next launch (it reads the env var at start).

## Reading the output

- `tier` — 1-BEST → 4-MID (rules above). Rows are pre-sorted: tier → has Discord → priority.
- `has_discord` / `discord_url` / `discord_members` / `discord_online` — invite verified live; dead invites are marked.
- `peak_active_seen` — highest CCU recorded for that game while this run has been alive.
- `visits_growth_per_day` — appears from the second pass of a `--loop` session onward.
- `owner_name` / `owner_url` — the actual person to contact (group owner for group-held games).
- `like_ratio` — sanity check: high CCU with a mediocre like ratio can mean bought/fake traffic.

## Social presence filter

Results keep only games with an online presence: a **Discord**, or any other social link
(YouTube/TikTok/Twitter/Twitch/Instagram/other), found on the game page, the group page,
the **owner's profile page**, or in the game/group/owner descriptions. Before a game is
dropped for having nothing, the owner's profile page and bio are checked; an owner bio that
mentions Roblox also counts as a presence signal. Everything with zero presence is removed.
Each pass logs `Social filter: kept X/Y matches (dropped ...)`. `--no-social-filter` disables.
The watchlist is not filtered (it isn't enriched). `harvest_curated.py` re-runs this
enrichment + filter over every match already in results_history.csv and writes
`curated_list.csv`.

## Do-not-buy exclusions

Games are checked (title + description) before tiering and silently dropped if they are:
**modded** (admin/owner panels, MODDED, x999…, free admin), **reuploaded** (uncopylocked,
leaked, copied, stolen, free model), **NSFW** (condo, 18+, sexy, …), or **non-English**
(Cyrillic/CJK/Arabic/etc. titles or foreign-language keywords). Excluded games never seed
the snowball or waste Discord checks. Each pass logs the counts per reason.
`--no-exclusions` disables this.

## When to look

You're in Kosovo (CET). Global Roblox concurrency peaks roughly **22:00–02:00** your time
(US afternoon/evening is the biggest block); weekends are higher all day. With `--loop 90`
you don't need to chase hours — peaks get recorded per game automatically.

## Weekly maintenance

When a new game blows up on Roblox, add its name/format so clones get caught early:
`--extra-keywords "steal a X"`, or permanently into `KW_TRENDING_HITS` near the top of the script.

## Troubleshooting

- Lots of `[429]` in finder.log → add `--delay 0.8` (edit start_finder.bat).
- Zero matches → run during 21:00–02:00 local, or try `--no-prefilter`.
- `Missing dependency` → `pip install requests`.
- Cookie source skipped (`401` in log) → set the ROBLOSECURITY env var (section above).
