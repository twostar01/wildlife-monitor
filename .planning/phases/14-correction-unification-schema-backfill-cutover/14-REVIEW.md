---
phase: 14-correction-unification-schema-backfill-cutover
reviewed: 2026-10-06T00:00:00Z
depth: standard
files_reviewed: 7
files_reviewed_list:
  - database.py
  - web_app.py
  - scripts/backfill_species_corrections.py
  - scripts/verify_backfill_species_corrections.py
  - scripts/verify_phase14.py
  - scripts/verify_phase12.py
  - scripts/verify_phase10.py
findings:
  critical: 2
  warning: 4
  info: 5
  total: 11
status: issues_found
---

# Phase 14: Code Review Report

**Depth:** standard. `database.py` and `web_app.py` reviewed as the diff against `b278b9d^`; the other five files read in full. Reviewer (gsd-code-reviewer) could not write this file itself, so the orchestrator saved it and added the "Orchestrator verification" notes below. The reviewer did not run any harness.

## Summary

The core write path is sound: the UPSERT is parameterised, the fan-out stamps one shared timestamp, the backfill's `WHERE excluded.corrected_at > species_corrections.corrected_at` guard matches its skip counter, and the apply gate, snapshot and audit-after-commit ordering are correct. The real problems are at the edges of the cutover, where other code still assumes the legacy tables.

## Critical Issues

### CR-01: Reprocess flow keeps stale `species_corrections` rows; the D-02 comment is false

**File:** `wildlife_processor.py:855-860`; claim at `database.py:268-271`

`--reprocess-flagged` does not create new detections. It runs `UPDATE species SET label=?, ... user_common_name=NULL, user_scientific_name=NULL, corrected_at=NULL WHERE detection_id=?` on the same `detection_id`s and never touches `species_corrections`.

Failure scenario: the operator blacklists a label and requeues the video (`requeue_species`). `species.label` changes per detection, but the old unified correction survives, so a Gallery or video-player rename keeps overriding the freshly classified result. A video-player suppress row (`suppressed=1`) also survives, so the crop stays hidden in the video player even if its new label is something else; the player's editor cannot reach the hidden crop, only a Gallery clear can.

Before Phase 14, Gallery corrections were wiped on reprocess and video corrections stopped matching once the label changed. The cutover turned both into permanent per-detection state.

**Fix:** delete the unified rows in the same loop (`DELETE FROM species_corrections WHERE detection_id=?`), or decide deliberately to keep them and correct the comment and D-02 text.

**Orchestrator verification:** CONFIRMED by reading `wildlife_processor.py:855-860`. Note that this contradicts the wording used in the 2026-10-06 BUILDLOG entry and PROJECT.md follow-up ("a reprocessed video's detections start uncorrected"); that holds only for genuinely new detections, not `--reprocess-flagged`.

### CR-02: `DELETE /api/corrections/{id}` deletes by a different id space than `GET /api/corrections` returns

**File:** `web_app.py:699-711`, `733-737`; `database.py:1624-1647`

`GET /api/corrections` still returns frozen `video_corrections` rows with their legacy `id`. `DELETE /api/corrections/{id}` runs `DELETE FROM species_corrections WHERE id=?` and always returns `{"ok": True}`, even when nothing was deleted. A client that lists corrections and deletes legacy id 57 removes whichever unrelated live row has id 57. The POST response also lost its old `id` key.

**Fix:** serve GET from `species_corrections` so ids match, or disable DELETE; delete by `detection_id`; return 404 when `rowcount == 0` (`delete_correction` should return `cur.rowcount`).

**Orchestrator verification:** CONFIRMED in code. Practical risk is LOW today: `static/index.html` has no DELETE caller (its only `/api/corrections` reference is the POST at line 2805), so it needs a manual API call.

## Warnings

### WR-01: `backfill_dedup_videos.py` is blind to `species_corrections` and now hits FK violations

**File:** `scripts/backfill_dedup_videos.py:121-146`, `565`

`correction_signal()` and `child_stats()` consult only `species.corrected_at` and `video_corrections`. A video whose only corrections are post-cutover looks uncorrected and can be chosen as a loser; `apply_group` then runs `DELETE FROM detections WHERE video_id=?` with foreign keys on, which violates `species_corrections.detection_id REFERENCES detections(id)` and aborts the run partway.

**Fix:** add the unified table to `correction_signal`, `child_stats` and the bulk prefetch; delete `species_corrections` rows for loser detections before deleting `detections`. (That script has already served its purpose in production, so this matters only if it is rerun.)

### WR-02: "Dry-run" is not read-only

**File:** `scripts/backfill_species_corrections.py:531-532`

`main()` always calls `database.init_db(args.db)`, including without `--apply`. A typo'd `--db` path creates parent directories and an empty database, and the dry-run then reports "0 rows would be written" and exits 0.

**Fix:** skip `init_db` when not applying; fail if the path does not exist or lacks a `species_corrections` table.

### WR-03: Species/Stats/Timeline keep showing a pre-cutover name after the operator clears or changes it

**File:** `database.py:1186-1212`; readers at `1880`, `1916-1917`, `2395`

The legacy `species.user_common_name` stays populated for every backfilled Gallery correction. `correct_species(det, "", "")` now only deletes the `species_corrections` row, so `DISPLAY_COMMON` keeps serving the old name in those three views. Failure scenario: the operator clears a bad pre-cutover correction; the Gallery shows the raw label, while Species, Stats and Timeline still show the cleared name, with no UI path to remove it.

**Fix:** properly resolved by Phase 15. Interim: keep nulling the legacy columns in the clear path (contradicts D-06) or record this as a known interim defect.

### WR-04: Backfill dedup of legacy rows has a non-deterministic tie-break

**File:** `scripts/backfill_species_corrections.py:120-137`

`ORDER BY video_id, original_label, corrected_at ASC` has no tie-breaker, so duplicate legacy rows with an identical `corrected_at` can resolve differently between dry-run and apply. **Fix:** append `, id ASC`. (Production run reported 0 duplicates, so no data impact.)

## Info

- **IN-01:** `--json --apply` prints two JSON documents (dry-run report, then result) and omits the `verification` dict in JSON mode; the harness works around it with `json_lines[-1]`. (`backfill_species_corrections.py:538, 640-645`)
- **IN-02:** any `OperationalError` is reported as "database is locked, safe to retry"; only use that message when `"locked"` is in the exception text. (`:585-593`)
- **IN-03:** the 8-column UPSERT SQL is duplicated in `database.py:1136-1146`, `:1602-1612` and `backfill_species_corrections.py:442-456`; share one constant.
- **IN-04:** a fan-out that matches nothing returns `{"ok": true, "detections": 0}` and the UI closes the modal as if it succeeded (`web_app.py:724-730`, `static/index.html:2817-2826`); treat `detections === 0` as "no matching crops".
- **IN-05:** harness fragility and gaps: `verify_phase14.py` A5 slices source on exact whitespace and P7's `"context" in line and "11" in line` can match unrelated numbers; nothing covers reprocess-keeps-corrections (CR-01), `DELETE /api/corrections` (CR-02), the dedup script (WR-01), lock contention, or the apply-failure path.

---

_Reviewed: 2026-10-06_
_Reviewer: Claude (gsd-code-reviewer), orchestrator verification notes added_
_Depth: standard_
