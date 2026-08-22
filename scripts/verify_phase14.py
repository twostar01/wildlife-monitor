"""
verify_phase14.py — stdlib-only verification harness for Phase 14
(Correction Unification: Schema, Backfill & Cutover), plan 14-01.

Suites:
    unified — CORR-01/D-00, proves a Gallery-popover correction travels
              correct_species() -> species_corrections -> EFFECTIVE_COMMON ->
              get_gallery()/get_species_detail() end to end: the raw-value
              baseline, a successful correction, the two readers agreeing,
              the UNIQUE(detection_id) upsert (most-recent-write-wins), the
              clear-correction path, the blank-common/set-scientific edge
              case, and the unknown-detection-id / clear-an-already-clear-
              correction 404-contract edge cases (U1-U7).
    audit   — CORR-04/D-06, pins the audit trail (species.label stays
              byte-identical, and every species_corrections row joins back
              to a non-NULL AI label) and the legacy-column freeze (the
              Gallery write path never touches species.user_common_name/
              user_scientific_name/corrected_at), as both fixture behaviour
              assertions and region-scoped source assertions over the
              write-path functions' bodies (A1-A5).

Follows scripts/verify_phase12.py's structure (itself following
scripts/verify_phase10.py's): a `_check(case_id, condition, detail)`
helper, per-suite `(passed, total)` returns, a dict suite registry, argparse
`--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary lines, and
`sys.exit(0 if all_passed else 1)`.

Uses its own `_seed_unified_fixture()` rather than importing
verify_phase12's — this suite's fixture shape (raccoon/unknown pair on one
video, two domestic-cat detections on a second) is specific to the unified-
table correction cases and unrelated to verify_phase12's badge/propagation
fixture. The `audit` suite reuses this same fixture.

Usage:
    python scripts/verify_phase14.py --suite unified|audit|all
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


SUITES = {
    "unified": (suite_unified, 7),
    "audit": (suite_audit, 5),
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
