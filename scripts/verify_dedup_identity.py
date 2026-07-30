"""
verify_dedup_identity.py — stdlib-only verification harness for file-identity dedup.

Drives database.find_existing_video, database.find_archived_duplicate, and
database.insert_video against a temp SQLite fixture DB built via
database.init_db(). Never touches the production database file.

Usage:
    python scripts/verify_dedup_identity.py --suite identity|archived-dup|insert|all
"""

import argparse
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


@contextmanager
def _fixture_db():
    """Create an isolated temp DB with a real schema via database.init_db()."""
    tmpdir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmpdir, "test.db")
        database.init_db(db_path)
        yield db_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _raw_insert(filename, filepath, camera_name=None, file_purged_at=None,
                 processed_at="2026-07-29T00:00:00"):
    """Insert one videos row via database.get_conn() with an explicit column
    list, so the fixture survives future schema additions. Returns the new
    row id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (filename, filepath, camera_name, processed_at, file_purged_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (filename, filepath, camera_name, processed_at, file_purged_at),
        )
        return cur.lastrowid


def _row_count():
    with database.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def suite_identity():
    """Eight cases against database.find_existing_video."""
    passed = 0
    total = 8
    try:
        target = database.find_existing_video

        # 1. identity/exact-match-returns-row
        case_id = "identity/exact-match-returns-row"
        with _fixture_db():
            id0 = _raw_insert("cam_20260729.mp4", "/staging/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamA")
            ok = result is not None and result["id"] == id0
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id0}")
            if ok:
                passed += 1

        # 2. identity/different-camera-no-match
        case_id = "identity/different-camera-no-match"
        with _fixture_db():
            _raw_insert("cam_20260729.mp4", "/staging/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamB")
            ok = result is None
            _check(case_id, ok, f"result={dict(result) if result else None}")
            if ok:
                passed += 1

        # 3. identity/null-camera-matches-null
        case_id = "identity/null-camera-matches-null"
        with _fixture_db():
            id0 = _raw_insert("cam_20260729.mp4", "/staging/cam_20260729.mp4", None)
            result = target("cam_20260729.mp4", None)
            ok = result is not None and result["id"] == id0
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id0}")
            if ok:
                passed += 1

        # 4. identity/empty-string-camera-distinct-from-null
        case_id = "identity/empty-string-camera-distinct-from-null"
        with _fixture_db():
            id_empty = _raw_insert("cam_20260729.mp4", "/staging/empty.mp4", "")
            id_null = _raw_insert("cam_20260729.mp4", "/staging/null.mp4", None)
            result_empty = target("cam_20260729.mp4", "")
            result_null = target("cam_20260729.mp4", None)
            ok = (
                result_empty is not None and result_empty["id"] == id_empty
                and result_null is not None and result_null["id"] == id_null
            )
            _check(
                case_id, ok,
                f"result_empty={dict(result_empty) if result_empty else None}, "
                f"result_null={dict(result_null) if result_null else None}",
            )
            if ok:
                passed += 1

        # 5. identity/no-match-returns-none
        case_id = "identity/no-match-returns-none"
        with _fixture_db():
            result = target("nonexistent.mp4", "CamA")
            ok = result is None
            _check(case_id, ok, f"result={dict(result) if result else None}")
            if ok:
                passed += 1

        # 6. identity/multiple-matches-prefers-live-filepath
        case_id = "identity/multiple-matches-prefers-live-filepath"
        with _fixture_db():
            id_null = _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at="2026-07-28T00:00:00")
            id_live = _raw_insert("cam_20260729.mp4", "/nas/archive/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamA")
            ok = result is not None and result["id"] == id_live
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id_live}, id_null={id_null}")
            if ok:
                passed += 1

        # 7. identity/multiple-matches-stable-lowest-id
        case_id = "identity/multiple-matches-stable-lowest-id"
        with _fixture_db():
            id_a = _raw_insert("cam_20260729.mp4", None, "CamA")
            id_b = _raw_insert("cam_20260729.mp4", None, "CamA")
            r1 = target("cam_20260729.mp4", "CamA")
            r2 = target("cam_20260729.mp4", "CamA")
            r3 = target("cam_20260729.mp4", "CamA")
            lowest = min(id_a, id_b)
            ok = (
                r1 is not None and r1["id"] == lowest
                and r2 is not None and r2["id"] == lowest
                and r3 is not None and r3["id"] == lowest
            )
            _check(
                case_id, ok,
                f"r1={dict(r1) if r1 else None}, r2={dict(r2) if r2 else None}, "
                f"r3={dict(r3) if r3 else None}, expected={lowest}",
            )
            if ok:
                passed += 1

        # 8. identity/like-metacharacter-filename
        case_id = "identity/like-metacharacter-filename"
        with _fixture_db():
            id_target = _raw_insert("cam_100%_2026.mp4", "/staging/cam_100pct.mp4", "CamA")
            # Decoy whose filename a LIKE pattern built from the first would match
            # (e.g. "cam_100%_2026.mp4" as a LIKE pattern matches "cam_100XYZ_2026.mp4").
            _raw_insert("cam_100XYZ_2026.mp4", "/staging/decoy.mp4", "CamA")
            result = target("cam_100%_2026.mp4", "CamA")
            ok = result is not None and result["id"] == id_target
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id_target}")
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: identity/function-missing")
        return (0, total)

    return (passed, total)


def suite_archived_dup():
    """Six cases against database.find_archived_duplicate."""
    passed = 0
    total = 6
    try:
        target = database.find_archived_duplicate

        # 1. archived-dup/archived-elsewhere-returns-row
        case_id = "archived-dup/archived-elsewhere-returns-row"
        with _fixture_db():
            id0 = _raw_insert("cam_20260729.mp4", "/nas/archive/worldwatch/2026/07/29/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            ok = result is not None and result["id"] == id0
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id0}")
            if ok:
                passed += 1

        # 2. archived-dup/purged-row-returns-row
        case_id = "archived-dup/purged-row-returns-row"
        with _fixture_db():
            id0 = _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at="2026-07-28T00:00:00")
            result = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            ok = result is not None and result["id"] == id0
            _check(case_id, ok, f"result={dict(result) if result else None}, expected id={id0}")
            if ok:
                passed += 1

        # 3. archived-dup/same-path-returns-none
        case_id = "archived-dup/same-path-returns-none"
        with _fixture_db():
            _raw_insert("cam_20260729.mp4", "/local/staging/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            ok = result is None
            _check(case_id, ok, f"result={dict(result) if result else None}")
            if ok:
                passed += 1

        # 4. archived-dup/no-row-returns-none
        case_id = "archived-dup/no-row-returns-none"
        with _fixture_db():
            result = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            ok = result is None
            _check(case_id, ok, f"result={dict(result) if result else None}")
            if ok:
                passed += 1

        # 5. archived-dup/different-camera-returns-none
        case_id = "archived-dup/different-camera-returns-none"
        with _fixture_db():
            _raw_insert("cam_20260729.mp4", "/nas/archive/cam_20260729.mp4", "CamA")
            result = target("cam_20260729.mp4", "CamB", "/local/staging/cam_20260729.mp4")
            ok = result is None
            _check(case_id, ok, f"result={dict(result) if result else None}")
            if ok:
                passed += 1

        # 6. archived-dup/deterministic-on-multiple-matches
        case_id = "archived-dup/deterministic-on-multiple-matches"
        with _fixture_db():
            # Production-shaped 4-member group for one identity: one blanks-archive
            # path, one main-archive path, two filepath-NULL rows.
            _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at="2026-07-26T00:00:00")
            _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at="2026-07-27T00:00:00")
            id_blanks = _raw_insert("cam_20260729.mp4", "/nas/archive/blanks/cam_20260729.mp4", "CamA")
            _raw_insert("cam_20260729.mp4", "/nas/archive/main/cam_20260729.mp4", "CamA")
            r1 = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            r2 = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            r3 = target("cam_20260729.mp4", "CamA", "/local/staging/cam_20260729.mp4")
            ok = (
                r1 is not None and r2 is not None and r3 is not None
                and r1["id"] == r2["id"] == r3["id"]
            )
            _check(
                case_id, ok,
                f"r1={dict(r1) if r1 else None}, r2={dict(r2) if r2 else None}, r3={dict(r3) if r3 else None}",
            )
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: archived-dup/function-missing")
        return (0, total)

    return (passed, total)


def suite_insert():
    """Ten cases against database.insert_video."""
    passed = 0
    total = 10
    try:
        target = database.insert_video

        def _call(filename, filepath, camera_name="CamA", has_animal=False, has_person=False,
                   kept=False, thumbnail_path=None, frame_count=0, lens_index=None,
                   file_size_mb=1.0, duration_secs=10.0, recorded_at="2026-07-29T00:00:00"):
            return target(
                filename, filepath, camera_name, file_size_mb, duration_secs, recorded_at,
                has_animal, has_person, kept, thumbnail_path, frame_count, lens_index,
            )

        # 1. insert/new-video-creates-row
        case_id = "insert/new-video-creates-row"
        with _fixture_db():
            new_id = _call("cam_new.mp4", "/local/staging/cam_new.mp4")
            ok = _row_count() == 1 and new_id is not None
            _check(case_id, ok, f"row_count={_row_count()}, new_id={new_id}")
            if ok:
                passed += 1

        # 2. insert/identity-match-updates-existing-no-new-row
        case_id = "insert/identity-match-updates-existing-no-new-row"
        with _fixture_db():
            existing_id = _raw_insert(
                "cam_20260729.mp4", "/nas/archive/worldwatch/2026/07/29/cam_20260729.mp4", "CamA"
            )
            result_id = _call("cam_20260729.mp4", "/local/staging/cam_20260729.mp4", camera_name="CamA")
            ok = _row_count() == 1 and result_id == existing_id
            _check(case_id, ok, f"row_count={_row_count()}, result_id={result_id}, expected={existing_id}")
            if ok:
                passed += 1

        # 3. insert/identity-match-preserves-archived-filepath
        case_id = "insert/identity-match-preserves-archived-filepath"
        with _fixture_db():
            archive_path = "/nas/archive/worldwatch/2026/07/29/cam_20260729.mp4"
            existing_id = _raw_insert("cam_20260729.mp4", archive_path, "CamA")
            _call("cam_20260729.mp4", "/local/staging/cam_20260729.mp4", camera_name="CamA")
            with database.get_conn() as conn:
                row = conn.execute("SELECT filepath FROM videos WHERE id=?", (existing_id,)).fetchone()
            ok = row["filepath"] == archive_path
            _check(case_id, ok, f"filepath={row['filepath']}, expected={archive_path}")
            if ok:
                passed += 1

        # 4. insert/identity-match-updates-metadata
        case_id = "insert/identity-match-updates-metadata"
        with _fixture_db():
            existing_id = _raw_insert(
                "cam_20260729.mp4", "/nas/archive/worldwatch/2026/07/29/cam_20260729.mp4", "CamA"
            )
            _call(
                "cam_20260729.mp4", "/local/staging/cam_20260729.mp4", camera_name="CamA",
                has_animal=True, kept=True, frame_count=42, thumbnail_path="/thumbs/new.jpg",
                duration_secs=99.5,
            )
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT has_animal, kept, frame_count, thumbnail_path, duration_secs "
                    "FROM videos WHERE id=?", (existing_id,)
                ).fetchone()
            ok = (
                row["has_animal"] == 1 and row["kept"] == 1 and row["frame_count"] == 42
                and row["thumbnail_path"] == "/thumbs/new.jpg" and row["duration_secs"] == 99.5
            )
            _check(case_id, ok, f"row={dict(row)}")
            if ok:
                passed += 1

        # 5. insert/purged-row-not-resurrected
        case_id = "insert/purged-row-not-resurrected"
        with _fixture_db():
            purge_time = "2026-07-28T00:00:00"
            existing_id = _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at=purge_time)
            _call("cam_20260729.mp4", "/local/staging/cam_20260729.mp4", camera_name="CamA")
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT filepath, file_purged_at FROM videos WHERE id=?", (existing_id,)
                ).fetchone()
            ok = row["filepath"] is None and row["file_purged_at"] == purge_time
            _check(case_id, ok, f"row={dict(row)}")
            if ok:
                passed += 1

        # 6. insert/located-row-gets-path
        case_id = "insert/located-row-gets-path"
        with _fixture_db():
            existing_id = _raw_insert("cam_20260729.mp4", None, "CamA", file_purged_at=None)
            _call("cam_20260729.mp4", "/local/staging/cam_20260729.mp4", camera_name="CamA")
            with database.get_conn() as conn:
                row = conn.execute("SELECT filepath FROM videos WHERE id=?", (existing_id,)).fetchone()
            ok = row["filepath"] == "/local/staging/cam_20260729.mp4"
            _check(case_id, ok, f"row={dict(row)}")
            if ok:
                passed += 1

        # 7. insert/same-path-update-in-place
        case_id = "insert/same-path-update-in-place"
        with _fixture_db():
            path = "/local/staging/cam_20260729.mp4"
            existing_id = _raw_insert("cam_20260729.mp4", path, "CamA")
            result_id = _call("cam_20260729.mp4", path, camera_name="CamA")
            ok = _row_count() == 1 and result_id == existing_id
            _check(case_id, ok, f"row_count={_row_count()}, result_id={result_id}, expected={existing_id}")
            if ok:
                passed += 1

        # 8. insert/null-camera-name
        case_id = "insert/null-camera-name"
        with _fixture_db():
            _call("cam_nocam.mp4", "/local/staging/v1.mp4", camera_name=None)
            _call("cam_nocam.mp4", "/local/staging/v2.mp4", camera_name=None)
            ok = _row_count() == 1
            _check(case_id, ok, f"row_count={_row_count()}")
            if ok:
                passed += 1

        # 9. insert/idempotent-repeat
        case_id = "insert/idempotent-repeat"
        with _fixture_db():
            id1 = _call("cam_repeat.mp4", "/local/staging/cam_repeat.mp4", camera_name="CamA")
            id2 = _call("cam_repeat.mp4", "/local/staging/cam_repeat.mp4", camera_name="CamA")
            ok = _row_count() == 1 and id1 == id2
            _check(case_id, ok, f"row_count={_row_count()}, id1={id1}, id2={id2}")
            if ok:
                passed += 1

        # 10. insert/distinct-filenames-create-distinct-rows
        case_id = "insert/distinct-filenames-create-distinct-rows"
        with _fixture_db():
            _call("cam_a.mp4", "/local/staging/cam_a.mp4", camera_name="CamA")
            _call("cam_b.mp4", "/local/staging/cam_b.mp4", camera_name="CamA")
            ok = _row_count() == 2
            _check(case_id, ok, f"row_count={_row_count()}")
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: insert/function-missing")
        return (0, total)

    return (passed, total)


def main():
    parser = argparse.ArgumentParser(description="File-identity dedup verification harness")
    parser.add_argument(
        "--suite", choices=["identity", "archived-dup", "insert", "all"], default="all"
    )
    args = parser.parse_args()

    suites = {
        "identity": suite_identity,
        "archived-dup": suite_archived_dup,
        "insert": suite_insert,
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
