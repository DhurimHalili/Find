@echo off
title Roblox Acquisition Finder
rem Rolling watcher: sorts + snowball run fully every pass (highest yield, ~5 min);
rem keyword search covers a rotating 250-keyword slice per pass (~10-15 min) via
rem keyword_cursor.json, so the full ~1300-keyword sweep finishes over ~5 passes
rem but EVERY pass yields fresh games. Deep 3-hop snowball catches what RoTrend
rem catches via recommendations (clones of trending hits share rec graphs).
rem   - match CSVs append to results_history.csv; progress goes to finder.log
rem Stop it by closing this window. Do NOT run a second copy at the same time.
cd /d "C:\Users\Gaming pc\Desktop\robloxfind"
python roblox_finder.py --loop 90 --keyword-mode full --search-pages 3 --sort-pages 15 --keyword-limit 250 --snowball-hops 3 --csv --watchlist-file watchlist_history.csv >> results_history.csv 2>> finder.log
