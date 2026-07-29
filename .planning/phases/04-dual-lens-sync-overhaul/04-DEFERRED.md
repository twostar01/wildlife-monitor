# Deferred: `nas_sync.sh` duplicate-row root cause

**Phase:** 04-dual-lens-sync-overhaul
**Deferred at:** 2026-07-29
**Status:** Open

## The defect

`nas_sync.sh`'s archive-collision handling (lines 418-506, 525-610) moves a locally-staged kept
video to the NAS archive with:

```python
if dest_path.exists():
    conn.execute("UPDATE videos SET filepath=NULL WHERE filepath=? AND id!=?", (str(dest_path), row["id"]))
    conn.execute("UPDATE videos SET filepath=? WHERE id=?", (str(dest_path), row["id"]))
```

If the archive destination already exists — i.e. this physical video was already fully processed
and archived under a *different* `videos.id` in an earlier run — this silently sets the **older
row's `filepath` to NULL** without setting `file_purged_at`, orphaning it in a way that's
indistinguishable from "not yet archived." The archive path is handed to the newer, duplicate row
instead. `insert_video()`'s `ON CONFLICT(filepath)` dedup only matches on the local staging path,
which is fresh on every re-copy, so it cannot recognize the re-scanned file as the same video.

## Evidence

Confirmed against the real production database (`data/wildlife.db`, 81,316 rows, 2026-07-29,
RESEARCH.md D-07 Investigation): **19,291 filenames appear more than once** —
2 rows: 5,554 filenames, 3 rows: 5,812, 4 rows: 4,073, 5 rows: 3,466, 6 rows: 386. A concrete
example: `id=18518` (`filepath=NULL`, `processed_at=2026-04-24T12:10:21`) and `id=19733`
(`filepath=.../wildlife_archive/backwall/2026/04/18/Back Wall_00_20260418155240.mp4`,
`processed_at=2026-04-25T11:45:55`) share the identical filename — two DB rows for one physical
file, ~23.5 hours apart.

## Why it's out of scope for phase 4

The defect never touches `paired_video_id` — it's upstream of the dual-lens pairing code paths
this phase owns. Fixing it means redesigning `insert_video()`'s dedup key (it currently keys on
local staging path, not a stable identity for the physical file) and `nas_sync.sh`'s
archive-collision handling. That's video-ingestion/dedup work, outside the "dual-lens pairing"
boundary CONTEXT.md scoped this phase to.

## Why deferring is safe

The exactly-two-members rule shipped in this phase (plan 04-01's `link_lens_pair()` rewrite and
plan 04-02's `_repair_lens_pairings()` migration) makes pairing provably correct *while duplicate
rows continue to occur*: a `(camera_base, timestamp)` group is only ever auto-linked when it has
exactly two members with differing lens indices. Any group inflated by a duplicate row (3+
members) is left unpaired rather than guessed. SYNC-01 and SYNC-04 are satisfied without needing
the duplicate-row problem solved first.

## Suggested follow-up

**Title:** `insert_video dedup across archive moves`

Redesign `insert_video()`'s dedup key so a re-scanned, already-archived file is recognized as the
same video regardless of its current staging path, and fix `nas_sync.sh`'s archive-collision
handling (lines 418-506, 525-610) to stop silently NULLing the older row's `filepath` without
setting `file_purged_at`.
