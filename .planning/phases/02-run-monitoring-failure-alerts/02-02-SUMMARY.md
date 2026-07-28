---
phase: 02-run-monitoring-failure-alerts
plan: 02
subsystem: notifications
tags: [smtp, email, stdlib, alerting]

# Dependency graph
requires:
  - phase: 02-run-monitoring-failure-alerts (plan 01)
    provides: "runs table + record_run_start/record_run_end producing the run dict shape this plan's decide_run_alert()/format_run_alert() consume"
provides:
  - "notifications.py — stdlib-only SMTP validation, non-raising send, alert-policy decision, alert formatter, and D-15 password hygiene helpers"
affects: [02-03, 02-04, wildlife_processor.py real-alert wiring, web_app.py settings/test-email endpoints]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "notifications.py follows image_quality.py's house style: module docstring, flat functions, no classes, stdlib-only imports"
    - "send_notification_email() never raises — validate-then-connect, single try/except Exception boundary, returns (bool, Optional[str]) on every path"
    - "_header_safe() defense-in-depth CRLF/NUL stripper applied before every EmailMessage header assignment"

key-files:
  created: [notifications.py]
  modified: []

key-decisions:
  - "validate_smtp_config() error messages match the UI-SPEC copywriting contract verbatim so they can be rendered directly as inline form errors"
  - "decide_run_alert() checks status (failure/partial -> 'error') before the zero-detection toggle, guaranteeing at most one alert kind per run (D-14 ordering)"
  - "merge_preserved_password() treats a blank/whitespace-only/None incoming password as 'keep stored', never as 'clear the password' (D-15)"

patterns-established:
  - "Shared stdlib-only helper module pattern for logic needed by both the batch CLI and the FastAPI app, avoiding cross-imports between wildlife_processor.py and web_app.py"

requirements-completed: [NOTIFY-01, NOTIFY-02, NOTIFY-04, NOTIFY-05]

coverage:
  - id: D1
    description: "validate_smtp_config() and send_notification_email() — SMTP config validation and a transport that never raises, with explicit 15s connect timeout and STARTTLS-before-login ordering"
    requirement: "NOTIFY-05"
    verification:
      - kind: unit
        ref: "task 1 automated verify block (02-02-PLAN.md) — python inline assertions against notifications.py"
        status: pass
    human_judgment: false
  - id: D2
    description: "decide_run_alert() and format_run_alert() — NOTIFY-01/NOTIFY-02 alert policy (error status wins, D-08 quiet-night guard, D-14 partial-also-alerts) and header-safe alert formatting"
    requirement: "NOTIFY-01"
    verification:
      - kind: unit
        ref: "task 2 automated verify block (02-02-PLAN.md) — python inline assertions against notifications.py"
        status: pass
    human_judgment: false
  - id: D3
    description: "decide_run_alert() zero-detection branch — NOTIFY-02 toggle-gated alert, default-off, excludes zero-videos quiet nights"
    requirement: "NOTIFY-02"
    verification:
      - kind: unit
        ref: "task 2 automated verify block (02-02-PLAN.md) — python inline assertions against notifications.py"
        status: pass
    human_judgment: false
  - id: D4
    description: "send_notification_email()/validate_smtp_config() as a callable pair usable with an arbitrary caller-supplied config dict — the primitive the 02-04 test-email endpoint will wire up"
    requirement: "NOTIFY-04"
    verification:
      - kind: unit
        ref: "task 1 automated verify block (02-02-PLAN.md) — validates against ad hoc dicts, not settings.json"
        status: pass
    human_judgment: false
  - id: D5
    description: "redact_smtp_password() and merge_preserved_password() — D-15 password hygiene pair for the settings GET/POST round trip"
    verification:
      - kind: unit
        ref: "task 3 automated verify block (02-02-PLAN.md) — python inline assertions against notifications.py"
        status: pass
    human_judgment: false

duration: 6min
completed: 2026-07-28
status: complete
---

# Phase 2 Plan 2: SMTP notifications module Summary

**`notifications.py` — stdlib-only smtplib/EmailMessage transport that never raises, an alert-policy gate implementing NOTIFY-01/NOTIFY-02, and the D-15 password-hygiene pair, shared by the batch CLI and the web app**

## Performance

- **Duration:** 6 min
- **Started:** 2026-07-28T06:02:00Z (approx, first commit 06:02:13Z)
- **Completed:** 2026-07-28T06:04:30Z
- **Tasks:** 3
- **Files modified:** 1

## Accomplishments
- `notifications.py` created at repo root with all eight public symbols (`SMTP_TIMEOUT_SECS`, `SMTP_SETTING_KEYS`, `validate_smtp_config`, `send_notification_email`, `decide_run_alert`, `format_run_alert`, `redact_smtp_password`, `merge_preserved_password`), imports limited to `smtplib`, `logging`, `email.message`, `typing`
- `send_notification_email()` validates before ever opening a socket, connects with an explicit 15s timeout, calls `starttls()` before `login()`, skips auth when no username is configured, and returns `(bool, Optional[str])` on every path including hard `OSError` failures — never raises
- `decide_run_alert()` implements the full NOTIFY-01/NOTIFY-02 gate: `failure`/`partial` status always wins ("error", D-14), the zero-detection alert only fires when the toggle is on AND status is success AND videos were processed AND detections are zero (D-08 quiet-night guard, D-09 default-off), and the two kinds are mutually exclusive
- `format_run_alert()` produces a CRLF/NUL-stripped subject and a body carrying every MONITOR-01 field (run id, trigger, status, start/end, duration, videos processed, detections found, per-camera counts, offline cameras, full error summary)
- `redact_smtp_password()`/`merge_preserved_password()` implement both halves of D-15 as pure, non-mutating functions, with the display-only `smtp_password_set` flag stripped before persistence

## Task Commits

Each task was committed atomically:

1. **Task 1: Create notifications.py with validate_smtp_config() and the non-raising send_notification_email()** - `598460c` (feat)
2. **Task 2: Add the alert-policy decision and the run alert formatter** - `4ae0ef7` (feat)
3. **Task 3: Add the D-15 password hygiene helpers** - `55b51cf` (feat)

**Plan metadata:** commit pending (worktree mode — orchestrator merges STATE.md/ROADMAP.md updates after wave completion)

## Files Created/Modified
- `notifications.py` - New stdlib-only module: SMTP config validation, non-raising SMTP send, run-alert decision/formatting, D-15 password hygiene pair (224 lines)

## Decisions Made
- No deviations from the plan's decision set (D-08, D-09, D-10, D-11, D-12, D-14, D-15 all implemented exactly as specified) — see `key-decisions` in frontmatter for the three most load-bearing implementation choices.

## Deviations from Plan

None — plan executed exactly as written. All three tasks' automated verification blocks (from `02-02-PLAN.md`) were run against the implementation and each printed `OK`. The plan's three whole-module checks (`py_compile`, public symbol list, no `fastapi`/`cv2`/`database` in `sys.modules` after import) were also run and passed.

## Issues Encountered
None.

## User Setup Required

None - no external service configuration required. This plan only creates the shared helper module; wiring SMTP settings into `web_app.py`'s Settings tab (with real SMTP credentials) is out of scope for this plan (handled in a later plan per the phase's task breakdown).

## Next Phase Readiness

- `notifications.py`'s eight public symbols are ready for `wildlife_processor.py` to wire into the real-alert path (`decide_run_alert()` → `format_run_alert()` → `send_notification_email()`) and for `web_app.py` to wire into the settings save/load path and the new test-email endpoint (`POST /api/notifications/test`).
- No blockers. This plan has zero dependency on plan 02-01 (database.py `runs` table work, executed in parallel in a sibling worktree) — `notifications.py`'s functions are pure/stdlib and only assume the run-dict *shape* documented in 02-01's plan file, not its actual implementation, so integration risk is limited to a shape mismatch that would surface immediately when the downstream wiring plan runs its own verification.

## Self-Check: PASSED

- FOUND: `notifications.py`
- FOUND: `598460c` (Task 1 commit)
- FOUND: `4ae0ef7` (Task 2 commit)
- FOUND: `55b51cf` (Task 3 commit)
- FOUND: `5a8ac55` (SUMMARY.md metadata commit)
