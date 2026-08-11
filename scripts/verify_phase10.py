"""
verify_phase10.py — stdlib-only verification harness for Phase 10
(Independent Bug Fixes): FIX-01, FIX-03.

Suites:
    fix01   — FIX-01, Unknown-species correction display bug (database.py's
              suppression filter vs. the two correction write paths, F1-F11).
    fix03   — FIX-03, backfill script's apply-mode Go/No-Go summary wording
              (scripts/backfill_dedup_videos.py, B1-B7).
    audit   — D-04 read-only historical audit of already-affected production
              rows (A1-A3).

Follows scripts/verify_phase7.py's structure: a `_check(case_id, condition,
detail)` helper, per-suite `(passed, total)` returns, a dict suite registry,
argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

Written RED on purpose — every assertion targets the post-fix state, so
running this harness before plans 10-02 (FIX-01) and 10-03 (FIX-03) land
reports FAIL for exactly the six not-yet-fixed cases (F2, F3, F4, B2, B4,
B5). That is the intended and required outcome — plans 10-02 and 10-03 are
the plans that turn this harness green.

Usage:
    python scripts/verify_phase10.py --suite fix01|fix03|audit|all
    python scripts/verify_phase10.py --list
    python scripts/verify_phase10.py --suite audit --db data/wildlife.db
"""

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _web_app_text():
    return (_repo_root() / "web_app.py").read_text(encoding="utf-8")


def _index_html_text():
    return (_repo_root() / "static" / "index.html").read_text(encoding="utf-8")


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


def _has_unknown_species_early_return(body):
    """Heuristic guard-detector for F11(c): flags any line inside `body`
    that mentions an unknown-species label ('nknown', matching both
    'Unknown' and 'unknown') where a `return` also appears within the next
    few lines and an `if` appears on the mentioning line itself — the shape
    an early-return guard on an unknown-species label would take. Returns
    False (no guard found) for an empty body."""
    if not body or "return" not in body:
        return False
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if "nknown" in line.lower() and "if" in line.lower():
            window = "\n".join(lines[i : i + 4])
            if "return" in window:
                return True
    return False


# ── FIX-01 fixture ───────────────────────────────────────────────────────


def _seed_fixture_db(path):
    """Build the FIX-01 fixture database: two videos on camera 'World
    Watch'. Video 1 carries a real species (domestic cat), an Unknown
    species detection, and a blank-labeled detection — the mixed case the
    suppression filter must handle correctly. Video 2 carries only two
    Unknown species detections — the all-Unknown case that must never be
    collaterally suppressed by a sibling correction (F8). Returns a dict of
    the real autoincrement ids assigned to each row."""
    database.set_db_path(path)
    database.init_db(path)
    now = "2026-08-06T04:36:34"

    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
            "VALUES (?, ?, 1, ?, 0, ?)",
            ("WorldWatch_00_fixture.mp4", "World Watch", now, now),
        )
        video1 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.execute(
            "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
            "VALUES (?, ?, 1, ?, 0, ?)",
            ("WorldWatch_00_allunknown.mp4", "World Watch", now, now),
        )
        video2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection(video_id, label, common_name):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, 0.9)",
                (det_id, label, common_name, common_name),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture_crop_{det_id}.jpg", 80.0, now),
            )
            return det_id

        d1 = _add_detection(video1, "domestic cat", "Domestic Cat")
        d2 = _add_detection(video1, "Unknown species", "Unknown species")
        d3 = _add_detection(video1, "no_cv_result;;;;;;blank", None)

        d4 = _add_detection(video2, "Unknown species", "Unknown species")
        d5 = _add_detection(video2, "Unknown species", "Unknown species")

    return {
        "video1": video1,
        "video2": video2,
        "d1": d1,
        "d2": d2,
        "d3": d3,
        "d4": d4,
        "d5": d5,
    }


def suite_fix01():
    """FIX-01 cases F1-F11 (11 total), proving the Unknown-species
    correction display bug and pinning both correction write paths against
    a fixture database. Wrapped in a temp directory and restores
    database.DB_PATH in a finally block so a suite run can never leave the
    module pointing at a temp path (T-10-01-03)."""
    passed = 0
    total = 11

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "fix01.db")
        ids = _seed_fixture_db(db_path)
        video1, video2 = ids["video1"], ids["video2"]
        d1, d2, d3, d4, d5 = ids["d1"], ids["d2"], ids["d3"], ids["d4"], ids["d5"]

        def _det_by_id(dets, det_id):
            return next((d for d in dets if d["id"] == det_id), None)

        # F1 — pre-correction suppression still holds. This is the guard
        # that stops the fix from being "passed" by removing suppression
        # wholesale; must pass both before and after the fix.
        case_id = "F1"
        dets = database.get_video_by_id(video1)["detections"]
        d1_present = _det_by_id(dets, d1) is not None
        d2_present = _det_by_id(dets, d2) is not None
        ok = d1_present and not d2_present
        _check(case_id, ok, f"d1_present={d1_present}, d2_present={d2_present}")
        if ok:
            passed += 1

        # F2 — gallery write path, video read path. Expected RED now.
        case_id = "F2"
        database.correct_species(d2, "Northern Raccoon", "Procyon lotor")
        dets = database.get_video_by_id(video1)["detections"]
        row = _det_by_id(dets, d2)
        ok = row is not None and row.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"row={row}")
        if ok:
            passed += 1

        # F3 — gallery write path, gallery read path. Expected RED now.
        case_id = "F3"
        gallery_items = database.get_gallery()["items"]
        gitem = next((it for it in gallery_items if it.get("detection_id") == d2), None)
        ok = gitem is not None and gitem.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"gitem={gitem}")
        if ok:
            passed += 1

        # F4 — video-player write path. Expected RED now.
        case_id = "F4"
        database.correct_species(d2, "", "")
        database.save_video_correction(
            video1, "Unknown species", "raccoon_fixture", "Northern Raccoon", "Procyon lotor"
        )
        dets = database.get_video_by_id(video1)["detections"]
        row = _det_by_id(dets, d2)
        ok = (
            row is not None
            and row.get("label") == "raccoon_fixture"
            and row.get("common_name") == "Northern Raccoon"
            and bool(row.get("corrected"))
        )
        _check(case_id, ok, f"row={row}")
        if ok:
            passed += 1

        # F5 — cleared correction re-suppresses. Must pass before and after.
        case_id = "F5"
        with database.get_conn() as conn:
            conn.execute(
                "DELETE FROM video_corrections WHERE video_id=? AND original_label=?",
                (video1, "Unknown species"),
            )
        database.correct_species(d2, "", "")
        dets = database.get_video_by_id(video1)["detections"]
        d2_present = _det_by_id(dets, d2) is not None
        ok = not d2_present
        _check(case_id, ok, f"d2_present={d2_present}")
        if ok:
            passed += 1

        # F6 — explicit suppress (corrected_label NULL) stays suppressed.
        # Must pass before and after.
        case_id = "F6"
        database.save_video_correction(video1, "Unknown species", None, None, None)
        dets = database.get_video_by_id(video1)["detections"]
        d2_present = _det_by_id(dets, d2) is not None
        ok = not d2_present
        _check(case_id, ok, f"d2_present={d2_present}")
        if ok:
            passed += 1

        # F7 — no collateral un-suppression of the blank-labeled detection,
        # across every state exercised above. Must pass before and after.
        case_id = "F7"
        dets = database.get_video_by_id(video1)["detections"]
        gallery_items = database.get_gallery()["items"]
        d3_in_dets = _det_by_id(dets, d3) is not None
        d3_in_gallery = any(it.get("detection_id") == d3 for it in gallery_items)
        ok = not d3_in_dets and not d3_in_gallery
        _check(case_id, ok, f"d3_in_dets={d3_in_dets}, d3_in_gallery={d3_in_gallery}")
        if ok:
            passed += 1

        # F8 — all-Unknown video is unaffected by correcting one sibling
        # (plan 10-02's decision P-01). Must pass before and after.
        case_id = "F8"
        dets_before = database.get_video_by_id(video2)["detections"]
        both_before = (
            _det_by_id(dets_before, d4) is not None and _det_by_id(dets_before, d5) is not None
        )
        database.correct_species(d4, "Some Animal", "Aliquid animalus")
        dets_after = database.get_video_by_id(video2)["detections"]
        both_after = (
            _det_by_id(dets_after, d4) is not None and _det_by_id(dets_after, d5) is not None
        )
        ok = both_before and both_after
        _check(case_id, ok, f"both_before={both_before}, both_after={both_after}")
        if ok:
            passed += 1

        # F9 — source assertion: the HTTP layer is a thin pass-through to
        # the same two functions this suite drives. Must pass before and
        # after (verify-only, no change expected in web_app.py).
        case_id = "F9"
        web_text = _web_app_text()
        correct_route = _slice(web_text, '@app.post("/api/species/correct")', "\n@app.")
        corrections_route = _slice(web_text, '@app.post("/api/corrections")', "\n@app.")
        correct_ok = "db.correct_species" in correct_route
        corrections_ok = "db.save_video_correction" in corrections_route
        ok = correct_ok and corrections_ok
        _check(case_id, ok, f"correct_ok={correct_ok}, corrections_ok={corrections_ok}")
        if ok:
            passed += 1

        # F10 — filter alias resolution at every interpolation site. Must
        # pass before and after (automated proof the SQL aliases resolve).
        case_id = "F10"
        f10_ok = True
        f10_detail = ""
        try:
            database.get_stats()
            database.get_species_list()
            database.get_gallery()
            database.get_videos()
            database.get_video_by_id(video1)
            database.get_timeline()
        except sqlite3.OperationalError as exc:
            f10_ok = False
            f10_detail = str(exc)
        _check(case_id, f10_ok, f10_detail)
        if f10_ok:
            passed += 1

        # F11 — discharge the operator's alternate root-cause hypothesis:
        # (a) correct_species() does not no-op on the Unknown-species row,
        # (b) save_video_correction() does not no-op either, (c) none of
        # the three frontend correction functions carries an early return
        # guarded on an unknown-species label. Must pass before and after —
        # this asserts behaviour that already works, per D-01's diagnosis
        # that the defect is entirely in the read-path SQL filter.
        case_id = "F11"
        rc = database.correct_species(d2, "Northern Raccoon", "Procyon lotor")
        rid = database.save_video_correction(
            video1, "Unknown species", "raccoon_fixture2", "Northern Raccoon", "Procyon lotor"
        )
        index_text = _index_html_text()
        quick_body = _slice(
            index_text, "async function applyCorrectionQuick(", "\nasync function applyCorrection("
        )
        correction_body = _slice(
            index_text, "async function applyCorrection(", "\n// Gallery entry point"
        )
        detection_body = _slice(
            index_text, "async function applyDetectionCorrection(", "\n// ── Species Blacklist"
        )
        bodies_found = bool(quick_body) and bool(correction_body) and bool(detection_body)
        guard_found = any(
            _has_unknown_species_early_return(b)
            for b in (quick_body, correction_body, detection_body)
        )
        ok = rc == 1 and rid is not None and bodies_found and not guard_found
        _check(
            case_id,
            ok,
            f"rowcount={rc}, correction_id={rid}, bodies_found={bodies_found}, guard_found={guard_found}",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


SUITES = {
    "fix01": (suite_fix01, 11),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 10 verification harness (FIX-01, FIX-03)"
    )
    parser.add_argument(
        "--suite",
        choices=list(SUITES.keys()) + ["all"],
        default="all",
    )
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--db", default="data/wildlife.db", help="Database path for the audit suite only"
    )
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
