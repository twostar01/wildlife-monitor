---
phase: 04-dual-lens-sync-overhaul
plan: 01
subsystem: dual-lens-pairing
tags: [database, sqlite, data-integrity, testing]
status: complete
dependency-graph:
  requires: []
  provides:
    - scripts/verify_lens_pairing.py (stdlib-only pairing verification harness)
    - database.link_lens_pair() rewritten for single-candidate-only, escape-free pairing
  affects:
    - database.py (link_lens_pair function body)
    - 04-02 plan (repair migration + consistency check build on this harness and function)
tech-stack:
  added: []
  patterns:
    - "conservative single-candidate-only pairing: link only when exactly one cross-lens candidate exists, never guess among duplicates"
    - "timestamp-only SQL LIKE pattern (digit-guaranteed by regex) instead of interpolating an unescaped free-text camera_base"
key-files:
  created:
    - scripts/verify_lens_pairing.py
  modified:
    - database.py
decisions:
  - "Dropped camera_base entirely out of the SQL LIKE pattern rather than adding an ESCAPE clause (D-05) — the remaining timestamp substring is guaranteed digit-only by parse_dual_lens_filename()'s regex, so no metacharacter can reach the query"
  - "Ambiguous (0 or >1 candidate) groups are declined and lens_index-only recorded, never guessed via a recency/id-order tiebreak (D-02)"
  - "Added a guard so link_lens_pair() never claims a candidate already paired to a third video (D-07 one-sided-overwrite fix)"
metrics:
  duration: "~35 minutes"
  completed: 2026-07-29
---

# Phase 04 Plan 01: Dual-Lens Pairing Write-Path Safety Summary

Rewrote `link_lens_pair()` to be provably safe at write time and built the project's first stdlib-only test harness to prove it.

## What Was Built

**`scripts/verify_lens_pairing.py`** — a new stdlib-only (`argparse`, `os`, `shutil`, `sqlite3`, `sys`, `tempfile`, `contextlib`, `pathlib`, `database`) verification harness. It builds an isolated tempfile SQLite database via `database.init_db()`, never touches the production database, and exposes three suites behind `--suite {link,repair,consistency,all}`:

- `suite_link()` — 6 cases against `database.link_lens_pair()`, all passing after task 2.
- `suite_repair()` — 6 cases targeting `database._repair_lens_pairings()`, which plan 04-02 has not yet implemented; degrades cleanly to `FAIL: repair/function-missing` with no traceback.
- `suite_consistency()` — 3 cases targeting `database.check_pairing_consistency()`, same clean degradation until 04-02 lands.

**`database.link_lens_pair()` rewrite** (`database.py`) — six concrete fixes over the prior implementation:
1. SQL LIKE pattern narrowed to the timestamp-only form `f"%_{timestamp}%"` — `camera_base` is no longer interpolated into the query at all (D-05). No `ESCAPE` clause added; the existing Python-side exact-match predicate (`p[0] == camera_base and p[2] == timestamp and p[1] != lens_index`) remains the disambiguation safety net, preserved verbatim.
2. Candidate selection now collects every SQL-returned row satisfying the Python-side predicate and only links when `len(candidates) == 1` — the prior first-match `break` loop (which could silently pick one of several duplicate-row candidates) is gone.
3. Non-linking paths (0 or >1 candidates) still record the video's own `lens_index`; the >1 case logs an `ambiguous candidates ... left unpaired` message via the existing module-level `log`. The 0-candidate case emits nothing, since a singleton (no same-second partner) is the dominant, normal production case (10,946 such groups per RESEARCH.md).
4. A new guard rejects a candidate already claimed by a third video (`candidate["paired_video_id"] not in (None, video_id)`) before writing the current video's own `paired_video_id` — this closes the one-sided-overwrite bug the D-07 investigation found, where the candidate's own UPDATE was correctly guarded but the current video's own UPDATE was not.
5. Both linking UPDATEs are unchanged in shape from the original implementation.
6. The trailing partner-lens_index back-fill now reuses the already-fetched `candidate["filename"]` instead of issuing a second `SELECT filename FROM videos WHERE id=?` round-trip.

## Verification

- `python scripts/verify_lens_pairing.py --suite link` → `PASS: link (6/6)`.
- `python -c "import database"` → exits 0, no import/syntax regression.
- `git diff --stat` after both commits shows exactly two paths touched across the plan: `scripts/verify_lens_pairing.py` (new) and `database.py`.
- `git diff database.py` confirms changes are confined to `link_lens_pair()`'s function body — no edits to `SCHEMA`, `init_db()`, `parse_dual_lens_filename()`, or any other write helper.
- All task-level acceptance-criteria greps/AST checks from the plan (LIKE pattern form, no `ESCAPE`, safety-net predicate preserved verbatim, `len(candidates) != 1` present, no `break` in the function body, redundant SELECT removed, `AND lens_index IS NULL` back-fill preserved, single top-level `import re`, docstring mentions unambiguous-candidate rule, no stray files under `data/`) all pass.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking issue] Fixture helper missing required `processed_at` column**
- **Found during:** Task 1, first harness run
- **Issue:** `videos.processed_at` is `NOT NULL` in `SCHEMA`; the initial `_insert_video()` helper omitted it, causing every fixture insert to raise `sqlite3.IntegrityError`.
- **Fix:** Added a fixed ISO-format `processed_at` value to `_insert_video()`'s INSERT statement and to the one raw `INSERT` used in the dangling-pointer consistency test case.
- **Files modified:** `scripts/verify_lens_pairing.py`
- **Commit:** `fc394bc` (folded into task 1's commit before it was finalized — no separate commit was made for this fix since it was caught during the same task's implementation, before verification)

**2. [Rule 1 - Bug] `repair/ambiguous-group-cleared` fixture inserted an invalid forward FK reference**
- **Found during:** Task 1, drafting the repair-suite fixture for a duplicate lens-00 row carrying a stale pointer
- **Issue:** The fixture attempted to `INSERT` a row with `paired_video_id=999999` directly (a non-existent id), which `get_conn()`'s `PRAGMA foreign_keys=ON` rejects immediately — the fixture needs the dangling pointer to exist post-insert, not at insert time.
- **Fix:** Insert the row with `paired_video_id` unset, then set the dangling pointer via a separate `UPDATE` under a temporary `PRAGMA foreign_keys=OFF` connection (matching the pattern already used for the `consistency/dangling-pointer-detected` case).
- **Files modified:** `scripts/verify_lens_pairing.py`
- **Commit:** `fc394bc` (same task-1 commit, fixed before the harness was verified/committed)

**3. [Rule 1 - Bug] Module docstring literally referenced `data/wildlife.db`, tripping the plan's own acceptance grep**
- **Found during:** Task 1, running the acceptance-criteria checks after the harness first passed its own tests
- **Issue:** `grep -c 'wildlife.db' scripts/verify_lens_pairing.py` must return `0` per the plan's acceptance criteria (the harness must never reference the production database file), but the module docstring said "Never touches data/wildlife.db."
- **Fix:** Reworded the docstring to "Never touches the production database file." — same meaning, no literal string match.
- **Files modified:** `scripts/verify_lens_pairing.py`
- **Commit:** `fc394bc` (same task-1 commit, fixed before the harness was committed)

None of these deviations affected `database.py` or the fix under test — all were harness-construction issues caught and corrected before task 1's commit landed, so the committed harness already reflects the corrected version. No deviations occurred during task 2.

## Known Stubs

None — both artifacts (`scripts/verify_lens_pairing.py`, the rewritten `link_lens_pair()`) are fully functional, not placeholders.

## Threat Flags

None — this plan's `<threat_model>` block anticipated exactly the surface touched (SQL LIKE pattern, parameterized queries, one-sided overwrite, the new harness's filesystem isolation). No new, unlisted surface was introduced.

## Self-Check: PASSED

- FOUND: `scripts/verify_lens_pairing.py`
- FOUND: `database.py` (modified)
- FOUND commit `fc394bc` (feat(04-01): add stdlib-only dual-lens pairing verification harness)
- FOUND commit `5523d25` (fix(04-01): rewrite link_lens_pair() for single-candidate-only, escape-free pairing)
