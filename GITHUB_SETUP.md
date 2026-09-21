# Run the watcher 24/7 on GitHub Actions (free, no card, PC can stay off)

How it works: GitHub runs one finder pass per hour on their servers and pushes
`results_history.csv` (+ radar feed, scan memory, keyword cursor) back to the
repo. You `git pull` on your PC, open the local CRM, hit Sync. The CRM itself
stays on your PC — only the heavy scanning moves to the cloud.

Quality: identical to the local watcher. Same flags, same cookie capability
(stored as an encrypted secret), and the scan memory + rolling keyword cursor
persist through git, so each cloud pass continues exactly where the last one
stopped. Two deliberate differences: it runs 24/7 UTC instead of golden-hours
(more coverage, not less), and scheduled starts can lag a few minutes when
GitHub is busy.

Cost: public repos get unlimited Actions minutes — use a **public** repo. The
data is public Roblox stats anyway. Your private stuff (`crm_data.json`:
statuses, notes) is git-ignored and never leaves your PC. On a private repo
you'd get 2,000 min/month, which an hourly ~15-min pass would exceed.

## 1. Publish the project (one time)

In PowerShell, in this folder:

```powershell
git init -b main
git add .                                   # .gitignore keeps crm_data.json + logs out
git status --short                          # confirm crm_data.json is NOT listed
git commit -m "roblox acquisition finder"
```

Create an empty **public** repo on github.com (no README/license, to avoid
merge work), then:

```powershell
git remote add origin https://github.com/DhurimHalili/Find.git
git push -u origin main
```

## 2. Add the cookie as an encrypted secret

Repo page → Settings → Secrets and variables → Actions → New repository secret:

- Name: `ROBLOSECURITY`
- Value: your throwaway-alt `.ROBLOSECURITY` value

Without it the passes still run, but the official game social-links endpoint is
skipped (same as running locally cookieless). Secrets are never shown in logs.

## 3. Run it

Actions tab → `watcher` → **Run workflow** (one manual pass to prove it works),
then leave the hourly schedule to it. Each run appends to `results_history.csv`
and pushes the files back automatically.

## 4. Daily use from your PC

```powershell
git pull            # fetch the cloud passes
python crm_server.py   # or start_crm.bat, then open http://127.0.0.1:8780
```

Hit **Sync** — new matches and radar rows flow in. Your statuses/notes live in
the local `crm_data.json`, untouched by pulls.

## 5. Maintenance

- **New code:** edit locally, `git add` the changed files, commit, push. The
  next scheduled pass uses them. Never `git add crm_data.json`.
- **Rotate the cookie:** update the `ROBLOSECURITY` secret (step 2), nothing else.
- **Stop the PC watcher:** close `start_finder.bat`. One watcher at a time.
- **Check health:** Actions tab → a run → its logs (same `Done in … N matches`
  summary as `finder.log`).

## Honest limitations

- Schedule starts can lag 5–30 min when GitHub is loaded; an hourly pass may
  effectively run every 75–90 min. It catches up by itself.
- GitHub pauses schedules on repos with 60+ days of zero activity — the bot's
  hourly commits count as activity, so this won't trigger while the watcher runs.
- No live cloud CRM in this setup. If you later want the CRM online too, pair
  this with a Tailscale-exposed home machine or a VPS — the Oracle guide in
  `ORACLE_SETUP.md` still applies.
