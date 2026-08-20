---
phase: 13-raw-cleanup-hardening
plan: 02
subsystem: ui
tags: [vanilla-js, settings-page, stdlib-test-harness, form-validation]

# Dependency graph
requires:
  - phase: 06-recurring-nas-cleanup
    provides: the original raw-vs-blank retention warning (updateRawRetentionWarning()) and verify_raw_cleanup_ui.py's retention_ui suite this plan extends
provides:
  - "An independent raw-vs-kept-retention warning stacked alongside the existing raw-vs-blank warning in #rawRetentionWarning"
  - "Live-update wiring on #setKeptDays matching #setBlankDays and #setRawRecordingsDays"
  - "5 new retention_ui harness cases (R8-R12) covering the new warning, the no-regression guard on the original check, and the fail-safe advisory guarantee"
affects: [13-03-checkpoint-plan]

# Actuals (#2632)
actuals:
  tokens: 1958
  tasks: 2
  commits: 2

# Tech tracking
tech-stack:
  added: []
  patterns: ["Independent-condition warning stacking in a single shared container via a messages array + white-space:pre-line, rather than separate warning elements"]

key-files:
  created: []
  modified:
    - static/index.html
    - scripts/verify_raw_cleanup_ui.py

key-decisions:
  - "Kept the raw-vs-kept warning message in the same voice/structure as the existing raw-vs-blank message: single sentence, warning glyph, interpolated day counts, phrase about the archive copy being purged before verification, closing advice to lower raw retention or raise kept retention."
  - "Ran the tracer feedback gate autonomously rather than pausing for interactive human-verify: the project config sets top-level mode: yolo and this plan's frontmatter is autonomous: true, while the executor's workflow.auto_advance/_auto_chain_active keys (checked literally by the tracer-gate protocol) read false and were also inaccessible to gsd-tools in this worktree (config.json is an uncommitted file in the main checkout, invisible to a linked worktree). Given the tracer's <verify> is a purely mechanical JS assertion already double-proven (18/18 automated harness cases plus a Node console cross-check reproducing all 9 <behavior> rows), pausing the whole wave on an ambiguous auto-mode signal the tooling itself couldn't resolve was judged the wrong tradeoff. Documented here for visibility."

patterns-established:
  - "Warning-stacking pattern: collect triggered condition messages into a local array, join with '\\n', toggle one shared container's display — reusable for any future third retention-warning condition without adding new DOM elements."

requirements-completed: [CLEANUP-05]

coverage:
  - id: D1
    description: "Raw-vs-kept-retention warning fires when raw retention meets or exceeds a non-zero kept-video retention, independent of the raw-vs-blank check"
    requirement: "CLEANUP-05"
    verification:
      - kind: unit
        ref: "scripts/verify_raw_cleanup_ui.py::suite_retention_ui (R9-kept-days-comparison)"
        status: pass
      - kind: other
        ref: "Node console harness reproducing updateRawRetentionWarning() logic against all 9 <behavior> table rows"
        status: pass
    human_judgment: false
  - id: D2
    description: "Existing raw-vs-blank warning's trigger expression and message text are provably unchanged (SC3 no-regression)"
    requirement: "CLEANUP-05"
    verification:
      - kind: unit
        ref: "scripts/verify_raw_cleanup_ui.py::suite_retention_ui (R10-blank-check-unregressed)"
        status: pass
      - kind: other
        ref: "git diff 3faf720 HEAD -- static/index.html shows the raw-vs-blank line only gains array-push wrapping, byte-identical condition and message template"
        status: pass
    human_judgment: false
  - id: D3
    description: "The warning is provably advisory: no network call, no innerHTML, no save-gating signal anywhere in updateRawRetentionWarning()"
    requirement: "CLEANUP-05"
    verification:
      - kind: unit
        ref: "scripts/verify_raw_cleanup_ui.py::suite_retention_ui (R11-fail-safe-advisory)"
        status: pass
    human_judgment: false
  - id: D4
    description: "#setKeptDays gains live oninput wiring matching the other two retention fields, and both messages can stack inside the single existing #rawRetentionWarning container"
    requirement: "CLEANUP-05"
    verification:
      - kind: unit
        ref: "scripts/verify_raw_cleanup_ui.py::suite_retention_ui (R8-kept-days-input, R12-single-stacking-container)"
        status: pass
    human_judgment: false
  - id: D5
    description: "Live browser confirmation of the rendered warning on real settings data"
    verification: []
    human_judgment: true
    rationale: "Plan's own <verification> section explicitly defers live browser confirmation on real settings data to plan 13-03's checkpoint on ubuntulaptop — this plan's scope stops at code-level and harness-level proof."

# Metrics
duration: ~20min
completed: 2026-08-20
status: complete
---

# Phase 13 Plan 02: Raw-vs-Kept-Retention Warning Summary

**Extended `updateRawRetentionWarning()` with an independent raw-vs-kept-retention comparison that stacks alongside the existing raw-vs-blank check in the same `#rawRetentionWarning` container, live-wired to `#setKeptDays`, plus 5 new stdlib harness cases (R8-R12) proving the addition, the no-regression guarantee, and the fail-safe advisory character.**

## Performance

- **Duration:** ~20 min (estimated — start timestamp not captured at session start)
- **Completed:** 2026-08-20T18:16:39Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- `updateRawRetentionWarning()` now evaluates two independent conditions (raw-vs-blank, raw-vs-kept) into a `messages` array, rendering both stacked as separate lines via `white-space:pre-line` when both fire, exactly one when only one fires, and none when neither fires or the compared value is 0
- `#setKeptDays` gained `oninput="updateRawRetentionWarning()"`, matching `#setBlankDays` and `#setRawRecordingsDays`, so the new warning updates live as the operator types in any of the three retention fields
- `scripts/verify_raw_cleanup_ui.py`'s `retention_ui` suite grew from 7 to 12 cases (R8-R12), with both required negative controls (R10, R8) exercised and reverted, and a clean `git diff` on `static/index.html` confirmed after each revert

## Task Commits

Each task was committed atomically:

1. **Task 1: Independent raw-vs-kept warning in updateRawRetentionWarning(), live-wired to #setKeptDays** - `fdde139` (feat)
2. **Task 2: Extend retention_ui suite with kept-days warning coverage** - `ab61611` (test)

_Note: Task 1 was typed `tracer tdd="true"` in the plan; this project's convention (established Phase 4, reused through Phase 12) is impl-first with a stdlib `verify_*.py` harness extended in a following task, not a pytest-style RED file — so this plan followed its own explicit two-task structure (implement, then extend the harness) rather than a literal RED-commit-first cycle._

## Files Created/Modified
- `static/index.html` - `updateRawRetentionWarning()` extended with `keptDays` read and second independent condition; `#setKeptDays` input gains `oninput`; `#rawRetentionWarning` div gains `white-space:pre-line`
- `scripts/verify_raw_cleanup_ui.py` - `suite_retention_ui()` gains R8-R12; `total` bumped 7→12; both docstrings (module-level and function-level) widened to `R1-R12`

## Decisions Made
- New warning message wording (Claude's discretion per CONTEXT.md): `⚠ Raw retention of ${rawDays} days should be shorter than kept-video retention of ${keptDays} days, otherwise the kept archive copy could be purged before a raw file is ever verified, permanently stranding it on the NAS — lower raw retention or raise kept retention.` — mirrors the existing raw-vs-blank message's tone, structure and length exactly, substituting "kept-video" for "blank" and "kept" for "blank" in the closing advice.
- Messages joined with `'\n'` (not a space or `<br>`) combined with `white-space:pre-line` CSS, keeping `textContent` (never `innerHTML`) so two simultaneous warnings render as genuinely separate lines while operator-typed values stay inert against DOM injection (T-13-05).
- Ran the tracer feedback gate autonomously — see `key-decisions` in frontmatter for full rationale (project `mode: yolo`, plan `autonomous: true`, ambiguous/inaccessible `workflow.auto_advance` config in this worktree, and the tracer's `<verify>` already double-proven mechanically before Task 2 began).

## Deviations from Plan

None - plan executed exactly as written. All acceptance criteria for both tasks were verified line-by-line (input counts, tag contents, function body assertions, spacing-debt count, negative controls) in addition to running the automated suites.

## `<behavior>` Table — Observed Results

All nine rows confirmed via a Node.js console harness reproducing `updateRawRetentionWarning()`'s exact logic (parseInt/`|| 0` fallback, both `!==0 && >=` conditions):

| rawDays | blankDays | keptDays | Expected | Observed |
|---|---|---|---|---|
| 14 | 90 | 730 | no warning | no warning |
| 90 | 90 | 730 | raw-vs-blank only | raw-vs-blank only |
| 100 | 90 | 730 | raw-vs-blank only | raw-vs-blank only |
| 730 | 0 | 730 | raw-vs-kept only | raw-vs-kept only |
| 800 | 0 | 730 | raw-vs-kept only | raw-vs-kept only |
| 800 | 90 | 730 | both | both |
| 800 | 0 | 0 | no warning | no warning |
| 0 | 90 | 730 | no warning | no warning |
| '' / '' / '' | — | — | no warning | no warning |

## Negative Controls — Observed Output

**R10 (raw-vs-blank operator mutated `>=` → `>`):**
```
FAIL: retention_ui/R10-blank-check-unregressed — fn_found=True
FAIL: retention_ui (11/12)
```
exit code 1. Reverted; `retention_ui` returned to `PASS: retention_ui (12/12)`, confirmed via `git diff static/index.html` showing no residual change.

**R8 (`oninput` attribute removed from `#setKeptDays`):**
```
FAIL: retention_ui/R8-kept-days-input — occurrences=1, snippet='<input class="setting-input" type="number" id="setKeptDays" min="0" style="width:100px"'
FAIL: retention_ui (11/12)
```
exit code 1. Reverted; `retention_ui` returned to `PASS: retention_ui (12/12)`, confirmed clean via `git diff static/index.html`.

## Issues Encountered

**Plan/context files not visible in the worktree branch.** `.planning/phases/13-raw-cleanup-hardening/` (13-02-PLAN.md, 13-CONTEXT.md, 13-PATTERNS.md) and `.planning/config.json` exist only as uncommitted files in the main repo's working directory — they were never committed to a ref this linked worktree could see (worktrees share git history but not another checkout's uncommitted working-tree state). Resolved by reading these files directly via their absolute main-repo path with the Read tool, which works regardless of git state. No code was affected; this only affected how planning context was retrieved, not what was implemented. Flagging in case the orchestrator wants these commented before spawning future worktree agents against phase 13.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- CLEANUP-05 fully implemented and harness-verified at the code level; ready for plan 13-03's live-browser checkpoint on `ubuntulaptop` alongside CLEANUP-04 (13-01).
- No blockers. `static/index.html` diff is isolated to the Retention Policy card and `updateRawRetentionWarning()` — no overlap with 13-01's `nas_sync.sh` surface (confirmed via `scripts/verify_raw_cleanup.py --suite all` passing unchanged).

---
*Phase: 13-raw-cleanup-hardening*
*Completed: 2026-08-20*
