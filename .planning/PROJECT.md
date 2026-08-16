# Wildlife Monitor

## What This Is

Automated wildlife detection pipeline for home security cameras and camera traps. Videos are pulled nightly from a NAS, run through MegaDetector (animal detection) and SpeciesNet (species ID), and cataloged in a local web dashboard. The focus is reliable hands-off operation — it should just work every night and surface accurate results without babysitting.

## Core Value

Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.

## Current State

**Shipped:** v1.2 Cleanup & Verification (2026-08-11) — see `.planning/milestones/v1.2-ROADMAP.md`

v1.0, v1.1, and now v1.2 are all shipped. v1.2 closed out the last known rough edges from the first two milestones: mobile-usable dashboard fixes and journald logging (Phase 7), honest handling of service-interrupted pipeline runs plus armed zero-detection alerting (Phase 8), and the historical duplicate-video backfill — the highest-risk item in the whole roadmap, deliberately run last (Phase 9). 19,289 of 19,291 pre-existing duplicate `videos` rows were consolidated in a 41-second production write on 2026-08-10, with zero data loss and full operator verification. That closes a data-quality deferral that had been open since Phase 4.

## Current Milestone: v1.3 Bug Fixes & Data Integrity

**Goal:** Close out the last known bugs, data-quality gaps, and monitoring decisions left over from v1.0-v1.2. No new user-facing features.

**Target features:**
- Fix species correction not saving from "unknown species"
- Migrate stale `/home/nash/...` file path references (~14,354 rows) to the current `/home/twostar/...` prefix
- Fix cosmetic "would be deleted" wording bug in the backfill script's `--apply` mode report
- Decide and implement additional `web_app.py` logging call sites at operational events
- Decide and implement confidence-badge behavior for human-corrected species
- Wire `WM_RAW_CLEANUP_DRY_RUN` into `nas_sync.sh`'s own `--dry-run` flag (WR-02)
- Extend raw-retention misconfiguration warning to also check raw-vs-kept retention (WR-03)
- Close NOTIFY-01 as an accepted limitation (documented, no code change — the failure path is unreachable with current video-corruption test tooling)

NOTIFY-02 remains a standing, no-deadline observation item — armed already, not scoped into this milestone.

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

### Active
- [ ] Should `web_app.py` gain new `logging` call sites at operational events beyond the 18 `print()` sites converted in Phase 7? — scoped into v1.3
- [ ] Should the gallery confidence badge be suppressed or annotated on tiles whose species was human-corrected, since the displayed confidence no longer reflects the corrected label? — scoped into v1.3
- [ ] Species correction from "unknown species" does not save (cat→raccoon correction worked, unknown→raccoon on the same video did not) — reported 2026-08-06, scoped into v1.3
- [ ] `WM_RAW_CLEANUP_DRY_RUN` raw-cleanup preview switch is undocumented/unreachable via `nas_sync.sh`'s own `--dry-run` flag (internal-only by design, Phase 6 WR-02) — scoped into v1.3
- [ ] Raw-retention misconfiguration warning only compares raw-vs-blank, not raw-vs-kept retention (fails safe — skip, never delete — but incomplete; Phase 6 WR-03) — scoped into v1.3
- [ ] ~22% of `crops`/`videos.thumbnail_path` references (14,354 rows) point at stale `/home/nash/...` paths from a pre-existing, unrelated home-directory rename — discovered during Phase 9's rehearsal, confirmed unaffected by the backfill — scoped into v1.3
- [ ] `scripts/backfill_dedup_videos.py`'s printed report says "would be deleted"/"would be removed" even in `--apply` mode — cosmetic wording bug, not a correctness issue — scoped into v1.3

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
- The codebase has zero automated test coverage of the pytest/unittest kind — pure/deterministic functions are covered instead by stdlib-only `scripts/verify_*.py` regression harnesses (8 as of Phase 9: dedup identity, archive collision, lens pairing, raw cleanup, raw cleanup UI, phase 7, run reconciliation, dedup backfill — the last one grew to 10 suites/53 cases across Phase 9's three plans, RED-first throughout), a pattern established in Phase 4 and reused through Phase 9
- `web_app.py` now configures a stdlib `logging` handler (Phase 7) — INFO+ lines reach journald under `wildlife-monitor.service` with timestamps/levels, confirmed exactly-once-per-line in production journald output
- The systemd timer fires at 05:00 daily (`TimeoutStartSec` raised 4h→12h in v1.1 to tolerate backlog/catch-up runs); operator can change the time from dashboard Settings (Phase 3)
- `search_taxonomy()` re-parses the full SpeciesNet classes JSON on every keypress — needs module-level caching
- WordPress publish workflow exists in `PROJECT.md` (root) for the project blog page
- v1.1 closed with the NAS `raw_recordings` backlog draining fast: 355GB/75,580-file baseline (2026-07-30) down to ~3.8GB freed per steady-state nightly run by 2026-08-05, confirming the recurring cleanup mechanism reached steady state within days of being armed
- Production DB is on `ubuntulaptop` (`/home/twostar/wildlife_monitor/data/wildlife.db`); as of Phase 9 it holds ~48,200 `videos` rows (down from ~93,400 pre-backfill) after the 2026-08-10 dedup consolidation. `database.py`'s SCHEMA gained 3 new indexes during that phase (`idx_crops_detection_id`, `idx_species_detection_id`, `idx_videos_paired_video_id`) — a shared-schema change discovered necessary only at real production scale (fixture-scale testing never surfaced it)
- Interrupted-run handling: `reconcile_interrupted_runs()` (Phase 8) sweeps NULL-status `runs` rows to `interrupted` on every `wildlife_processor.py` start — the original NULL-status gap from a 2026-08-05 mid-run service restart is now handled automatically, no manual intervention needed
- A pre-existing, unrelated data-quality issue surfaced during Phase 9's rehearsal: ~22% of `crops`/`thumbnail_path` file references in production point at a stale `/home/nash/...` path prefix from an old, un-migrated home-directory rename. Confirmed unaffected by and unrelated to the dedup backfill; not yet triaged

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
*Last updated: 2026-08-11 — v1.3 (Bug Fixes & Data Integrity) milestone started*
