"""
verify_run_reconciliation.py — stdlib-only verification harness for Phase 8
Plan 1 (Run Reconciliation): RUN-01, orphaned `runs` row reconciliation.

Suites:
    sweep      — reconcile_interrupted_runs() behavioural correctness against
                 a throwaway SQLite database (R1-R7).
    source     — database.py / wildlife_processor.py source-shape assertions:
                 write pattern, parameterized SQL, no age threshold, call-site
                 position, no alert path (S1-S6).
    frontend   — static/index.html render-site assertions: label, dot color,
                 CSS, loadLastRun's error-line gate, no regression (F1-F6).

Follows scripts/verify_phase7.py's structure: a `_check(case_id, condition,
detail)` helper, per-suite `(passed, total)` returns, a dict suite registry,
argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

Written RED on purpose — every assertion targets the post-fix state, so
running this harness before Tasks 2 and 3 land reports FAIL for every suite.
That is the intended and required outcome for Task 1. RUN-01 is the
requirement under test.

Usage:
    python scripts/verify_run_reconciliation.py --suite sweep|source|frontend|all
    python scripts/verify_run_reconciliation.py --list
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles often default stdout to a legacy codepage (cp1252) that
# cannot encode every character that can legitimately appear in a FAIL
# detail line (source snippets may contain em dashes, curly quotes, or the
# "▾" glyph from static/index.html's button label). Reconfigure to UTF-8
# with a safe fallback so a FAIL print can never itself raise
# UnicodeEncodeError and mask the actual assertion failure.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _index_html_text():
    return (_repo_root() / "static" / "index.html").read_text(encoding="utf-8")


def _database_text():
    return (_repo_root() / "database.py").read_text(encoding="utf-8")


def _processor_text():
    return (_repo_root() / "wildlife_processor.py").read_text(encoding="utf-8")


def _nows(s):
    """Strip all whitespace so CSS/SQL comparisons are immune to formatting."""
    return re.sub(r"\s+", "", s)


def _slice(text, start, end):
    """Return the substring from the first occurrence of `start` up to the
    next occurrence of `end` after that point (or end of text if `end` is
    None or not found). Returns "" when `start` is absent."""
    start_idx = text.find(start)
    if start_idx == -1:
        return ""
    if end is None:
        return text[start_idx:]
    end_idx = text.find(end, start_idx + len(start))
    if end_idx == -1:
        return text[start_idx:]
    return text[start_idx:end_idx]


def _rule(block, selector):
    """Given an already-normalized CSS block and a selector string, return the
    declarations between `selector{` and the next `}`. Returns "" when the
    selector is absent."""
    needle = selector + "{"
    start_idx = block.find(needle)
    if start_idx == -1:
        return ""
    start_idx += len(needle)
    end_idx = block.find("}", start_idx)
    if end_idx == -1:
        return ""
    return block[start_idx:end_idx]


def _strip_py_comments(src):
    """Remove the function's docstring (the first triple-quoted string in the
    snippet) and every `#`-to-end-of-line comment, so a source assertion can
    never be invalidated by prose that legitimately names the symbol it is
    asserting the absence of (e.g. a docstring explaining "deliberately
    set-based with no LIMIT" or referencing `start_time` while describing the
    D-05 safety argument)."""
    m = re.search(r'("""|\'\'\')((?:(?!\1).)*?)\1', src, re.DOTALL)
    if m:
        src = src[: m.start()] + src[m.end():]
    return re.sub(r"#.*", "", src)


def suite_sweep():
    """Seven behavioural cases proving reconcile_interrupted_runs() sweeps
    correctly against a throwaway SQLite database (R1-R7). The sweep never
    touches the real database: the temp path is asserted different from the
    captured database.DB_PATH, and database.set_db_path is restored in a
    finally block."""
    passed = 0
    total = 7

    import database  # noqa: E402  (deferred import — only this suite needs it)

    fn = getattr(database, "reconcile_interrupted_runs", None)
    if fn is None:
        for n in range(1, 8):
            _check(f"sweep/R{n}", False, "reconcile_interrupted_runs missing")
        return (0, total)

    original_db_path = database.DB_PATH
    tmpdir = tempfile.mkdtemp()
    try:
        tmp_db_path = os.path.join(tmpdir, "t.db")
        assert tmp_db_path != original_db_path, "temp db path collides with production DB_PATH"
        database.init_db(tmp_db_path)

        def _seed(status=None, end_time=None, start_time=None, **extra):
            cols = ["start_time", '"trigger"']
            vals = [start_time or datetime.now().isoformat(), "manual"]
            if status is not None:
                cols.append("status")
                vals.append(status)
            if end_time is not None:
                cols.append("end_time")
                vals.append(end_time)
            for k, v in extra.items():
                cols.append(k)
                vals.append(v)
            placeholders = ",".join("?" for _ in vals)
            with database.get_conn() as conn:
                cur = conn.execute(
                    f"INSERT INTO runs ({','.join(cols)}) VALUES ({placeholders})",
                    tuple(vals),
                )
                return cur.lastrowid

        def _row(run_id):
            with database.get_conn() as conn:
                return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

        def _count_interrupted():
            with database.get_conn() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status='interrupted'"
                ).fetchone()[0]

        def _count_all():
            with database.get_conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        def _reset():
            with database.get_conn() as conn:
                conn.execute("DELETE FROM runs")

        # R1 — empty case: nothing NULL-status to reconcile.
        _reset()
        _seed(status="success", end_time=datetime.now().isoformat())
        result = fn()
        ok = result == 0 and _count_interrupted() == 0
        _check("sweep/R1", ok, f"result={result}, interrupted_count={_count_interrupted()}")
        if ok:
            passed += 1

        # R2 — single stale row.
        _reset()
        rid = _seed(start_time="2026-08-04T13:37:49")
        result = fn()
        row = _row(rid)
        expected_error = "Process did not complete — likely interrupted by a service restart or crash"
        end_time_ok = False
        if row and row["end_time"]:
            try:
                datetime.fromisoformat(row["end_time"])
                end_time_ok = True
            except (TypeError, ValueError):
                end_time_ok = False
        ok = (
            result == 1
            and row is not None
            and row["status"] == "interrupted"
            and end_time_ok
            and row["error_summary"] == expected_error
        )
        _check("sweep/R2", ok, f"result={result}, row={dict(row) if row else None}")
        if ok:
            passed += 1

        # R3 — multiple stale rows, set-based, no LIMIT.
        _reset()
        r1 = _seed(start_time="2026-08-01T00:00:00")
        r2 = _seed(start_time="2026-08-02T00:00:00")
        r3 = _seed(start_time="2026-08-03T00:00:00")
        result = fn()
        rows = [_row(r1), _row(r2), _row(r3)]
        ok = result == 3 and all(r["status"] == "interrupted" for r in rows)
        _check("sweep/R3", ok, f"result={result}, statuses={[r['status'] for r in rows]}")
        if ok:
            passed += 1

        # R4 — no widening: an already-terminal row is untouched.
        _reset()
        success_id = _seed(
            status="success",
            end_time=datetime.now().isoformat(),
            error_summary=None,
            videos_processed=12,
            detections_found=5,
        )
        before = dict(_row(success_id))
        _seed()  # NULL-status row alongside it
        fn()
        after = dict(_row(success_id))
        ok = before == after
        _check("sweep/R4", ok, f"before={before}, after={after}")
        if ok:
            passed += 1

        # R5 — UPDATE-only: total row count unchanged.
        _reset()
        _seed(status="success", end_time=datetime.now().isoformat())
        _seed()
        before_count = _count_all()
        fn()
        after_count = _count_all()
        ok = before_count == after_count
        _check("sweep/R5", ok, f"before={before_count}, after={after_count}")
        if ok:
            passed += 1

        # R6 — no fabricated observations: only status/end_time/error_summary move.
        _reset()
        rid = _seed(
            videos_processed=7,
            detections_found=3,
            cameras_json='{"cam":1}',
            offline_cameras_json='["cam"]',
            raw_cleanup_removed=4,
        )
        fn()
        row = _row(rid)
        ok = (
            row is not None
            and row["videos_processed"] == 7
            and row["detections_found"] == 3
            and row["cameras_json"] == '{"cam":1}'
            and row["offline_cameras_json"] == '["cam"]'
            and row["raw_cleanup_removed"] == 4
        )
        _check("sweep/R6", ok, f"row={dict(row) if row else None}")
        if ok:
            passed += 1

        # R7 — duration is derivable after reconciliation.
        _reset()
        rid = _seed(start_time="2026-08-04T13:37:49")
        fn()
        run = database.get_run_by_id(rid)
        ok = run is not None and run.get("duration_secs") is not None and run["duration_secs"] > 0
        _check("sweep/R7", ok, f"run={run}")
        if ok:
            passed += 1

    finally:
        database.set_db_path(original_db_path)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return (passed, total)


def suite_source():
    """Six source assertions against database.py and wildlife_processor.py
    (S1-S6)."""
    passed = 0
    total = 6

    db_text = _database_text()
    db_fn = _strip_py_comments(_slice(db_text, "def reconcile_interrupted_runs(", "\ndef "))
    processor_text = _processor_text()
    startup = _strip_py_comments(_slice(processor_text, "init_db(db_path)", "record_run_start("))

    case_id = "source/S1-defined-below-record-run-end"
    idx_fn = db_text.find("def reconcile_interrupted_runs(")
    idx_end = db_text.find("def record_run_end(")
    ok = idx_fn != -1 and idx_end != -1 and idx_fn > idx_end
    _check(case_id, ok, f"idx_fn={idx_fn}, idx_end={idx_end}")
    if ok:
        passed += 1

    case_id = "source/S2-uses-get-conn"
    ok = bool(db_fn) and "get_conn()" in db_fn and "sqlite3.connect(" not in db_fn
    _check(case_id, ok, f"db_fn_found={bool(db_fn)}")
    if ok:
        passed += 1

    case_id = "source/S3-parameterized-sql-only"
    db_fn_nows = _nows(db_fn)
    has_placeholders = all(v in db_fn_nows for v in ("status=?", "end_time=?", "error_summary=?"))
    no_bad_construction = (
        ".format(" not in db_fn
        and "% (" not in db_fn
        and 'f"""' not in db_fn
        and "f'''" not in db_fn
    )
    ok = has_placeholders and no_bad_construction
    _check(case_id, ok, f"has_placeholders={has_placeholders}, no_bad_construction={no_bad_construction}")
    if ok:
        passed += 1

    case_id = "source/S4-no-age-threshold"
    ok = "WHEREstatusISNULL" in db_fn_nows and "LIMIT" not in db_fn and "start_time" not in db_fn
    _check(case_id, ok, f"db_fn={db_fn!r}")
    if ok:
        passed += 1

    case_id = "source/S5-call-site-position"
    import_tuple = _slice(processor_text, "from database import (", ")")
    idx_init = processor_text.find("init_db(db_path)")
    # Search for the call-site occurrences strictly after init_db(db_path) —
    # both "reconcile_interrupted_runs(" and "record_run_start(" can
    # legitimately appear earlier in the file in prose/docstrings (e.g.
    # _finish_run()'s docstring: "Close the run row opened by
    # record_run_start()"), which must not satisfy this positional check.
    idx_call = processor_text.find("reconcile_interrupted_runs(", idx_init) if idx_init != -1 else -1
    idx_start = processor_text.find("record_run_start(", idx_init) if idx_init != -1 else -1
    ok = (
        "reconcile_interrupted_runs" in import_tuple
        and -1 not in (idx_call, idx_init, idx_start)
        and idx_init < idx_call < idx_start
    )
    _check(
        case_id,
        ok,
        f"in_import={'reconcile_interrupted_runs' in import_tuple}, "
        f"idx_init={idx_init}, idx_call={idx_call}, idx_start={idx_start}",
    )
    if ok:
        passed += 1

    case_id = "source/S6-no-alert-path"
    ok = (
        "log.warning(" in startup
        and "_finish_run" not in startup
        and "_maybe_send_alerts" not in startup
        and "decide_run_alert" not in startup
    )
    _check(case_id, ok, f"startup={startup!r}")
    if ok:
        passed += 1

    return (passed, total)


def suite_frontend():
    """Six source assertions against static/index.html (F1-F6)."""
    passed = 0
    total = 6

    text = _index_html_text()
    label_fn = _nows(_slice(text, "function runStatusLabel(", "\nfunction "))
    dot_fn = _nows(_slice(text, "function runStatusDot(", "\nfunction "))
    last_run = _slice(text, "async function loadLastRun()", "\nasync function ")
    last_run_nows = _nows(last_run)

    case_id = "frontend/F1-label-branch"
    ok = "if(status==='interrupted')return'Interrupted';" in label_fn and "return'InProgress';" in label_fn
    _check(case_id, ok, f"label_fn={label_fn!r}")
    if ok:
        passed += 1

    case_id = "frontend/F2-dot-branch"
    ok = "status==='interrupted'?'gray'" in dot_fn
    _check(case_id, ok, f"dot_fn={dot_fn!r}")
    if ok:
        passed += 1

    case_id = "frontend/F3-gray-css"
    rule = _rule(_nows(text), ".status-dot.gray")
    achromatic = False
    # 6-digit alternative must be tried first: regex alternation matches the
    # first alternative that succeeds, and a fixed {3} quantifier always
    # succeeds against the leading 3 chars of a 6-digit hex, truncating
    # "8a8a8a" to "8a8" (which mis-expands to a non-achromatic "88aa88").
    hex_match = re.search(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})", rule)
    if hex_match:
        h = hex_match.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = h[0:2], h[2:4], h[4:6]
        achromatic = r == g == b
    ok = (
        bool(rule)
        and "background:" in rule
        and "box-shadow:" in rule
        and "var(--muted)" not in rule
        and achromatic
    )
    _check(case_id, ok, f"rule={rule!r}")
    if ok:
        passed += 1

    case_id = "frontend/F4-error-gate-extended"
    ok = (
        "run.status==='interrupted'" in last_run_nows
        and "run.status==='failure'" in last_run_nows
        and "run.status==='partial'" in last_run_nows
    )
    _check(case_id, ok, f"last_run_nows={last_run_nows!r}")
    if ok:
        passed += 1

    case_id = "frontend/F5-no-regression"
    label_ok = all(
        v in label_fn
        for v in (
            "if(status==='success')return'Success';",
            "if(status==='partial')return'Partial';",
            "if(status==='failure')return'Failure';",
        )
    )
    dot_ok = all(
        v in dot_fn
        for v in ("status==='success'?'green'", "status==='partial'?'gold'", "status==='failure'?'red'")
    )
    ok = label_ok and dot_ok
    _check(case_id, ok, f"label_ok={label_ok}, dot_ok={dot_ok}")
    if ok:
        passed += 1

    case_id = "frontend/F6-error-prefix"
    ok = "Runinterrupted:" in last_run_nows and "Runfailed:" in last_run_nows
    _check(case_id, ok, f"last_run_nows={last_run_nows!r}")
    if ok:
        passed += 1

    return (passed, total)


SUITES = {
    "sweep": (suite_sweep, 7),
    "source": (suite_source, 6),
    "frontend": (suite_frontend, 6),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 8 Plan 1 verification harness (RUN-01, run reconciliation)"
    )
    parser.add_argument(
        "--suite",
        choices=list(SUITES.keys()) + ["all"],
        default="all",
    )
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, (_, total) in SUITES.items():
            print(f"{name}: {total} cases")
        sys.exit(0)

    selected = list(SUITES.keys()) if args.suite == "all" else [args.suite]

    all_passed = True
    for name in selected:
        fn, total = SUITES[name]
        passed, total = fn()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            print(f"FAIL: {name} ({passed}/{total})")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
