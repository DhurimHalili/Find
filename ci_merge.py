#!/usr/bin/env python3
"""CI union-merge for watcher data files (used by .github/workflows/watcher.yml).

Situation: a pass finished and `git push` was refused because origin moved
mid-run (a user push or another writer). Instead of rebasing (which conflicts
on append-only data files and strands runners in detached-HEAD hell), this
folds origin's version of each data file together with the worktree version:

- match/watchlist/no-discord CSVs: block-level union keyed by
  (universe_id, checked_at_utc); nothing from either side is ever dropped.
  Stray git conflict markers are skipped defensively.
- seen_ledger.json: max expiry per key (permanent FOREVER skips always win).
- keyword_cursor.json: the worktree (this run's) version wins; rotation
  continues correctly from either offset.

Usage:  python ci_merge.py <upstream-ref>   (e.g. origin/main)
Merges upstream blobs into the worktree files and `git add`s them.
Exits nonzero on real errors (a half-merged commit must never push).
Safe to import: union helpers are pure functions (unit-tested locally).
"""

import csv
import io
import json
import os
import subprocess
import sys

DATA_CSVS = ("results_history.csv", "watchlist_history.csv", "nodiscord_history.csv")
LEDGER = "seen_ledger.json"
CURSOR = "keyword_cursor.json"


def _sh(*args):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[1:])} failed: {p.stderr.strip()[:200]}")
    return p.stdout


def split_blocks(text, header_first_col):
    """Split CSV text into [(header_line, [row_lines])], tolerating repeated
    headers (multiblock appends) and skipping conflict markers / separators."""
    blocks, cur_h, cur_rows = [], None, []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.lstrip("\ufeff").strip()
        if not s or s.startswith("====") or s.startswith("<<<<<<<") \
                or s.startswith("=======") or s.startswith(">>>>>>>"):
            continue
        if s.split(",")[0].strip().lower() == header_first_col:
            if cur_h is not None:
                blocks.append((cur_h, cur_rows))
            cur_h, cur_rows = line, []
        elif cur_h is not None:
            cur_rows.append(line)
    if cur_h is not None:
        blocks.append((cur_h, cur_rows))
    return blocks


def row_key(header_line, row_line):
    try:
        h = next(csv.reader(io.StringIO(header_line.lstrip("\ufeff"))))
        r = next(csv.reader(io.StringIO(row_line)))
        d = dict(zip(h, r))
        uid = (d.get("universe_id") or "").strip()
        ts = (d.get("checked_at_utc") or d.get("last_seen") or "").strip()
        if not uid:
            return None
        return (uid, ts)
    except Exception:
        return None


def union_csv(origin_text, work_text, header_first_col):
    """origin_text first, then worktree-only blocks appended with their own
    headers (parsers reset on repeated headers, so mixed schemas stay valid)."""
    origin_blocks = split_blocks(origin_text or "", header_first_col)
    seen = set()
    for h, rows in origin_blocks:
        for r in rows:
            k = row_key(h, r)
            if k:
                seen.add(k)
    extra = []
    for h, rows in split_blocks(work_text or "", header_first_col):
        missing = [r for r in rows if (k := row_key(h, r)) and k not in seen]
        for r in missing:
            seen.add(row_key(h, r))
        if missing:
            extra.append(h)
            extra.extend(missing)
    merged = (origin_text or "").rstrip("\n") + "\n"
    if extra:
        merged += "\n".join(extra) + "\n"
    return merged


def union_ledger(origin_text, work_text):
    o = json.loads(origin_text or "{}")
    w = json.loads(work_text or "{}")
    return json.dumps({k: max(o.get(k, ""), w.get(k, "")) for k in set(o) | set(w)},
                      ensure_ascii=False)


def read_worktree(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def origin_blob(ref, path):
    p = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True, encoding="utf-8")
    return p.stdout if p.returncode == 0 else None


def main(ref):
    staged = []

    def take(path, content):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        _sh("git", "add", path)
        staged.append(path)

    for path in DATA_CSVS:
        origin = origin_blob(ref, path)
        work = read_worktree(path)
        if origin is None:
            if work.strip():  # brand-new file this run; stage as-is
                take(path, work if work.endswith("\n") else work + "\n")
                print(f"new {path}: staged as-is")
            continue
        header = ("priority_score" if "results" in path or "nodiscord" in path
                  else "title")
        merged = union_csv(origin, work, header)
        take(path, merged)
        print(f"union {path}: {len(merged.splitlines())} lines total")

    origin_led = origin_blob(ref, LEDGER)
    if origin_led is not None:
        merged = union_ledger(origin_led, read_worktree(LEDGER))
        take(LEDGER, merged)
        n_forever = sum(1 for v in json.loads(merged).values()
                        if str(v).startswith("9999"))
        print(f"union {LEDGER}: {len(json.loads(merged))} keys, {n_forever} permanent")
    else:
        work_led = read_worktree(LEDGER)
        if work_led.strip():
            take(LEDGER, work_led)
            print(f"new {LEDGER}: staged as-is")

    # cursor: this run's version wins (rotation is valid from any offset)
    if os.path.exists(CURSOR):
        _sh("git", "add", CURSOR)
        staged.append(CURSOR)
    print("staged:", ", ".join(staged) if staged else "(nothing)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "origin/main")
