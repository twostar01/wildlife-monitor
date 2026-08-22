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
import json
import os
import re
import sqlite3
import subprocess
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

        # F4 — video-player write path. Revised for Phase 14 plan 14-02's
        # get_video_by_id() cutover: `label` is now the RAW SpeciesNet
        # label ("Unknown species"), not the corrected label the old
        # Python-side overlay used to substitute — a re-correction posts
        # `label` back as `original_label`, and the write-time fan-out
        # (save_video_correction()) matches on the raw label, so the raw
        # value must survive in the response. `original_label` duplicates
        # the same raw value under its own key. `common_name` and
        # `corrected` are unaffected by this change.
        case_id = "F4"
        database.correct_species(d2, "", "")
        database.save_video_correction(
            video1, "Unknown species", "raccoon_fixture", "Northern Raccoon", "Procyon lotor"
        )
        dets = database.get_video_by_id(video1)["detections"]
        row = _det_by_id(dets, d2)
        ok = (
            row is not None
            and row.get("label") == "Unknown species"
            and row.get("common_name") == "Northern Raccoon"
            and row.get("original_label") == "Unknown species"
            and bool(row.get("corrected"))
        )
        _check(case_id, ok, f"row={row}")
        if ok:
            passed += 1

        # F5 — cleared correction re-suppresses. Must pass before and
        # after. Revised for Phase 14: the video-player correction now
        # lives in the unified species_corrections table (not the frozen
        # legacy video_corrections table), so the delete targets that row
        # directly by detection_id instead of the old raw DELETE against
        # video_corrections — the case's intent ("the video-player
        # correction is removed, then the Gallery correction is cleared,
        # so the row re-suppresses") is unchanged.
        case_id = "F5"
        with database.get_conn() as conn:
            conn.execute(
                "DELETE FROM species_corrections WHERE detection_id=?",
                (d2,),
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


# ── FIX-03 fixture ───────────────────────────────────────────────────────


def _seed_dup_fixture_db(path, tmpdir):
    """Build the FIX-03 fixture database: one duplicate (filename,
    camera_name) group — two `videos` rows sharing filename
    'dup_fixture_00.mp4' and camera_name 'World Watch', each with a
    distinct filepath pointing at a real placeholder file in `tmpdir`.
    Both members carry a detection/species/crop row: the winner (lower id,
    since group_member_ids() orders by (filepath IS NULL), id and both
    filepaths are non-NULL here) must hold at least one detection itself,
    or choose_reparent_source() re-parents the loser's children onto it
    instead of deleting them — leaving the crops/species/detections delete
    counts at zero, which would defeat B3/B4's non-zero-delete assertions.
    Returns the real autoincrement winner/loser ids."""
    database.set_db_path(path)
    database.init_db(path)
    now = "2026-08-06T04:00:00"

    winner_file = os.path.join(tmpdir, "dup_fixture_winner.mp4")
    loser_file = os.path.join(tmpdir, "dup_fixture_loser.mp4")
    Path(winner_file).write_bytes(b"placeholder")
    Path(loser_file).write_bytes(b"placeholder")

    with database.get_conn() as conn:

        def _insert_video(filepath):
            conn.execute(
                "INSERT INTO videos (filename, filepath, camera_name, kept, recorded_at, processed_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                ("dup_fixture_00.mp4", filepath, "World Watch", now, now),
            )
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection_with_crop(video_id, crop_path):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, 'domestic cat', 'Domestic Cat', 'Felis catus', 0.9)",
                (det_id,),
            )
            Path(crop_path).write_bytes(b"placeholder")
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, crop_path, 75.0, now),
            )
            return det_id

        winner_id = _insert_video(winner_file)
        loser_id = _insert_video(loser_file)
        _add_detection_with_crop(winner_id, os.path.join(tmpdir, "dup_fixture_winner_crop.jpg"))
        _add_detection_with_crop(loser_id, os.path.join(tmpdir, "dup_fixture_loser_crop.jpg"))

    return {"winner_id": winner_id, "loser_id": loser_id}


def _video_count(db_path):
    """Read the `videos` row count from `db_path`, via database.get_conn()
    per CLAUDE.md's rule that all database access goes through it."""
    database.set_db_path(db_path)
    with database.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def _parse_deleted_videos_count(stdout):
    """Extract the integer 'videos' count from the Go/No-Go summary's
    'Rows that ... deleted: {...}' line — the dict's 'videos' key is the
    only sub-count that tracks the actual `videos` table row drop (the
    crops/species/detections sub-counts can be zero even for a real delete,
    e.g. when the group's children are re-parented rather than removed).
    Returns None if the line or key is not found."""
    m = re.search(r"Rows that .*deleted:.*'videos':\s*(\d+)", stdout)
    return int(m.group(1)) if m else None


def _run_backfill(args_list):
    """Run scripts/backfill_dedup_videos.py via subprocess with the given
    argument list. Must never be called without an explicit --db argument —
    raises immediately if one is missing, so no call site can silently fall
    through to the script's own data/wildlife.db default (T-10-01-01)."""
    if "--db" not in args_list:
        raise ValueError("_run_backfill requires an explicit --db argument")
    return subprocess.run(
        [sys.executable, str(_repo_root() / "scripts" / "backfill_dedup_videos.py"), *args_list],
        capture_output=True,
        text=True,
    )


def suite_fix03():
    """FIX-03 cases B1-B7 (7 total), proving the backfill script's Go/No-Go
    summary correctly conditions its 'would be'/actual wording on
    args.apply, without changing any of the underlying data it reports
    (D-06/D-07). Because --apply is destructive, every --db value this
    suite uses is tracked and safety-checked before the subprocess ever
    runs — the suite raises immediately (not merely a failed case) if a
    --db value is ever outside its own temp directory or equals the
    production database path (T-10-01-01)."""
    passed = 0
    total = 7

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmpdir = tmpdir_obj.name
    tmp_root = Path(tmpdir).resolve()
    forbidden_db = (_repo_root() / "data" / "wildlife.db").resolve()
    used_db_paths = []

    def _run(args_list):
        db_idx = args_list.index("--db")
        db_value = Path(args_list[db_idx + 1]).resolve()
        used_db_paths.append(db_value)
        if db_value == forbidden_db:
            raise RuntimeError(
                f"suite_fix03 refused a --db value equal to the production database: {db_value}"
            )
        if not db_value.is_relative_to(tmp_root):
            raise RuntimeError(
                f"suite_fix03 refused a --db value outside its temp directory: {db_value}"
            )
        return _run_backfill(args_list)

    try:
        zero_rows = {
            "crops": 0,
            "species": 0,
            "detections": 0,
            "video_corrections": 0,
            "videos": 0,
        }
        expected_zero_rows_line = f"Rows that would be deleted: {zero_rows}"
        expected_zero_files_line = "Files that would be removed: 0"

        # B1 — dry-run wording on an empty database. Must pass before and
        # after (roadmap AC3's guard).
        case_id = "B1"
        empty_db = os.path.join(tmpdir, "b1_empty.db")
        database.init_db(empty_db)
        proc_b1 = _run(["--consolidate", "--db", empty_db])
        ok = expected_zero_rows_line in proc_b1.stdout and expected_zero_files_line in proc_b1.stdout
        _check(case_id, ok, f"stdout={proc_b1.stdout!r}")
        if ok:
            passed += 1

        # B2 — apply-mode wording on an empty database. Expected RED now.
        case_id = "B2"
        empty_db2 = os.path.join(tmpdir, "b2_empty.db")
        database.init_db(empty_db2)
        proc_b2 = _run(
            [
                "--consolidate",
                "--apply",
                "--confirm-irreversible",
                "--db",
                empty_db2,
                "--snapshot-dir",
                os.path.join(tmpdir, "b2_snap"),
                "--audit-log",
                os.path.join(tmpdir, "b2_audit.jsonl"),
            ]
        )
        stdout_lines = proc_b2.stdout.splitlines()
        rows_line = next(
            (ln for ln in stdout_lines if ln.startswith("Rows that ") and "deleted:" in ln), None
        )
        files_line = next(
            (ln for ln in stdout_lines if ln.startswith("Files that ") and "removed:" in ln), None
        )
        no_dryrun_wording = (
            "would be deleted" not in proc_b2.stdout and "would be removed" not in proc_b2.stdout
        )
        ok = (
            proc_b2.returncode == 0
            and rows_line is not None
            and files_line is not None
            and no_dryrun_wording
        )
        _check(
            case_id,
            ok,
            f"returncode={proc_b2.returncode}, rows_line={rows_line!r}, files_line={files_line!r}",
        )
        if ok:
            passed += 1

        # B3 — dry-run does not mutate. Must pass before and after.
        case_id = "B3"
        b3_dir = os.path.join(tmpdir, "b3")
        os.makedirs(b3_dir, exist_ok=True)
        b3_db = os.path.join(b3_dir, "b3.db")
        _seed_dup_fixture_db(b3_db, b3_dir)
        count_before_b3 = _video_count(b3_db)
        proc_b3 = _run(["--consolidate", "--db", b3_db])
        deleted_videos_b3 = _parse_deleted_videos_count(proc_b3.stdout)
        count_after_b3 = _video_count(b3_db)
        ok = (
            deleted_videos_b3 is not None
            and deleted_videos_b3 > 0
            and count_after_b3 == count_before_b3
        )
        _check(
            case_id,
            ok,
            f"deleted_videos={deleted_videos_b3}, before={count_before_b3}, after={count_after_b3}",
        )
        if ok:
            passed += 1

        # B4 — apply-mode report matches reality. Expected RED now (wording
        # half); the numeric half documents the roadmap goal.
        case_id = "B4"
        b4_dir = os.path.join(tmpdir, "b4")
        os.makedirs(b4_dir, exist_ok=True)
        b4_db = os.path.join(b4_dir, "b4.db")
        _seed_dup_fixture_db(b4_db, b4_dir)
        count_before_b4 = _video_count(b4_db)
        proc_b4 = _run(
            [
                "--consolidate",
                "--apply",
                "--confirm-irreversible",
                "--db",
                b4_db,
                "--snapshot-dir",
                os.path.join(b4_dir, "snap"),
                "--audit-log",
                os.path.join(b4_dir, "audit.jsonl"),
            ]
        )
        deleted_videos_b4 = _parse_deleted_videos_count(proc_b4.stdout)
        count_after_b4 = _video_count(b4_db)
        wording_ok_b4 = (
            "would be deleted" not in proc_b4.stdout and "would be removed" not in proc_b4.stdout
        )
        ok = (
            deleted_videos_b4 is not None
            and deleted_videos_b4 == (count_before_b4 - count_after_b4)
            and wording_ok_b4
        )
        _check(
            case_id,
            ok,
            f"deleted_videos={deleted_videos_b4}, drop={count_before_b4 - count_after_b4}, "
            f"wording_ok={wording_ok_b4}",
        )
        if ok:
            passed += 1

        # B5 — idempotency (the FIX-03 idempotency edge probe's resolution).
        # Expected RED now (wording half).
        case_id = "B5"
        count_before_b5 = _video_count(b4_db)
        proc_b5 = _run(
            [
                "--consolidate",
                "--apply",
                "--confirm-irreversible",
                "--db",
                b4_db,
                "--snapshot-dir",
                os.path.join(b4_dir, "snap"),
                "--audit-log",
                os.path.join(b4_dir, "audit.jsonl"),
            ]
        )
        count_after_b5 = _video_count(b4_db)
        wording_ok_b5 = (
            "would be deleted" not in proc_b5.stdout and "would be removed" not in proc_b5.stdout
        )
        ok = (
            proc_b5.returncode == 0
            and "Groups discovered: 0" in proc_b5.stdout
            and count_after_b5 == count_before_b5
            and wording_ok_b5
        )
        _check(
            case_id,
            ok,
            f"returncode={proc_b5.returncode}, before={count_before_b5}, after={count_after_b5}, "
            f"wording_ok={wording_ok_b5}",
        )
        if ok:
            passed += 1

        # B6 — production-safety guard. Must pass before and after.
        case_id = "B6"
        all_inside_tmp = all(p.is_relative_to(tmp_root) for p in used_db_paths)
        none_is_production = all(p != forbidden_db for p in used_db_paths)
        ok = bool(used_db_paths) and all_inside_tmp and none_is_production
        _check(case_id, ok, f"used_db_paths={used_db_paths}")
        if ok:
            passed += 1

        # B7 — out-of-scope surfaces unchanged (D-06). Must pass before and
        # after.
        case_id = "B7"
        header = "Dedup Backfill Consolidation Plan (dry-run unless --apply --confirm-irreversible)"
        header_in_dry = header in proc_b1.stdout
        header_in_apply = header in proc_b2.stdout
        json_db = os.path.join(tmpdir, "b7.db")
        database.init_db(json_db)
        proc_json = _run(["--consolidate", "--json", "--db", json_db])
        json_ok = False
        json_detail = ""
        try:
            parsed = json.loads(proc_json.stdout)
            summary_keys = set(parsed.get("summary", {}).keys())
            json_ok = {"rows_deleted_projected", "files_would_remove", "files_would_retain"} <= (
                summary_keys
            )
            json_detail = f"summary_keys={sorted(summary_keys)}"
        except json.JSONDecodeError as exc:
            json_detail = str(exc)
        ok = header_in_dry and header_in_apply and json_ok
        _check(
            case_id,
            ok,
            f"header_in_dry={header_in_dry}, header_in_apply={header_in_apply}, {json_detail}",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── D-04 historical audit ────────────────────────────────────────────────


def suite_audit(db_path):
    """D-04 read-only historical audit (A1-A3): counts how many existing
    production corrections are historically invisible under the pre-fix
    suppression filter. Informational cases A1/A2 always pass and simply
    report counts; A3 is a real runtime assertion that every statement this
    suite executes is a SELECT.

    The two SQL blocks below are frozen literals copied from the pre-fix
    SUPPRESS_UNKNOWN_IF_IDENTIFIED text (database.py lines 213-237, as of
    2026-08-11) — deliberately NOT database.KNOWN_SPECIES_FILTER or
    database.SUPPRESS_UNKNOWN_IF_IDENTIFIED. This audit answers "how many
    corrections were historically invisible under the OLD filter"; importing
    the live constant would make that figure silently collapse to zero the
    moment plan 10-02 changes it, defeating the audit's purpose."""
    total = 3

    if not Path(db_path).exists():
        print(f"SKIP: audit (no database at {db_path})")
        return (3, 3)

    original_db_path = database.get_db_path()
    select_only_ok = True

    def _select(conn, sql, params=()):
        nonlocal select_only_ok
        if not sql.strip().startswith("SELECT"):
            select_only_ok = False
            raise AssertionError("suite_audit attempted a non-SELECT statement")
        return conn.execute(sql, params)

    a1_count = a2_count = a2b_count = None
    try:
        database.set_db_path(db_path)
        with database.get_conn() as conn:
            # A1 — video-player-path backlog: video_corrections rows keyed
            # to Unknown species with a real corrected_label, whose video
            # also carries at least one real identified species elsewhere.
            a1_count = _select(
                conn,
                """
                SELECT COUNT(*) FROM video_corrections vc
                WHERE vc.original_label = 'Unknown species'
                  AND vc.corrected_label IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM species s2
                      JOIN detections d2 ON s2.detection_id = d2.id
                      WHERE d2.video_id = vc.video_id
                        AND s2.label != 'Unknown species'
                        AND s2.label NOT LIKE '%;;;;;;blank'
                  )
                """,
            ).fetchone()[0]

            # A2 — gallery-path backlog: Unknown-species rows with a
            # gallery-set user_common_name, whose video also carries at
            # least one real identified species elsewhere.
            a2_count = _select(
                conn,
                """
                SELECT COUNT(*) FROM species s
                JOIN detections d ON s.detection_id = d.id
                WHERE s.label = 'Unknown species'
                  AND NULLIF(s.user_common_name,'') IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM species s2
                      JOIN detections d2 ON s2.detection_id = d2.id
                      WHERE d2.video_id = d.video_id
                        AND s2.label != 'Unknown species'
                        AND s2.label NOT LIKE '%;;;;;;blank'
                  )
                """,
            ).fetchone()[0]

            # A2b — informational only: distinct videos affected by A2.
            a2b_count = _select(
                conn,
                """
                SELECT COUNT(DISTINCT d.video_id) FROM species s
                JOIN detections d ON s.detection_id = d.id
                WHERE s.label = 'Unknown species'
                  AND NULLIF(s.user_common_name,'') IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM species s2
                      JOIN detections d2 ON s2.detection_id = d2.id
                      WHERE d2.video_id = d.video_id
                        AND s2.label != 'Unknown species'
                        AND s2.label NOT LIKE '%;;;;;;blank'
                  )
                """,
            ).fetchone()[0]
    except AssertionError:
        select_only_ok = False
    finally:
        database.set_db_path(original_db_path)

    passed = 0

    # A1/A2 are informational — they record counts and always pass.
    print(f"AUDIT A1 video_corrections_unknown_suppressed: {a1_count}")
    passed += 1
    print(f"AUDIT A2 gallery_corrections_unknown_suppressed: {a2_count}")
    print(f"AUDIT A2b affected_videos: {a2b_count}")
    passed += 1

    case_id = "A3"
    _check(case_id, select_only_ok, f"select_only_ok={select_only_ok}")
    if select_only_ok:
        passed += 1

    return (passed, total)


SUITES = {
    "fix01": (suite_fix01, 11),
    "fix03": (suite_fix03, 7),
    "audit": (suite_audit, 3),
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
        if name == "audit":
            passed, total = fn(args.db)
        else:
            passed, total = fn()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            print(f"FAIL: {name} ({passed}/{total})")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
