"""
verify_phase14.py — stdlib-only verification harness for Phase 14
(Correction Unification: Schema, Backfill & Cutover), plans 14-01/14-02.

Suites:
    unified     — CORR-01/D-00, proves a Gallery-popover correction travels
                  correct_species() -> species_corrections -> EFFECTIVE_COMMON
                  -> get_gallery()/get_species_detail() end to end: the
                  raw-value baseline, a successful correction, the two
                  readers agreeing, the UNIQUE(detection_id) upsert
                  (most-recent-write-wins), the clear-correction path, the
                  blank-common/set-scientific edge case, and the
                  unknown-detection-id / clear-an-already-clear-correction
                  404-contract edge cases (U1-U7).
    audit       — CORR-04/D-06, pins the audit trail (species.label stays
                  byte-identical, and every species_corrections row joins
                  back to a non-NULL AI label) and the legacy-column freeze
                  (the Gallery write path never touches
                  species.user_common_name/user_scientific_name/
                  corrected_at), as both fixture behaviour assertions and
                  region-scoped source assertions over the write-path
                  functions' bodies (A1-A5).
    fanout      — CORR-02/D-01 (plan 14-02), the video-player write path
                  fans out a snapshot of species_corrections rows at save
                  time — matching detections, snapshot semantics (a later
                  detection with the same raw label is unaffected),
                  UPSERT-in-place on re-save, the None/0 404-vs-no-match
                  return contract, and Gallery-visible propagation (FO1-FO6).
    suppress    — CORR-02/D-01 (plan 14-02), the video-player suppress
                  action and get_video_by_id()'s read path, both keyed on
                  the dedicated `suppressed` column rather than a NULL
                  corrected_label sentinel (S1-S6).
    precedence  — CORR-02/D-03 (plan 14-02), most-recent-write-wins pinned
                  in BOTH write-order directions (Gallery-then-video and
                  video-then-Gallery), with get_gallery() and
                  get_video_by_id() asserted never to disagree (PR1-PR4).

Follows scripts/verify_phase12.py's structure (itself following
scripts/verify_phase10.py's): a `_check(case_id, condition, detail)`
helper, per-suite `(passed, total)` returns, a dict suite registry, argparse
`--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary lines, and
`sys.exit(0 if all_passed else 1)`.

Uses its own `_seed_unified_fixture()`/`_seed_fanout_fixture()`/
`_seed_suppress_fixture()`/`_seed_precedence_fixture()` rather than
importing verify_phase12's — this module's fixture shapes are specific to
the unified-table correction cases and unrelated to verify_phase12's
badge/propagation fixture. The `audit` suite reuses `_seed_unified_fixture()`.

Usage:
    python scripts/verify_phase14.py --suite unified|audit|fanout|suppress|precedence|all
    python scripts/verify_phase14.py --list
"""

import argparse
import os
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


def _database_py_text():
    return (_repo_root() / "database.py").read_text(encoding="utf-8")


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


def _strip_comment_lines(text):
    """Return `text` with any line whose lstrip() begins with '#' removed —
    so a future explanatory comment can never accidentally satisfy or
    invalidate a source-assertion case."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# ── `unified` suite fixture ──────────────────────────────────────────────


def _seed_unified_fixture(path):
    """Build the `unified` suite's fixture: one camera "World Watch", two
    videos, four detections.

      - video1 / det_raccoon, label "Northern raccoon" — left uncorrected;
        also reused by U7's second half (an existing, currently-uncorrected
        detection).
      - video1 / det_unknown, label "Unknown species" — same video as
        det_raccoon, so KNOWN_SPECIES_FILTER's suppression of Unknown rows
        genuinely fires here (the video also has a known species), pinning
        NOT_EFFECTIVELY_UNKNOWN against a real sibling-detection case rather
        than an isolated one.
      - video2 / det_cat_a, label "domestic cat" — the correction cases
        (U2-U6) apply to this detection sequentially.
      - video2 / det_cat_b, label "domestic cat" — left uncorrected; used by
        U1 (the raw-value baseline case).

    Returns a dict of the real autoincrement ids assigned to each row.
    """
    database.set_db_path(path)
    database.init_db(path)
    now = "2026-08-21T04:00:00"

    with database.get_conn() as conn:

        def _insert_video(filename):
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, ?, 1, ?, 0, ?)",
                (filename, "World Watch", now, now),
            )
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection(video_id, label, common_name, scientific_name, confidence):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', ?)",
                (video_id, confidence),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (det_id, label, common_name, scientific_name, confidence),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture14_crop_{det_id}.jpg", 80.0, now),
            )
            return det_id

        video1 = _insert_video("WorldWatch_00_video1.mp4")
        video2 = _insert_video("WorldWatch_00_video2.mp4")

        det_raccoon = _add_detection(video1, "Northern raccoon", "Northern Raccoon", "Procyon lotor", 0.8)
        det_unknown = _add_detection(video1, "Unknown species", None, None, 0.3)
        det_cat_a = _add_detection(video2, "domestic cat", "Domestic Cat", "Felis catus", 0.9)
        det_cat_b = _add_detection(video2, "domestic cat", "Domestic Cat", "Felis catus", 0.85)

    return {
        "video1": video1,
        "video2": video2,
        "det_raccoon": det_raccoon,
        "det_unknown": det_unknown,
        "det_cat_a": det_cat_a,
        "det_cat_b": det_cat_b,
    }


def suite_unified():
    """`unified` suite cases U1-U7 (7 total). See module docstring."""
    passed = 0
    total = 7

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "unified.db")
        ids = _seed_unified_fixture(db_path)
        det_raccoon = ids["det_raccoon"]
        det_cat_a = ids["det_cat_a"]
        det_cat_b = ids["det_cat_b"]

        def _gallery_item(det_id):
            items = database.get_gallery(per_page=100)["items"]
            return next((it for it in items if it.get("detection_id") == det_id), None)

        def _species_detail_crop(label, det_id):
            crops = database.get_species_detail(label)["crops"]
            return next((c for c in crops if c.get("detection_id") == det_id), None)

        def _correction_count(det_id):
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM species_corrections WHERE detection_id=?",
                    (det_id,),
                ).fetchone()
            return row[0]

        # U1 — an uncorrected detection's get_gallery() item reports its raw
        # SpeciesNet common_name and has_correction==0.
        case_id = "U1"
        item_b = _gallery_item(det_cat_b)
        ok = (
            item_b is not None
            and item_b.get("common_name") == "Domestic Cat"
            and item_b.get("has_correction") == 0
        )
        _check(case_id, ok, f"item_b={item_b}")
        if ok:
            passed += 1

        # U2 — after correct_species(), the corrected detection's gallery
        # item reports the corrected common/scientific name and
        # has_correction==1.
        case_id = "U2"
        rc = database.correct_species(det_cat_a, "Northern Raccoon", "Procyon lotor")
        item_a = _gallery_item(det_cat_a)
        ok = (
            rc == 1
            and item_a is not None
            and item_a.get("common_name") == "Northern Raccoon"
            and item_a.get("scientific_name") == "Procyon lotor"
            and item_a.get("has_correction") == 1
        )
        _check(case_id, ok, f"rc={rc}, item_a={item_a}")
        if ok:
            passed += 1

        # U3 — get_species_detail()'s crops list agrees with get_gallery()
        # for the same detection (the two readers never disagree).
        case_id = "U3"
        detail_crop = _species_detail_crop("domestic cat", det_cat_a)
        ok = (
            detail_crop is not None
            and detail_crop.get("common_name") == "Northern Raccoon"
            and detail_crop.get("scientific_name") == "Procyon lotor"
            and detail_crop.get("has_correction") == 1
        )
        _check(case_id, ok, f"detail_crop={detail_crop}")
        if ok:
            passed += 1

        # U4 — calling correct_species() a second time on the same
        # detection leaves exactly one species_corrections row, carrying the
        # second call's values (UNIQUE(detection_id) upsert).
        case_id = "U4"
        rc2 = database.correct_species(det_cat_a, "Bobcat", "Lynx rufus")
        count_after_second = _correction_count(det_cat_a)
        item_a2 = _gallery_item(det_cat_a)
        ok = (
            rc2 == 1
            and count_after_second == 1
            and item_a2 is not None
            and item_a2.get("common_name") == "Bobcat"
            and item_a2.get("scientific_name") == "Lynx rufus"
        )
        _check(case_id, ok, f"rc2={rc2}, count_after_second={count_after_second}, item_a2={item_a2}")
        if ok:
            passed += 1

        # U5 — correct_species(det, "", "") clears the correction: zero
        # species_corrections rows remain, and the detection's gallery item
        # is back to its raw common_name with has_correction==0.
        case_id = "U5"
        rc3 = database.correct_species(det_cat_a, "", "")
        count_after_clear = _correction_count(det_cat_a)
        item_a3 = _gallery_item(det_cat_a)
        ok = (
            rc3 == 1
            and count_after_clear == 0
            and item_a3 is not None
            and item_a3.get("common_name") == "Domestic Cat"
            and item_a3.get("has_correction") == 0
        )
        _check(case_id, ok, f"rc3={rc3}, count_after_clear={count_after_clear}, item_a3={item_a3}")
        if ok:
            passed += 1

        # U6 — a correction with an empty corrected common name but a set
        # scientific name does not blank the displayed common_name; it
        # falls back to the raw SpeciesNet value.
        case_id = "U6"
        rc4 = database.correct_species(det_cat_a, "", "Procyon lotor")
        item_a4 = _gallery_item(det_cat_a)
        ok = rc4 == 1 and item_a4 is not None and item_a4.get("common_name") == "Domestic Cat"
        _check(case_id, ok, f"rc4={rc4}, item_a4={item_a4}")
        if ok:
            passed += 1

        # U7 — correct_species() on a nonexistent detection_id returns 0,
        # raises nothing, and leaves the species_corrections row count
        # unchanged; correct_species() on an existing, currently-uncorrected
        # detection with empty strings returns 1 (clearing an already-clear
        # correction is not a 404).
        case_id = "U7"
        nonexistent_id = 999_999_999
        count_before_bad_call = _correction_count(nonexistent_id)
        rc_missing = database.correct_species(nonexistent_id, "X", "Y")
        count_after_bad_call = _correction_count(nonexistent_id)
        rc_clear_clean = database.correct_species(det_raccoon, "", "")
        ok = (
            rc_missing == 0
            and count_before_bad_call == count_after_bad_call == 0
            and rc_clear_clean == 1
        )
        _check(
            case_id,
            ok,
            f"rc_missing={rc_missing}, count_before={count_before_bad_call}, "
            f"count_after={count_after_bad_call}, rc_clear_clean={rc_clear_clean}",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


def suite_audit():
    """`audit` suite cases A1-A5 (5 total): CORR-04's audit trail and D-06's
    legacy freeze, pinned both as behaviour assertions on fixture data and as
    region-scoped source assertions over the write-path functions' bodies.
    Reuses `_seed_unified_fixture()` (no behavioural change to database.py in
    this suite — if any case fails, the fix belongs in suite_unified()'s
    write path, not in weakening the assertion here).
    """
    passed = 0
    total = 5

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "audit.db")
        ids = _seed_unified_fixture(db_path)
        det_cat_a = ids["det_cat_a"]

        def _species_row(det_id):
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT label, user_common_name, user_scientific_name, corrected_at "
                    "FROM species WHERE detection_id=?",
                    (det_id,),
                ).fetchone()
            return dict(row) if row else None

        # Capture the pre-correction species.label inside the case (not
        # hardcoded) so the assertion survives a future fixture change.
        pre_row = _species_row(det_cat_a)
        pre_label = pre_row["label"] if pre_row else None

        rc = database.correct_species(det_cat_a, "Northern Raccoon", "Procyon lotor")
        post_row = _species_row(det_cat_a)

        # A1 — species.label is byte-identical before and after a Gallery
        # correction (CORR-04 audit trail preserved).
        case_id = "A1"
        ok = rc == 1 and post_row is not None and post_row["label"] == pre_label
        _check(case_id, ok, f"pre_label={pre_label!r}, post_row={post_row}")
        if ok:
            passed += 1

        # A2 — species.user_common_name/user_scientific_name/corrected_at
        # are unchanged (still NULL) — D-06: the Gallery write path no
        # longer touches them.
        case_id = "A2"
        ok = (
            post_row is not None
            and post_row["user_common_name"] is None
            and post_row["user_scientific_name"] is None
            and post_row["corrected_at"] is None
        )
        _check(case_id, ok, f"post_row={post_row}")
        if ok:
            passed += 1

        # A3 — for every species_corrections row, a join back to species on
        # detection_id yields a non-NULL label (the original AI label is
        # still readable for every corrected row, CORR-04).
        case_id = "A3"
        with database.get_conn() as conn:
            orphans = conn.execute(
                """SELECT sc.id FROM species_corrections sc
                   LEFT JOIN species s ON s.detection_id = sc.detection_id
                   WHERE s.label IS NULL"""
            ).fetchall()
        ok = len(orphans) == 0
        _check(case_id, ok, f"orphans={[dict(r) for r in orphans]}")
        if ok:
            passed += 1

        # A4 — source assertion: correct_species()'s body (sliced from
        # "def correct_species(" to the next top-level "def ", comment
        # lines stripped) contains "species_corrections" and assigns none
        # of the three frozen species columns.
        case_id = "A4"
        db_text = _database_py_text()
        cs_body = _strip_comment_lines(_slice(db_text, "def correct_species(", "\ndef "))
        has_unified = "species_corrections" in cs_body
        no_legacy_write = (
            "user_common_name=" not in cs_body
            and "user_scientific_name=" not in cs_body
            and "corrected_at=" not in cs_body
        )
        ok = bool(cs_body) and has_unified and no_legacy_write
        _check(case_id, ok, f"has_unified={has_unified}, no_legacy_write={no_legacy_write}")
        if ok:
            passed += 1

        # A5 — source assertion: the UPSERT statement is fully
        # parameterised (T-14-02) -- no `%`-formatting, `.format(` or
        # f-string interpolation of a caller-supplied value into SQL. The
        # UPSERT SQL lives in _upsert_species_correction(), not
        # correct_species() itself (correct_species() only calls it), so
        # that function's body is sliced instead, per 14-01-PLAN.md task
        # 2's instruction for this case.
        case_id = "A5"
        upsert_body = _slice(db_text, "def _upsert_species_correction(", "\ndef ")
        sql_literal = _slice(upsert_body, '"""INSERT INTO species_corrections', '""",')
        placeholder_count = sql_literal.count("?")
        bound_values_block = _slice(upsert_body, "        (\n", "\n        ),\n    )")
        bound_count = len(
            [
                ln
                for ln in bound_values_block.splitlines()
                if ln.strip().rstrip(",") and ln.strip() != "("
            ]
        )
        no_bad_interp = "%" not in sql_literal and ".format(" not in upsert_body
        ok = (
            bool(sql_literal)
            and placeholder_count > 0
            and placeholder_count == bound_count
            and no_bad_interp
        )
        _check(
            case_id,
            ok,
            f"placeholder_count={placeholder_count}, bound_count={bound_count}, "
            f"no_bad_interp={no_bad_interp} (sliced _upsert_species_correction(), "
            "not correct_species(), since that's where the UPSERT SQL lives)",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── `fanout` suite fixture ───────────────────────────────────────────────


def _seed_fanout_fixture(path):
    """Build the `fanout` suite's fixture: one camera "World Watch", one
    video carrying two "domestic cat" detections and one "Northern raccoon"
    detection — exactly the shape FO1's docstring describes. Returns a dict
    of the real autoincrement ids assigned to each row."""
    database.set_db_path(path)
    database.init_db(path)
    now = "2026-08-22T04:00:00"

    with database.get_conn() as conn:

        def _insert_video(filename):
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, ?, 1, ?, 0, ?)",
                (filename, "World Watch", now, now),
            )
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection(video_id, label, common_name, scientific_name, confidence=0.9):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', ?)",
                (video_id, confidence),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (det_id, label, common_name, scientific_name, confidence),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture14fo_crop_{det_id}.jpg", 80.0, now),
            )
            return det_id

        video = _insert_video("WorldWatch_00_videofo.mp4")
        det_cat_1 = _add_detection(video, "domestic cat", "Domestic Cat", "Felis catus")
        det_cat_2 = _add_detection(video, "domestic cat", "Domestic Cat", "Felis catus")
        det_raccoon = _add_detection(video, "Northern raccoon", "Northern Raccoon", "Procyon lotor")

    return {
        "video": video,
        "det_cat_1": det_cat_1,
        "det_cat_2": det_cat_2,
        "det_raccoon": det_raccoon,
    }


def suite_fanout():
    """`fanout` suite cases FO1-FO6 (6 total). See module docstring."""
    passed = 0
    total = 6

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "fanout.db")
        ids = _seed_fanout_fixture(db_path)
        video = ids["video"]
        det_cat_1, det_cat_2, det_raccoon = ids["det_cat_1"], ids["det_cat_2"], ids["det_raccoon"]

        def _correction_rows(det_id=None):
            with database.get_conn() as conn:
                if det_id is None:
                    rows = conn.execute("SELECT * FROM species_corrections").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM species_corrections WHERE detection_id=?", (det_id,)
                    ).fetchall()
            return [dict(r) for r in rows]

        def _gallery_item(det_id):
            items = database.get_gallery(per_page=100)["items"]
            return next((it for it in items if it.get("detection_id") == det_id), None)

        # FO1 — a video-player correction on the two "domestic cat"
        # detections creates exactly 2 species_corrections rows, both
        # source='video_player', suppressed=0, corrected_label='Northern
        # raccoon'. The sibling "Northern raccoon" detection is untouched.
        case_id = "FO1"
        applied = database.save_video_correction(
            video, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor"
        )
        rows_1 = _correction_rows(det_cat_1)
        rows_2 = _correction_rows(det_cat_2)
        rows_raccoon = _correction_rows(det_raccoon)
        ok = (
            applied == 2
            and len(rows_1) == 1
            and rows_1[0]["source"] == "video_player"
            and rows_1[0]["suppressed"] == 0
            and rows_1[0]["corrected_label"] == "Northern raccoon"
            and len(rows_2) == 1
            and rows_2[0]["source"] == "video_player"
            and rows_2[0]["suppressed"] == 0
            and rows_2[0]["corrected_label"] == "Northern raccoon"
            and len(rows_raccoon) == 0
        )
        _check(case_id, ok, f"applied={applied}, rows_1={rows_1}, rows_2={rows_2}, rows_raccoon={rows_raccoon}")
        if ok:
            passed += 1

        # FO2 — a detection inserted into the same video with the same raw
        # label AFTER the correction has no species_corrections row (D-01
        # snapshot, not a standing rule).
        case_id = "FO2"
        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video,),
            )
            det_cat_3 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, 'domestic cat', 'Domestic Cat', 'Felis catus', 0.9)",
                (det_cat_3,),
            )
        rows_3 = _correction_rows(det_cat_3)
        ok = len(rows_3) == 0
        _check(case_id, ok, f"rows_3={rows_3}")
        if ok:
            passed += 1

        # FO3 — re-saving the same video correction with different values
        # updates det_cat_1/det_cat_2's EXISTING rows in place (UPSERT, not
        # a duplicate insert — each stays at exactly one row). This fresh
        # save also now matches det_cat_3 (FO2's later-added detection) —
        # that's correct, not a regression: FO2 pinned that det_cat_3 did
        # NOT retroactively inherit FO1's already-executed correction, but
        # a brand new save_video_correction() call is a fresh operator
        # action and fans out over whatever currently matches, det_cat_3
        # included.
        case_id = "FO3"
        applied2 = database.save_video_correction(
            video, "domestic cat", "Bobcat", "Bobcat", "Lynx rufus"
        )
        rows_1_after = _correction_rows(det_cat_1)
        rows_2_after = _correction_rows(det_cat_2)
        rows_3_after = _correction_rows(det_cat_3)
        ok = (
            applied2 == 3
            and len(rows_1_after) == 1
            and rows_1_after[0]["corrected_label"] == "Bobcat"
            and len(rows_2_after) == 1
            and rows_2_after[0]["corrected_label"] == "Bobcat"
            and len(rows_3_after) == 1
            and rows_3_after[0]["corrected_label"] == "Bobcat"
        )
        _check(
            case_id, ok,
            f"applied2={applied2}, rows_1_after={rows_1_after}, "
            f"rows_2_after={rows_2_after}, rows_3_after={rows_3_after}",
        )
        if ok:
            passed += 1

        # FO4 — save_video_correction() with a video_id that does not exist
        # returns None and writes nothing.
        case_id = "FO4"
        before_count = len(_correction_rows())
        applied_missing_video = database.save_video_correction(
            999_999_999, "domestic cat", "X", "X", "X"
        )
        after_count = len(_correction_rows())
        ok = applied_missing_video is None and before_count == after_count
        _check(case_id, ok, f"applied_missing_video={applied_missing_video}, before={before_count}, after={after_count}")
        if ok:
            passed += 1

        # FO5 — save_video_correction() with an original_label matching no
        # detection on an existing video returns 0 (not None), writes
        # nothing, and raises nothing.
        case_id = "FO5"
        before_count5 = len(_correction_rows())
        applied_no_match = database.save_video_correction(
            video, "nonexistent label", "X", "X", "X"
        )
        after_count5 = len(_correction_rows())
        ok = applied_no_match == 0 and before_count5 == after_count5
        _check(case_id, ok, f"applied_no_match={applied_no_match}, before={before_count5}, after={after_count5}")
        if ok:
            passed += 1

        # FO6 — after the fan-out, get_gallery() reports the corrected
        # common/scientific name and has_correction==1 for each fanned-out
        # detection, and reports the raw values for the non-matching
        # "Northern raccoon" detection.
        case_id = "FO6"
        item_1 = _gallery_item(det_cat_1)
        item_raccoon = _gallery_item(det_raccoon)
        ok = (
            item_1 is not None
            and item_1.get("common_name") == "Bobcat"
            and item_1.get("scientific_name") == "Lynx rufus"
            and item_1.get("has_correction") == 1
            and item_raccoon is not None
            and item_raccoon.get("common_name") == "Northern Raccoon"
            and item_raccoon.get("has_correction") == 0
        )
        _check(case_id, ok, f"item_1={item_1}, item_raccoon={item_raccoon}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


SUITES = {
    "unified": (suite_unified, 7),
    "audit": (suite_audit, 5),
    "fanout": (suite_fanout, 6),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 14 verification harness (Correction Unification: Schema, Backfill & Cutover)"
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
