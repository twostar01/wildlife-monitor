---
phase: 14-correction-unification-schema-backfill-cutover
plan: 04
subsystem: database
tags: [production-migration, species-corrections, backfill, retroactive-record]

requires: [14-02, 14-03]
provides:
  - Production data/wildlife.db migrated: species_corrections holds every historical correction
  - Two pre-write snapshots and a JSONL audit log on ubuntulaptop (data/migrations/)
affects: [15]

key-decisions:
  - "Retroactive record: the production --apply ran 2026-08-22 ~10:37 MDT with the operator present, but no SUMMARY was written at the time. This document was reconstructed 2026-10-06 from the on-box audit log, snapshot files, journalctl and live database queries -- not from a contemporaneous transcript."
  - "Operator authorisation: the operator stated on 2026-10-06 that the 2026-08-22 run was done together with Claude. The Go/No-Go wording itself was not captured and cannot be quoted verbatim."
  - "P10 fixture fix (b33d044): verify_phase12 case P10 failed from date rot (fixture dated 2026-08-16 aged out of get_timeline()'s default 30-day window); now uses an explicit window."
---

# Phase 14 Plan 04: Production deploy, backfill and sign-off Summary

**Production migration completed 2026-08-22 and independently re-verified 2026-10-06: 15 historical corrections written to `species_corrections`, audit-trail digests unchanged, zero FK violations, dashboard on cutover code.**

## Status of this record

Written retroactively. The migration itself was executed on 2026-08-22; the Go/No-Go, the post-write reconciliation and the browser checks were not written down then. Everything below marked **verified** was re-checked read-only against production on 2026-10-06. Anything the operator saw in a browser on 2026-08-22 is **not recorded** and is not claimed.

## Production run (2026-08-22, from `data/migrations/species-corrections-audit.jsonl`)

| Pass | Time (MDT) | rows_written | skipped_not_newer | Snapshot |
|------|-----------|--------------|-------------------|----------|
| 1 (real write) | 10:37:37 | 15 | 0 | `data/migrations/species-corrections-snapshot-20260822T103737204431.db` |
| 2 (window-closing, after restart) | 10:38:11 | 0 | 15 | `data/migrations/species-corrections-snapshot-20260822T103810941013.db` |

- Audit log: `data/migrations/species-corrections-audit.jsonl` (32 lines: 30 detail + 2 summary).
- Dashboard restarted between the passes: `wildlife-monitor.service` stopped 10:37:51, started 10:37:57 (journalctl). Pass 2 was a no-op, which is the expected best answer.
- `audit_trail_digest[video_corrections.original_label]` identical in both summaries (`266edf8e...d15097`); `audit_trail_digest[species.label]` identical across both passes (`4134f7e2...f648`) -- CORR-04.
- Planned count 15 = 4 gallery-source + 12 video-source after fan-out of 11 legacy rows, minus 1 detection reachable from both (D-03 precedence). The crops total (22,337 today) is context only.

## Verified 2026-10-06 (read-only against production)

- `wildlife-monitor.service`: active (since 2026-08-22 10:37:56).
- `PRAGMA foreign_key_check`: 0 violations. Orphaned `detection_id`: 0. Duplicate `detection_id`: 0.
- `species_corrections`: 17 rows = 15 from the backfill + 2 real corrections made after cutover (2026-09-30, `source=video_player`, Northern Raccoon) -- evidence the new write path works in production.
- By source: gallery 4, video_player 8, video_player suppressed 5.
- Legacy tables untouched since freeze: `video_corrections` 11 rows (max `corrected_at` 2026-08-20), 4 `species.user_common_name` rows (max 2026-08-20) -- D-06.
- Harnesses on ubuntulaptop (Python 3.12.3, SQLite 3.45.1): `verify_phase14` 5/5 suites, `verify_backfill_species_corrections` 3/3, `verify_phase10` 3/3, `verify_phase12` 3/3 (after P10 fix), `import database, web_app` OK.
- Dry-run re-run today: 15 planned, 1 unmatched legacy row.

## Deviations and findings

1. **P10 fixture date rot** (fixed, `b33d044`): the fixture's fixed 2026-08-16 dates left `get_timeline()`'s default 30-day window, emptying the timeline. Test-only change; same failure reproduced on the Windows dev machine.
2. **`verify_phase12_ops` cannot run on ubuntulaptop**: it reads `.planning/REQUIREMENTS.md`, which is local-only. Run it on the dev machine.
3. **Unmatched legacy row**: `video_corrections` row for `video_id=31680`, label `...crocuta;crocuta;spotted hyaena`, `corrected_at=2026-06-15T09:36:35`, matched 0 detections. Reported, never silently dropped; not in `species_corrections`. Likely a video consolidated by the Phase 9 dedup backfill. Open follow-up.
4. **Backfill script re-run on a migrated DB is misleading**: it exits 1 because `verify_post_conditions` requires `row_count == planned_count` (17 != 15 once post-cutover corrections exist), and its report prints "rows written this run: 15" when 0 were written. The script is a one-shot migration and is not meant to be re-run, but both are worth fixing if it is ever reused.
5. The plan's task order (rehearsal -> explicit Go/No-Go -> apply) was **not documented** for the 2026-08-22 run. A full-scale rehearsal on a byte-copy was run on 2026-10-06 and is a no-op for the reason in item 4.

## Browser verification (2026-10-06, operator in a real browser at http://192.168.86.6:8080)

The 2026-08-22 browser answers were never recorded, so the checks were repeated against production on 2026-10-06, one at a time, with Claude confirming each backend state.

| Check | Result | Operator observation |
|-------|--------|----------------------|
| a Gallery grid | PASS | Detections 110788, 120579 (AI: domestic cat) and 120502 (AI: Unknown species) show Northern Raccoon / Northern Raccoon / Western Gray Squirrel with the edit badge. |
| b Species detail modal | PASS | 120579 shows Northern Raccoon with edit badge; the rest of that group still reads "Domestic Cat" (the old grouping quirk, see below). |
| c Videos tab | PASS | `20260925030610` pair lists Northern Raccoon; `20260815025411` pair lists Mule Deer. |
| d Video player | PASS | Corrected chips show corrected names with the edit marker, including the Gallery-only correction on `20260806043634`. |
| e Suppression | PASS | Player on `World Watch_00_20260405171526.mp4` shows only Mule Deer, no edit badge; backend confirms the suppressed Wild Boar crop (23412) is still returned by the Gallery under its original name. |
| f New Gallery correction | PASS | `World Watch_00_20260630013837.mp4`: row #33, `source=gallery`, Wild Boar -> Northern Raccoon; shown immediately. |
| g New video-player correction | PASS | `World Watch_00_20260925034640.mp4`: rows #34/#35, `source=video_player`, Wild Boar -> Northern Raccoon (fan-out over 2 matching detections); chip and Gallery both updated with the edit badge. |
| h Intentional changes | ACCEPTED | Operator: fine, no duplicate chips seen. |
| i Accepted staleness | ACCEPTED | Operator: fine. |

Audit-trail spot check (CORR-04): after the two new corrections `species_corrections` holds 20 rows, 0 with an empty original AI label; "AI: Domestic Cat" is visible on the corrected 120579 tile. Legacy tables unchanged (`video_corrections` 11, `species.user_common_name` 4) after both new writes.

The operator's 2026-08-22 Go/No-Go wording is still not recoverable; the 2026-10-06 statement that the run was done jointly is the only record.

## Observations for later (none block this phase)

- **Phase 15 input -- group display name:** the `domestic cat` label group (23 detections) displays under the name "Northern Raccoon" because one member was corrected; its other 22 crops still read "Domestic Cat" (all AI misidentifications). Species list therefore has two "Northern Raccoon" entries. Pre-existing: the pre-Phase-14 code does the same against the pre-migration snapshot.
- **Phase 15 input -- casing:** video-player corrections are stored lowercase (`northern raccoon`, `mule deer`) while Gallery-popover corrections and SpeciesNet names are title-case. Grouping on `corrected_common` would split buckets by case; decide normalise-on-write, normalise-on-read, or case-insensitive grouping.
- **Phase 15 / UX -- species dropdowns are unsorted** (database order), making species hard to find; sort alphabetically when `populateSpeciesFilters()` is reworked.
- **Videos tab list** still shows suppressed species (Mule Deer, Wild Boar on `20260405171526`); identical under the pre-Phase-14 code.
- **Stale Videos-tab state:** a search returned 0 results until a hard refresh, although the API returned the video; likely leftover filter state from an earlier click-through. Not reproduced, not investigated.

## Requirements

CORR-01..04 are satisfied in code (14-01..14-03) and in production data, and ROADMAP SC3 (no visible display regression) is confirmed by the operator's browser checks above.
