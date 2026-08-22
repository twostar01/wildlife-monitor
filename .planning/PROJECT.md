# Wildlife Monitor

## What This Is

Automated wildlife detection pipeline for home security cameras and camera traps. Videos are pulled nightly from a NAS, run through MegaDetector (animal detection) and SpeciesNet (species ID), and cataloged in a local web dashboard. The focus is reliable hands-off operation — it should just work every night and surface accurate results without babysitting.

## Core Value

Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.

## Current State

**Shipped:** v1.3 Bug Fixes & Data Integrity (2026-08-21) — see `.planning/milestones/v1.3-ROADMAP.md`

v1.3 closed out the last known bugs, data-quality gaps, and monitoring decisions left over from v1.0-v1.2, with no new user-facing features. Species correction from "unknown species" now saves correctly, including a previously-undiscovered dual-lens video-id misattribution bug found and fixed while closing that exact case out (Phase 10). ~14,354 stale `/home/nash/...` path references in `crops`/`videos` were migrated to `/home/twostar/...` in a single production write, reconciled byte-identical against a pre-write baseline (Phase 11). Gallery tiles for a human-corrected species now show a "corrected" pencil indicator instead of a stale confidence score, `web_app.py` gained five new operational logging call sites, and the NOTIFY-01 live-verification gap was formally closed as a permanent, documented limitation (Phase 12). The raw-cleanup preview is now reachable from `nas_sync.sh --dry-run` directly, and the retention-misconfiguration warning independently checks raw-vs-kept retention alongside the existing raw-vs-blank check — including a post-review fix for a `set -e` bug in the new preview branch, redeployed and reverified in production before the milestone closed (Phase 13).

<details>
<summary>Previous state (v1.2, shipped 2026-08-11)</summary>

v1.2 closed out the last known rough edges from the first two milestones: mobile-usable dashboard fixes and journald logging (Phase 7), honest handling of service-interrupted pipeline runs plus armed zero-detection alerting (Phase 8), and the historical duplicate-video backfill — the highest-risk item in the whole roadmap, deliberately run last (Phase 9). 19,289 of 19,291 pre-existing duplicate `videos` rows were consolidated in a 41-second production write on 2026-08-10, with zero data loss and full operator verification. That closed a data-quality deferral that had been open since Phase 4.

</details>

## Current Milestone: v1.4 Effective Species Labeling

**Goal:** A corrected detection's species stops appearing under its stale AI label everywhere in the app (Species tab, Stats, Timeline, filters), and the two correction mechanisms get a single authoritative resolution order.

**Target features:**
- Effective-label grouping/filtering across `get_species_list()`, `get_stats()`, `get_timeline()`, and the gallery/video species filters — a corrected detection fully leaves its original stale AI label's bucket everywhere, not just in display (which Phase 12 already fixed)
- Research whether to unify the two correction mechanisms (`species.user_common_name` vs. `video_corrections`) into one schema or reconcile at read-time with a fixed precedence order, then implement the chosen approach

One standing observation item remains open with no deadline (NOTIFY-02, see Live-Verification Follow-ups below), and three non-blocking documentation-hygiene items surfaced during the v1.3 milestone audit (see `.planning/milestones/v1.3-MILESTONE-AUDIT.md`) remain deferred, out of scope for v1.4: stale/missing `requirements-completed` frontmatter on a few SUMMARY files, and a stale CLAUDE.md claim that `.planning/` is entirely untracked in git (several files, including `PROJECT.md`/`REQUIREMENTS.md` as of Phase 12, are intentionally tracked).

## Requirements

### Validated

- ✓ NAS video sync via rsync (NFS/SMB) — existing
- ✓ Animal detection via MegaDetector V6 — existing
- ✓ Species identification via SpeciesNet V4 with geo-filtering — existing
- ✓ Image quality scoring per crop (sharpness, brightness, contrast, size) — existing
- ✓ SQLite storage (videos, detections, species, crops, blacklist, corrections) — existing
- ✓ FastAPI web dashboard serving a vanilla JS SPA — existing
- ✓ Species gallery with crop browsing — existing
- ✓ Video browser with playback — existing
- ✓ Activity timeline and detection stats — existing
- ✓ Species blacklist (suppress false/unwanted IDs) — existing
- ✓ Manual species corrections (override SpeciesNet labels) — existing
- ✓ Taxonomy search — existing
- ✓ Reprocess queue (re-run SpeciesNet on flagged videos) — existing
- ✓ Dual-lens camera pairing (wide + telephoto synchronized playback) — hardened in Phase 4, validated 2026-07-29 (safe write-path pairing, production repair migration, player sync fix)
- ✓ Daily scheduled processing via systemd timer — existing
- ✓ Manual "Run Now" trigger from dashboard — existing
- ✓ NAS archive management (keep/purge with configurable retention) — existing
- ✓ Run monitoring: dashboard shows last run outcome (status, timestamp, duration, videos processed, detections found, per-camera offline detection) — Phase 2, validated 2026-07-28
- ✓ Run history: 30-run queryable log with status and key counts, manual/scheduled trigger tagging — Phase 2, validated 2026-07-28
- ✓ Failure notification: SMTP email alert on run error/partial failure or zero-detection runs, configurable from dashboard with test-email button — Phase 2, code-verified 2026-07-28 (3 live-behavior items still pending real-world observation: scheduled-trigger badge, a real partial-run alert, and a real zero-detection alert — tracked in STATE.md Deferred Items)
- ✓ Dual-lens sync overhaul: safe write-path pairing (no more guessed/ambiguous links), a production repair migration for existing bad pairings, a standing consistency check, and a play/pause/seek sync fix with no feedback loops — Phase 4, validated 2026-07-29
- ✓ Dashboard-driven scheduling: change the daily pipeline run time (HH:MM) from the settings tab via a systemd drop-in override, no CLI/systemd access needed; dashboard shows next scheduled run — Phase 3, validated 2026-07-28 on real hardware
- ✓ Gallery filtering: persistent species/camera/date-range/confidence filters that survive navigation and reload, with visually removable filter chips — Phase 3, validated 2026-07-28
- ✓ Inline species corrections: correct a wrong species ID directly from the gallery via a one-click popover showing top SpeciesNet candidates, no separate corrections page needed — Phase 3, validated 2026-07-28
- ✓ Foundation stability fixes: all six documented blocking bugs resolved (DB startup crash, settings path bug, schema drift silently dropping columns, duplicate CorrectionRequest model, taxonomy search lag, shell injection risk on date params) — Phase 1, validated 2026-06-15
- ✓ File-identity dedup fix: `insert_video()` now keys on `(filename, camera_name)` instead of local staging path, `wildlife_processor.py` skips ML compute on already-archived videos, and `nas_sync.sh`'s archive-collision handler pairs `filepath=NULL` with `file_purged_at` atomically — Phase 5, validated 2026-07-30 on production (81,316-row DB, zero duplicate-count growth across three real pipeline runs, no pairing regression)
- ✓ Recurring NAS `raw_recordings` cleanup: operator-configurable retention (days) symmetric to existing archive retention, verify-then-delete (archive copy exists + size match) before any deletion, Storage Usage dashboard tiles showing removed/reclaimed/skipped — Phase 6, validated 2026-08-05 on production (armed at 14-day retention, real downward disk-usage trend confirmed across three cleanup-capable runs: 119.3GB → 81.2GB → 3.8GB freed, zero missing/mismatched-archive reports)
- ✓ Mobile nav bar wraps onto multiple rows at ≤700px so every tab including Settings stays reachable without horizontal scroll — Phase 7, validated 2026-08-06 on production (real mobile viewport, operator-confirmed)
- ✓ "Next scheduled run" card renders its full text with no overflow, label/value stacked and wrapping — Phase 7, validated 2026-08-06 on production (operator-confirmed against the real long inactive-timer string)
- ✓ Gallery species dropdown populates at page boot regardless of which tab is opened first (no more Species-tab-visit dependency) — Phase 7, validated 2026-08-06 on production
- ✓ Gallery confidence badge shown alongside the existing quality score on every tile, in both the Gallery grid and the species-detail modal grid, red/bold below 70% — Phase 7, validated 2026-08-06 on production (operator confirmed both render sites separately)
- ✓ `web_app.py` structured logging: stdlib `logging` module with idempotent StreamHandler setup, all `print()` sites converted — INFO+ lines now reach journald under `wildlife-monitor.service` with timestamps and levels, exactly once per line — Phase 7, validated 2026-08-06 on production
- ✓ Orphaned-run reconciliation: `reconcile_interrupted_runs()` sweeps NULL-status `runs` rows to an honest `interrupted` status on every `wildlife_processor.py` start, rendered as a gray "Interrupted" badge across all three run-display surfaces — Phase 8, confirmed firing on a real production restart 2026-08-08 (run id=13 reconciled row id=8)
- ✓ MONITOR-02 (scheduled-trigger badge renders correctly on a real nightly automatic run) — Phase 8, confirmed 2026-08-08 via run id=13 (`trigger='scheduled'`, corroborated by `journalctl`/`systemctl list-timers`)
- ✓ Historical dedup backfill: `scripts/backfill_dedup_videos.py`, dry-run-first with two flags required to write, rehearsed at full production scale before the real write — Phase 9, executed in production 2026-08-10: 19,289 of 19,291 duplicate-filename `videos` row groups consolidated (2 skipped intact and reported, no safe automatic answer existed), zero FK violations, zero broken pairings, 4 operator corrections preserved, operator dashboard-verified
- ✓ CLEANUP-04: raw-cleanup preview mode reachable via `nas_sync.sh`'s own `--dry-run` flag (standalone branch, no longer requires hand-setting `WM_RAW_CLEANUP_DRY_RUN`) — Phase 13, validated 2026-08-21 on production (operator-witnessed live preview against the real NAS, file count proven unchanged, ordinary sync path confirmed unaffected)
- ✓ CLEANUP-05: raw-retention misconfiguration warning extended to also fire on raw-vs-kept retention, independent of and stacking with the existing raw-vs-blank warning — Phase 13, validated 2026-08-21 on production (operator confirmed all five retention combinations plus save-never-blocks in the live dashboard)
- ✓ FIX-01: species correction from "unknown species" to a specific species now saves and persists after reload, matching existing non-unknown correction behavior — Phase 10, validated 2026-08-15 on production (including a previously-undiscovered dual-lens video-id misattribution bug found and fixed while closing the exact reported case)
- ✓ FIX-02: ~14,354 stale `/home/nash/...` path references in `crops.crop_path`/`videos.thumbnail_path` migrated to `/home/twostar/...` — Phase 11, executed in production 2026-08-16: zero nash-prefixed rows remain, row counts and all non-path columns byte-identical to the pre-write baseline
- ✓ FIX-03: `scripts/backfill_dedup_videos.py`'s printed report says "deleted"/"removed" (not "would be deleted"/"would be removed") when run with `--apply`; dry-run wording unchanged — Phase 10, validated 2026-08-15
- ✓ OBS-02: `web_app.py` gained five `log.info` call sites at decided operational events (correction save/delete, settings/schedule save) beyond the 18 `print()` sites converted in Phase 7 — Phase 12, validated 2026-08-20 on production (journald-confirmed)
- ✓ UI-05: gallery confidence badge replaced with a "corrected" pencil indicator on tiles whose species was human-corrected via either the Gallery popover or the video player's per-crop editor, in both the Gallery grid and species-detail modal — Phase 12, validated 2026-08-20 on production (operator confirmed both correction paths, both grids)
- ✓ NOTIFY-03: NOTIFY-01 (partial-run failure alert live-verification) recorded as a permanent, accepted limitation and removed from the open live-verification backlog — Phase 12, closed 2026-08-16 (alert code itself unchanged and remains live)

### Active

(None — planning next milestone)

### Live-Verification Follow-ups (deferred from Phase 2, code-verified but unobserved in production)

- [ ] NOTIFY-02: zero-detection alert fires on a real no-animal night but not on an empty directory — `alert_on_zero_detections` armed true in production 2026-08-07; the firing shape has never occurred in 13+ runs to date; remains a standing, no-deadline observation item, not scoped into v1.3

### Out of Scope

- Authentication / login — trusted home network; known risk accepted for now
- Cloud sync or remote access — local deployment only
- Real-time / streaming detection — batch processing is the right model for this use case
- Mobile app — dashboard is browser-based, accessed from the local network
- PostgreSQL migration — SQLite is sufficient for home-scale deployments

## Context

- Multiple known tech debt items and bugs are documented in `.planning/codebase/CONCERNS.md` — these should be addressed as a foundation before adding new features
- The codebase has zero automated test coverage of the pytest/unittest kind — pure/deterministic functions are covered instead by stdlib-only `scripts/verify_*.py` regression harnesses (14+ as of v1.3, RED-first throughout: dedup identity, archive collision, lens pairing, raw cleanup + its `preview` suite (PRV1-PRV10), raw cleanup UI + `retention_ui` R8-R12, phase 7, run reconciliation, dedup backfill, `verify_phase10` (fix01/fix03/audit), `verify_stale_paths` (6 suites/37 cases), `verify_phase12`/`verify_phase12_ops` (badge/ui/propagation/logging/docs)), a pattern established in Phase 4 and reused through every subsequent milestone
- `web_app.py`'s stdlib `logging` handler (Phase 7) gained five more `log.info` call sites in v1.3 (Phase 12, OBS-02) at correction and config-change endpoints — journald-confirmed on production
- Production DB is on `ubuntulaptop` (`/home/twostar/wildlife_monitor/data/wildlife.db`); as of v1.3 it holds ~48,464 `videos` rows / 17,298 `crops` rows. `crops.crop_path`/`videos.thumbnail_path` now resolve exclusively to `/home/twostar/...` — the historical `/home/nash/...` stale-prefix issue (14,354 rows, from an old un-migrated home-directory rename) was fully resolved in Phase 11
- Gallery/species-detail tiles show a "corrected" pencil indicator (not a stale confidence score) for any species with a Gallery-popover or video-player correction, via `database.py`'s `HAS_CORRECTION`/`HAS_VIDEO_CORRECTION` SQL constants (Phase 12, UI-05) — but grouping/filter-matching (`get_species_list()`, `get_stats()`, `get_timeline()`, species filters) still key off the original SpeciesNet label, not the corrected one; filed as a follow-up todo (`.planning/todos/pending/2026-08-16-effective-label-grouping-and-correction-unification.md`)
- The systemd timer fires at 05:00 daily (`TimeoutStartSec` raised 4h→12h in v1.1 to tolerate backlog/catch-up runs); operator can change the time from dashboard Settings (Phase 3)
- `nas_sync.sh --dry-run` reaches the raw-cleanup preview branch standalone (Phase 13, CLEANUP-04); the retention-misconfiguration warning checks both raw-vs-blank and raw-vs-kept retention (Phase 13, CLEANUP-05); both paths fail safe (report-only, never trigger deletion)
- `search_taxonomy()` re-parses the full SpeciesNet classes JSON on every keypress — needs module-level caching
- WordPress publish workflow exists in `PROJECT.md` (root) for the project blog page
- CLAUDE.md's claim that all of `.planning/` is "local only, not tracked in git" is stale — `.planning/STATE.md`, `.planning/codebase/*.md`, and (as of Phase 12) `.planning/PROJECT.md`/`.planning/REQUIREMENTS.md` are intentionally force-tracked; the file should be corrected to match actual practice
- Interrupted-run handling: `reconcile_interrupted_runs()` (Phase 8) sweeps NULL-status `runs` rows to `interrupted` on every `wildlife_processor.py` start — the original NULL-status gap from a 2026-08-05 mid-run service restart is now handled automatically, no manual intervention needed

## Constraints

- **Tech stack**: Python 3.11 / FastAPI / vanilla JS — no new frameworks, no build step for the frontend
- **Platform**: Ubuntu 20.04+ with systemd — scheduling changes must interact with systemd units
- **Deployment**: Single-host local network; no load balancer, no containerization required
- **ML models**: MegaDetector and SpeciesNet versions are fixed; no model swapping planned

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Single-file vanilla JS frontend | No build step — simpler to edit and deploy on a headless server | ✓ Good — held through 18 plans of feature work with no build tooling needed |
| SQLite only (no ORM, no Postgres) | Home-scale; `get_conn()` abstraction keeps DB layer replaceable if needed | ✓ Good — survived an 81,316-row production repair migration with no issues |
| Batch processing (not real-time) | Camera footage is reviewed the next morning; latency is not a concern | ✓ Good |
| Systemd for scheduling | Native to the target OS; no cron/celery dependency | ✓ Good — drop-in override + scoped sudo grant shipped in Phase 3, validated live |
| Phase 1 as a mandatory gate before any feature work | Six blocking bugs caused silent failures that would undermine trust in any new feature built on top | ✓ Good — all six closed before Phases 2-4 started; no regressions traced back to pre-gate bugs |
| Phases 2, 3, 4 run in parallel after Phase 1 | Independent subsystems (monitoring, scheduling/gallery, lens sync) with no shared files | ✓ Good — no merge conflicts or cross-phase coupling surfaced |
| Repair migration takes an already-open DB connection instead of `get_conn()` | Must share `init_db()`'s transaction so a failed repair rolls back as a unit, not partial-committed | ✓ Good |
| Sync guard rewritten from shared boolean to target-scoped suppress-echo flag | Shared boolean caused feedback loops between wide/telephoto players on seek | ✓ Good — 0.3s seek tolerance eliminated jitter in manual verification |
| Dedup identity tie-break: prefer live-`filepath` row, then lowest `id` | No upstream spec for which of several equal-identity rows is authoritative | ✓ Confirmed correct by operator against production data, Phase 5 |
| Empty-string and NULL `camera_name` kept as distinct identity buckets (not collapsed) | Production has zero rows in either bucket — no camera is currently unnamed | ✓ Accepted as-is; revisit only if an unnamed camera appears |
| No stronger guarantee added for concurrent/interrupted pipeline runs | Serialized `insert_video()` + atomic collision sites was judged sufficient; true simultaneous runs remain unguaranteed | ⚠️ Revisit — a 2026-08-01 real collision between a scheduled and a manual run did occur (killed by `TimeoutStartSec`) and produced both a misattributed run-stats row and a later orphaned NULL-status row; fixed with a flock guard (`9aee0d1`) but the underlying "no guarantee" decision is now proven to bite in practice, not just theoretical |
| Phase 5 and Phase 6 run sequentially, not in parallel | Phase 6 (raw_recordings deletion) is unsafe until Phase 5 fixes the dedup key and archive-collision NULL bug — until then the system cannot reliably tell which DB row is authoritative for a given physical file | ✓ Good — confirmed structurally enforced: Phase 6's `get_raw_cleanup_candidates()` WHERE clause cannot select a row Phase 5's collision handler already purged (verified by integration check at milestone close) |
| Backfill repair of the ~19,291 existing duplicate-filename rows explicitly out of scope for v1.1 | v1.1 stops new duplicates at the source only; historical repair is separate, higher-risk data-surgery work | ✓ Accepted — reconfirmed at v1.1 close, no operator-facing symptom from the historical rows |
| Raw-cleanup fix-pass scope limited to CR-01 (negative-retention floor) and WR-01 (exception hardening) only | Operator explicitly chose not to also fix WR-02/WR-03/IN-01/IN-02 given none introduce a data-loss path (system fails safe in every case) | ✓ Accepted — held through milestone audit, no must-have failure traced to any of the 4 deferred findings |
| Operator armed raw_recordings cleanup at the shipped 14-day default retention | Confirmed the feature only ever acts on already-archived (`filepath NOT NULL`) videos, never an unprocessed backlog | ✓ Good — first sweep and subsequent runs confirmed the downward trend with zero `same_file`/`escapes_root`/`size_mismatch` reports |
| Phase 7's deploy plan (07-05) ran directly by the orchestrator, not via a spawned executor subagent | It pushes to `origin/main` and restarts the live `wildlife-monitor.service` — judged too consequential to delegate opaquely; also `git push origin main` must run from the actual `main` branch, not a detached worktree-agent branch | ✓ Good — clean push/deploy, no worktree-branch mismatch risk |
| `journalctl \| grep -c 'Wildlife Monitor'` acceptance check must scope to the app's own process (`wildlife-monitor[PID]`) | systemd's own unit-lifecycle log lines share the substring "Wildlife Monitor" (from the unit's `Description=`), producing 5 matches per restart instead of the expected 1 — a verification-script false positive, not a duplicate-logging bug | ✓ Confirmed during Phase 7 Plan 5 — scoped grep gives the correct count of 1 |
| Phase 9 sequenced as a layered plan: read-only production audit first, then a one-group fixture tracer proving the full write path, then widening to every real shape, then the full-scale rehearsal before the real write | Irreversible production data surgery on ~19,291 rows demanded proving the architecture cheaply (fixture) before proving it at scale (rehearsal) before running it for real | ✓ Good — caught two data hazards (H-1 shared thumbnails, H-2 crop migration) by reading code before any delete logic existed, and three production-scale-only bugs during rehearsal, all before the live database was ever opened for writing |
| Full-scale rehearsal against a real production data copy is mandatory before any irreversible write, not just a fixture-DB test suite | A 53-case fixture harness (6-row test data) passed clean the whole way through Phase 9, but two N+1 query patterns and a missing-index gap were completely invisible at that scale — real volume (19,291 groups) is a different animal | ✓ Confirmed — without the rehearsal, the real production write would have taken a projected ~2.3 hours instead of the actual 41 seconds, with the pipeline services down the whole time |
| A shared-file schema change (`database.py`) discovered mid-phase gets escalated to explicit operator sign-off rather than treated as a same-file auto-fix | `database.py` is used by `web_app.py` and `wildlife_processor.py` beyond the phase's own script — materially broader blast radius than a same-file fix | ✓ Good — authorized, landed as its own atomic commit with full regression check across all 8 project harnesses, no issues |
| Quarantine (not permanent deletion) chosen for the one-way production write's orphaned-file handling | Converts the file half of an otherwise-irreversible operation into something reversible for as long as the quarantine directory is kept, at near-zero cost since almost no files were actually projected to be affected | ✓ Good — quarantine directory ended up empty (0 files) since 98.7% of groups shared their thumbnail with the surviving winner; the safety margin cost nothing in practice |
| NOTIFY-01 (partial-run failure alert live-verification) closed as a permanent accepted limitation rather than pursued further — NOTIFY-03 / Phase 12 | no available video-corruption strategy reaches `wildlife_processor.py`'s error path — OpenCV/ffmpeg absorbs every decode failure as "0 frames extracted" | ✓ Accepted — the alert code itself is unchanged and remains live; only the ability to observe it firing in production is unverified, not the alert. Four failure-injection strategies (truncated header, truncated mid-stream, zeroed chunk, random bytes, no-read-permission file) were tried empirically in Phase 8 and every one was absorbed by OpenCV/ffmpeg as a benign "0 frames extracted", per `08-02-SUMMARY.md` |
| Dual-lens video-id misattribution bug (found while closing FIX-01) treated as part of FIX-01's scope, not a separate follow-up item | `10-02-PLAN.md`'s must-have was outcome-framed ("the exact reported case is confirmed fixed"), and the misattribution was the actual root cause blocking that exact case — genuinely undiscovered at planning time but discovered, fixed, and confirmed within the same closure effort | ✓ Good — root-caused, fixed (`42dc17d`), deployed, and confirmed against the live production DB/API with no dangling remnant on the original video |
| FIX-02's path rewrite computed in Python via a leading-prefix slice, not a SQL `REPLACE()` | Prevents corrupting any non-leading `/home/nash/...` occurrence that might appear mid-string | ✓ Good — zero collateral rewrites, confirmed by exhaustive existence checks pre/post-write |
| FIX-02 followed the same layered de-risking sequence as Phase 9's dedup backfill: fixture harness → deploy + read-only production rehearsal → operator Go/No-Go → production `--apply` write | Irreversible production data surgery (14,354 rows) demanded proving the architecture cheaply before running it for real, mirroring the precedent that worked for Phase 9's higher-risk write | ✓ Good — rehearsal baseline (4,274/10,080/14,354, zero orphans/collisions) matched the real write exactly; reconciliation digests byte-identical pre/post |
| Phase 12 scope expanded mid-milestone to fold the separate `video_corrections` propagation-gap todo into UI-05 | Operator explicitly confirmed folding it in in the 2026-08-16 discuss-phase session rather than leaving Gallery/Videos/Stats permanently blind to video-player corrections | ✓ Good — delivered in two halves (display propagation in 12-03; grouping/filter-matching explicitly deferred as a new follow-up todo, not silently dropped) |
| Phase 13's CR-01 fix (preview branch's exit-code capture unreachable under `set -e`) treated as a blocking post-review fix, not deferred tech debt | The bug could crash the nightly systemd unit on an unhandled exception inside the preview heredoc instead of warning and exiting cleanly — a data-safety-adjacent failure mode, not cosmetic | ✓ Good — fixed, given its own regression guard (PRV10), redeployed to production, and reverified (preview suite 9/9 → 10/10) before the phase was marked complete |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-08-21 — milestone v1.4 (Effective Species Labeling) started*
