---
phase: 01-foundation-stability
fixed_at: 2026-06-15T00:00:00Z
iteration: 1
findings_in_scope: 10
fixed: 10
skipped: 0
status: all_fixed
fix_scope: critical_warning
---

# Phase 1: Code Review Fix Report

**Fixed:** 2026-06-15
**Scope:** Critical + Warning (10 findings)
**Status:** all_fixed

## Fixes Applied

### CR-01 — LIKE backslash escapes in `link_lens_pair` (database.py:338)
**Commit:** df729d6
Changed `f"{camera_base}\\_%\\_{timestamp}%"` to `f"{camera_base}_%_{timestamp}%"`.
SQLite does not treat backslash as an escape character in LIKE without an explicit
`ESCAPE` clause, so the prior pattern matched a literal backslash followed by
a wildcard — never matching real filenames. Dual-lens pairing now works.

### CR-02 — FK enforcement lost after `executescript` in `init_db` (database.py:250)
**Commit:** f5dc973
Removed `PRAGMA foreign_keys=ON` from inside the migration script body and added
`conn.execute("PRAGMA foreign_keys=ON")` after the `executescript()` call returns.
`executescript()` issues an implicit COMMIT that resets session-level pragma state;
the inline PRAGMA was a no-op. FK enforcement is now explicitly restored.

### CR-03 — Race condition reading `_run_log_f` without `_run_lock` (web_app.py:836)
**Commit:** 75b06ef
Wrapped the `_run_process` and `_run_log_f` reads and the `close()` call inside
`with _run_lock:` in `api_run_status`. Concurrent calls to `api_trigger_run` can
open a new log file handle while `api_run_status` is closing the previous one;
the lock prevents the race that would write to a closed FD.

### CR-04 — `get_timeline` f-string SQL interpolation (database.py:1300)
**Commit:** d6a86d7
Changed `where = f"AND v.recorded_at >= DATE('now', '-{n} days')"` to use a
bound parameter: `where = "AND v.recorded_at >= DATE('now', '-' || ? || ' days')"`.
Also added `int()` cast on `n` to fail fast on non-integer internal callers.

### WR-01 — Double DB call in `api_reprocess_queue` (web_app.py:568)
**Commit:** b99cbb8
Extracted `db.get_reprocess_queue()` into a local variable so the SELECT runs
once and `count` cannot diverge from `len(videos)` under concurrent writes.

### WR-02 — Falsy `if video_id` check in `api_get_corrections` (web_app.py:538)
**Commit:** b99cbb8
Changed `if video_id:` to `if video_id is not None:` to correctly handle
`video_id=0` (FastAPI coerces `?video_id=0` to integer 0, which is falsy).

### WR-03 — Missing date validation in `api_timeline` (web_app.py:414)
**Commit:** 036e263
Added YYYY-MM-DD regex check for `date_from` and `date_to` before calling
`get_timeline`. Malformed strings previously propagated to `date.fromisoformat()`
inside `get_timeline` and raised unhandled `ValueError` 500s. Now returns HTTP 400.
Also covers IN-04: `date_from > date_to` now raises HTTP 400 instead of returning
an empty result with no error.

### WR-04 — Wrong `window_days` when only `date_to` is provided (database.py:1297)
**Commit:** d9bb0e7
The `else` branch (only `date_to`, no `date_from`) previously set
`window_days = days or 30`, reporting `granularity: day` for multi-year queries.
Now computes `window_days` from a 2020-01-01 epoch so granularity correctly
resolves to `month` for large spans.

### WR-05 — `purge_video_file` marks record purged on `unlink()` failure (database.py:567)
**Commit:** 83c4fe7
Added early `return False` on `OSError` from `p.unlink()` so the DB UPDATE
(`filepath=NULL`, `file_purged_at=...`) only runs when the file was actually
deleted. Operator now sees the record as not purged and can retry.

### WR-06 — `_load_nas_config` over-exposes NAS config keys (web_app.py:93)
**Commit:** d8152cf
Replaced blocklist (`result.pop("NAS_SMB_PASS", None)`) with an allowlist
`{"configured", "mounted", "NAS_MOUNT"}`. Keys like `NAS_HOST`, `NAS_SMB_USER`,
and `NAS_SHARE` are no longer returned to LAN clients via `/api/settings`.

## Info Items Applied

### IN-01 + IN-02 — Inline imports promoted to module level (database.py)
**Commit:** a084572
Added `import re` and `from datetime import datetime, date` at module level.
Removed two inline `import re as _re` and three inline `from datetime import date`
from function bodies. No behavior change.

## Skipped

None — all 10 in-scope findings were applied.

---

_Fixed: 2026-06-15_
_Fixer: Claude (gsd-code-fixer / orchestrator continuation)_
