"""
verify_dedup_backfill.py — stdlib-only verification harness for the historical
dedup backfill (scripts/backfill_dedup_videos.py).

Drives backfill_dedup_videos.find_duplicate_groups(), group_member_ids(), and
run_audit() against a temp SQLite fixture DB built via database.init_db().
Never touches the production database file.

Usage:
    python scripts/verify_dedup_backfill.py --suite grouping|audit-readonly|all
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backfill_dedup_videos


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


def _seed_video(filename, filepath=None, camera_name=None, file_purged_at=None,
                 processed_at="2026-07-29T00:00:00", thumbnail_path=None,
                 lens_index=None, paired_video_id=None):
    """Insert one videos row via database.get_conn() with an explicit column
    list, so the fixture survives future schema additions. Returns the new
    row id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos "
            "(filename, filepath, camera_name, file_purged_at, processed_at, "
            "thumbnail_path, lens_index, paired_video_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, filepath, camera_name, file_purged_at, processed_at,
             thumbnail_path, lens_index, paired_video_id),
        )
        return cur.lastrowid


def _seed_detection(video_id, category="animal", confidence=0.9, frame_number=1,
                     timestamp_secs=1.0, bbox_json="[0,0,1,1]"):
    """Insert one detections row via database.get_conn(). Returns the new
    detection id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO detections "
            "(video_id, frame_number, timestamp_secs, category, confidence, bbox_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (video_id, frame_number, timestamp_secs, category, confidence, bbox_json),
        )
        return cur.lastrowid


def _seed_species(detection_id, label, common_name=None, scientific_name=None,
                   confidence=0.8, user_common_name=None, user_scientific_name=None,
                   corrected_at=None):
    """Insert one species row via database.get_conn(). Returns the new
    species id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO species "
            "(detection_id, label, common_name, scientific_name, confidence, "
            "user_common_name, user_scientific_name, corrected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (detection_id, label, common_name, scientific_name, confidence,
             user_common_name, user_scientific_name, corrected_at),
        )
        return cur.lastrowid


def _seed_crop(detection_id, crop_path, quality_score=50.0, created_at="2026-07-29T00:00:00"):
    """Insert one crops row via database.get_conn(). Returns the new crop id.
    Remember crops.crop_path is UNIQUE."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
            "VALUES (?, ?, ?, ?)",
            (detection_id, crop_path, quality_score, created_at),
        )
        return cur.lastrowid


def _seed_video_correction(video_id, original_label, corrected_label=None,
                            corrected_at="2026-07-29T00:00:00", note=None):
    """Insert one video_corrections row via database.get_conn(). Returns the
    new row id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO video_corrections "
            "(video_id, original_label, corrected_label, corrected_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (video_id, original_label, corrected_label, corrected_at, note),
        )
        return cur.lastrowid


def _table_snapshot():
    """Return a mapping of table name to the full sorted list of that
    table's rows as plain tuples — the equality fixture the read-only
    assertion compares."""
    tables = ["videos", "detections", "species", "crops", "video_corrections"]
    snapshot = {}
    with database.get_conn() as conn:
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            snapshot[table] = sorted(tuple(row) for row in rows)
    return snapshot


def _dir_snapshot(root):
    """Recursive listing of a directory: sorted (relative_path, size) tuples."""
    entries = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            entries.append((rel, os.path.getsize(full)))
    return sorted(entries)


def suite_grouping():
    """Six cases exercising backfill_dedup_videos.find_duplicate_groups() /
    group_member_ids() against synthetic fixture data."""
    passed = 0
    total = 6

    # 1. grouping/single-row-not-a-group
    case_id = "grouping/single-row-not-a-group"
    with _fixture_db():
        _seed_video("solo.mp4", camera_name="CamA")
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
        ok = groups == []
        _check(case_id, ok, f"groups={groups}")
        if ok:
            passed += 1

    # 2. grouping/two-way-group-found
    case_id = "grouping/two-way-group-found"
    with _fixture_db():
        id1 = _seed_video("dup.mp4", camera_name="CamA")
        id2 = _seed_video("dup.mp4", camera_name="CamA")
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
            ok_groups = len(groups) == 1
            members = (
                backfill_dedup_videos.group_member_ids(conn, "dup.mp4", "CamA")
                if ok_groups else []
            )
        ok = ok_groups and sorted(members) == sorted([id1, id2])
        _check(case_id, ok, f"groups={groups}, members={members}")
        if ok:
            passed += 1

    # 3. grouping/six-way-group-found
    case_id = "grouping/six-way-group-found"
    with _fixture_db():
        ids = [_seed_video("sixway.mp4", camera_name="worldwatch") for _ in range(6)]
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
            ok_groups = len(groups) == 1 and groups[0]["n"] == 6
            members = (
                backfill_dedup_videos.group_member_ids(conn, "sixway.mp4", "worldwatch")
                if ok_groups else []
            )
        ok = ok_groups and sorted(members) == sorted(ids)
        _check(case_id, ok, f"groups={groups}, members={members}, ids={ids}")
        if ok:
            passed += 1

    # 4. grouping/different-camera-not-grouped
    case_id = "grouping/different-camera-not-grouped"
    with _fixture_db():
        _seed_video("cross.mp4", camera_name="CamA")
        _seed_video("cross.mp4", camera_name="CamB")
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
        ok = groups == []
        _check(case_id, ok, f"groups={groups}")
        if ok:
            passed += 1

    # 5. grouping/null-camera-matches-null-only
    case_id = "grouping/null-camera-matches-null-only"
    with _fixture_db():
        _seed_video("nullmix.mp4", camera_name=None)
        _seed_video("nullmix.mp4", camera_name="CamA")
        id_n1 = _seed_video("nullpair.mp4", camera_name=None)
        id_n2 = _seed_video("nullpair.mp4", camera_name=None)
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
            mixed_group = [g for g in groups if g["filename"] == "nullmix.mp4"]
            null_group = [g for g in groups if g["filename"] == "nullpair.mp4"]
            null_members = backfill_dedup_videos.group_member_ids(conn, "nullpair.mp4", None)
        ok = (
            mixed_group == []
            and len(null_group) == 1 and null_group[0]["n"] == 2
            and sorted(null_members) == sorted([id_n1, id_n2])
        )
        _check(case_id, ok, f"groups={groups}, null_members={null_members}")
        if ok:
            passed += 1

    # 6. grouping/empty-db-zero-groups
    case_id = "grouping/empty-db-zero-groups"
    with _fixture_db():
        with database.get_conn() as conn:
            groups = backfill_dedup_videos.find_duplicate_groups(conn)
        ok = groups == []
        _check(case_id, ok, f"groups={groups}")
        if ok:
            passed += 1

    return (passed, total)


def suite_audit_readonly():
    """Two cases exercising backfill_dedup_videos.run_audit()'s read-only
    guarantee and its zero-groups baseline behavior."""
    passed = 0
    total = 2

    # 1. audit/leaves-db-unchanged
    case_id = "audit/leaves-db-unchanged"
    with _fixture_db() as db_path:
        data_dir = os.path.join(os.path.dirname(db_path), "data")
        os.makedirs(os.path.join(data_dir, "crops"), exist_ok=True)
        with open(os.path.join(data_dir, "crops", "sample.jpg"), "w") as f:
            f.write("fixture")

        v1 = _seed_video("audit1.mp4", camera_name="CamA")
        v2 = _seed_video("audit1.mp4", camera_name="CamA")
        det = _seed_detection(v1)
        _seed_species(det, "felis catus;;;cat")
        _seed_crop(det, os.path.join(data_dir, "crops", "audit1_f0000001_d00.jpg"))
        _seed_video_correction(v2, "felis catus;;;cat", corrected_label="lynx rufus;;;bobcat")

        snapshot_before = _table_snapshot()
        dir_before = _dir_snapshot(data_dir)

        with database.get_conn() as conn:
            backfill_dedup_videos.run_audit(conn)

        snapshot_after = _table_snapshot()
        dir_after = _dir_snapshot(data_dir)

        ok = snapshot_before == snapshot_after and dir_before == dir_after
        _check(
            case_id, ok,
            f"snapshot_equal={snapshot_before == snapshot_after}, "
            f"dir_equal={dir_before == dir_after}",
        )
        if ok:
            passed += 1

    # 2. audit/zero-groups-exits-clean
    case_id = "audit/zero-groups-exits-clean"
    with _fixture_db():
        _seed_video("solo1.mp4", camera_name="CamA")
        _seed_video("solo2.mp4", camera_name="CamB")
        with database.get_conn() as conn:
            metrics = backfill_dedup_videos.run_audit(conn)
        ok = isinstance(metrics, dict) and metrics.get("groups") == 0
        _check(case_id, ok, f"metrics={metrics}")
        if ok:
            passed += 1

    return (passed, total)


def main():
    parser = argparse.ArgumentParser(description="Dedup backfill verification harness")
    parser.add_argument("--suite", choices=["grouping", "audit-readonly", "all"], default="all")
    args = parser.parse_args()

    suites = {
        "grouping": suite_grouping,
        "audit-readonly": suite_audit_readonly,
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
