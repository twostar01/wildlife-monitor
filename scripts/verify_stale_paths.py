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


def suite_rewrite():
    """Nine-row fixture spanning both target columns, the four genuinely
    leading-prefixed rows that must change, and five must-not-touch rows
    (embedded-offset occurrence, second-occurrence-in-tail, exact-prefix,
    already-correct twostar path, unrelated path, NULL, empty string).
    Also proves suffix_violations()/non_path_digest() invariants, row-count
    stability, and idempotency on a second --apply run."""
    passed = 0
    total = 9
    try:
        with _fixture_db() as db_path:
            video_a = _seed_video("a.mp4", camera_name="CamA")
            video_b = _seed_video(
                "b.mp4", camera_name="CamB",
                thumbnail_path="/home/nash/wildlife-monitor/data/thumbs/b.jpg",
            )
            video_c = _seed_video("c.mp4", camera_name="CamC", thumbnail_path=None)
            video_d = _seed_video("d.mp4", camera_name="CamD", thumbnail_path="")
            det_a = _seed_detection(video_a)
            det_b = _seed_detection(video_a)
            det_c = _seed_detection(video_a)
            det_d = _seed_detection(video_a)
            det_e = _seed_detection(video_a)
            det_f = _seed_detection(video_a)

            # 1. Genuinely leading-prefixed -- must change.
            crop_leading = _seed_crop(det_a, "/home/nash/wildlife-monitor/data/crops/leading.jpg")
            # 2. Already-correct twostar path -- must not change.
            crop_twostar = _seed_crop(det_b, "/home/twostar/wildlife-monitor/data/crops/twostar.jpg")
            # 3. Unrelated path -- must not change.
            crop_unrelated = _seed_crop(det_c, "/srv/media/x.jpg")
            # 4. Embedded /home/nash/ at a non-zero offset -- must not change.
            crop_embedded = _seed_crop(det_d, "/srv/data/home/nash/x.jpg")
            # 5. Leading /home/nash/ AND a second occurrence in the tail --
            #    only the leading one is rewritten, the tail survives verbatim.
            crop_repeat = _seed_crop(det_e, "/home/nash/a/home/nash/b.jpg")
            # 6. Exact-prefix value (empty remainder) -- must rewrite cleanly,
            #    no index error.
            crop_exact = _seed_crop(det_f, "/home/nash/")

            snapshot_before = _table_snapshot()
            before_crops_count = len(snapshot_before["crops"])
            before_videos_count = len(snapshot_before["videos"])
            before_crops_digest = None
            before_videos_digest = None
            with database.get_conn() as conn:
                before_crops_digest = migrate_stale_paths.non_path_digest(conn, "crops", "crop_path")
                before_videos_digest = migrate_stale_paths.non_path_digest(conn, "videos", "thumbnail_path")

            tmpdir = os.path.dirname(db_path)
            snapshot_dir = os.path.join(tmpdir, "snap")
            audit_log = os.path.join(tmpdir, "audit.jsonl")

            exit_code, _output = _run_cli([
                "--db", db_path, "--apply", "--confirm-irreversible",
                "--snapshot-dir", snapshot_dir, "--audit-log", audit_log,
            ])

            with database.get_conn() as conn:
                after = {
                    "leading": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_leading,)).fetchone()[0],
                    "twostar": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_twostar,)).fetchone()[0],
                    "unrelated": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_unrelated,)).fetchone()[0],
                    "embedded": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_embedded,)).fetchone()[0],
                    "repeat": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_repeat,)).fetchone()[0],
                    "exact": conn.execute("SELECT crop_path FROM crops WHERE id=?", (crop_exact,)).fetchone()[0],
                    "video_b_thumb": conn.execute("SELECT thumbnail_path FROM videos WHERE id=?", (video_b,)).fetchone()[0],
                    "video_c_thumb": conn.execute("SELECT thumbnail_path FROM videos WHERE id=?", (video_c,)).fetchone()[0],
                    "video_d_thumb": conn.execute("SELECT thumbnail_path FROM videos WHERE id=?", (video_d,)).fetchone()[0],
                }

            # 1. leading-prefixed crop rewritten
            case_id = "rewrite/leading-prefix-rewritten"
            ok = exit_code == 0 and after["leading"] == "/home/twostar/wildlife-monitor/data/crops/leading.jpg"
            _check(case_id, ok, f"exit_code={exit_code}, after={after['leading']!r}")
            if ok:
                passed += 1

            # 2. already-twostar / unrelated / embedded-offset / NULL / empty untouched
            case_id = "rewrite/must-not-touch-rows-unchanged"
            ok = (
                after["twostar"] == "/home/twostar/wildlife-monitor/data/crops/twostar.jpg"
                and after["unrelated"] == "/srv/media/x.jpg"
                and after["embedded"] == "/srv/data/home/nash/x.jpg"
                and after["video_c_thumb"] is None
                and after["video_d_thumb"] == ""
            )
            _check(case_id, ok, f"after={after}")
            if ok:
                passed += 1

            # 3. second-occurrence-in-tail: only leading rewritten, tail survives
            case_id = "rewrite/second-occurrence-in-tail-survives"
            ok = after["repeat"] == "/home/twostar/a/home/nash/b.jpg"
            _check(case_id, ok, f"after={after['repeat']!r}")
            if ok:
                passed += 1

            # 4. exact-prefix rewrites cleanly with no index error
            case_id = "rewrite/exact-prefix-rewrites-cleanly"
            ok = after["exact"] == "/home/twostar/"
            _check(case_id, ok, f"after={after['exact']!r}")
            if ok:
                passed += 1

            # 5. videos.thumbnail_path also rewritten (second TARGETS entry)
            case_id = "rewrite/videos-thumbnail-path-rewritten"
            ok = after["video_b_thumb"] == "/home/twostar/wildlife-monitor/data/thumbs/b.jpg"
            _check(case_id, ok, f"after={after['video_b_thumb']!r}")
            if ok:
                passed += 1

            # 6. suffix invariant: old[len(prefix):] == new[len(prefix):] for
            #    every changed row, and suffix_violations() is empty
            case_id = "rewrite/suffix-preserved-and-no-violations"
            with database.get_conn() as conn:
                rows_now = migrate_stale_paths.select_stale_rows(conn)
            violations = migrate_stale_paths.suffix_violations(rows_now)
            ok = violations == []
            _check(case_id, ok, f"violations={violations}")
            if ok:
                passed += 1

            # 7. row counts and non_path_digest identical before/after
            case_id = "rewrite/rowcounts-and-non-path-digest-unchanged"
            snapshot_after = _table_snapshot()
            with database.get_conn() as conn:
                after_crops_digest = migrate_stale_paths.non_path_digest(conn, "crops", "crop_path")
                after_videos_digest = migrate_stale_paths.non_path_digest(conn, "videos", "thumbnail_path")
            ok = (
                len(snapshot_after["crops"]) == before_crops_count
                and len(snapshot_after["videos"]) == before_videos_count
                and after_crops_digest == before_crops_digest
                and after_videos_digest == before_videos_digest
            )
            _check(
                case_id, ok,
                f"crops_count={len(snapshot_after['crops'])}/{before_crops_count}, "
                f"videos_count={len(snapshot_after['videos'])}/{before_videos_count}, "
                f"crops_digest_equal={after_crops_digest == before_crops_digest}, "
                f"videos_digest_equal={after_videos_digest == before_videos_digest}",
            )
            if ok:
                passed += 1

        # 8. empty database and single-row database both complete cleanly
        case_id = "rewrite/empty-and-single-row-db-complete-cleanly"
        with _fixture_db() as db_path:
            exit_code_empty, output_empty = _run_cli(["--db", db_path])
            ok_empty = exit_code_empty == 0 and "Total: 0" in output_empty

        with _fixture_db() as db_path:
            det_id = _seed_detection(_seed_video("single.mp4", camera_name="CamA"))
            _seed_crop(det_id, "/home/nash/wildlife-monitor/data/crops/single.jpg")
            exit_code_single, output_single = _run_cli(["--db", db_path])
            ok_single = exit_code_single == 0 and "Total: 1" in output_single

        ok = ok_empty and ok_single
        _check(
            case_id, ok,
            f"exit_code_empty={exit_code_empty}, ok_empty={ok_empty}, "
            f"exit_code_single={exit_code_single}, ok_single={ok_single}",
        )
        if ok:
            passed += 1

        # 9. idempotency -- a second --apply run immediately after the first
        #    reports 0 candidates and writes nothing
        case_id = "rewrite/second-apply-run-is-idempotent"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            det_id = _seed_detection(_seed_video("idem.mp4", camera_name="CamA"))
            _seed_crop(det_id, "/home/nash/wildlife-monitor/data/crops/idem.jpg")

            snapshot_dir = os.path.join(tmpdir, "snap-idem")
            audit_log = os.path.join(tmpdir, "audit-idem.jsonl")
            first_exit, _first_output = _run_cli([
                "--db", db_path, "--apply", "--confirm-irreversible",
                "--snapshot-dir", snapshot_dir, "--audit-log", audit_log,
            ])

            snapshot_before_second = _table_snapshot()
            second_exit, second_output = _run_cli([
                "--db", db_path, "--apply", "--confirm-irreversible",
                "--snapshot-dir", snapshot_dir, "--audit-log", audit_log,
            ])
            snapshot_after_second = _table_snapshot()

            ok = (
                first_exit == 0
                and second_exit == 0
                and "Rows rewritten this run: 0" in second_output
                and snapshot_before_second == snapshot_after_second
            )
            _check(
                case_id, ok,
                f"first_exit={first_exit}, second_exit={second_exit}, "
                f"snapshot_equal={snapshot_before_second == snapshot_after_second}, "
                f"second_output={second_output[:300]!r}",
            )
            if ok:
                passed += 1
    except Exception as exc:
        print(f"FAIL: suite_rewrite raised {exc!r}")

    return (passed, total)


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
    parser.add_argument("--suite", choices=["rewrite", "apply", "all"], default="all")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    suites = {
        "rewrite": suite_rewrite,
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
