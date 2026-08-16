---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: Bug Fixes & Data Integrity
current_phase: 11
current_phase_name: stale-file-path-migration
status: complete
stopped_at: Phase 11 verified (11-VERIFICATION.md, PASS 4/4) — FIX-02 closed; production migration complete, independently re-verified against ubuntulaptop
last_updated: "2026-08-16T05:39:02.134Z"
last_activity: 2026-08-15
last_activity_desc: Phase 11 (stale-file-path-migration) executed and verified — 14,354 production rows migrated, ROADMAP updated
progress:
  total_phases: 4
  completed_phases: 3
  total_plans: 11
  completed_plans: 11
  percent: 75
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-08-11)

**Core value:** Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.
**Current focus:** Phase 11 — stale-file-path-migration

## Current Position

Phase: 11 (stale-file-path-migration) — EXECUTING
Plan: 4 of 4
Status: Phase complete — ready for verification
Last activity: 2026-08-15 — Phase 11 execution started

Progress: [██████████] 100%

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
| 08 | 3 | - | - |
| 09 | 4 | - | - |
| 10 | TBD | - | - |
| 11 | TBD | - | - |
| 12 | TBD | - | - |
| 13 | TBD | - | - |

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
| Phase 11 P01 | 7min | 3 tasks | 2 files |
| Phase 11 P02 | 30min | 3 tasks | 2 files |
| Phase 11 P03 | 25min | 2 tasks | 0 files |
| Phase 11 P04 | 24min | 3 tasks | 2 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- v1.3 continues phase numbering from v1.2 (starts at Phase 10, not reset)
- v1.3 roadmap groups the 8 requirements into 4 phases by risk/coherence, not by REQUIREMENTS.md category alone: Phase 10 bundles the two independent, low-risk code fixes (FIX-01, FIX-03) that touch different files; Phase 11 isolates FIX-02 (the ~14,354-row `/home/nash/...` → `/home/twostar/...` path migration) into its own phase despite being in the same "Bug Fixes" category, since it's a production DB write and gets the same verification-harness-first treatment as every prior production write in this project (Phase 6, Phase 9), even though it is a lower-risk string rewrite, not a row deletion; Phase 12 folds the single-requirement NOTIFY-03 (pure documentation, no code) in with the two decide-then-implement UX/observability items (OBS-02, UI-05) rather than giving it a standalone phase; Phase 13 keeps CLEANUP-04/CLEANUP-05 together since both touch `nas_sync.sh`'s raw-cleanup subsystem
- v1.2 continues phase numbering from v1.1 (starts at Phase 7, not reset to Phase 1)
- v1.2 roadmap follows research's recommended 3-phase split: Phase 7 (independent low-risk frontend/logging fixes), Phase 8 (orphaned-run reconciliation landed before the live-verification observation window opens, so a stray row doesn't contaminate it), Phase 9 (dedup backfill isolated last — highest-risk, irreversible item, mirroring v1.1's own Phase 5 → Phase 6 sequencing precedent)
- Phase 5 and Phase 6 run sequentially, NOT in parallel — Phase 6 (raw_recordings deletion) is unsafe until Phase 5 fixes the dedup key and archive-collision NULL bug, since until then the system cannot reliably tell which DB row is authoritative for a given physical file
- v1.1 continues phase numbering from v1.0 (starts at Phase 5, not reset to Phase 1)
- Backfill repair of the ~19,291 existing duplicate-filename rows is explicitly out of scope for v1.1 — this milestone stops new duplicates at the source only (now in scope for v1.2 as BACKFILL-01 / Phase 9)
- [Phase ?]: Path rewrite computed in Python (leading-prefix slice) not SQL REPLACE() -- prevents corrupting non-leading /home/nash/ occurrences (11-01)
- [Phase ?]: 11-02: check_paths_exist()/find_orphans()/find_collisions() form an unconditional three-gate sequence (existence -> orphan -> collision) upstream of the snapshot in both dry-run and apply mode; audit-log lines deferred until after commit to avoid claiming an unrewritten row was rewritten (T-11-11)
- [Phase ?]: Operator authorized GO on the 11-03 production rehearsal (exact match to 4,274/10,080/14,354 baseline, 0 orphans, 0 collisions, FA-1 confirmed dashboard never broken) — plan 11-04 authorized to run the production --apply write
- [Phase ?]: 11-04: Production --apply write executed on ubuntulaptop -- 14,354 rows rewritten (4,274 crops.crop_path, 10,080 videos.thumbnail_path), reconciled byte-identical against 11-03 baseline (row counts, both non_path_digest values, zero FK violations, exhaustive existence check). Operator confirmed no dashboard regression (SC3). FIX-02 closed.

### Pending Todos

- ~~NAS raw_recordings source files never cleaned up (355GB/48,690 files duplicating the 144GB/20,359-file archive)~~ — resolved 2026-07-30: implemented as Phase 6 (CLEANUP-01/02/03) and armed in production at a 14-day retention (operator decision, 06-04-SUMMARY.md Task 3). First real sweep runs 2026-07-31 via the existing `wildlife-analysis.timer`. `.planning/todos/pending/2026-07-29-nas-raw-recordings-source-files-never-cleaned-up.md` should be moved to `.planning/todos/done/` (or deleted) in a follow-up session.
- Mobile nav bar doesn't wrap — Settings unreachable without horizontal scroll on mobile viewports. Now tracked as UI-01 / Phase 7. `.planning/todos/pending/2026-08-02-mobile-nav-bar-doesn-t-wrap.md`
- "Next scheduled run" text overflows its card on both sides when the timer is inactive (no line break between label and value). Now tracked as UI-02 / Phase 7. `.planning/todos/pending/2026-08-02-next-scheduled-run-text-overflows-card.md`
- ~~Orphaned `runs` row (id=8, 2026-08-04)~~ — resolved 2026-08-08: reconciled automatically by the 2026-08-08 ~06:00 MDT scheduled run (run id=13). See Deferred Items below (RUN-01, confirmed).
- ~~Species correction from "unknown species" does not save (cat→raccoon correction worked, unknown→raccoon on the same video did not)~~ — reported 2026-08-06 on `World Watch_00_20260806043634.mp4`. **Resolved 2026-08-15 as FIX-01 / Phase 10.** Root cause turned out to be two-fold: (1) the suppression filter didn't consider corrected/effective labels (fixed in `database.py`'s `NOT_EFFECTIVELY_UNKNOWN`), and (2) a second bug found during UAT — the reported video is one lens of a dual-lens pair, and the video player always attributed corrections to the primary video's ID regardless of which lens a crop actually came from, so the exact reported case still failed until this was also fixed (`static/index.html`, commit `42dc17d`). Both confirmed live on `ubuntulaptop` against production data.
- ~22% of `crops`/`videos.thumbnail_path` references (14,354 rows) in production point at stale `/home/nash/...` paths that no longer exist — a pre-existing historical home-directory rename (`nash` → `twostar`) with no accompanying database path-rewrite migration. Discovered during Phase 9's 09-04 rehearsal; confirmed unrelated to and unaffected by the dedup backfill. Now tracked as FIX-02 / Phase 11.
- ~~`scripts/backfill_dedup_videos.py`'s printed summary report says "would be deleted"/"would be removed" even when run with `--apply`~~ — cosmetic wording bug in `render_plan_report()`'s shared dry-run/apply template. **Resolved 2026-08-15 as FIX-03 / Phase 10** (fix was written 2026-08-11 but stranded on an orphaned worktree branch until merged to `main`/deployed this session).
- Video-player species corrections (the `video_corrections` table) are only ever displayed inside that video's own detail view — Gallery, the Videos tab/filename search, and every Stats/Species/Timeline chart still show the original AI label forever, since none of those readers know `video_corrections` exists (only the Gallery popover's `species.user_common_name` writes propagate everywhere). Discovered 2026-08-15 during Phase 10 UAT. Not yet scoped into a phase — needs its own plan, same scale as FIX-01's 9-call-site sweep. `.planning/todos/pending/2026-08-15-video-player-corrections-not-reflected-in-gallery-videos-stats.md`

### Blockers/Concerns

None open. v1.3 roadmap created (Phases 10-13); ready to plan Phase 10.

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
| Phase 8 standing checkpoint (08-03) | ~~RUN-03~~: confirm partial-run failure alert fires when a corrupt video is mixed with a good one. **Blocked on a real gap, not just timing**: the original Phase-2 corrupt test video no longer exists on `ubuntulaptop`, and empirical testing (08-02-SUMMARY.md) confirmed no video-corruption strategy (truncated header/mid-stream, zeroed chunk, random bytes, no-read-permission) reaches `wildlife_processor.py`'s error path — OpenCV/ffmpeg absorbs every decode failure as "0 frames extracted," which the code treats as a benign empty video, not a failure. **CLOSED 2026-08-16** as NOTIFY-03 / Phase 12: recorded as a permanent accepted limitation in `.planning/PROJECT.md`'s Key Decisions table (the authoritative record) rather than continuing to chase it — the alert code itself remains live and untouched. | v1.3 Phase 12 (NOTIFY-03) — closed 2026-08-16 | 2026-07-28 (opened) / 2026-08-07 (blocker confirmed) / 2026-08-11 (scoped into v1.3) / 2026-08-16 (closed) |
| Phase 8 standing checkpoint (08-03) | ~~RUN-04a~~: confirm zero-detection alert fires on a genuinely quiet night (videos processed, 0 detections). **CONFIRMED 2026-08-13** — run id=20 (`trigger=scheduled`, 2026-08-13T06:01:52–06:05:14, `worldwatch`: 16 videos/0 detections, `backwall` offline) hit the exact firing shape for the first time in 20 runs. `data/run_20260813_060150.log` on `ubuntulaptop` logs `INFO Alert email sent (zero_detections) to nclemens.cp@gmail.com`. RUN-04b (no alert on a zero-*video* night) remains open — separate scenario, not yet observed. | RUN-04a closed 2026-08-13; RUN-04b standing, no deadline (D-10), NOT scoped into v1.3 | 2026-07-28 (opened) / 2026-08-07 (armed) / 2026-08-08 (checked, still open) / 2026-08-13 (RUN-04a confirmed) |
| Phase 4 root-cause deferral | insert_video dedup across archive moves: nas_sync.sh's archive-collision handling sets an older row's filepath to NULL without setting file_purged_at when the archive destination already exists, producing multiple videos rows for one physical file (19,291 filenames affected in production). See 04-DEFERRED.md. | **Fully resolved 2026-08-10** — Phase 5 stopped new duplicates; Phase 9 (BACKFILL-01) consolidated all 19,289 of 19,291 pre-existing duplicate groups in production (2 skipped intact, `winner-crops-migrated`, reported not silently dropped). See `09-04-SUMMARY.md`. | 2026-07-29 → closed 2026-08-10 |
| Phase 4 root-cause deferral (source-side) | NAS raw_recordings source files never cleaned up — 355GB/48,690 files fully duplicating the 144GB/20,359-file wildlife_archive. See pending todo. | Resolved — Phase 6 (06-04, 2026-07-30): operator enabled 14-day retention cleanup, armed in production, first real sweep 2026-07-31 | 2026-07-29 |
| v1.2 milestone close | ~~Species correction from "unknown species" does not save~~ (cat→raccoon worked, unknown→raccoon on the same video did not). `.planning/todos/pending/2026-08-06-species-correction-from-unknown-species-does-not-save.md` | **Resolved 2026-08-15** as FIX-01 / Phase 10 — see Pending Todos above for the two-part root cause (suppression filter + dual-lens video-id misattribution). | 2026-08-06 (opened) / 2026-08-11 (scoped into Phase 10) / 2026-08-15 (closed) |
| v1.2 milestone close | Formal verification override — Phases 8 and 9 have no scripted `*-VERIFICATION.md` report (the automated verify step never ran in either phase's execution flow). Both are functionally complete: Phase 8's 3 plans executed and its own standing checkpoints (RUN-01/02 confirmed, RUN-03/04 tracked above) were worked through directly against production; Phase 9's 4 plans included an extensive manual production rehearsal, real production write, and full post-run verification (0 FK violations, 0 broken pairings, all deltas reconciled), all operator-witnessed and recorded in `09-04-SUMMARY.md`. | **Operator-authorized override at v1.2 close (2026-08-11)** — proceeding without the formal artifact; the underlying verification work was done, just not through the scripted tool path. | 2026-08-11 |

## Session Continuity

Last session: 2026-08-16T05:39:02.110Z
Stopped at: Completed 11-04-PLAN.md (FIX-02 closed; production migration complete, operator-verified)
Resume file: None

## Operator Next Steps

- Start Phase 11 with `/gsd-discuss-phase 11` (FIX-02: `/home/nash/...` → `/home/twostar/...` path migration, ~14,354 rows)
