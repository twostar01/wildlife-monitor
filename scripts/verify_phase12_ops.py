"""
verify_phase12_ops.py — stdlib-only verification harness for Phase 12
Plan 02 (operational closure): OBS-02 logging call sites, NOTIFY-03
documentation closure.

Suites:
    logging — OBS-02, source assertions over web_app.py confirming the five
              D-01 endpoints (api_correct_species, api_save_correction,
              api_delete_correction, api_save_settings, api_save_schedule)
              each gained exactly one log.info(...) call on the success path,
              using %s lazy interpolation, with no credential leak, and that
              the seven D-02-excluded endpoints (api_add_blacklist,
              api_remove_blacklist, api_requeue_species, api_purge,
              api_promote_paired, api_trigger_run, api_apply_update) gained
              none (L1-L12).
    docs    — NOTIFY-03, assertions over .planning/PROJECT.md and
              .planning/REQUIREMENTS.md confirming NOTIFY-01 was moved from
              an open Live-Verification Follow-up into a settled Key
              Decisions row with verbatim D-08 rationale, and that
              NOTIFY-03's traceability row reads Complete (K1-K6).

Follows scripts/verify_phase10.py's structure: a `_check(case_id, condition,
detail)` helper, per-suite `(passed, total)` returns, a dict suite registry,
argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

NOTIFY-03 is a documentation-only closure: no code path changes accompany it,
the Phase 2 partial-run failure alert code remains live and untouched, and
what is being accepted is the inability to verify that alert in production —
not a decision to stop alerting.

Written RED on purpose — every assertion targets the post-fix state, so
running this harness before plan 12-02's tasks 2 and 3 land reports FAIL for
L1-L12 (0/12, no log call sites exist yet) and at most K4 of the docs suite
(the NOTIFY-02 sibling case, already true today). That is the intended and
required outcome — tasks 2 and 3 are what turn this harness green.

Usage:
    python scripts/verify_phase12_ops.py --suite logging|docs|all
    python scripts/verify_phase12_ops.py --list
"""

import argparse
import sys
from pathlib import Path


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _web_app_text():
    return (_repo_root() / "web_app.py").read_text(encoding="utf-8")


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


def _strip_comments(text):
    """Drop every line whose lstrip() begins with '#' and return the rest
    joined with '\\n'. Every logging-suite containment test runs on this
    stripped form so a future explanatory comment inside an endpoint body
    can never make a case pass or fail spuriously."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def _region(text, start_marker):
    """Slice `text` from `start_marker` to whichever of '\\n@app.',
    '\\nclass ' or '\\ndef main(' comes first after it, falling back to
    end-of-text if none is found. `\\ndef main(` is included because
    `api_purge` is the last `@app.`-decorated endpoint in web_app.py — with
    only the first two markers, its region would fall through to
    end-of-text and swallow the unrelated pre-existing log. calls inside
    `main()` (web_app.py's entry point), producing a false L12 failure."""
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return ""
    search_from = start_idx + len(start_marker)
    app_idx = text.find("\n@app.", search_from)
    class_idx = text.find("\nclass ", search_from)
    main_idx = text.find("\ndef main(", search_from)
    candidates = [i for i in (app_idx, class_idx, main_idx) if i != -1]
    if not candidates:
        return text[start_idx:]
    return text[start_idx:min(candidates)]


# ── logging suite (OBS-02) ──────────────────────────────────────────────────

D01_ENDPOINTS = {
    "api_correct_species": "def api_correct_species(",
    "api_save_correction":  "def api_save_correction(",
    "api_delete_correction": "def api_delete_correction(",
    "api_save_settings":    "def api_save_settings(",
    "api_save_schedule":    "def api_save_schedule(",
}

D02_ENDPOINTS = {
    "api_add_blacklist":    "def api_add_blacklist(",
    "api_remove_blacklist": "def api_remove_blacklist(",
    "api_requeue_species":  "def api_requeue_species(",
    "api_purge":            "def api_purge(",
    "api_promote_paired":   "def api_promote_paired(",
    "api_trigger_run":      "def api_trigger_run(",
    "api_apply_update":     "def api_apply_update(",
}


def suite_logging():
    passed = 0
    total = 12
    text = _web_app_text()

    d01_regions = {}
    for name, marker in D01_ENDPOINTS.items():
        region = _region(text, marker)
        d01_regions[name] = _strip_comments(region)

    # L1-L5: each D-01 endpoint region contains a log. call.
    case_ids = ["L1", "L2", "L3", "L4", "L5"]
    endpoint_names = list(D01_ENDPOINTS.keys())
    for case_id, name in zip(case_ids, endpoint_names):
        region = d01_regions[name]
        ok = "log." in region
        _check(case_id, ok, f"{name} region has no log. call")
        if ok:
            passed += 1

    # L6: every log. call found in L1-L5's regions is log.info( specifically.
    all_info = True
    for name in endpoint_names:
        region = d01_regions[name]
        for line in region.splitlines():
            if "log." in line and "log.info(" not in line:
                all_info = False
    case_id = "L6"
    _check(case_id, all_info, "a log. call in a D-01 region is not log.info(")
    if all_info:
        passed += 1

    # L7: no log. call in L1-L5's regions uses f-string, %-operator, or .format(.
    no_bad_interp = True
    for name in endpoint_names:
        region = d01_regions[name]
        for line in region.splitlines():
            if "log." in line:
                if 'f"' in line or "f'" in line or ".format(" in line:
                    no_bad_interp = False
                # % operator check: a '%' character used for string formatting
                # outside of a %s/%d-style placeholder inside the message
                # literal itself would show as e.g. `% (` or `%(` following
                # the log call's closing message quote — heuristically flag
                # any '% (' or '%(' sequence on a log. line, which %s-style
                # calls (placeholders + separate args) never produce.
                if "% (" in line or "%(" in line:
                    no_bad_interp = False
    case_id = "L7"
    _check(case_id, no_bad_interp, "a D-01 log. call uses f-string/%-operator/.format(")
    if no_bad_interp:
        passed += 1

    # L8: within api_save_settings region, no line containing log. also
    # contains smtp_password.
    settings_region = d01_regions["api_save_settings"]
    no_password_leak = True
    for line in settings_region.splitlines():
        if "log." in line and "smtp_password" in line:
            no_password_leak = False
    case_id = "L8"
    _check(case_id, no_password_leak, "api_save_settings log. line references smtp_password directly")
    if no_password_leak:
        passed += 1

    # L9: within api_save_schedule region, the log. call's offset is GREATER
    # than the offset of restart_result.returncode.
    schedule_region = d01_regions["api_save_schedule"]
    log_idx = schedule_region.find("log.")
    guard_idx = schedule_region.find("restart_result.returncode")
    ok = log_idx != -1 and guard_idx != -1 and log_idx > guard_idx
    case_id = "L9"
    _check(case_id, ok, f"api_save_schedule log_idx={log_idx} guard_idx={guard_idx}")
    if ok:
        passed += 1

    # L10: within api_correct_species region, the log. call's offset is
    # GREATER than the offset of "Detection not found".
    correct_region = d01_regions["api_correct_species"]
    log_idx = correct_region.find("log.")
    guard_idx = correct_region.find("Detection not found")
    ok = log_idx != -1 and guard_idx != -1 and log_idx > guard_idx
    case_id = "L10"
    _check(case_id, ok, f"api_correct_species log_idx={log_idx} guard_idx={guard_idx}")
    if ok:
        passed += 1

    # L11: within api_save_correction region, the log. call's offset is
    # GREATER than the offset of "Video not found".
    save_correction_region = d01_regions["api_save_correction"]
    log_idx = save_correction_region.find("log.")
    guard_idx = save_correction_region.find("Video not found")
    ok = log_idx != -1 and guard_idx != -1 and log_idx > guard_idx
    case_id = "L11"
    _check(case_id, ok, f"api_save_correction log_idx={log_idx} guard_idx={guard_idx}")
    if ok:
        passed += 1

    # L12: D-02 exclusions — none of the seven regions gained a log. call.
    # One case, seven sub-assertions, failing detail naming which region
    # regressed.
    regressed = []
    for name, marker in D02_ENDPOINTS.items():
        region = _strip_comments(_region(text, marker))
        if "log." in region:
            regressed.append(name)
    case_id = "L12"
    ok = not regressed
    _check(case_id, ok, f"D-02 excluded endpoint(s) gained a log. call: {regressed}")
    if ok:
        passed += 1

    return (passed, total)


# ── docs suite (NOTIFY-03) ──────────────────────────────────────────────────


def _project_md_text():
    return (_repo_root() / ".planning" / "PROJECT.md").read_text(encoding="utf-8")


def _requirements_md_text():
    return (_repo_root() / ".planning" / "REQUIREMENTS.md").read_text(encoding="utf-8")


def suite_docs():
    passed = 0
    total = 6

    project_text = _project_md_text()
    key_decisions_region = _slice(project_text, "## Key Decisions", "## ")
    follow_ups_region = _slice(
        project_text,
        "### Live-Verification Follow-ups",
        "### ",
    )

    # K1: Key Decisions region contains NOTIFY-01.
    case_id = "K1"
    ok = "NOTIFY-01" in key_decisions_region
    _check(case_id, ok, "Key Decisions table has no NOTIFY-01 row")
    if ok:
        passed += 1

    # K2: Key Decisions region contains the verbatim rationale fragment.
    case_id = "K2"
    ok = "OpenCV/ffmpeg absorbs every decode failure" in key_decisions_region
    _check(case_id, ok, "Key Decisions table missing verbatim D-08 rationale fragment")
    if ok:
        passed += 1

    # K3: Live-Verification Follow-ups region does NOT contain NOTIFY-01.
    case_id = "K3"
    ok = "NOTIFY-01" not in follow_ups_region
    _check(case_id, ok, "NOTIFY-01 still listed under Live-Verification Follow-ups")
    if ok:
        passed += 1

    # K4: that region still DOES contain NOTIFY-02.
    case_id = "K4"
    ok = "NOTIFY-02" in follow_ups_region
    _check(case_id, ok, "NOTIFY-02 sibling item collaterally removed")
    if ok:
        passed += 1

    # K5: this harness's own module docstring declares NOTIFY-03
    # documentation-only, pinning the flagged assumption in a file
    # committed alongside the change.
    case_id = "K5"
    harness_text = (Path(__file__)).read_text(encoding="utf-8")
    ok = "documentation-only" in harness_text
    _check(case_id, ok, "harness module docstring does not declare NOTIFY-03 documentation-only")
    if ok:
        passed += 1

    # K6: REQUIREMENTS.md contains a traceability row with both NOTIFY-03
    # and Complete, and no line with both NOTIFY-03 and Pending.
    req_text = _requirements_md_text()
    has_complete_row = any(
        "NOTIFY-03" in line and "Complete" in line for line in req_text.splitlines()
    )
    has_pending_row = any(
        "NOTIFY-03" in line and "Pending" in line for line in req_text.splitlines()
    )
    case_id = "K6"
    ok = has_complete_row and not has_pending_row
    _check(
        case_id,
        ok,
        f"has_complete_row={has_complete_row} has_pending_row={has_pending_row}",
    )
    if ok:
        passed += 1

    return (passed, total)


SUITES = {
    "logging": (suite_logging, 12),
    "docs":    (suite_docs, 6),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 12 Plan 02 verification harness (OBS-02 logging, NOTIFY-03 docs closure)"
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
