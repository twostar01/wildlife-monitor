---
gsd_state_version: "1.0"
milestone: v1.5
milestone_name: Unified Species Buckets & Legacy Cleanup
status: Awaiting next milestone
stopped_at: Phase 18 complete — all phases complete
last_updated: "2026-10-10T05:47:50.198Z"
last_activity: 2026-10-09
last_activity_desc: Milestone v1.5 completed and archived
state_head: 89c34ea9b0c57c5041bf1026e63414330a576013
progress:
  total_phases: 3
  completed_phases: 3
  total_plans: 12
  completed_plans: 12
  percent: 100
current_phase: 18
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-10-09)

**Core value:** Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.
**Current focus:** Planning next milestone (v1.5 shipped 2026-10-09)

## Current Position

Phase: Milestone v1.5 complete
Plan: —
Status: Awaiting next milestone
Last activity: 2026-10-09 — Milestone v1.5 completed and archived

## Performance Metrics

**Velocity:**

- Total plans completed: 12 (v1.0-v1.4, Phases 1-15; per-plan history is in `.planning/milestones/`)
- v1.5 plans completed: 0 of TBD
- Average duration: —
- Total execution time: —

**By Phase (v1.5):**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 16 | 3 | - | - |
| 17 | 5 | - | - |
| 18 | 4 | - | - |
**Per-Plan Metrics:**

| Plan | Duration | Tasks | Files |
|------|----------|-------|-------|
| Phase 16 P01 | spans operator checkpoint | 3 tasks | 3 files |
| Phase 16 P02 | 11 min | 2 tasks | 4 files |
| Phase 16 P03 | 20 min | 3 tasks | 5 files |
| Phase 17 P01 | 5 min | 3 tasks | 11 files |
| Phase 17 P02 | 9 min | 2 tasks | 4 files |
| Phase 17 P03 | 14 min | 3 tasks | 2 files |
| Phase 17 P04 | 66 min | 3 tasks | 0 files |
| Phase 17 P05 | 20min | 2 tasks | 1 files |
| Phase 18 P01 | 35min | 3 tasks | 3 files |
| Phase 18 P02 | spans operator checkpoint | 3 tasks | 0 files |
| Phase 18 P03 | spans operator checkpoints | 3 tasks | 0 files |
| Phase 18 P04 | spans nightly wait | 2 tasks | 1 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- v1.5 continues phase numbering from v1.4 (starts at Phase 16, not reset)
- v1.5 groups the 11 requirements into 3 phases at coarse granularity: Phase 16 (BUCKET-01..04 + READER-01..03: the three raw-label readers reuse `EFFECTIVE_KEY`/`EFFECTIVE_COMMON`, so they are converted once after the merge key is settled), Phase 17 (LEGACY-01/02: reversible decoupling, no schema change), Phase 18 (LEGACY-03/04: the irreversible production drop isolated last, mirroring the Phase 9/11/14 precedent)
- LEGACY-04 (reprocess observation) is non-gating by its own definition; it sits in Phase 18 because the observation window opens once Phase 17's decoupled code is live, and Phase 18 closes without waiting for a natural reprocess
- Merge-key design is an open decision for discuss-phase 16: research recommends Design B (key native rows on normalized common name too, `EFFECTIVE_KEY = COALESCE(CORRECTED_KEY, NATIVE_KEY)`), gated by a read-only production byte-copy audit; Design A (map correction onto native label) is the fallback
- Phase 17 must be live in production before Phase 18's drop: `init_db()` (`SCHEMA` + `MIGRATION_ADD_CORRECTIONS`) recreates the legacy schema on every start until decoupled
- [Phase 16]: Design B approved after production audit: 51 keys become 47 via four native-vs-corrected merges, zero collisions, zero blank/odd names, A1 count 0 (go recorded in 16-01-SUMMARY.md)
- [Phase 16]: 16-02: NATIVE_KEY maps any native row named 'unknown species' to the Unknown literal; resolve_species_key lives in database.py (web_app.py unchanged); native_display counts all native rows incl. hidden (casing tie-break only)
- [Phase 16]: 16-03: has_species uses KNOWN_SPECIES_FILTER plus the Unknown-key comparison, not NOT_EFFECTIVELY_UNKNOWN — A scientific-only correction on an Unknown detection leaves the key Unknown and must not count
- [Phase 16]: 16-03: frontend canonicalizes old gallery links on the resolved key d.label; species half of search() carries the uncorrected-only taxonomy branch — Exact match needs no API change; keeps both halves of global search consistent
- [Phase 17]: 17-01: four one-shot backfill scripts deleted outright (D-01); verify_phase10 lost its --db flag and audit suite, so no harness can open the live database by default
- [Phase 17]: 17-02: dropped the unread corrections key from get_video_by_id and deleted get_video_corrections/apply_corrections_to_species (no consumer found); reprocess write extracted to database.rewrite_species_for_reprocess so a harness runs the real SQL
- [Phase 17]: 17-02: verify_phase17 guard scans comments too, with a three-region allowlist for the live user_common_name API field and an empty GUARD_EXEMPT that Phase 18 extends
- [Phase 17]: 17-03: endpoints suite skips under --suite all without fastapi/httpx but FAILs when requested by name, so the phase gate cannot be met by a skip
- [Phase 17]: 17-03: harness re-runs itself in UTF-8 mode on a non-UTF-8 locale instead of changing web_app.py (index.html read is a dev-box-only false 5xx)
- [Phase 17]: 17-04: decoupled code live in production at ca3b514 (operator approved restart and browser pass); web service restarted 2026-10-07 15:33:56 MDT, cron.log line 319532, soak inputs for 17-05
- [Phase 17]: 17-05: Nightly run 79 (9 videos, success) closes D-04(b); Phase 18 precondition met, legacy objects untouched in production
- [Phase 18]: 18-01: drop script holds the nas_sync lock once for the whole apply (apply-mode report does not probe it); LEGACY-03 stays open until the production drop in 18-02/18-03
- [Phase 18]: 18-01: production checkout is at ca3b514 (b354919 is a local-only docs commit); 18-02 deploy must push first

### Pending Todos

- [2026-10-06] [database] Merge corrected and AI-detected species into a single bucket — [todo](.planning/todos/pending/2026-10-06-merge-corrected-and-ai-detected-species-into-a-single-bucket.md) — scoped as BUCKET-01..04 / Phase 16
- [2026-10-06] [backend] Three video readers still match the raw SpeciesNet label — [todo](.planning/todos/pending/2026-10-06-has-species-and-video-search-raw-label.md) — scoped as READER-01..03 / Phase 16
- [2026-08-21] [database] Remove frozen legacy correction columns and table (D-07) — [todo](.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md) — scoped as LEGACY-01..03 / Phases 17-18
- [2026-08-06] [backend] Species correction from unknown species does not save — [todo](.planning/todos/pending/2026-08-06-species-correction-from-unknown-species-does-not-save.md) — already resolved as FIX-01 / Phase 10; file only needs moving to done/

### Blockers/Concerns

None open. Risks to design around, from `research/PITFALLS.md`:

- Phase 16: the `'Unknown species'` literal at two `get_stats` sites breaks under name-keying, and the Phase 15 fixture hides it by seeding NULL `common_name`; the harness ED4 case flips to one bucket, so separate `LBL_*` from `KEY_*` constants
- Phase 17: no blanket grep-and-delete: `user_common_name` is also the live API field and `corrected_at` exists in three tables. Known dormant crashes to remove first: the reprocess `UPDATE` at `wildlife_processor.py:858` and `get_video_by_id()` calling `get_video_corrections()`
- Phase 18: DDL autocommits in Python `sqlite3` (use an explicit `BEGIN IMMEDIATE` and `Connection.backup()` for the WAL database); the drop needs SQLite 3.35 or later, so confirm the production version first

## Deferred Items

Items acknowledged and deferred, most recent first. Open items only; resolved history is in PROJECT.md and `.planning/MILESTONES.md`.

| Category | Item | Status | Deferred At | Milestone |
|----------|------|--------|-------------|-----------|
| Standing observation | LEGACY-04: observe on the next real reprocess that a reprocessed video's detections start uncorrected (D-02, accepted consequence of snapshot fan-out) | Standing, no deadline, nothing to build (carried like NOTIFY-02); Phase 18 closed without a natural reprocess | 2026-10-06 | v1.5 |
| Phase 15 UAT | Old `#gallery?species=<raw label>` bookmarks land on "Species not found" instead of the merged bucket (BUCKET-05); read-only production audit as a named requirement (BUCKET-06) | Future requirements, not in this roadmap | 2026-10-06 | v1.5 |
| v1.4 milestone close | Archived v1.1 open artifacts: Phase 02 UAT, Phase 06 UAT, Phase 02 verification (human_needed) | Acknowledged, carried forward | 2026-10-06 | v1.4 |
| Standing observation | NOTIFY-02: zero-detection alert fires on a real no-animal night but not on an empty directory; `alert_on_zero_detections` armed 2026-08-07, firing shape not yet observed for the zero-video case | Standing, no deadline, nothing to build | 2026-08-07 | v1.2 |

## Session Continuity

Last session: 2026-10-09T20:06:00.739Z
Stopped at: Phase 18 complete — all phases complete
Resume file: None

## Operator Next Steps

- Start the next milestone with /gsd-new-milestone
