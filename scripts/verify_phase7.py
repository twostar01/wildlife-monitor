"""
verify_phase7.py — stdlib-only verification harness for Phase 7 (Frontend &
Logging Quick Fixes): UI-01 through UI-04, plus OBS-01.

Suites:
    nav               — UI-01, mobile nav bar wrap (static/index.html CSS,
                         N1-N5).
    nextrun           — UI-02, "Next scheduled run" card overflow fix
                         (static/index.html inline styles, X1-X5).

Follows scripts/verify_raw_cleanup_ui.py's structure: a `_check(case_id,
condition, detail)` helper, per-suite `(passed, total)` returns, a dict suite
registry, argparse `--suite` with an `all` choice, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

Written RED on purpose — every assertion targets the post-fix state, so
running this harness before plans 07-02 through 07-04 land reports FAIL for
every not-yet-implemented case. That is the intended and required outcome.

Usage:
    python scripts/verify_phase7.py --suite nav|nextrun|all
    python scripts/verify_phase7.py --list
"""

import argparse
import contextlib
import io
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _index_html_text():
    return (_repo_root() / "static" / "index.html").read_text(encoding="utf-8")


def _web_app_text():
    return (_repo_root() / "web_app.py").read_text(encoding="utf-8")


def _database_text():
    return (_repo_root() / "database.py").read_text(encoding="utf-8")


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


def _tag_attrs(text, elem_id):
    """Locate id="{elem_id}", walk backwards to the nearest '<' and forwards
    to the next '>', returning that opening-tag substring. Returns "" when
    the id is absent."""
    needle = f'id="{elem_id}"'
    idx = text.find(needle)
    if idx == -1:
        return ""
    tag_start = text.rfind("<", 0, idx)
    tag_end = text.find(">", idx)
    if tag_start == -1 or tag_end == -1:
        return ""
    return text[tag_start:tag_end]


def suite_nav():
    """Five cases proving the mobile nav wrap fix (UI-01) is present and does
    not hide any of the 8 nav tabs (N1-N5)."""
    passed = 0
    total = 5
    text = _index_html_text()

    block_raw = _slice(text, "UI-01: MOBILE NAV WRAP", "/* ──")
    block = _nows(block_raw)

    case_id = "nav/N1-marker-block"
    ok = bool(block_raw) and "@media" in block and "max-width:700px" in block
    _check(case_id, ok, f"marker found={bool(block_raw)}")
    if ok:
        passed += 1

    case_id = "nav/N2-header-wrap"
    header_rule = _rule(block, "header")
    ok = "flex-wrap:wrap" in header_rule and "row-gap:8px" in header_rule
    _check(case_id, ok, f"header_rule={header_rule!r}")
    if ok:
        passed += 1

    case_id = "nav/N3-nav-wrap"
    nav_rule = _rule(block, "nav")
    ok = all(
        v in nav_rule
        for v in ("flex-wrap:wrap", "width:100%", "justify-content:flex-start", "row-gap:4px")
    )
    _check(case_id, ok, f"nav_rule={nav_rule!r}")
    if ok:
        passed += 1

    case_id = "nav/N4-searchbar-order"
    search_rule = _rule(block, ".search-bar")
    ok = "order:3" in search_rule and "width:100%" in search_rule
    _check(case_id, ok, f"search_rule={search_rule!r}")
    if ok:
        passed += 1

    case_id = "nav/N5-no-tab-hiding"
    hiding_declarations = ("display:none", "overflow:hidden", "visibility:hidden")
    no_hiding = not any(d in block for d in hiding_declarations)
    nav_elem = _slice(text, "<nav>", "</nav>")
    button_count = nav_elem.count("<button")
    ok = no_hiding and button_count == 8
    _check(case_id, ok, f"no_hiding={no_hiding}, button_count={button_count}")
    if ok:
        passed += 1

    return (passed, total)


def suite_nextrun():
    """Five cases proving the "Next scheduled run" card overflow fix (UI-02)
    is present and does not truncate its text (X1-X5)."""
    passed = 0
    total = 5
    text = _index_html_text()

    row = _nows(_tag_attrs(text, "nextRunRow"))
    val = _nows(_tag_attrs(text, "nextRunValue"))

    case_id = "nextrun/X1-column"
    ok = "flex-direction:column" in row
    _check(case_id, ok, f"row={row!r}")
    if ok:
        passed += 1

    case_id = "nextrun/X2-no-space-between"
    ok = "align-items:flex-start" in row and "justify-content:space-between" not in row
    _check(case_id, ok, f"row={row!r}")
    if ok:
        passed += 1

    case_id = "nextrun/X3-value-wraps"
    ok = "white-space:normal" in val and "white-space:nowrap" not in val
    _check(case_id, ok, f"val={val!r}")
    if ok:
        passed += 1

    case_id = "nextrun/X4-spacing-preserved"
    ok = all(v in row for v in ("gap:4px", "margin-top:10px", "padding-top:10px"))
    _check(case_id, ok, f"row={row!r}")
    if ok:
        passed += 1

    case_id = "nextrun/X5-no-truncation"
    ok = not any(
        d in row or d in val
        for d in ("text-overflow", "overflow:hidden", "-webkit-line-clamp")
    )
    _check(case_id, ok, f"row={row!r}, val={val!r}")
    if ok:
        passed += 1

    return (passed, total)


SUITES = {
    "nav": (suite_nav, 5),
    "nextrun": (suite_nextrun, 5),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 7 verification harness (UI-01, UI-02, UI-03, UI-04, OBS-01)"
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
