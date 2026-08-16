"""
verify_stale_paths.py — stdlib-only verification harness for the stale
/home/nash/ -> /home/twostar/ path migration (scripts/migrate_stale_paths.py).

Drives migrate_stale_paths.main() against a temp SQLite fixture DB built via
database.init_db(), under redirect_stdout, so the harness always tests the
real shipped CLI entry point, not a re-implementation. Never touches the
production database file.

Usage:
    python scripts/verify_stale_paths.py --suite rewrite|dry-run|apply-gate|apply|all
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database

sys.path.insert(0, str(Path(__file__).resolve().parent))

import migrate_stale_paths


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
                 processed_at="2026-08-15T00:00:00", thumbnail_path=None,
                 lens_index=None, paired_video_id=None):
    """Insert one videos row via database.get_conn(). Returns the new row id."""
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


def _seed_crop(detection_id, crop_path, quality_score=50.0, created_at="2026-08-15T00:00:00"):
    """Insert one crops row via database.get_conn(). Returns the new crop id.
    Remember crops.crop_path is UNIQUE."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
            "VALUES (?, ?, ?, ?)",
            (detection_id, crop_path, quality_score, created_at),
        )
        return cur.lastrowid


def _seed_crop_file(tmpdir, name):
    """Write a small real placeholder file under a `crops` subdirectory of
    the fixture temp directory and return its absolute path."""
    crops_dir = os.path.join(tmpdir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    path = os.path.join(crops_dir, name)
    with open(path, "w") as f:
        f.write("fixture-crop")
    return os.path.abspath(path)


def _table_snapshot():
    """Return a mapping of table name to the full sorted list of that
    table's rows as plain tuples — the equality fixture the read-only
    assertions compare."""
    tables = ["videos", "detections", "species", "crops", "video_corrections"]
    snapshot = {}
    with database.get_conn() as conn:
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            snapshot[table] = sorted(tuple(row) for row in rows)
    return snapshot


def _run_cli(argv):
    """Invoke migrate_stale_paths.main(argv) under redirect_stdout, catching
    SystemExit (treating a clean return as exit code 0). Returns
    (exit_code, stdout)."""
    buf = io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(buf):
            migrate_stale_paths.main(argv)
    except SystemExit as exc:
        exit_code = exc.code if exc.code is not None else 0
    return exit_code, buf.getvalue()


def suite_apply():
    """Tracer: one stale crops.crop_path row travels the whole CLI pipeline
    -- dry-run report, then --apply with snapshot + audit log, verifying
    the row was rewritten and every other column is untouched."""
    passed = 0
    total = 2
    try:
        # 1. apply/dry-run-reports-one-candidate-and-writes-nothing
        case_id = "apply/dry-run-reports-one-candidate-and-writes-nothing"
        with _fixture_db() as db_path:
            video_id = _seed_video("tracer.mp4", camera_name="CamA")
            det_id = _seed_detection(video_id)
            _seed_crop(det_id, "/home/nash/wildlife-monitor/data/crops/tracer.jpg")

            snapshot_before = _table_snapshot()
            exit_code, output = _run_cli(["--db", db_path])
            snapshot_after = _table_snapshot()

            ok = (
                exit_code == 0
                and "Rows that would be rewritten: 1" in output
                and snapshot_before == snapshot_after
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, snapshot_equal={snapshot_before == snapshot_after}, "
                f"output={output[:300]!r}",
            )
            if ok:
                passed += 1

        # 2. apply/apply-rewrites-row-and-leaves-other-columns-untouched
        case_id = "apply/apply-rewrites-row-and-leaves-other-columns-untouched"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            video_id = _seed_video("tracer.mp4", camera_name="CamA")
            det_id = _seed_detection(video_id)
            crop_id = _seed_crop(det_id, "/home/nash/wildlife-monitor/data/crops/tracer.jpg")

            with database.get_conn() as conn:
                before_row = conn.execute("SELECT * FROM crops WHERE id=?", (crop_id,)).fetchone()
                before_cols = before_row.keys()

            snapshot_dir = os.path.join(tmpdir, "snap")
            audit_log = os.path.join(tmpdir, "audit.jsonl")

            exit_code, output = _run_cli([
                "--db", db_path, "--apply", "--confirm-irreversible",
                "--snapshot-dir", snapshot_dir, "--audit-log", audit_log,
            ])

            with database.get_conn() as conn:
                after_row = conn.execute("SELECT * FROM crops WHERE id=?", (crop_id,)).fetchone()

            new_path_ok = after_row["crop_path"] == "/home/twostar/wildlife-monitor/data/crops/tracer.jpg"
            other_cols_ok = all(
                before_row[c] == after_row[c] for c in before_cols if c != "crop_path"
            )

            snapshot_files = os.listdir(snapshot_dir) if os.path.isdir(snapshot_dir) else []
            snapshot_ok = len(snapshot_files) == 1 and snapshot_files[0].endswith(".db")

            audit_lines_ok = False
            if os.path.isfile(audit_log):
                with open(audit_log) as f:
                    lines = [json.loads(line) for line in f if line.strip()]
                required_keys = {"table", "column", "row_id", "old_path", "new_path"}
                audit_lines_ok = any(required_keys <= set(line.keys()) for line in lines)

            ok = exit_code == 0 and new_path_ok and other_cols_ok and snapshot_ok and audit_lines_ok
            _check(
                case_id, ok,
                f"exit_code={exit_code}, new_path_ok={new_path_ok}, other_cols_ok={other_cols_ok}, "
                f"snapshot_ok={snapshot_ok}, audit_lines_ok={audit_lines_ok}",
            )
            if ok:
                passed += 1
    except Exception as exc:
        print(f"FAIL: suite_apply raised {exc!r}")

    return (passed, total)


def build_parser():
    parser = argparse.ArgumentParser(description="Stale path migration verification harness")
    parser.add_argument("--suite", choices=["apply", "all"], default="all")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    suites = {
        "apply": suite_apply,
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
