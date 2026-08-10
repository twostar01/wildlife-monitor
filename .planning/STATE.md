---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: Cleanup & Verification
current_phase: 09
current_phase_name: historical-dedup-backfill
status: complete
stopped_at: Phase 9 complete — BACKFILL-01 executed against production, operator-verified
last_updated: "2026-08-10T14:50:00.000Z"
last_activity: 2026-08-10
last_activity_desc: Phase 09 (all 4 plans) executed, merged, and closed — production backfill complete
progress:
  total_phases: 3
  completed_phases: 3
  total_plans: 12
  completed_plans: 12
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-08-05)

**Core value:** Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.
**Current focus:** v1.2 milestone complete — all 3 phases (7, 8, 9) done; ready for milestone close/audit

## Current Position

Phase: 09 (historical-dedup-backfill) — COMPLETE
Plan: 4 of 4
Status: All plans executed and merged; production write authorized, executed, and operator-verified 2026-08-10
Last activity: 2026-08-10 — Phase 09 closed out (REQUIREMENTS.md/ROADMAP.md updated, BACKFILL-01 marked complete)

Progress: [██████████] 100% of v1.2 plans (12/12 across Phases 7-9)

## Performance Metrics

**Velocity:**

- Total plans completed: 31
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01 | 3 | - | - |
| 02 | 6 | - | - |
| 03 | 6 | - | - |
| 04 | 3 | - | - |
| 05 | 4 | - | - |
| 06 | 4 | - | - |
| 07 | 5 | - | - |
| 08 | TBD | - | - |
| 09 | TBD | - | - |

**Recent Trend:**

- Last 5 plans: —
- Trend: —

*Updated after each plan completion*
**Per-Plan Metrics:**

| Plan | Duration | Tasks | Files |
|------|----------|-------|-------|
| Phase 06 P01 | 22min | 3 tasks | 2 files |
| Phase 06 P02 | 8min | 3 tasks | 3 files |
| Phase 06 P03 | 15min | 2 tasks | 2 files |
| Phase 06 P04 | 2 sessions (~45min combined) | 3 tasks | 0 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- v1.2 continues phase numbering from v1.1 (starts at Phase 7, not reset to Phase 1)
- v1.2 roadmap follows research's recommended 3-phase split: Phase 7 (independent low-risk frontend/logging fixes), Phase 8 (orphaned-run reconciliation landed before the live-verification observation window opens, so a stray row doesn't contaminate it), Phase 9 (dedup backfill isolated last — highest-risk, irreversible item, mirroring v1.1's own Phase 5 → Phase 6 sequencing precedent)
- Phase 5 and Phase 6 run sequentially, NOT in parallel — Phase 6 (raw_recordings deletion) is unsafe until Phase 5 fixes the dedup key and archive-collision NULL bug, since until then the system cannot reliably tell which DB row is authoritative for a given physical file
- v1.1 continues phase numbering from v1.0 (starts at Phase 5, not reset to Phase 1)
- Backfill repair of the ~19,291 existing duplicate-filename rows is explicitly out of scope for v1.1 — this milestone stops new duplicates at the source only (now in scope for v1.2 as BACKFILL-01 / Phase 9)
- [Phase ?]: raw_purged_at and runs.raw_cleanup_* required real PRAGMA-guarded ALTER-TABLE migrations, not just SCHEMA additions, since CREATE-TABLE-IF-NOT-EXISTS is a no-op against already-deployed databases (Phase 6 Plan 1)
- [Phase ?]: _row_older_than() extracted to module level from get_purgeable_videos()'s should_purge_by_age closure so the recorded_at->processed_at (IN-05) fallback has exactly one implementation shared with get_raw_cleanup_candidates() (Phase 6 Plan 1)
- [Phase ?]: R4's grid-detection assertion anchors on the unique 'Retention Policy' card title, not the first 'Blank Videos' text match, since an unrelated tab section shares that title earlier in static/index.html (Phase 6 Plan 2)
- [Phase ?]: loadStorageStats() fetches /api/runs/last and /api/maintenance/storage concurrently via Promise.all() with asymmetric failure handling: storage-fetch failure keeps the existing fallback, runs-fetch failure degrades to the never-ran state instead of breaking the card (Phase 6 Plan 2)
- [Phase ?]: The never-ran check on raw_cleanup_removed is null-aware (=== null / === undefined), not a falsy check, since 0 is a valid ran-cleanly result that must render the completed-run line (Phase 6 Plan 2)
- [Phase ?]: verify_raw_candidate() checks resolved-path equality before os.path.samefile() for the same-file guard, catching a pure-reconstruction misconfiguration even before either file exists (Phase 6 Plan 3)
- [Phase ?]: Per-row try/except OSError added around the whole loop body (beyond verify_raw_candidate()'s own guard) so a stat() failure during size-capture after verification also degrades to skip rather than escaping set -euo pipefail (Phase 6 Plan 3)
- [Phase ?]: Phase 6 Plan 4 Task 3: operator chose ENABLE for raw_recordings cleanup at the shipped 14-day default retention, after confirming the feature only ever acts on already-archived (filepath NOT NULL) videos and never touches an unprocessed backlog; armed via the app's own /api/settings save path (not a hand-edit), confirmed written to settings.json on ubuntulaptop, first real sweep left to run via the existing wildlife-analysis.timer (next due 2026-07-31 ~05:00 MDT) rather than a manual invocation

### Pending Todos

- ~~NAS raw_recordings source files never cleaned up (355GB/48,690 files duplicating the 144GB/20,359-file archive)~~ — resolved 2026-07-30: implemented as Phase 6 (CLEANUP-01/02/03) and armed in production at a 14-day retention (operator decision, 06-04-SUMMARY.md Task 3). First real sweep runs 2026-07-31 via the existing `wildlife-analysis.timer`. `.planning/todos/pending/2026-07-29-nas-raw-recordings-source-files-never-cleaned-up.md` should be moved to `.planning/todos/done/` (or deleted) in a follow-up session.
- Mobile nav bar doesn't wrap — Settings unreachable without horizontal scroll on mobile viewports. Now tracked as UI-01 / Phase 7. `.planning/todos/pending/2026-08-02-mobile-nav-bar-doesn-t-wrap.md`
- "Next scheduled run" text overflows its card on both sides when the timer is inactive (no line break between label and value). Now tracked as UI-02 / Phase 7. `.planning/todos/pending/2026-08-02-next-scheduled-run-text-overflows-card.md`
- ~~Orphaned `runs` row (id=8, 2026-08-04)~~ — resolved 2026-08-08: reconciled automatically by the 2026-08-08 ~06:00 MDT scheduled run (run id=13). See Deferred Items below (RUN-01, confirmed).
- Species correction from "unknown species" does not save (cat→raccoon correction worked, unknown→raccoon on the same video did not) — reported 2026-08-06 on `World Watch_00_20260806043634.mp4`. Not yet triaged to a phase. `.planning/todos/pending/2026-08-06-species-correction-from-unknown-species-does-not-save.md`
- ~22% of `crops`/`videos.thumbnail_path` references (14,354 rows) in production point at stale `/home/nash/...` paths that no longer exist — a pre-existing historical home-directory rename (`nash` → `twostar`) with no accompanying database path-rewrite migration. Discovered during Phase 9's 09-04 rehearsal; confirmed unrelated to and unaffected by the dedup backfill (crops table row count unchanged by that run). Not yet triaged to a phase.
- `scripts/backfill_dedup_videos.py`'s printed summary report says "would be deleted"/"would be removed" even when run with `--apply` — cosmetic wording bug in `render_plan_report()`'s shared dry-run/apply template, not a correctness issue (independently verified the actual production write was genuine via live row-count deltas and `PRAGMA foreign_key_check`). Low priority; script is one-time/historical and unlikely to run again.

### Blockers/Concerns

None open. v1.2 roadmap created (Phases 7-9); ready to plan Phase 7.

**Resolved at milestone close (2026-08-05, v1.1):**

- ~~Roadmap Phase 6 Success Criterion 4 (multi-run downward `raw_recordings` disk-usage trend)~~ — confirmed during the v1.1 milestone audit: real production `runs` data shows 119.3GB → 81.2GB → 3.8GB freed across three cleanup-capable nightly runs. See `.planning/milestones/v1.1-MILESTONE-AUDIT.md`.
- ~~Confirm post-review fix commits (`9a2b662`, `62c9678`) are live on `ubuntulaptop`~~ — confirmed via `git merge-base --is-ancestor 62c9678 HEAD` (exit 0); production HEAD (`74af844`) is 4 commits ahead.

**Resolved during Phase 3:**

- ~~Phase 3 (SCHED): Sudoers setup for systemd daemon-reload requires one-time root access~~ — done; `/etc/sudoers.d/wildlife-monitor` now grants passwordless `daemon-reload` + `restart wildlife-monitor.service` for `twostar`.
- ~~G1: species correction popover renders behind the video player~~ — fixed in 03-04-PLAN.md.
- ~~No passwordless sudo on `ubuntulaptop`~~ — configured 2026-07-28 (scoped to the two commands above only; other sudo calls, e.g. restarting `wildlife-analysis.service`, still require a password).
- ~~06-02 (Retention setting + Storage dashboard): --suite settings, --suite all, and the plan's live-browser verification step still need to run on the real Python 3.11/FastAPI environment~~ — resolved 2026-07-30 during 06-04 Task 2: `verify_raw_cleanup_ui.py --suite all` (settings 4/4, retention_ui 7/7, storage_ui 7/7) and `import database, web_app` both confirmed passing on ubuntulaptop's real Python 3.11/FastAPI environment.

**Resolved — 06-04 Task 3 (blocking checkpoint):**

- ~~Operator go/no-go on arming the first destructive raw_recordings sweep~~ — resolved 2026-07-30. Operator chose ENABLE at the 14-day shipped default retention after confirming the feature only ever acts on already-archived videos (never an unprocessed backlog). Armed via the app's own `/api/settings` save path on `ubuntulaptop`; confirmed written to `settings.json`. `nas_sync.sh` was not run manually — first real sweep is left to `wildlife-analysis.timer`'s next scheduled trigger (2026-07-31, ~05:00 MDT). See `.planning/phases/06-recurring-nas-cleanup/06-04-SUMMARY.md` for the full operator decision transcript and the dated Roadmap AC4 follow-up check (after ~2026-08-02).

## Deferred Items

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Phase 8 standing checkpoint (08-03) | ~~RUN-01 (D-06)~~ | **CONFIRMED 2026-08-08** — run id=13 (`trigger=scheduled`, 2026-08-08T06:05:38) logged `WARNING Reconciled 1 interrupted run(s) from a prior invocation`; row 8 now reads `status='interrupted'`, `end_time=2026-08-08T06:05:38`, exact D-03 error text. Pipeline mechanism confirmed, not a hand-edit. Zero NULL-status rows remain. | 2026-08-07 → closed 2026-08-08 |
| Phase 8 standing checkpoint (08-03) | ~~RUN-02~~ | **CONFIRMED 2026-08-08** — run id=13 (above watermark 12), `trigger='scheduled'`, corroborated independently by `journalctl -u wildlife-analysis.service` (service started 06:00:09) and `systemctl list-timers` (LAST = Sat 2026-08-08 06:00:09 MDT). Not a manual run. | 2026-07-28 → closed 2026-08-08 |
| Phase 8 standing checkpoint (08-03) | RUN-03: confirm partial-run failure alert fires when a corrupt video is mixed with a good one. **Blocked on a real gap, not just timing**: the original Phase-2 corrupt test video no longer exists on `ubuntulaptop`, and empirical testing (08-02-SUMMARY.md) confirmed no video-corruption strategy (truncated header/mid-stream, zeroed chunk, random bytes, no-read-permission) reaches `wildlife_processor.py`'s error path — OpenCV/ffmpeg absorbs every decode failure as "0 frames extracted," which the code treats as a benign empty video, not a failure. Needs either a different failure mechanism or a disclosed code-behavior decision before it can be closed. | Standing checkpoint — Phase 8 (08-03), no deadline (D-10) | 2026-07-28 (opened) / 2026-08-07 (blocker confirmed) |
| Phase 8 standing checkpoint (08-03) | RUN-04a/b: confirm zero-detection alert fires on a genuinely quiet night (videos processed, 0 detections) but not on a night with zero videos. `alert_on_zero_detections` armed true 2026-08-07. 2026-08-08's run (id=13) does not qualify either way — 197 videos processed, 135 raw detections (mostly Blank/Unknown-species/1-Human, operator-confirmed as "no real wildlife" but not literally zero) — so `decide_run_alert()`'s zero-detection branch correctly did not fire; neither confirms nor denies RUN-04. Historical note: the exact firing shape (`success` + videos>0 + detections=0) had never occurred in 12 runs as of the watermark, and still hasn't in 13. | Standing checkpoint — Phase 8 (08-03), no deadline (D-10) | 2026-07-28 (opened) / 2026-08-07 (armed) / 2026-08-08 (checked, still open) |
| Phase 4 root-cause deferral | insert_video dedup across archive moves: nas_sync.sh's archive-collision handling sets an older row's filepath to NULL without setting file_purged_at when the archive destination already exists, producing multiple videos rows for one physical file (19,291 filenames affected in production). See 04-DEFERRED.md. | **Fully resolved 2026-08-10** — Phase 5 stopped new duplicates; Phase 9 (BACKFILL-01) consolidated all 19,289 of 19,291 pre-existing duplicate groups in production (2 skipped intact, `winner-crops-migrated`, reported not silently dropped). See `09-04-SUMMARY.md`. | 2026-07-29 → closed 2026-08-10 |
| Phase 4 root-cause deferral (source-side) | NAS raw_recordings source files never cleaned up — 355GB/48,690 files fully duplicating the 144GB/20,359-file wildlife_archive. See pending todo. | Resolved — Phase 6 (06-04, 2026-07-30): operator enabled 14-day retention cleanup, armed in production, first real sweep 2026-07-31 | 2026-07-29 |

## Session Continuity

Last session: 2026-08-10T14:50:00.000Z
Stopped at: Phase 9 complete — all 3 v1.2 phases (7, 8, 9) now done
Resume file: none — ready for `/gsd-complete-milestone` (v1.2) once Phase 8's standing checkpoints are resolved or explicitly accepted as open

## Operator Next Steps

- RUN-03 needs a decision in a future session (a different failure mechanism, or an explicit code-behavior change) before it can close — see the empirical finding in `08-02-SUMMARY.md`.
- RUN-04a/b: `alert_on_zero_detections` is armed; watch for a naturally quiet night and a naturally empty night, no deadline.
- Phase 9 is complete: 19,289 of 19,291 duplicate-filename groups consolidated in production 2026-08-10, 2 skipped intact and reported, all post-run checks clean, dashboard operator-verified. See `09-04-SUMMARY.md` for the full record.
- Two small new follow-ups surfaced during Phase 9 (see Pending Todos above): the pre-existing `/home/nash/...` stale file-path condition, and a cosmetic report-wording issue in the now-largely-obsolete `backfill_dedup_videos.py` script. Neither is urgent.
- v1.2 milestone (Phases 7-9) is now functionally complete; RUN-03/RUN-04 remain open standing checkpoints with no deadline (D-10) and don't block milestone close if the operator is comfortable leaving them open.
