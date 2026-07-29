---
phase: 04-dual-lens-sync-overhaul
plan: 02
subsystem: database
tags: [sqlite, data-integrity, migration, dual-lens-pairing]

requires:
  - phase: 04-dual-lens-sync-overhaul (plan 01)
    provides: scripts/verify_lens_pairing.py harness; rewritten link_lens_pair() (single-candidate-only, escape-free)
provides:
  - "database._repair_lens_pairings(conn) -> dict — startup migration that re-validates ALL videos.paired_video_id values"
  - "database.check_pairing_consistency() -> int — read-only standing tripwire for asymmetric/dangling pairing pointers"
affects: [04-03 (JS player sync fix — depends on trustworthy paired_video_id going in)]

tech-stack:
  added: []
  patterns:
    - "conservative group-based repair: link only exactly-2-member cross-lens (camera_base, timestamp) groups; unlink/leave-untouched everything else, never guess (D-02/D-04)"
    - "repair runs inside init_db()'s existing with get_conn() block (shared transaction); consistency check runs after that block exits (fresh-connection read of committed state)"

key-files:
  created: []
  modified:
    - database.py

key-decisions:
  - "Fixed a NULL-comparison bug in the RESEARCH.md-derived consistency-check SQL: 'v2.paired_video_id != v1.id' evaluates to NULL (not TRUE) in SQLite when v2.paired_video_id IS NULL, silently missing the asymmetric case where B points at A but A points at nothing. Added an explicit 'v2.paired_video_id IS NULL' OR-arm."
  - "Task 3 (production deploy + nas_sync.sh deferral record) was NOT executed by this worktree-isolated agent — see Deviations."

requirements-completed: [SYNC-04, SYNC-01]

coverage:
  - id: D1
    description: "_repair_lens_pairings(conn) added and wired into init_db(); re-validates all paired_video_id values, links only unambiguous exactly-2-member groups, clears anything else"
    requirement: "SYNC-04"
    verification:
      - kind: unit
        ref: "scripts/verify_lens_pairing.py --suite repair (6/6)"
        status: pass
    human_judgment: false
  - id: D2
    description: "check_pairing_consistency() added and wired into init_db() after the repair's transaction commits; warns only when a broken/asymmetric pointer exists"
    requirement: "SYNC-01"
    verification:
      - kind: unit
        ref: "scripts/verify_lens_pairing.py --suite consistency (3/3)"
        status: pass
    human_judgment: false
  - id: D3
    description: "Production deployment, real-database repair verification, and the nas_sync.sh deferral record (04-DEFERRED.md + STATE.md)"
    requirement: "SYNC-04"
    verification: []
    human_judgment: true
    rationale: "Task 3 requires pushing merged code to origin main, SSH deployment to ubuntulaptop, a production DB backup, and a service restart — none of which are safe or meaningful to run from an isolated per-plan worktree before the orchestrator has merged this branch. Deferred to the checkpoint; see Deviations and CHECKPOINT REACHED below."

duration: ~20min
completed: 2026-07-29
status: complete
---

# Phase 04 Plan 02: Dual-Lens Pairing Repair Migration & Consistency Check Summary

Added `_repair_lens_pairings()` (conservative group-based startup repair, SYNC-04) and `check_pairing_consistency()` (read-only asymmetric/dangling-pointer tripwire, D-06) to `database.py`, both wired into `init_db()`; production deployment (task 3) is a pending checkpoint, not executed by this worktree-isolated agent.

## Performance

- **Duration:** ~20 min
- **Completed:** 2026-07-29
- **Tasks:** 2 of 3 completed (task 3 is a `checkpoint:human-verify` — see below)
- **Files modified:** 1 (`database.py`)

## Accomplishments

- `_repair_lens_pairings(conn) -> dict` groups all dual-lens-parseable videos by `(camera_base, timestamp)`, links only groups with exactly 2 members and differing `lens_index`, short-circuits on already-correct pairs (idempotent no-op), and clears `paired_video_id` on any member of any other group shape that currently points somewhere. Runs inside `init_db()`'s existing `with get_conn() as conn:` block, sharing that transaction.
- `check_pairing_consistency() -> int` does a single read-only `LEFT JOIN` counting videos whose `paired_video_id` doesn't point back symmetrically (dangling or asymmetric). Runs after the `with get_conn()` block exits, so it reads the just-committed repair state on a fresh connection rather than a stale pre-commit snapshot.
- Both are wired into `init_db()` with `log.info`/`log.warning` lines matching the existing `"DB migration: ..."` startup-logging style; the repair summary logs unconditionally (a zeroes line is the useful "it ran and found nothing" signal per D-03), the consistency warning logs only when `broken > 0`.
- `python scripts/verify_lens_pairing.py --suite all` → `PASS: link (6/6)`, `PASS: repair (6/6)`, `PASS: consistency (3/3)`.

## Task Commits

1. **Task 1: Add `_repair_lens_pairings()` and wire it into `init_db()`** - `d2f3c68` (feat)
2. **Task 2: Add `check_pairing_consistency()` and wire it into `init_db()`** - `8c6d3ec` (feat)
3. **Task 3: Deploy to ubuntulaptop, run the repair against production data, record the `nas_sync.sh` deferral** - NOT EXECUTED (checkpoint reached; see below)

**Plan metadata:** committed alongside this SUMMARY (worktree mode — STATE.md/ROADMAP.md updates deferred to the orchestrator per `commit_docs: false` / worktree convention)

## Files Created/Modified

- `database.py` - added `from collections import defaultdict` (module-level import); added `_repair_lens_pairings(conn) -> dict` and `check_pairing_consistency() -> int`, both placed after `link_lens_pair()`; wired both into `init_db()` (repair inside the `with get_conn()` block, consistency check after it exits)

## Decisions Made

- Placed `_repair_lens_pairings()` and `check_pairing_consistency()` immediately after `link_lens_pair()` in `database.py`, keeping all pairing logic colocated (per PATTERNS.md).
- Fixed a NULL-comparison bug in the consistency-check SQL (see Deviations) rather than shipping RESEARCH.md's exact snippet verbatim, since the harness's own `consistency/asymmetric-pointer-detected` test case exposed it as genuinely broken, not a hypothetical edge case.
- Did not attempt Task 3's production-deployment steps from this worktree — see Deviations and the checkpoint state below.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed NULL-comparison bug in `check_pairing_consistency()`'s SQL**
- **Found during:** Task 2, running `verify_lens_pairing.py --suite consistency` for the first time
- **Issue:** The exact SQL from RESEARCH.md's "Pairing consistency check (D-06)" section — `WHERE v1.paired_video_id IS NOT NULL AND (v2.id IS NULL OR v2.paired_video_id != v1.id)` — silently misses the case where `v2.paired_video_id IS NULL` (B points at A, but A points at nothing at all). In SQLite (and standard SQL), `NULL != v1.id` evaluates to `NULL`, not `TRUE`, so that row is excluded from the count instead of being flagged as asymmetric. The harness's own `consistency/asymmetric-pointer-detected` case constructs exactly this scenario and failed (`result=0`, expected `1`).
- **Fix:** Added an explicit `OR v2.paired_video_id IS NULL` arm: `WHERE v1.paired_video_id IS NOT NULL AND (v2.id IS NULL OR v2.paired_video_id IS NULL OR v2.paired_video_id != v1.id)`.
- **Files modified:** `database.py`
- **Verification:** `python scripts/verify_lens_pairing.py --suite consistency` → `PASS: consistency (3/3)` (was `2/3` before the fix)
- **Committed in:** `8c6d3ec` (Task 2 commit)

### Scope Note (not a deviation rule — checkpoint deferral)

**Task 3 (production deployment + `nas_sync.sh` deferral record) was not executed.** This plan's Task 3 is a `checkpoint:human-verify` with `gate="blocking"` whose own automated steps require `git push origin main`, SSH deployment to `ubuntulaptop`, a production database backup, and a `systemctl restart`. This SUMMARY was produced by a parallel wave executor running in an isolated git worktree (`worktree-agent-a3f7a1b410738ea3d`), not on `main` — this branch has not yet been merged by the orchestrator. Pushing to `origin main` or deploying to production from an unmerged worktree branch would be premature and is outside a worktree-isolated agent's remit (per this agent's `<parallel_execution>` instructions: "If you reach a checkpoint task or auth gate, STOP and return the structured checkpoint state... do not guess the user's answer or proceed past the checkpoint."). Tasks 1 and 2 (the code changes) are complete, committed, and fully green against the synthetic-fixture harness. Task 3 — writing `.planning/phases/04-dual-lens-sync-overhaul/04-DEFERRED.md`, appending the `nas_sync.sh` deferral to `STATE.md`, pushing, deploying, and verifying against the real 81,316-row production database — remains outstanding and should run after this branch is merged to `main`.

---

**Total deviations:** 1 auto-fixed (1 bug), plus 1 scope note (checkpoint deferred to post-merge)
**Impact on plan:** The auto-fix was necessary for correctness — the consistency check must correctly identify asymmetric pointers, not just dangling ones, for D-06 to hold. No scope creep. Task 3's deferral is a process/timing necessity of worktree-isolated parallel execution, not a plan or code deficiency.

## Issues Encountered

None beyond the SQL bug documented above.

## User Setup Required

None for tasks 1-2 — no external service configuration required. Task 3, once executed post-merge, requires SSH access to `ubuntulaptop` (already configured per `STATE.md`) and the existing passwordless-sudo scoping for `daemon-reload`/`restart wildlife-monitor.service` (already configured per `STATE.md`'s "Resolved during Phase 3" note).

## Known Stubs

None — both `_repair_lens_pairings()` and `check_pairing_consistency()` are fully functional against the synthetic-fixture harness. They have not yet been proven against the real production database (that's Task 3's job).

## Threat Flags

None — this plan's `<threat_model>` (T-04-06 through T-04-11, T-04-SC) anticipated exactly the surface touched: the bulk `UPDATE` in `_repair_lens_pairings()`, the read-only join in `check_pairing_consistency()`, and the commit-boundary ordering between them. No new, unlisted surface was introduced. T-04-06's rollback-path mitigation (a pre-restart `.bak`) is specifically Task 3's job and has not yet been exercised against production.

## Next Phase Readiness

- `database.py`'s pairing write path (`link_lens_pair()`, from plan 04-01) and repair path (`_repair_lens_pairings()`, this plan) now share the same exactly-two-members rule, so plan 04-03's JS player-sync fix can rely on `paired_video_id` being trustworthy once this branch merges and Task 3 runs.
- **Blocker for full phase completion:** Task 3 (production deployment, real-data verification, `nas_sync.sh` deferral record) must run after this worktree merges to `main` — it cannot run from within this isolated worktree. Recommend re-invoking this plan's Task 3 (or a dedicated follow-up) once `04-02`'s commits land on `main`.

---
*Phase: 04-dual-lens-sync-overhaul*
*Completed: 2026-07-29 (tasks 1-2; task 3 pending post-merge)*
