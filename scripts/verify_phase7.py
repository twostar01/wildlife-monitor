"""
verify_phase7.py — stdlib-only verification harness for Phase 7 (Frontend &
Logging Quick Fixes): UI-01 through UI-04, plus OBS-01.

Suites:
    nav               — UI-01, mobile nav bar wrap (static/index.html CSS,
                         N1-N5).
    nextrun           — UI-02, "Next scheduled run" card overflow fix
                         (static/index.html inline styles, X1-X5).
    species_filter    — UI-03, species/camera dropdown tab-coupling fix
                         (static/index.html JS, S1-S6).
    confidence_badge  — UI-04, gallery tile confidence badge
                         (database.py + static/index.html, C1-C8).
    logging           — OBS-01, web_app.py journald logging
                         (web_app.py source + runtime behaviour, L1-L8).

Follows scripts/verify_raw_cleanup_ui.py's structure: a `_check(case_id,
condition, detail)` helper, per-suite `(passed, total)` returns, a dict suite
registry, argparse `--suite` with an `all` choice, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

Written RED on purpose — every assertion targets the post-fix state, so
running this harness before plans 07-02 through 07-04 land reports FAIL for
every not-yet-implemented case. That is the intended and required outcome.

Usage:
    python scripts/verify_phase7.py --suite nav|nextrun|species_filter|confidence_badge|logging|all
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


def _meta_windows(text):
    """Return, for every occurrence of `<div class="gallery-item-meta">`, the
    substring from 400 characters before the match to 700 characters after
    it, clamped to the text bounds."""
    windows = []
    needle = '<div class="gallery-item-meta">'
    search_from = 0
    while True:
        idx = text.find(needle, search_from)
        if idx == -1:
            break
        start = max(0, idx - 400)
        end = min(len(text), idx + len(needle) + 700)
        windows.append(text[start:end])
        search_from = idx + 1
    return windows


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


def suite_species_filter():
    """Six cases proving the species/camera dropdown tab-coupling fix (UI-03)
    is present without regressing the existing tab-open fallback or the
    camera dropdown (S1-S6)."""
    passed = 0
    total = 6
    text = _index_html_text()

    dash = _slice(text, "async function loadDashboard()", "\nasync function ")
    species_fn = _slice(text, "async function loadSpecies()", "\nasync function ")
    cameras_fn = _slice(text, "async function loadCameras()", "\nasync function ")
    boot = _slice(text, "// ── Boot ─", None)

    case_id = "species_filter/S1-boot-populate"
    ok = "populateSpeciesFilters(" in dash
    _check(case_id, ok, "loadDashboard() does not call populateSpeciesFilters(")
    if ok:
        passed += 1

    case_id = "species_filter/S2-guarded"
    ok = dash.count("state.speciesList.length") >= 2
    _check(case_id, ok, f"count={dash.count('state.speciesList.length')}")
    if ok:
        passed += 1

    case_id = "species_filter/S3-no-boot-loadSpecies"
    ok = "loadDashboard();" in boot and "loadCameras();" in boot and "loadSpecies(" not in boot
    _check(case_id, ok, f"has_loadDashboard={'loadDashboard();' in boot}, has_loadCameras={'loadCameras();' in boot}, has_loadSpecies={'loadSpecies(' in boot}")
    if ok:
        passed += 1

    case_id = "species_filter/S4-fallback-intact"
    fallback_marker = "if (tab === 'species') loadSpecies();"
    species_fn_ok = "populateSpeciesFilters(data)" in species_fn
    fallback_wired = fallback_marker in text
    ok = species_fn_ok and fallback_wired
    _check(case_id, ok, f"species_fn_ok={species_fn_ok}, fallback_wired={fallback_wired}")
    if ok:
        passed += 1

    case_id = "species_filter/S5-selection-preserved"
    ok = "state.gallerySpecies" in dash and "gallerySpeciesFilter" in dash
    _check(case_id, ok, f"has_gallerySpecies={'state.gallerySpecies' in dash}, has_filter_id={'gallerySpeciesFilter' in dash}")
    if ok:
        passed += 1

    case_id = "species_filter/S6-camera-unchanged"
    ok = "gallerySel.value = state.galleryCamera" in cameras_fn and "loadCameras();" in boot
    _check(case_id, ok, f"cameras_fn_ok={'gallerySel.value = state.galleryCamera' in cameras_fn}, boot_ok={'loadCameras();' in boot}")
    if ok:
        passed += 1

    return (passed, total)


def suite_confidence_badge():
    """Eight cases proving the gallery tile confidence badge (UI-04) is wired
    at both render sites, backed by a SQL column addition, and that the
    verification fixture itself never touches the production database
    (C1-C8)."""
    passed = 0
    total = 8
    index_text = _index_html_text()

    case_id = "confidence_badge/C1-sql-column"
    detail_fn = _nows(_slice(_database_text(), "def get_species_detail(", "def get_gallery("))
    ok = "s.confidenceASspecies_confidence" in detail_fn
    _check(case_id, ok, "get_species_detail() crops query is missing s.confidence AS species_confidence")
    if ok:
        passed += 1

    # C2/C3: shared fixture — a temp SQLite database with one video, two
    # detections, two species rows (confidence 0.0 and 0.87) on the same
    # label, and one crop per detection.
    import database  # noqa: E402  (deferred import — only this suite needs it)

    c2_ok = False
    c3_ok = False
    c2_detail = ""
    c3_detail = ""
    original_db_path = database.DB_PATH
    tmpdir = tempfile.mkdtemp()
    try:
        tmp_db_path = os.path.join(tmpdir, "t.db")
        assert tmp_db_path != original_db_path, "temp db path collides with production DB_PATH"
        database.init_db(tmp_db_path)
        label = "phase7_probe_species"
        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO videos (filename, processed_at) VALUES (?, ?)",
                ("probe.mp4", "2026-08-06T00:00:00"),
            )
            video_id = conn.execute("SELECT id FROM videos WHERE filename = ?", ("probe.mp4",)).fetchone()[0]

            det_ids = []
            for _ in range(2):
                conn.execute(
                    "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                    (video_id,),
                )
                det_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

            confidences = (0.0, 0.87)
            for det_id, conf in zip(det_ids, confidences):
                conn.execute(
                    "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                    "VALUES (?, ?, 'Probe Animal', 'Probus animalus', ?)",
                    (det_id, label, conf),
                )

            for i, det_id in enumerate(det_ids):
                conn.execute(
                    "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) VALUES (?, ?, ?, ?)",
                    (det_id, f"probe_crop_{i}.jpg", 50.0 + i, "2026-08-06T00:00:00"),
                )

        detail = database.get_species_detail(label)
        crops = detail.get("crops", [])
        crop_confidences = [c.get("species_confidence") for c in crops if "species_confidence" in c]

        c2_ok = len(crop_confidences) == len(crops) and len(crops) > 0 and 0.0 in crop_confidences
        c2_detail = f"crops={crops}"
        c3_ok = 0.87 in crop_confidences
        c3_detail = f"crop_confidences={crop_confidences}"
    finally:
        database.set_db_path(original_db_path)
        try:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    case_id = "confidence_badge/C2-roundtrip-zero"
    _check(case_id, c2_ok, c2_detail)
    if c2_ok:
        passed += 1

    case_id = "confidence_badge/C3-roundtrip-value"
    _check(case_id, c3_ok, c3_detail)
    if c3_ok:
        passed += 1

    case_id = "confidence_badge/C4-two-render-sites"
    windows = _meta_windows(index_text)
    ok = len(windows) == 2
    _check(case_id, ok, f"found {len(windows)} gallery-item-meta occurrence(s)")
    if ok:
        passed += 1

    case_id = "confidence_badge/C5-helper-contract"
    helper = _slice(index_text, "function confidenceBadge(", "\nfunction ")
    ok = all(
        v in helper
        for v in (
            "!== null",
            "!== undefined",
            "Math.round(",
            "100",
            "< 70",
            "var(--danger)",
            "font-weight:700",
            "quality-score",
            "%",
        )
    )
    _check(case_id, ok, f"helper_found={bool(helper)}")
    if ok:
        passed += 1

    case_id = "confidence_badge/C6-main-grid-wired"
    main_grid_window = next((w for w in windows if "item.quality_score" in w), "")
    ok = "confidenceBadge(item.species_confidence)" in main_grid_window and 'class="badge-pair"' in main_grid_window
    _check(case_id, ok, f"main_grid_window_found={bool(main_grid_window)}")
    if ok:
        passed += 1

    case_id = "confidence_badge/C7-modal-grid-wired"
    modal_grid_window = next((w for w in windows if "c.quality_score" in w), "")
    ok = "confidenceBadge(c.species_confidence)" in modal_grid_window and 'class="badge-pair"' in modal_grid_window
    _check(case_id, ok, f"modal_grid_window_found={bool(modal_grid_window)}")
    if ok:
        passed += 1

    case_id = "confidence_badge/C8-badge-pair-css"
    badge_pair_rule = _rule(_nows(index_text), ".badge-pair")
    ok = all(v in badge_pair_rule for v in ("display:flex", "align-items:center", "gap:4px"))
    _check(case_id, ok, f"badge_pair_rule={badge_pair_rule!r}")
    if ok:
        passed += 1

    return (passed, total)


def suite_logging():
    """Eight cases proving web_app.py's journald logging (OBS-01) is wired up
    correctly: handler shape, no file handler, idempotent setup, no
    propagation, exactly-once emission with the expected format, no
    remaining print() calls, correct setup ordering, and no secret leakage
    (L1-L8)."""
    passed = 0
    total = 8

    import web_app  # noqa: E402  (deferred import — only this suite needs it)

    text = _web_app_text()
    main_src = _slice(text, "def main():", None)

    case_id = "logging/L1-module-logger"
    log_attr = getattr(web_app, "log", None)
    setup_fn = getattr(web_app, "setup_logging", None)
    ok = (
        log_attr is not None
        and getattr(log_attr, "name", None) == "web_app"
        and callable(setup_fn)
    )
    _check(case_id, ok, f"has_log={log_attr is not None}, has_setup_logging={callable(setup_fn)}")
    if ok:
        passed += 1

    case_id = "logging/L2-handler-shape"
    l2_ok = False
    l2_detail = ""
    if callable(setup_fn):
        setup_fn()
        handlers = list(web_app.log.handlers)
        l2_ok = (
            web_app.log.level == logging.INFO
            and len(handlers) == 1
            and isinstance(handlers[0], logging.StreamHandler)
            and handlers[0].stream is sys.stdout
        )
        l2_detail = f"level={web_app.log.level}, handler_count={len(handlers)}"
    _check(case_id, l2_ok, l2_detail)
    if l2_ok:
        passed += 1

    case_id = "logging/L3-no-file-handler"
    l3_ok = False
    l3_detail = ""
    if callable(setup_fn):
        no_file_handler_instance = not any(
            isinstance(h, logging.FileHandler) for h in web_app.log.handlers
        )
        no_file_handler_text = "FileHandler" not in text
        l3_ok = no_file_handler_instance and no_file_handler_text
        l3_detail = f"no_instance={no_file_handler_instance}, no_text={no_file_handler_text}"
    _check(case_id, l3_ok, l3_detail)
    if l3_ok:
        passed += 1

    case_id = "logging/L4-idempotent"
    l4_ok = False
    l4_detail = ""
    if callable(setup_fn):
        setup_fn()
        setup_fn()
        l4_ok = len(web_app.log.handlers) == 1
        l4_detail = f"handler_count={len(web_app.log.handlers)}"
    _check(case_id, l4_ok, l4_detail)
    if l4_ok:
        passed += 1

    case_id = "logging/L5-no-propagate"
    l5_ok = False
    if callable(setup_fn):
        l5_ok = web_app.log.propagate is False
    _check(case_id, l5_ok, f"propagate={getattr(web_app.log, 'propagate', None) if log_attr else 'n/a'}")
    if l5_ok:
        passed += 1

    case_id = "logging/L6-exactly-once"
    l6_ok = False
    l6_detail = ""
    if callable(setup_fn):
        marker = "phase7-probe"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup_fn()
            web_app.log.info(marker)
        setup_fn()  # rebind to the real sys.stdout (T-07-H2)
        output = buf.getvalue()
        occurrences = output.count(marker)
        lines = [ln for ln in output.splitlines() if marker in ln]
        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+INFO\s+" + re.escape(marker) + r"$"
        )
        format_ok = len(lines) == 1 and bool(pattern.match(lines[0]))
        l6_ok = occurrences == 1 and format_ok
        l6_detail = f"occurrences={occurrences}, output={output!r}"
    _check(case_id, l6_ok, l6_detail)
    if l6_ok:
        passed += 1

    case_id = "logging/L7-no-print"
    print_count = text.count("print(")
    ok = print_count == 0
    _check(case_id, ok, f"found {print_count} occurrence(s) of print(")
    if ok:
        passed += 1

    case_id = "logging/L8-setup-before-serve-and-no-secrets"
    setup_idx = main_src.find("setup_logging()")
    uvicorn_idx = main_src.find("uvicorn.run(")
    order_ok = setup_idx != -1 and uvicorn_idx != -1 and setup_idx < uvicorn_idx
    log_call_count = main_src.count("log.info(") + main_src.count("log.warning(")
    enough_calls = log_call_count >= 8
    no_secrets = True
    for line in text.splitlines():
        if "log." in line and ("password" in line.lower() or "smtp" in line.lower()):
            no_secrets = False
            break
    ok = order_ok and enough_calls and no_secrets
    _check(
        case_id, ok,
        f"order_ok={order_ok}, log_call_count={log_call_count}, no_secrets={no_secrets}",
    )
    if ok:
        passed += 1

    # Leave the module in a known-good state (real stdout) regardless of
    # which cases above ran.
    if callable(setup_fn):
        setup_fn()

    return (passed, total)


SUITES = {
    "nav": (suite_nav, 5),
    "nextrun": (suite_nextrun, 5),
    "species_filter": (suite_species_filter, 6),
    "confidence_badge": (suite_confidence_badge, 8),
    "logging": (suite_logging, 8),
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
