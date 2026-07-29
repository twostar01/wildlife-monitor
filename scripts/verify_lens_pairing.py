"""
verify_lens_pairing.py — stdlib-only verification harness for dual-lens pairing.

Drives database.link_lens_pair, database._repair_lens_pairings, and
database.check_pairing_consistency against a temp SQLite fixture DB built via
database.init_db(). Never touches the production database file.

Usage:
    python scripts/verify_lens_pairing.py --suite link|repair|consistency|all
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database

_results = []


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    _results.append((case_id, bool(condition)))
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


def _insert_video(filename, filepath, lens_index=None, paired_video_id=None) -> int:
    """Insert one videos row via database.get_conn(). Returns the new row id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (filename, filepath, processed_at, lens_index, paired_video_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (filename, filepath, "2026-07-29T00:00:00", lens_index, paired_video_id),
        )
        return cur.lastrowid


def suite_link():
    """Six cases against database.link_lens_pair — see PLAN.md task 1 for spec."""
    passed = 0
    total = 6
    try:
        # 1. link/single-candidate-links-both-sides
        case_id = "link/single-candidate-links-both-sides"
        with _fixture_db() as db_path:
            id0 = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4"
            )
            id1 = _insert_video(
                "World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4"
            )
            result = database.link_lens_pair(id1, "World Watch_01_20260327160902.mp4")
            with database.get_conn() as conn:
                row0 = conn.execute(
                    "SELECT lens_index, paired_video_id FROM videos WHERE id=?", (id0,)
                ).fetchone()
                row1 = conn.execute(
                    "SELECT lens_index, paired_video_id FROM videos WHERE id=?", (id1,)
                ).fetchone()
            ok = (
                result == id0
                and row0["paired_video_id"] == id1
                and row1["paired_video_id"] == id0
                and row0["lens_index"] == 0
                and row1["lens_index"] == 1
            )
            _check(case_id, ok, f"result={result}, row0={dict(row0)}, row1={dict(row1)}")
            if ok:
                passed += 1

        # 2. link/ambiguous-group-left-unpaired
        case_id = "link/ambiguous-group-left-unpaired"
        with _fixture_db() as db_path:
            _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0a.mp4")
            id0b = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.v0b.mp4"
            )
            id1 = _insert_video(
                "World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4"
            )
            result = database.link_lens_pair(id1, "World Watch_01_20260327160902.mp4")
            with database.get_conn() as conn:
                row1 = conn.execute(
                    "SELECT lens_index, paired_video_id FROM videos WHERE id=?", (id1,)
                ).fetchone()
                rows0 = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE lens_index=0 OR id=?",
                    (id0b,),
                ).fetchall()
            ok = (
                result is None
                and row1["paired_video_id"] is None
                and row1["lens_index"] == 1
                and all(r["paired_video_id"] is None for r in rows0)
            )
            _check(case_id, ok, f"result={result}, row1={dict(row1)}")
            if ok:
                passed += 1

        # 3. link/no-partner-records-lens-only
        case_id = "link/no-partner-records-lens-only"
        with _fixture_db() as db_path:
            id0 = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4")
            result = database.link_lens_pair(id0, "World Watch_00_20260327160902.mp4")
            with database.get_conn() as conn:
                row0 = conn.execute(
                    "SELECT lens_index, paired_video_id FROM videos WHERE id=?", (id0,)
                ).fetchone()
            ok = result is None and row0["paired_video_id"] is None and row0["lens_index"] == 0
            _check(case_id, ok, f"result={result}, row0={dict(row0)}")
            if ok:
                passed += 1

        # 4. link/candidate-already-paired-elsewhere-not-stolen
        case_id = "link/candidate-already-paired-elsewhere-not-stolen"
        with _fixture_db() as db_path:
            id_a = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.a.mp4")
            id_b = _insert_video(
                "World Watch_01_20260327160902.mp4", f"{db_path}.b.mp4",
                lens_index=1, paired_video_id=id_a,
            )
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET paired_video_id=?, lens_index=? WHERE id=?",
                    (id_b, 0, id_a),
                )
            id_c = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.c.mp4"
            )
            result = database.link_lens_pair(id_c, "World Watch_00_20260327160902.mp4")
            with database.get_conn() as conn:
                row_c = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id_c,)
                ).fetchone()
                row_a = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id_a,)
                ).fetchone()
                row_b = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id_b,)
                ).fetchone()
            ok = (
                result is None
                and row_c["paired_video_id"] is None
                and row_a["paired_video_id"] == id_b
                and row_b["paired_video_id"] == id_a
            )
            _check(case_id, ok, f"result={result}, a={dict(row_a)}, b={dict(row_b)}, c={dict(row_c)}")
            if ok:
                passed += 1

        # 5. link/like-metacharacter-camera-base
        case_id = "link/like-metacharacter-camera-base"
        with _fixture_db() as db_path:
            id0 = _insert_video(
                "Back_Wall%Cam_00_20260418155240.mp4", f"{db_path}.w0.mp4"
            )
            id1 = _insert_video(
                "Back_Wall%Cam_01_20260418155240.mp4", f"{db_path}.w1.mp4"
            )
            _insert_video("OtherCam_01_20260418155240.mp4", f"{db_path}.decoy.mp4")
            result = database.link_lens_pair(id1, "Back_Wall%Cam_01_20260418155240.mp4")
            ok = result == id0
            _check(case_id, ok, f"result={result}, expected={id0}")
            if ok:
                passed += 1

        # 6. link/idempotent-rerun
        case_id = "link/idempotent-rerun"
        with _fixture_db() as db_path:
            id0 = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4"
            )
            id1 = _insert_video(
                "World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4"
            )
            database.link_lens_pair(id1, "World Watch_01_20260327160902.mp4")
            with database.get_conn() as conn:
                before0 = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id0,)
                ).fetchone()["paired_video_id"]
                before1 = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id1,)
                ).fetchone()["paired_video_id"]
            database.link_lens_pair(id1, "World Watch_01_20260327160902.mp4")
            with database.get_conn() as conn:
                after0 = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id0,)
                ).fetchone()["paired_video_id"]
                after1 = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (id1,)
                ).fetchone()["paired_video_id"]
            ok = before0 == after0 and before1 == after1
            _check(case_id, ok, f"before=({before0},{before1}), after=({after0},{after1})")
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: link/function-missing")
        return (0, total)

    return (passed, total)


def suite_repair():
    """Six cases against database._repair_lens_pairings(conn)."""
    passed = 0
    total = 6
    try:
        target = database._repair_lens_pairings

        # 1. repair/links-missing-pair
        case_id = "repair/links-missing-pair"
        with _fixture_db() as db_path:
            id0 = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4")
            id1 = _insert_video("World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4")
            with database.get_conn() as conn:
                summary = target(conn)
            with database.get_conn() as conn:
                row0 = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id0,)).fetchone()
                row1 = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id1,)).fetchone()
            ok = (
                summary["linked"] == 1
                and row0["paired_video_id"] == id1
                and row1["paired_video_id"] == id0
            )
            _check(case_id, ok, f"summary={summary}, row0={dict(row0)}, row1={dict(row1)}")
            if ok:
                passed += 1

        # 2. repair/leaves-correct-pair-untouched
        case_id = "repair/leaves-correct-pair-untouched"
        with _fixture_db() as db_path:
            id0 = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4", lens_index=0)
            id1 = _insert_video(
                "World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4",
                lens_index=1, paired_video_id=id0,
            )
            with database.get_conn() as conn:
                conn.execute("UPDATE videos SET paired_video_id=? WHERE id=?", (id1, id0))
            with database.get_conn() as conn:
                summary = target(conn)
            with database.get_conn() as conn:
                row0 = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id0,)).fetchone()
                row1 = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id1,)).fetchone()
            ok = (
                summary["linked"] == 0
                and summary["unlinked"] == 0
                and row0["paired_video_id"] == id1
                and row1["paired_video_id"] == id0
            )
            _check(case_id, ok, f"summary={summary}")
            if ok:
                passed += 1

        # 3. repair/relinks-wrong-pointer
        case_id = "repair/relinks-wrong-pointer"
        with _fixture_db() as db_path:
            id_d = _insert_video("Front door_00_20260101120000.mp4", f"{db_path}.d.mp4")
            id_a = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.a.mp4",
                lens_index=0, paired_video_id=id_d,
            )
            id_b = _insert_video("World Watch_01_20260327160902.mp4", f"{db_path}.b.mp4")
            with database.get_conn() as conn:
                summary = target(conn)
            with database.get_conn() as conn:
                row_a = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id_a,)).fetchone()
                row_b = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id_b,)).fetchone()
                row_d = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id_d,)).fetchone()
            ok = (
                row_a["paired_video_id"] == id_b
                and row_b["paired_video_id"] == id_a
                and summary["linked"] == 1
                and row_d["paired_video_id"] is None
            )
            _check(case_id, ok, f"summary={summary}, a={dict(row_a)}, b={dict(row_b)}, d={dict(row_d)}")
            if ok:
                passed += 1

        # 4. repair/ambiguous-group-cleared
        case_id = "repair/ambiguous-group-cleared"
        with _fixture_db() as db_path:
            id0a = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0a.mp4")
            id0b = _insert_video(
                "World Watch_00_20260327160902.mp4", f"{db_path}.v0b.mp4",
                lens_index=0,
            )
            id1 = _insert_video("World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4")
            with database.get_conn() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    "UPDATE videos SET paired_video_id=999999 WHERE id=?", (id0b,)
                )
            with database.get_conn() as conn:
                summary = target(conn)
            with database.get_conn() as conn:
                rows = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id IN (?, ?, ?)",
                    (id0a, id0b, id1),
                ).fetchall()
            ok = (
                all(r["paired_video_id"] is None for r in rows)
                and summary["ambiguous_groups"] == 1
                and summary["unlinked"] >= 1
            )
            _check(case_id, ok, f"summary={summary}, rows={[dict(r) for r in rows]}")
            if ok:
                passed += 1

        # 5. repair/singleton-not-counted-ambiguous
        case_id = "repair/singleton-not-counted-ambiguous"
        with _fixture_db() as db_path:
            id0 = _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4")
            with database.get_conn() as conn:
                summary = target(conn)
            with database.get_conn() as conn:
                row0 = conn.execute("SELECT paired_video_id FROM videos WHERE id=?", (id0,)).fetchone()
            ok = row0["paired_video_id"] is None and summary["ambiguous_groups"] == 0
            _check(case_id, ok, f"summary={summary}, row0={dict(row0)}")
            if ok:
                passed += 1

        # 6. repair/idempotent
        case_id = "repair/idempotent"
        with _fixture_db() as db_path:
            _insert_video("World Watch_00_20260327160902.mp4", f"{db_path}.v0.mp4")
            _insert_video("World Watch_01_20260327160902.mp4", f"{db_path}.v1.mp4")
            with database.get_conn() as conn:
                target(conn)
            with database.get_conn() as conn:
                summary2 = target(conn)
            ok = summary2["linked"] == 0 and summary2["unlinked"] == 0
            _check(case_id, ok, f"summary2={summary2}")
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: repair/function-missing")
        return (0, total)

    return (passed, total)


def suite_consistency():
    """Three cases against database.check_pairing_consistency()."""
    passed = 0
    total = 3
    try:
        target = database.check_pairing_consistency

        # 1. consistency/clean-db-returns-zero
        case_id = "consistency/clean-db-returns-zero"
        with _fixture_db():
            id0 = _insert_video("World Watch_00_20260327160902.mp4", "clean0.mp4")
            id1 = _insert_video(
                "World Watch_01_20260327160902.mp4", "clean1.mp4",
                lens_index=1, paired_video_id=id0,
            )
            with database.get_conn() as conn:
                conn.execute("UPDATE videos SET paired_video_id=? WHERE id=?", (id1, id0))
            result = target()
            ok = result == 0
            _check(case_id, ok, f"result={result}")
            if ok:
                passed += 1

        # 2. consistency/asymmetric-pointer-detected
        case_id = "consistency/asymmetric-pointer-detected"
        with _fixture_db():
            id_a = _insert_video("World Watch_00_20260327160902.mp4", "asym0.mp4")
            id_b = _insert_video(
                "World Watch_01_20260327160902.mp4", "asym1.mp4",
                paired_video_id=id_a,
            )
            result = target()
            ok = result == 1
            _check(case_id, ok, f"result={result}")
            if ok:
                passed += 1

        # 3. consistency/dangling-pointer-detected
        case_id = "consistency/dangling-pointer-detected"
        with _fixture_db():
            with database.get_conn() as conn:
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute(
                    "INSERT INTO videos (filename, filepath, processed_at, paired_video_id) "
                    "VALUES (?, ?, ?, ?)",
                    ("World Watch_00_20260327160902.mp4", "dangling0.mp4", "2026-07-29T00:00:00", 999999),
                )
            result = target()
            ok = result == 1
            _check(case_id, ok, f"result={result}")
            if ok:
                passed += 1

    except AttributeError:
        print("FAIL: consistency/function-missing")
        return (0, total)

    return (passed, total)


def main():
    parser = argparse.ArgumentParser(description="Dual-lens pairing verification harness")
    parser.add_argument(
        "--suite", choices=["link", "repair", "consistency", "all"], default="all"
    )
    args = parser.parse_args()

    suites = {
        "link": suite_link,
        "repair": suite_repair,
        "consistency": suite_consistency,
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
