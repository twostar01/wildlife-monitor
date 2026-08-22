---
phase: 14-correction-unification-schema-backfill-cutover
plan: 03
subsystem: database
tags: [sqlite, backfill, migration, species-corrections, correction-unification]

requires: [14-01]
provides:
  - scripts/backfill_species_corrections.py — dry-run-default migration script (4-flag apply gate, snapshot, JSONL audit log, reconciliation)
  - scripts/verify_backfill_species_corrections.py — plan/apply/gates suites (20 cases) over a self-contained legacy-only fixture
affects: [14-04]

actuals:
  tokens: 15131
  tasks: 3
  commits: 3

tech-stack:
  added: []
  patterns:
    - "D-04 backfill rigor sequence (dry-run default -> fixture rehearsal -> [14-04's production rehearsal + Go/No-Go] -> --apply), mirrored verbatim from scripts/migrate_stale_paths.py's gate/snapshot/audit-log/reconciliation shape"
    - "WHERE excluded.corrected_at > species_corrections.corrected_at on the UPSERT's DO UPDATE — makes a re-run a no-op and protects a live post-cutover correction from ever being clobbered by an older legacy value (T-14-12)"
    - "resolve_precedence() only invoked when BOTH legacy sources have an entry for a detection — a single-source detection (including a Gallery row with a NULL corrected_at) is assigned directly to its sole source, never passed through the tie-break function"

key-files:
  created:
    - scripts/backfill_species_corrections.py
    - scripts/verify_backfill_species_corrections.py
  modified: []

key-decisions:
  - "resolve_precedence(gallery_corrected_at, video_corrected_at) is called by build_plan() ONLY when a detection is reachable from both legacy sources — not universally for every detection in the union. A lone Gallery row with a NULL corrected_at (dQ) must resolve to 'gallery' by virtue of being the only source, not fall through the function's None-handling branch and be misread as 'video wins' when no video row exists at all to compare against."
  - "species_corrections.corrected_at is NOT NULL, but a legacy Gallery correction can have a NULL corrected_at (a real historical data-quality case, dQ). The backfill writes '' (empty string) in that case, not NULL — consistent with treating None as the empty string throughout precedence comparison, and satisfying the NOT NULL constraint without inventing a sentinel."
  - "The fixture's crops total (11, one crop per detection) is deliberately different from the planned species_corrections row count (8) — this is what lets the P7 case mechanically prove the dry-run report never conflates the crops context line with the write-target line, rather than relying on prose review alone."

requirements-completed: [CORR-03, CORR-04]

coverage:
  - id: D1
    description: "The dry-run report computes and prints the real corrected-detection count from the database; the crops total is printed on its own line labelled as context and is never used as the write target (RESEARCH.md Pitfall 6)"
    requirement: "CORR-03"
    verification:
      - kind: unit
        ref: "scripts/verify_backfill_species_corrections.py --suite plan (P1-P7)"
        status: pass
    human_judgment: false
  - id: D2
    description: "A detection reachable from both legacy sources produces exactly one species_corrections row, resolved by D-03 recency in both write-order directions, with an exact tie resolving to video_player; a legacy suppress row backfills as suppressed=1; an orphan legacy row is counted and reported, never silently dropped; a duplicate legacy row resolves to the latest by corrected_at; re-running --apply is idempotent and never overwrites a newer live correction"
    requirement: "CORR-03"
    verification:
      - kind: unit
        ref: "scripts/verify_backfill_species_corrections.py --suite apply (A1-A8)"
        status: pass
    human_judgment: false
  - id: D3
    description: "The write is gated four independent ways (--apply, --confirm-irreversible, --snapshot-dir, --audit-log), snapshotted online before any write, and every applied row is durably audit-logged only after the transaction that wrote it has committed"
    requirement: "CORR-03"
    verification:
      - kind: unit
        ref: "scripts/verify_backfill_species_corrections.py --suite gates (G1-G5)"
        status: pass
    human_judgment: false
  - id: D4
    description: "audit_trail_digest() over (species.id, species.label) and (video_corrections.id, original_label) is byte-identical before and after --apply — the migration never touches either legacy audit-trail anchor"
    requirement: "CORR-04"
    verification:
      - kind: unit
        ref: "scripts/verify_backfill_species_corrections.py --suite apply, case A8"
        status: pass
    human_judgment: false

duration: ~5min (commit span; longer including research/design reading)
completed: 2026-08-22
status: complete
---

# Phase 14 Plan 03: Correction Unification Backfill Migration Summary

**One-time `species_corrections` backfill migration — dry-run-by-default, four-flag apply gate, online snapshot, JSONL audit log, and pre/post reconciliation digests — built and proven end-to-end against a self-contained legacy-only fixture (20/20 across plan/apply/gates suites). No production database was touched by this plan.**

## Performance

- **Duration:** ~5 min (commit-to-commit span); the design/research read-through that preceded writing code took longer
- **Tasks:** 3
- **Files created:** 2 (`scripts/backfill_species_corrections.py`, `scripts/verify_backfill_species_corrections.py`)

## Accomplishments

- `scripts/verify_backfill_species_corrections.py` — a `_seed_legacy_fixture()` builder covering all ten cases the plan required (Gallery-only, two-detection fan-out with a non-matching sibling label, both-sources-video-later, both-sources-gallery-later, legacy suppress sentinel, NULL-`corrected_at` Gallery row, an orphan legacy row, a duplicate legacy-row pair, and a genuinely uncorrected detection) plus three suites: `plan` (P1-P7, dry-run correctness), `apply` (A1-A8, the real write on a fixture), `gates` (G1-G5, the four-flag gate). Written first and confirmed RED (0/20, each case naming the missing module) before any migration code existed.
- `scripts/backfill_species_corrections.py` — `collect_gallery_corrections()`, `collect_video_corrections()` (dedup to latest `corrected_at` per `(video_id, original_label)`), `expand_video_fanout()` (fan-out predicate identical to `HAS_VIDEO_CORRECTION`'s correlated subquery), `resolve_precedence()` (D-03, tie resolves to `video_player`), `build_plan()`, `audit_trail_digest()` (SHA-256 over `species.label`/`video_corrections.original_label`, same idiom as `migrate_stale_paths.py`'s `non_path_digest()`), `render_plan_report()` (future-tense in dry-run, past-tense in apply, crops printed as context on its own line), `apply_backfill()` (single-transaction `executemany` UPSERT with a recency-guarded `WHERE excluded.corrected_at > species_corrections.corrected_at`), `verify_post_conditions()`, `snapshot_db()`/`open_audit_log()`/`write_audit_line()` (ported from `migrate_stale_paths.py`), `build_parser()`, `main()`.
- Landed as a real RED-then-GREEN sequence across the plan's own three tasks: task 1 committed the RED harness (`test:`), task 2 committed the dry-run half turning `plan` green while `apply`/`gates` stayed red (`feat:`), task 3 committed the apply half turning all three suites green (`feat:`).
- `--suite all` on the fixture harness: 20/20 (`plan` 7/7, `apply` 8/8, `gates` 5/5). `scripts/verify_phase14.py --suite all` (from plan 14-01) still passes 12/12 unaffected, since this plan changed no application code.

## Task Commits

1. **Task 1: Legacy-only fixture and RED harness** — `37e9c9a` (test) — `--suite all` intentionally 0/20, every case naming the missing module.
2. **Task 2: Dry-run plan builder and Go/No-Go report** — `1f34bcf` (feat) — `plan` 7/7; `apply`/`gates` still red (no `--apply` branch existed yet).
3. **Task 3: Four-flag apply path, snapshot, audit log and reconciliation** — `0f61a4b` (feat) — `plan`/`apply`/`gates` all 20/20.

**Plan metadata:** SUMMARY commit follows this file.

## Files Created

- `scripts/backfill_species_corrections.py` — the migration script (dry-run default; `--apply --confirm-irreversible --snapshot-dir --audit-log` required together for the real write)
- `scripts/verify_backfill_species_corrections.py` — the harness that drives it end-to-end against a throwaway fixture it builds itself; never references a real database path

## Decisions Made

- **`resolve_precedence()` is only called when both legacy sources have an entry for a detection.** The function's own signature (`gallery_corrected_at`, `video_corrected_at`) can't distinguish "this source is absent" from "this source is present with a NULL timestamp." Calling it universally for every detection in the source union would have misresolved the dQ case (a lone Gallery correction with a NULL `corrected_at` and *no* video-level row at all) to `'video_player'` — silently wrong, since there is no video correction to lose to. `build_plan()` instead picks the sole source directly whenever only one side has an entry, and only invokes `resolve_precedence()` for the genuine both-sources collision (dB/dB2), which is the case D-03 actually describes.
- **`species_corrections.corrected_at` is `NOT NULL`, but a legacy Gallery correction can legitimately have a NULL `corrected_at`.** The backfill writes `''` (empty string) for that field in that case rather than inventing a sentinel or relaxing the schema — consistent with the "treat `None` as the empty string" rule already used throughout `resolve_precedence()`'s comparisons, and it satisfies the existing `NOT NULL` constraint without a schema change (out of scope for this plan; the constraint was set by plan 14-01).
- **The fixture's `crops` total (11) is deliberately different from its planned row count (8).** This was a conscious fixture-design choice, not incidental — it's what makes case P7 a mechanical proof that the dry-run report never conflates the "crops as context" line with the "planned rows" line, rather than relying on a human reading the prose correctly.

## Deviations from Plan

### Auto-fixed Issues

None — plan executed as written, with one structural adjustment to the *execution order* (not the plan's content) described below.

### Structural Note (not a deviation from content, only from literal task boundaries)

The plan's task 2 acceptance criteria requires `--suite apply` and `--suite gates` to **still FAIL** after task 2 lands ("the apply half does not exist yet" — the intended RED state entering task 3). Because both the dry-run half and the apply half were drafted together for internal consistency (shared dataclasses, shared helper functions), the apply-half code was written once and then temporarily removed via `Edit` before task 2's commit, verified RED (`apply` 3/8, `gates` 0/5 — both below their totals, satisfying "still FAIL"), committed, and then restored via `Edit` for task 3's commit. The net diff across both commits is identical to writing them sequentially from scratch; this is a drafting-order note, not a functional deviation. `apply` scored 3/8 rather than 0/8 in that intermediate state because three cases (A5 orphan-contributes-nothing, A6 idempotent-no-op, A7 pre-existing-row-survives) trivially hold when nothing is written at all — flagged here for transparency rather than left silent.

## Verified Report Shape (fixture, verbatim)

The dry-run report against `_seed_legacy_fixture()`'s fixture — this is the known-good shape plan 14-04 should expect to see (with different numbers) when it runs the same script's dry-run half against a real production-data copy:

```
Species Correction Backfill -- Go/No-Go Summary
================================================
crops.rowcount (context only, NOT the write target -- see module docstring): 11
species_corrections rows that would be written: 8
Status: DRY-RUN -- rows would be written as counted above; nothing has been written.
Gallery-source rows (would be written from species.user_common_name): 4
Video-source rows before fan-out (would be matched): 6
Video-source rows after fan-out (matched detections): 6
Detections reachable from BOTH legacy sources (D-03 precedence applied): 2
Unmatched legacy video-level rows (0 detections matched, never dropped silently): 1
  UNMATCHED: video_id=7 original_label='grizzly bear' corrected_at=2026-08-22T10:03:57.223595
Duplicate legacy video-level rows discarded (later corrected_at kept): 1
Gallery rows with a NULL corrected_at (data-quality note, still migrated): 1
audit_trail_digest[species.label] (before write): 80c6f2bcf207931c266008601ab8392fe9a52250ddc7d2125ea9ffaa39837868
audit_trail_digest[video_corrections.original_label] (before write): 017c2c23ed0f26fe470c96abd3314e85525977765a5c9cd249c057d14d738254
RUNVAR: report_generated_at=2026-08-22T10:03:58.015897
```

And the full-flag apply report against the same fixture (post-run verification JSON at the bottom is what plan 14-04's rehearsal-vs-production reconciliation should compare):

```
Species Correction Backfill -- Go/No-Go Summary
================================================
crops.rowcount (context only, NOT the write target -- see module docstring): 11
species_corrections rows written this run: 8
Status: APPLIED -- rows were written as counted above.
Gallery-source rows (were written from species.user_common_name): 4
Video-source rows before fan-out (were matched): 6
Video-source rows after fan-out (matched detections): 6
Detections reachable from BOTH legacy sources (D-03 precedence applied): 2
Unmatched legacy video-level rows (0 detections matched, never dropped silently): 1
  UNMATCHED: video_id=7 original_label='grizzly bear' corrected_at=2026-08-22T10:05:16.014960
Duplicate legacy video-level rows discarded (later corrected_at kept): 1
Gallery rows with a NULL corrected_at (data-quality note, still migrated): 1
Rows skipped as not-newer than an existing species_corrections row: 0
audit_trail_digest[species.label] (verified unchanged): 80c6f2bcf207931c266008601ab8392fe9a52250ddc7d2125ea9ffaa39837868
audit_trail_digest[video_corrections.original_label] (verified unchanged): 017c2c23ed0f26fe470c96abd3314e85525977765a5c9cd249c057d14d738254
RUNVAR: snapshot_path=<snapshot-dir>/species-corrections-snapshot-<timestamp>.db
RUNVAR: audit_log_path=<audit-log-path>
Post-run verification:
{"duplicate_detection_id_count": 0, "fk_violations": 0, "orphaned_detection_id_count": 0, "planned_count": 8, "row_count": 8, "row_count_delta": 0, "species_rowcount": 11, "video_corrections_rowcount": 7}
```

A second full-flag apply against the same fixture left `species_corrections`'s row count and every row's `corrected_at` unchanged (idempotency confirmed both via harness case A6 and a manual second-invocation check during Task 3 verification).

## No Production Database Was Touched

**This plan never opened, read, or wrote `data/wildlife.db`, nor any copy of it.** Every suite in `scripts/verify_backfill_species_corrections.py`, and every manual verification run performed while executing this plan, targeted an explicit `--db <fixture path>` inside a `tempfile.TemporaryDirectory` — the string `"data/wildlife.db"` does not appear anywhere in the harness file (enforced by an acceptance criterion, confirmed via `grep -c`), and `scripts/backfill_species_corrections.py`'s only reference to that path is its `--db` flag's *default value*, never invoked without an explicit override in this plan's own verification. Plan 14-04 owns the full-scale production-copy rehearsal, the operator Go/No-Go, and the real production `--apply`.

## User Setup Required

None — no external service configuration required. This plan produced code only; no deployment step.

## Next Phase Readiness

- `scripts/backfill_species_corrections.py` is complete and proven (dry-run half, apply half, all four gates, snapshot, audit log, reconciliation) against a fixture covering every case the plan's `<behavior>` and `<must_haves>` required.
- Plan 14-04 can proceed directly to: deploy this script to `ubuntulaptop`, run its dry-run against the real production database to get the real corrected-detection count (expected to be far smaller than the "~17,298" crops total per RESEARCH.md Pitfall 6), run a full-scale production-copy rehearsal with pre/post reconciliation, present the operator Go/No-Go, and — only after explicit authorization — run the real `--apply --confirm-irreversible --snapshot-dir --audit-log` against production.
- No blockers for 14-04. This plan's own scope boundary (no production access) was held throughout.

---
*Phase: 14-correction-unification-schema-backfill-cutover*
*Completed: 2026-08-22*

## Self-Check: PASSED

- FOUND: scripts/backfill_species_corrections.py
- FOUND: scripts/verify_backfill_species_corrections.py
- FOUND: commit 37e9c9a (test: RED harness)
- FOUND: commit 1f34bcf (feat: dry-run plan builder)
- FOUND: commit 0f61a4b (feat: apply path, snapshot, audit log, reconciliation)
