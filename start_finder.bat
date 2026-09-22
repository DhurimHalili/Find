@echo off
title Roblox Acquisition Finder
rem Rolling watcher: sorts + snowball run fully every pass (highest yield); keyword
rem search covers a rotating 500-keyword slice per pass via keyword_cursor.json,
rem so the full ~1300-keyword sweep finishes over ~3 passes but EVERY pass yields
rem fresh games. 8 request threads hide network latency (same polite request
rem rate). Deep 3-hop snowball catches what RoTrend catches via recommendations
rem (clones of trending hits share rec graphs).
rem   - match CSVs append to results_history.csv; progress goes to finder.log
rem STOP -- the cloud watcher (GitHub Actions) owns scanning now. Running this
rem too forks results_history.csv / seen_ledger.json and the next git pull will
rem refuse. Only run this for offline testing, then delete its rows or tell
rem the assistant so local + cloud can be merged again.
set /p LOCALRUN="Cloud watcher is primary. Run a LOCAL pass anyway? (y/N) "
if /i not "%LOCALRUN%"=="y" exit /b 0
cd /d "C:\Users\Gaming pc\Desktop\robloxfind"
python roblox_finder.py --loop 90 --keyword-mode full --search-pages 3 --sort-pages 20 --keyword-limit 500 --snowball-hops 3 --csv --watchlist-file watchlist_history.csv --nodiscord-file nodiscord_history.csv >> results_history.csv 2>> finder.log
