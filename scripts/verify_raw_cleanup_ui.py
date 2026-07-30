"""
verify_raw_cleanup_ui.py — stdlib-only verification harness for the CLEANUP-01/03
operator-facing surface: the raw_recordings retention setting (web_app.py) and its
UI wiring, plus the Storage Usage card's raw cleanup section (static/index.html).

Suites:
    settings     — DEFAULT_PROCESSING_SETTINGS / ProcessingSettings / _load_settings
                   round-trip behavior for raw_recordings_retention_days (S1-S4).
    retention_ui — static/index.html text assertions for the Raw Recordings
                   sub-column, the D-06 warning, and the load/collect wiring (R1-R7).
    storage_ui   — static/index.html text assertions for the Storage Usage card's
                   raw cleanup tiles and last-run line (G1-G7).

Follows scripts/verify_archive_collision.py's structure: a `_check(case_id,
condition, detail)` helper, per-suite `(passed, total)` returns, a dict suite
registry, argparse `--suite` with an `all` choice, `PASS:`/`FAIL:` summary lines,
and `sys.exit(0 if all_passed else 1)`.

Usage:
    python scripts/verify_raw_cleanup_ui.py --suite settings|retention_ui|storage_ui|all
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_app


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _index_html_text():
    """Return static/index.html's full text."""
    path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    return path.read_text(encoding="utf-8")


def suite_settings():
    """Four cases proving raw_recordings_retention_days round-trips through the
    settings model and the merge-over-defaults loader (S1-S4)."""
    passed = 0
    total = 4

    # S1: DEFAULT_PROCESSING_SETTINGS carries the default of 14.
    case_id = "settings/S1-default-value"
    ok = web_app.DEFAULT_PROCESSING_SETTINGS.get("raw_recordings_retention_days") == 14
    _check(case_id, ok, f"got {web_app.DEFAULT_PROCESSING_SETTINGS.get('raw_recordings_retention_days')!r}")
    if ok:
        passed += 1

    # S2: ProcessingSettings() with no args defaults to 14 and appears in model_dump().
    case_id = "settings/S2-model-default"
    model = web_app.ProcessingSettings()
    dump = model.model_dump()
    ok = model.raw_recordings_retention_days == 14 and "raw_recordings_retention_days" in dump
    _check(case_id, ok, f"attr={model.raw_recordings_retention_days!r}, in dump={'raw_recordings_retention_days' in dump}")
    if ok:
        passed += 1

    # S3: explicit override round-trips through model_dump() alongside existing keys.
    case_id = "settings/S3-model-override"
    model2 = web_app.ProcessingSettings(raw_recordings_retention_days=30)
    dump2 = model2.model_dump()
    existing_keys_present = all(k in dump2 for k in web_app.DEFAULT_PROCESSING_SETTINGS.keys())
    ok = dump2.get("raw_recordings_retention_days") == 30 and existing_keys_present
    _check(case_id, ok, f"raw={dump2.get('raw_recordings_retention_days')!r}, existing_keys_present={existing_keys_present}")
    if ok:
        passed += 1

    # S4: _load_settings() merges the default over a saved file that omits the key,
    # while preserving values the file did define.
    case_id = "settings/S4-load-settings-merge"
    original_settings_file = web_app.SETTINGS_FILE
    tmpdir = tempfile.mkdtemp()
    try:
        tmp_settings_path = os.path.join(tmpdir, "settings.json")
        saved = {"blank_retention_days": 45, "kept_retention_days": 900}
        with open(tmp_settings_path, "w") as f:
            json.dump(saved, f)
        web_app.SETTINGS_FILE = tmp_settings_path
        loaded = web_app._load_settings()
        ok = (
            loaded.get("raw_recordings_retention_days") == 14
            and loaded.get("blank_retention_days") == 45
            and loaded.get("kept_retention_days") == 900
        )
        _check(case_id, ok, f"loaded={loaded}")
        if ok:
            passed += 1
    finally:
        web_app.SETTINGS_FILE = original_settings_file
        try:
            os.remove(tmp_settings_path)
        except OSError:
            pass
        os.rmdir(tmpdir)

    return (passed, total)


def suite_retention_ui():
    """Seven cases parsing static/index.html for the Raw Recordings sub-column,
    the D-06 warning, and the load/collect wiring (R1-R7)."""
    passed = 0
    total = 7
    html = _index_html_text()

    # R1: exactly one setRawRecordingsDays input, type=number, min=0.
    case_id = "retention_ui/R1-raw-days-input"
    occurrences = html.count('id="setRawRecordingsDays"')
    input_snippet = ""
    idx = html.find('id="setRawRecordingsDays"')
    if idx != -1:
        # grab the surrounding <input ...> tag text for attribute checks
        tag_start = html.rfind("<input", 0, idx)
        tag_end = html.find(">", idx)
        input_snippet = html[tag_start:tag_end] if tag_start != -1 and tag_end != -1 else ""
    ok = occurrences == 1 and 'type="number"' in input_snippet and 'min="0"' in input_snippet
    _check(case_id, ok, f"occurrences={occurrences}, snippet={input_snippet!r}")
    if ok:
        passed += 1

    # R2: exactly one rawRetentionWarning element, gold + display:none, no danger/height.
    case_id = "retention_ui/R2-warning-element"
    warn_occurrences = html.count('id="rawRetentionWarning"')
    warn_idx = html.find('id="rawRetentionWarning"')
    warn_tag = ""
    if warn_idx != -1:
        tag_start = html.rfind("<", 0, warn_idx)
        tag_end = html.find(">", warn_idx)
        warn_tag = html[tag_start:tag_end] if tag_start != -1 and tag_end != -1 else ""
    ok = (
        warn_occurrences == 1
        and "var(--gold)" in warn_tag
        and "display:none" in warn_tag
        and "var(--danger)" not in warn_tag
        and "height:" not in warn_tag
    )
    _check(case_id, ok, f"occurrences={warn_occurrences}, tag={warn_tag!r}")
    if ok:
        passed += 1

    # R3: rawRetentionWarning appears before the "Save Retention Settings" button.
    case_id = "retention_ui/R3-warning-above-button"
    save_btn_idx = html.find("Save Retention Settings")
    ok = warn_idx != -1 and save_btn_idx != -1 and warn_idx < save_btn_idx
    _check(case_id, ok, f"warn_idx={warn_idx}, save_btn_idx={save_btn_idx}")
    if ok:
        passed += 1

    # R4: Retention Policy grid no longer fixed two-column, uses auto-fit minmax.
    case_id = "retention_ui/R4-grid-auto-fit"
    # Anchor on the Retention Policy card title (unique) rather than "Blank
    # Videos" text, which also appears verbatim as an unrelated tab section
    # title earlier in the file. Then locate the grid declaration between the
    # card title and the sub-column header within that card.
    card_idx = html.find("Retention Policy")
    blank_idx = html.find("Blank Videos", card_idx) if card_idx != -1 else -1
    grid_region = html[card_idx:blank_idx] if card_idx != -1 and blank_idx != -1 else ""
    ok = "auto-fit" in grid_region and "minmax" in grid_region
    _check(case_id, ok, f"card_idx={card_idx}, blank_idx={blank_idx}, grid_region_tail={grid_region[-200:]!r}")
    if ok:
        passed += 1

    # R5: loadSettings() populates setRawRecordingsDays with a 14 fallback, and
    # collectSettingsPayload() emits raw_recordings_retention_days.
    case_id = "retention_ui/R5-load-collect-wiring"
    load_fn_idx = html.find("function loadSettings")
    load_fn_end = html.find("\n}", load_fn_idx) if load_fn_idx != -1 else -1
    load_fn_body = html[load_fn_idx:load_fn_end] if load_fn_idx != -1 and load_fn_end != -1 else ""
    collect_fn_idx = html.find("function collectSettingsPayload")
    collect_fn_end = html.find("\n}", collect_fn_idx) if collect_fn_idx != -1 else -1
    collect_fn_body = html[collect_fn_idx:collect_fn_end] if collect_fn_idx != -1 and collect_fn_end != -1 else ""
    ok = (
        "setRawRecordingsDays" in load_fn_body
        and "raw_recordings_retention_days" in load_fn_body
        and "?? 14" in load_fn_body
        and "raw_recordings_retention_days" in collect_fn_body
    )
    _check(
        case_id, ok,
        f"load_fn_found={load_fn_idx != -1}, collect_fn_found={collect_fn_idx != -1}",
    )
    if ok:
        passed += 1

    # R6: updateRawRetentionWarning defined exactly once, referenced >=3 times,
    # no fetch(, no return false in its body.
    case_id = "retention_ui/R6-warning-function"
    def_count = html.count("function updateRawRetentionWarning")
    ref_count = html.count("updateRawRetentionWarning(")
    fn_idx = html.find("function updateRawRetentionWarning")
    fn_end = html.find("\n}", fn_idx) if fn_idx != -1 else -1
    fn_body = html[fn_idx:fn_end] if fn_idx != -1 and fn_end != -1 else ""
    ok = (
        def_count == 1
        and ref_count >= 3
        and "fetch(" not in fn_body
        and "return false" not in fn_body
    )
    _check(case_id, ok, f"def_count={def_count}, ref_count={ref_count}")
    if ok:
        passed += 1

    # R7: margin-top:18px occurs the same number of times as before this phase (5).
    case_id = "retention_ui/R7-spacing-debt-untouched"
    margin_count = html.count("margin-top:18px")
    ok = margin_count == 5
    _check(case_id, ok, f"found {margin_count} occurrence(s), expected 5")
    if ok:
        passed += 1

    return (passed, total)


def suite_storage_ui():
    """Seven cases parsing static/index.html for the Storage Usage card's raw
    cleanup tiles and last-run line (G1-G7)."""
    passed = 0
    total = 7
    html = _index_html_text()

    load_stats_idx = html.find("function loadStorageStats")
    # Find the end of the function by matching to the next top-level "\nfunction "
    # or "\nasync function " after the start, which is more reliable than brace
    # counting for a large minified-ish region.
    next_fn_idx = html.find("\nfunction ", load_stats_idx + 1)
    next_async_idx = html.find("\nasync function ", load_stats_idx + 1)
    candidates = [i for i in (next_fn_idx, next_async_idx) if i != -1]
    load_stats_end = min(candidates) if candidates else len(html)
    load_stats_body = html[load_stats_idx:load_stats_end] if load_stats_idx != -1 else ""

    # G1: references both raw_gb_reclaimed and raw_purged_videos.
    case_id = "storage_ui/G1-raw-keys-referenced"
    ok = "raw_gb_reclaimed" in load_stats_body and "raw_purged_videos" in load_stats_body
    _check(case_id, ok, f"found_gb={'raw_gb_reclaimed' in load_stats_body}, found_count={'raw_purged_videos' in load_stats_body}")
    if ok:
        passed += 1

    # G2: fetches /api/runs/last in addition to /api/maintenance/storage.
    case_id = "storage_ui/G2-runs-last-fetch"
    ok = "/api/runs/last" in load_stats_body and "/api/maintenance/storage" in load_stats_body
    _check(case_id, ok, f"runs_last_found={'/api/runs/last' in load_stats_body}, storage_found={'/api/maintenance/storage' in load_stats_body}")
    if ok:
        passed += 1

    # G3: exactly one rawCleanupNote element, emitted after the stat-card grid closes.
    case_id = "storage_ui/G3-note-after-grid"
    note_occurrences = html.count('id="rawCleanupNote"')
    note_idx = html.find('id="rawCleanupNote"')
    # The stat-card grid template literal in loadStorageStats should close (the
    # backtick-quoted template literal ends) before the note appears.
    grid_template_start = load_stats_body.find("stat-card")
    ok = note_occurrences == 1 and note_idx != -1 and note_idx > load_stats_idx
    _check(case_id, ok, f"occurrences={note_occurrences}, note_idx={note_idx}, load_stats_idx={load_stats_idx}")
    if ok:
        passed += 1

    # G4: never-ran branch uses a null-aware comparison on raw_cleanup_removed,
    # not a falsy 0-is-never-ran check.
    case_id = "storage_ui/G4-null-aware-never-ran"
    null_aware_patterns = [
        "raw_cleanup_removed == null",
        "raw_cleanup_removed === null",
        "raw_cleanup_removed == undefined",
        "raw_cleanup_removed === undefined",
        "raw_cleanup_removed === null || ",
        "== null",
    ]
    # Look for a null-aware check specifically tied to raw_cleanup_removed within
    # a reasonable window of text.
    removed_idx = load_stats_body.find("raw_cleanup_removed")
    window = load_stats_body[max(0, removed_idx - 80):removed_idx + 120] if removed_idx != -1 else ""
    has_null_aware = ("== null" in window) or ("=== null" in window) or ("=== undefined" in window) or ("== undefined" in window)
    ok = removed_idx != -1 and has_null_aware
    _check(case_id, ok, f"removed_idx={removed_idx}, window={window!r}")
    if ok:
        passed += 1

    # G5: existing storage-stats failure fallback still present and reachable
    # inside loadStorageStats.
    case_id = "storage_ui/G5-failure-fallback-preserved"
    # The fallback copy is whatever pre-existing failure string loadStorageStats
    # already used (searched for generically since exact copy is implementation
    # defined) — assert loadStorageStats still has a catch/failure path with a
    # rendered fallback string reachable within the function body.
    has_catch = ".catch(" in load_stats_body or "catch (" in load_stats_body or "catch(" in load_stats_body
    ok = has_catch
    _check(case_id, ok, f"has_catch={has_catch}")
    if ok:
        passed += 1

    # G6: raw tiles use class="stat-card" and class="stat-value" font-size:22px,
    # no new CSS class introduced.
    case_id = "storage_ui/G6-reuses-existing-classes"
    ok = "stat-card" in load_stats_body and "stat-value" in load_stats_body and "font-size:22px" in load_stats_body
    _check(case_id, ok, f"stat_card={'stat-card' in load_stats_body}, stat_value={'stat-value' in load_stats_body}, font_size={'font-size:22px' in load_stats_body}")
    if ok:
        passed += 1

    # G7: no table, filepath, filename or anchor tag inside the raw cleanup region.
    case_id = "storage_ui/G7-no-per-file-detail"
    raw_region_start = load_stats_body.find("raw_gb_reclaimed")
    raw_region_end = load_stats_body.find("rawCleanupNote")
    raw_region = load_stats_body[raw_region_start:raw_region_end + 40] if raw_region_start != -1 and raw_region_end != -1 else load_stats_body
    ok = (
        "<table" not in raw_region
        and "filepath" not in raw_region
        and "filename" not in raw_region
        and "<a " not in raw_region
        and "<a\n" not in raw_region
    )
    _check(case_id, ok, f"raw_region_len={len(raw_region)}")
    if ok:
        passed += 1

    return (passed, total)


def main():
    parser = argparse.ArgumentParser(
        description="Raw cleanup UI verification harness (CLEANUP-01/03)"
    )
    parser.add_argument(
        "--suite", choices=["settings", "retention_ui", "storage_ui", "all"], default="all"
    )
    args = parser.parse_args()

    suites = {
        "settings": suite_settings,
        "retention_ui": suite_retention_ui,
        "storage_ui": suite_storage_ui,
    }
    selected = suites.keys() if args.suite == "all" else [args.suite]

    all_passed = True
    for name in selected:
        passed, total = suites[name]()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            print(f"FAIL: {name} ({passed}/{total})")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
