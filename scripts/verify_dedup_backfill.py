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
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
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


def _seed_crop_file(tmpdir, name):
    """Write a small real placeholder file under a `crops` subdirectory of
    the fixture temp directory and return its absolute path. File-cleanup
    assertions must operate on genuine filesystem state, not on path strings
    alone — a guard that only compares strings would pass while still
    deleting the wrong file."""
    crops_dir = os.path.join(tmpdir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    path = os.path.join(crops_dir, name)
    with open(path, "w") as f:
        f.write("fixture-crop")
    return os.path.abspath(path)


def _seed_tracer_group(tmpdir, filename="tracer.mp4", camera_name="CamA"):
    """Seed the tracer's canonical two-member group: a winner holding two of
    its own detections/species/crops, a loser holding two of its own,
    distinct real crop files on disk, and one identical thumbnail_path
    string shared by both rows (hazard H-1, measured at ~98.7% of production
    groups per 09-01-SUMMARY.md). The winner is seeded with a live filepath
    so the tie-break selects it regardless of insertion order. Returns a
    dict of ids and paths used by the assertions in suite_consolidate_tracer
    and suite_fk_integrity."""
    shared_thumb = _seed_crop_file(tmpdir, f"{Path(filename).stem}_thumb_shared.jpg")

    loser_id = _seed_video(
        filename, filepath=None, camera_name=camera_name, thumbnail_path=shared_thumb
    )
    winner_id = _seed_video(
        filename, filepath=f"/nas/archive/{filename}", camera_name=camera_name,
        thumbnail_path=shared_thumb,
    )

    winner_crop_paths = []
    for i in range(2):
        det = _seed_detection(winner_id, frame_number=i)
        _seed_species(det, "felis catus;;;cat")
        crop_path = _seed_crop_file(tmpdir, f"{Path(filename).stem}_winner_crop_{i}.jpg")
        _seed_crop(det, crop_path)
        winner_crop_paths.append(crop_path)

    loser_crop_paths = []
    for i in range(2):
        det = _seed_detection(loser_id, frame_number=i)
        _seed_species(det, "felis catus;;;cat")
        crop_path = _seed_crop_file(tmpdir, f"{Path(filename).stem}_loser_crop_{i}.jpg")
        _seed_crop(det, crop_path)
        loser_crop_paths.append(crop_path)

    return {
        "filename": filename,
        "camera_name": camera_name,
        "winner_id": winner_id,
        "loser_id": loser_id,
        "shared_thumbnail": shared_thumb,
        "winner_crop_paths": winner_crop_paths,
        "loser_crop_paths": loser_crop_paths,
    }


class _FlakyConnProxy:
    """Wraps a real sqlite3.Connection and raises on the Nth call to
    execute(), forwarding every other call and attribute access to the real
    connection. sqlite3.Connection instances don't allow arbitrary attribute
    assignment (`execute` is a read-only slot), so a proxy object — rather
    than monkeypatching the connection in place — is the only way to inject
    a mid-transaction fault. Used only by
    consolidate/interrupt-leaves-group-whole; never touches the real
    connection's attributes, so there is nothing to restore afterward."""

    def __init__(self, real_conn, fail_on_call):
        self._real = real_conn
        self._fail_on_call = fail_on_call
        self._n = 0

    def execute(self, sql, params=()):
        self._n += 1
        if self._n == self._fail_on_call:
            raise sqlite3.OperationalError("injected fault for interrupt test")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def suite_tiebreak():
    """Five cases exercising backfill_dedup_videos.select_winner()'s
    tie-break, mirroring verify_dedup_identity.suite_identity()'s cases 6-7
    but retargeted at the backfill's own winner-selection call."""
    passed = 0
    total = 5
    try:
        # 1. tiebreak/live-filepath-wins
        case_id = "tiebreak/live-filepath-wins"
        with _fixture_db():
            _seed_video("tie1.mp4", filepath=None, camera_name="CamA")
            id_live = _seed_video("tie1.mp4", filepath="/nas/archive/tie1.mp4", camera_name="CamA")
            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "tie1.mp4", "CamA")
                winner_id, rule, _skipped = backfill_dedup_videos.select_winner(conn, member_ids)
            ok = winner_id == id_live and rule == "default-tiebreak"
            _check(case_id, ok, f"winner_id={winner_id}, expected={id_live}, rule={rule}")
            if ok:
                passed += 1

        # 2. tiebreak/lowest-id-wins-when-tied
        case_id = "tiebreak/lowest-id-wins-when-tied"
        with _fixture_db():
            ids = [_seed_video("tie2.mp4", filepath=None, camera_name="CamA") for _ in range(3)]
            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "tie2.mp4", "CamA")
                winner_id, _rule, _skipped = backfill_dedup_videos.select_winner(conn, member_ids)
            ok = winner_id == min(ids)
            _check(case_id, ok, f"winner_id={winner_id}, expected={min(ids)}")
            if ok:
                passed += 1

        # 3. tiebreak/lowest-id-wins-when-all-live
        case_id = "tiebreak/lowest-id-wins-when-all-live"
        with _fixture_db():
            ids = [
                _seed_video("tie3.mp4", filepath=f"/nas/archive/tie3_{i}.mp4", camera_name="CamA")
                for i in range(3)
            ]
            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "tie3.mp4", "CamA")
                winner_id, _rule, _skipped = backfill_dedup_videos.select_winner(conn, member_ids)
            ok = winner_id == min(ids)
            _check(case_id, ok, f"winner_id={winner_id}, expected={min(ids)}")
            if ok:
                passed += 1

        # 4. tiebreak/deterministic-across-calls
        case_id = "tiebreak/deterministic-across-calls"
        with _fixture_db():
            for i in range(6):
                _seed_video(
                    "tie4.mp4",
                    filepath=(f"/nas/archive/tie4_{i}.mp4" if i % 2 == 0 else None),
                    camera_name="CamA",
                )
            with database.get_conn() as conn:
                plan1 = backfill_dedup_videos.plan_group(conn, "tie4.mp4", "CamA")
                plan2 = backfill_dedup_videos.plan_group(conn, "tie4.mp4", "CamA")
            ok = plan1.winner_id == plan2.winner_id and plan1.loser_ids == plan2.loser_ids
            _check(
                case_id, ok,
                f"plan1=({plan1.winner_id},{plan1.loser_ids}), "
                f"plan2=({plan2.winner_id},{plan2.loser_ids})",
            )
            if ok:
                passed += 1

        # 5. tiebreak/matches-database-helper
        case_id = "tiebreak/matches-database-helper"
        with _fixture_db():
            _seed_video("tie5.mp4", filepath=None, camera_name="CamA")
            _seed_video("tie5.mp4", filepath="/nas/archive/tie5.mp4", camera_name="CamA")
            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "tie5.mp4", "CamA")
                winner_id, _rule, _skipped = backfill_dedup_videos.select_winner(conn, member_ids)
            db_row = database.find_existing_video("tie5.mp4", "CamA")
            ok = db_row is not None and winner_id == db_row["id"]
            _check(
                case_id, ok,
                f"winner_id={winner_id}, db_row_id={db_row['id'] if db_row else None}",
            )
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: tiebreak/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


def suite_consolidate_tracer():
    """Seven cases exercising the tracer's end-to-end consolidation path:
    plan_group()/apply_group()/cleanup_files() and the backfill CLI's
    dry-run-default, two-flag write gate."""
    passed = 0
    total = 7
    try:
        # 1. consolidate/tracer-happy-path
        case_id = "consolidate/tracer-happy-path"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="tracer1.mp4")

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, g["filename"], g["camera_name"])
                deleted_counts = backfill_dedup_videos.apply_group(conn, plan)

            with database.get_conn() as conn:
                remaining = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM videos WHERE filename=? AND camera_name IS ?",
                        (g["filename"], g["camera_name"]),
                    ).fetchall()
                ]
                winner_dets = conn.execute(
                    "SELECT COUNT(*) FROM detections WHERE video_id=?", (g["winner_id"],)
                ).fetchone()[0]
                loser_dets = conn.execute(
                    "SELECT COUNT(*) FROM detections WHERE video_id=?", (g["loser_id"],)
                ).fetchone()[0]
                loser_video_exists = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE id=?", (g["loser_id"],)
                ).fetchone()[0]
                loser_corrections = conn.execute(
                    "SELECT COUNT(*) FROM video_corrections WHERE video_id=?", (g["loser_id"],)
                ).fetchone()[0]

            ok = (
                plan.winner_id == g["winner_id"]
                and remaining == [g["winner_id"]]
                and winner_dets == 2
                and loser_dets == 0
                and loser_video_exists == 0
                and loser_corrections == 0
                and deleted_counts.get("detections") == 2
                and deleted_counts.get("crops") == 2
                and deleted_counts.get("species") == 2
                and deleted_counts.get("videos") == 1
            )
            _check(
                case_id, ok,
                f"remaining={remaining}, winner_dets={winner_dets}, loser_dets={loser_dets}, "
                f"loser_video_exists={loser_video_exists}, deleted_counts={deleted_counts}",
            )
            if ok:
                passed += 1

        # 2. consolidate/dry-run-writes-nothing
        case_id = "consolidate/dry-run-writes-nothing"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="tracer2.mp4")

            snapshot_before = _table_snapshot()
            dir_before = _dir_snapshot(tmpdir)

            buf = io.StringIO()
            exit_code = None
            try:
                with redirect_stdout(buf):
                    backfill_dedup_videos.main(["--db", db_path, "--consolidate"])
            except SystemExit as exc:
                exit_code = exc.code
            output = buf.getvalue()

            snapshot_after = _table_snapshot()
            dir_after = _dir_snapshot(tmpdir)

            ok = (
                exit_code in (0, None)
                and snapshot_before == snapshot_after
                and dir_before == dir_after
                and str(g["winner_id"]) in output
                and str(g["loser_id"]) in output
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, snapshot_equal={snapshot_before == snapshot_after}, "
                f"dir_equal={dir_before == dir_after}, output={output[:200]!r}",
            )
            if ok:
                passed += 1

        # 3. consolidate/apply-requires-confirmation
        case_id = "consolidate/apply-requires-confirmation"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            _seed_tracer_group(tmpdir, filename="tracer3.mp4")

            snapshot_before = _table_snapshot()

            buf = io.StringIO()
            exit_code = None
            try:
                with redirect_stdout(buf):
                    backfill_dedup_videos.main(["--db", db_path, "--consolidate", "--apply"])
            except SystemExit as exc:
                exit_code = exc.code

            snapshot_after = _table_snapshot()

            ok = exit_code not in (0, None) and snapshot_before == snapshot_after
            _check(
                case_id, ok,
                f"exit_code={exit_code}, snapshot_equal={snapshot_before == snapshot_after}",
            )
            if ok:
                passed += 1

        # 4. consolidate/loser-crop-file-removed
        case_id = "consolidate/loser-crop-file-removed"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="tracer4.mp4")
            snapshot_dir = os.path.join(tmpdir, "snapshots")
            audit_log_path = os.path.join(tmpdir, "audit4.jsonl")

            loser_only_crop = g["loser_crop_paths"][0]
            existed_before = os.path.exists(loser_only_crop)

            buf = io.StringIO()
            exit_code = None
            try:
                with redirect_stdout(buf):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            exists_after = os.path.exists(loser_only_crop)
            ok = existed_before and exit_code == 0 and not exists_after
            _check(
                case_id, ok,
                f"exit_code={exit_code}, existed_before={existed_before}, exists_after={exists_after}",
            )
            if ok:
                passed += 1

        # 5. consolidate/shared-thumbnail-retained
        case_id = "consolidate/shared-thumbnail-retained"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="tracer5.mp4")
            snapshot_dir = os.path.join(tmpdir, "snapshots")
            audit_log_path = os.path.join(tmpdir, "audit5.jsonl")

            buf = io.StringIO()
            exit_code = None
            try:
                with redirect_stdout(buf):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            thumb_exists = os.path.exists(g["shared_thumbnail"])
            ok = exit_code == 0 and thumb_exists
            _check(case_id, ok, f"exit_code={exit_code}, thumbnail_exists={thumb_exists}")
            if ok:
                passed += 1

        # 6. consolidate/idempotent-rerun
        case_id = "consolidate/idempotent-rerun"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            _seed_tracer_group(tmpdir, filename="tracer6.mp4")
            snapshot_dir = os.path.join(tmpdir, "snapshots")
            audit_log_path = os.path.join(tmpdir, "audit6.jsonl")
            run_args = [
                "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
            ]

            exit1 = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main(list(run_args))
            except SystemExit as exc:
                exit1 = exc.code

            snapshot_between = _table_snapshot()

            exit2 = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main(list(run_args))
            except SystemExit as exc:
                exit2 = exc.code

            snapshot_after_second = _table_snapshot()

            ok = exit1 == 0 and exit2 == 0 and snapshot_between == snapshot_after_second
            _check(
                case_id, ok,
                f"exit1={exit1}, exit2={exit2}, "
                f"snapshots_equal={snapshot_between == snapshot_after_second}",
            )
            if ok:
                passed += 1

        # 7. consolidate/interrupt-leaves-group-whole
        case_id = "consolidate/interrupt-leaves-group-whole"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="tracer7.mp4")

            snapshot_before = _table_snapshot()

            raised = False
            try:
                with database.get_conn() as conn:
                    plan = backfill_dedup_videos.plan_group(conn, g["filename"], g["camera_name"])
                    proxy = _FlakyConnProxy(conn, fail_on_call=2)
                    backfill_dedup_videos.apply_group(proxy, plan)
            except sqlite3.OperationalError:
                raised = True

            snapshot_after = _table_snapshot()

            ok = raised and snapshot_before == snapshot_after
            _check(
                case_id, ok,
                f"raised={raised}, snapshot_equal={snapshot_before == snapshot_after}",
            )
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: consolidate-tracer/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


def suite_fk_integrity():
    """Three cases confirming apply_group() leaves the fixture DB with zero
    foreign-key violations, zero orphaned children, and zero broken
    dual-lens pairings."""
    passed = 0
    total = 3
    try:
        # 1. fk-integrity/zero-violations-after-apply
        case_id = "fk-integrity/zero-violations-after-apply"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="fk1.mp4")
            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, g["filename"], g["camera_name"])
                backfill_dedup_videos.apply_group(conn, plan)
            with database.get_conn() as conn:
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            ok = violations == []
            _check(case_id, ok, f"violations={violations}")
            if ok:
                passed += 1

        # 2. fk-integrity/no-orphaned-children
        case_id = "fk-integrity/no-orphaned-children"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="fk2.mp4")
            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, g["filename"], g["camera_name"])
                backfill_dedup_videos.apply_group(conn, plan)
            with database.get_conn() as conn:
                orphan_detections = conn.execute(
                    "SELECT COUNT(*) FROM detections d LEFT JOIN videos v ON d.video_id = v.id "
                    "WHERE v.id IS NULL"
                ).fetchone()[0]
                orphan_species = conn.execute(
                    "SELECT COUNT(*) FROM species s LEFT JOIN detections d ON s.detection_id = d.id "
                    "WHERE d.id IS NULL"
                ).fetchone()[0]
                orphan_crops = conn.execute(
                    "SELECT COUNT(*) FROM crops c LEFT JOIN detections d ON c.detection_id = d.id "
                    "WHERE d.id IS NULL"
                ).fetchone()[0]
            ok = orphan_detections == 0 and orphan_species == 0 and orphan_crops == 0
            _check(
                case_id, ok,
                f"orphan_detections={orphan_detections}, orphan_species={orphan_species}, "
                f"orphan_crops={orphan_crops}",
            )
            if ok:
                passed += 1

        # 3. fk-integrity/pairing-consistency-zero
        case_id = "fk-integrity/pairing-consistency-zero"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            g = _seed_tracer_group(tmpdir, filename="fk3.mp4")
            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, g["filename"], g["camera_name"])
                backfill_dedup_videos.apply_group(conn, plan)
            broken = database.check_pairing_consistency()
            ok = broken == 0
            _check(case_id, ok, f"broken={broken}")
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: fk-integrity/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


def suite_correction_precedence():
    """Nine cases exercising D-01/D-02 correction precedence:
    correction_signal() detecting either write path, select_winner()'s
    override for a single correction holder, the conflicting-holders skip,
    and the tie-break fallback when siblings disagree with no correction
    present."""
    passed = 0
    total = 9
    try:
        # 1. correction/species-corrected-at-detected
        case_id = "correction/species-corrected-at-detected"
        with _fixture_db():
            vid = _seed_video("corr1.mp4", camera_name="CamA")
            det = _seed_detection(vid)
            _seed_species(det, "felis catus;;;cat", corrected_at="2026-08-01T00:00:00")
            with database.get_conn() as conn:
                signal = backfill_dedup_videos.correction_signal(conn, vid)
            ok = signal is True
            _check(case_id, ok, f"signal={signal}")
            if ok:
                passed += 1

        # 2. correction/video-correction-row-detected
        case_id = "correction/video-correction-row-detected"
        with _fixture_db():
            vid = _seed_video("corr2.mp4", camera_name="CamA")
            det = _seed_detection(vid)
            _seed_species(det, "felis catus;;;cat")
            _seed_video_correction(vid, "felis catus;;;cat", corrected_label="lynx rufus;;;bobcat")
            with database.get_conn() as conn:
                signal = backfill_dedup_videos.correction_signal(conn, vid)
            ok = signal is True
            _check(case_id, ok, f"signal={signal}")
            if ok:
                passed += 1

        # 3. correction/no-signal-when-uncorrected
        case_id = "correction/no-signal-when-uncorrected"
        with _fixture_db():
            vid = _seed_video("corr3.mp4", camera_name="CamA")
            det = _seed_detection(vid)
            _seed_species(det, "felis catus;;;cat")
            with database.get_conn() as conn:
                signal = backfill_dedup_videos.correction_signal(conn, vid)
            ok = signal is False
            _check(case_id, ok, f"signal={signal}")
            if ok:
                passed += 1

        # 4. correction/single-holder-wins-over-tiebreak
        case_id = "correction/single-holder-wins-over-tiebreak"
        with _fixture_db():
            _id_a = _seed_video("corr4.mp4", filepath=None, camera_name="CamA")
            _id_b = _seed_video("corr4.mp4", filepath=None, camera_name="CamA")
            id_c = _seed_video("corr4.mp4", filepath=None, camera_name="CamA")
            det_c = _seed_detection(id_c)
            _seed_species(det_c, "felis catus;;;cat", corrected_at="2026-08-01T00:00:00")
            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "corr4.mp4", "CamA")
                winner_id, rule, skipped_reason = backfill_dedup_videos.select_winner(
                    conn, member_ids
                )
            ok = (
                winner_id == id_c
                and winner_id != member_ids[0]
                and rule == "correction-precedence"
                and skipped_reason == ""
            )
            _check(
                case_id, ok,
                f"winner_id={winner_id}, first_member={member_ids[0]}, rule={rule}, "
                f"skipped_reason={skipped_reason!r}",
            )
            if ok:
                passed += 1

        # 5. correction/single-holder-survives-apply
        case_id = "correction/single-holder-survives-apply"
        with _fixture_db() as db_path:
            _id_a = _seed_video("corr5.mp4", filepath=None, camera_name="CamA")
            _id_b = _seed_video("corr5.mp4", filepath=None, camera_name="CamA")
            id_c = _seed_video("corr5.mp4", filepath=None, camera_name="CamA")
            det_c = _seed_detection(id_c)
            _seed_species(
                det_c, "felis catus;;;cat", user_common_name="Bobcat",
                corrected_at="2026-08-01T00:00:00",
            )
            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap5")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit5.jsonl")

            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            with database.get_conn() as conn:
                remaining = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM videos WHERE filename=? AND camera_name IS ?",
                        ("corr5.mp4", "CamA"),
                    ).fetchall()
                ]
                species_row = conn.execute(
                    "SELECT s.corrected_at, s.user_common_name FROM species s "
                    "JOIN detections d ON s.detection_id = d.id WHERE d.video_id=?",
                    (id_c,),
                ).fetchone()

            ok = (
                exit_code == 0
                and remaining == [id_c]
                and species_row is not None
                and species_row["corrected_at"] == "2026-08-01T00:00:00"
                and species_row["user_common_name"] == "Bobcat"
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, remaining={remaining}, "
                f"species_row={dict(species_row) if species_row else None}",
            )
            if ok:
                passed += 1

        # 6. correction/conflicting-holders-skipped
        case_id = "correction/conflicting-holders-skipped"
        with _fixture_db() as db_path:
            id_a = _seed_video("corr6.mp4", filepath=None, camera_name="CamA")
            id_b = _seed_video("corr6.mp4", filepath=None, camera_name="CamA")
            det_a = _seed_detection(id_a)
            _seed_species(det_a, "felis catus;;;cat", corrected_at="2026-08-01T00:00:00")
            det_b = _seed_detection(id_b)
            _seed_species(det_b, "lynx rufus;;;bobcat", corrected_at="2026-08-02T00:00:00")

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "corr6.mp4", "CamA")
            ok_plan = plan.skipped_reason == "conflicting-corrections"

            snapshot_before = _table_snapshot()

            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap6")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit6.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            snapshot_after = _table_snapshot()

            ok = ok_plan and exit_code == 0 and snapshot_before == snapshot_after
            _check(
                case_id, ok,
                f"plan.skipped_reason={plan.skipped_reason!r}, exit_code={exit_code}, "
                f"snapshot_equal={snapshot_before == snapshot_after}",
            )
            if ok:
                passed += 1

        # 7. correction/same-member-two-signals-not-conflicting
        case_id = "correction/same-member-two-signals-not-conflicting"
        with _fixture_db():
            _id_a = _seed_video("corr7.mp4", filepath=None, camera_name="CamA")
            id_b = _seed_video("corr7.mp4", filepath=None, camera_name="CamA")
            det_b = _seed_detection(id_b)
            _seed_species(det_b, "felis catus;;;cat", corrected_at="2026-08-01T00:00:00")
            _seed_video_correction(id_b, "felis catus;;;cat", corrected_label="lynx rufus;;;bobcat")

            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "corr7.mp4", "CamA")
                winner_id, rule, skipped_reason = backfill_dedup_videos.select_winner(
                    conn, member_ids
                )

            ok = winner_id == id_b and rule == "correction-precedence" and skipped_reason == ""
            _check(
                case_id, ok,
                f"winner_id={winner_id}, rule={rule}, skipped_reason={skipped_reason!r}",
            )
            if ok:
                passed += 1

        # 8. correction/label-disagreement-without-correction-uses-tiebreak
        case_id = "correction/label-disagreement-without-correction-uses-tiebreak"
        with _fixture_db():
            id_a = _seed_video("corr8.mp4", filepath=None, camera_name="CamA")
            id_b = _seed_video("corr8.mp4", filepath=None, camera_name="CamA")
            det_a = _seed_detection(id_a)
            _seed_species(det_a, "felis catus;;;cat")
            det_b = _seed_detection(id_b)
            _seed_species(det_b, "lynx rufus;;;bobcat")

            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "corr8.mp4", "CamA")
                winner_id, rule, skipped_reason = backfill_dedup_videos.select_winner(
                    conn, member_ids
                )

            ok = winner_id == member_ids[0] and rule == "default-tiebreak" and skipped_reason == ""
            _check(
                case_id, ok,
                f"winner_id={winner_id}, rule={rule}, skipped_reason={skipped_reason!r}",
            )
            if ok:
                passed += 1

        # 9. correction/quality-disagreement-without-correction-uses-tiebreak
        case_id = "correction/quality-disagreement-without-correction-uses-tiebreak"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            id_a = _seed_video("corr9.mp4", filepath=None, camera_name="CamA")
            id_b = _seed_video("corr9.mp4", filepath=None, camera_name="CamA")
            det_a = _seed_detection(id_a)
            _seed_species(det_a, "felis catus;;;cat")
            _seed_crop(det_a, _seed_crop_file(tmpdir, "corr9_a.jpg"), quality_score=20.0)
            det_b = _seed_detection(id_b)
            _seed_species(det_b, "felis catus;;;cat")
            _seed_crop(det_b, _seed_crop_file(tmpdir, "corr9_b.jpg"), quality_score=90.0)

            with database.get_conn() as conn:
                member_ids = backfill_dedup_videos.group_member_ids(conn, "corr9.mp4", "CamA")
                winner_id, rule, skipped_reason = backfill_dedup_videos.select_winner(
                    conn, member_ids
                )

            ok = winner_id == member_ids[0] and rule == "default-tiebreak" and skipped_reason == ""
            _check(
                case_id, ok,
                f"winner_id={winner_id}, rule={rule}, skipped_reason={skipped_reason!r}",
            )
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: correction-precedence/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


def suite_reparent_and_skip():
    """Eight cases exercising the zero-detections re-parent rule (rule 4)
    and the H-2 crop-migrated-winner skip (rule 5): choose_reparent_source()
    picking the richest/lowest-id loser, plan_group() recording
    reparent_from and the skip reason, and apply_group() leaving a skipped
    group byte-identical."""
    passed = 0
    total = 8
    try:
        # 1. reparent/empty-winner-adopts-loser-detections
        case_id = "reparent/empty-winner-adopts-loser-detections"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            winner_id = _seed_video("rep1.mp4", filepath="/nas/archive/rep1.mp4", camera_name="CamA")
            loser_id = _seed_video("rep1.mp4", filepath=None, camera_name="CamA")
            for i in range(3):
                det = _seed_detection(loser_id, frame_number=i)
                _seed_species(det, "felis catus;;;cat")
                crop_path = _seed_crop_file(tmpdir, f"rep1_loser_crop_{i}.jpg")
                _seed_crop(det, crop_path)

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "rep1.mp4", "CamA")
                if plan.reparent_from is not None:
                    backfill_dedup_videos.reparent_detections(conn, plan.reparent_from, plan.winner_id)
                backfill_dedup_videos.apply_group(conn, plan)

            with database.get_conn() as conn:
                winner_dets = conn.execute(
                    "SELECT COUNT(*) FROM detections WHERE video_id=?", (winner_id,)
                ).fetchone()[0]
                winner_species = conn.execute(
                    "SELECT COUNT(*) FROM species s JOIN detections d ON s.detection_id=d.id "
                    "WHERE d.video_id=?", (winner_id,),
                ).fetchone()[0]
                winner_crops = conn.execute(
                    "SELECT COUNT(*) FROM crops c JOIN detections d ON c.detection_id=d.id "
                    "WHERE d.video_id=?", (winner_id,),
                ).fetchone()[0]
                loser_exists = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE id=?", (loser_id,)
                ).fetchone()[0]

            ok = (
                plan.reparent_from == loser_id
                and winner_dets == 3
                and winner_species == 3
                and winner_crops == 3
                and loser_exists == 0
            )
            _check(
                case_id, ok,
                f"reparent_from={plan.reparent_from}, winner_dets={winner_dets}, "
                f"winner_species={winner_species}, winner_crops={winner_crops}, "
                f"loser_exists={loser_exists}",
            )
            if ok:
                passed += 1

        # 2. reparent/picks-richest-loser
        case_id = "reparent/picks-richest-loser"
        with _fixture_db():
            winner_id = _seed_video("rep2.mp4", filepath="/nas/archive/rep2.mp4", camera_name="CamA")
            loser_small = _seed_video("rep2.mp4", filepath=None, camera_name="CamA")
            loser_big = _seed_video("rep2.mp4", filepath=None, camera_name="CamA")
            _seed_detection(loser_small, frame_number=0)
            for i in range(4):
                _seed_detection(loser_big, frame_number=i)

            with database.get_conn() as conn:
                source = backfill_dedup_videos.choose_reparent_source(
                    conn, winner_id, [loser_small, loser_big]
                )
            ok = source == loser_big
            _check(case_id, ok, f"source={source}, expected={loser_big}")
            if ok:
                passed += 1

        # 3. reparent/ties-broken-by-lowest-id
        case_id = "reparent/ties-broken-by-lowest-id"
        with _fixture_db():
            winner_id = _seed_video("rep3.mp4", filepath="/nas/archive/rep3.mp4", camera_name="CamA")
            loser_a = _seed_video("rep3.mp4", filepath=None, camera_name="CamA")
            loser_b = _seed_video("rep3.mp4", filepath=None, camera_name="CamA")
            for i in range(2):
                _seed_detection(loser_a, frame_number=i)
            for i in range(2):
                _seed_detection(loser_b, frame_number=i)

            with database.get_conn() as conn:
                source = backfill_dedup_videos.choose_reparent_source(
                    conn, winner_id, [loser_a, loser_b]
                )
            ok = source == min(loser_a, loser_b)
            _check(case_id, ok, f"source={source}, expected={min(loser_a, loser_b)}")
            if ok:
                passed += 1

        # 4. reparent/winner-with-detections-not-reparented
        case_id = "reparent/winner-with-detections-not-reparented"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            winner_id = _seed_video("rep4.mp4", filepath="/nas/archive/rep4.mp4", camera_name="CamA")
            loser_id = _seed_video("rep4.mp4", filepath=None, camera_name="CamA")
            det_w = _seed_detection(winner_id)
            _seed_species(det_w, "felis catus;;;cat")
            _seed_crop(det_w, _seed_crop_file(tmpdir, "rep4_winner.jpg"))
            det_l = _seed_detection(loser_id)
            _seed_species(det_l, "felis catus;;;cat")

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "rep4.mp4", "CamA")
                backfill_dedup_videos.apply_group(conn, plan)

            with database.get_conn() as conn:
                loser_dets = conn.execute(
                    "SELECT COUNT(*) FROM detections WHERE video_id=?", (loser_id,)
                ).fetchone()[0]
                loser_exists = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE id=?", (loser_id,)
                ).fetchone()[0]

            ok = plan.reparent_from is None and loser_dets == 0 and loser_exists == 0
            _check(
                case_id, ok,
                f"reparent_from={plan.reparent_from}, loser_dets={loser_dets}, "
                f"loser_exists={loser_exists}",
            )
            if ok:
                passed += 1

        # 5. reparent/plan-records-source
        case_id = "reparent/plan-records-source"
        with _fixture_db():
            winner_id = _seed_video("rep5.mp4", filepath="/nas/archive/rep5.mp4", camera_name="CamA")
            loser_id = _seed_video("rep5.mp4", filepath=None, camera_name="CamA")
            _seed_detection(loser_id)

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "rep5.mp4", "CamA")

            ok = plan.reparent_from == loser_id and "reparent" in plan.rule
            _check(case_id, ok, f"reparent_from={plan.reparent_from}, rule={plan.rule!r}")
            if ok:
                passed += 1

        # 6. skip/crop-migrated-winner-skipped
        case_id = "skip/crop-migrated-winner-skipped"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            winner_id = _seed_video("skip6.mp4", filepath="/nas/archive/skip6.mp4", camera_name="CamA")
            loser_id = _seed_video("skip6.mp4", filepath=None, camera_name="CamA")
            for i in range(2):
                det_w = _seed_detection(winner_id, frame_number=i)
                _seed_species(det_w, "felis catus;;;cat")
            loser_crop_paths = []
            for i in range(2):
                det_l = _seed_detection(loser_id, frame_number=i)
                _seed_species(det_l, "felis catus;;;cat")
                crop_path = _seed_crop_file(tmpdir, f"skip6_loser_crop_{i}.jpg")
                _seed_crop(det_l, crop_path)
                loser_crop_paths.append(crop_path)

            snapshot_before = _table_snapshot()

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "skip6.mp4", "CamA")
                backfill_dedup_videos.apply_group(conn, plan)

            snapshot_after = _table_snapshot()

            ok = (
                plan.skipped_reason == "winner-crops-migrated"
                and snapshot_before == snapshot_after
            )
            _check(
                case_id, ok,
                f"skipped_reason={plan.skipped_reason!r}, "
                f"snapshot_equal={snapshot_before == snapshot_after}",
            )
            if ok:
                passed += 1

        # 7. skip/crop-migrated-files-untouched
        case_id = "skip/crop-migrated-files-untouched"
        with _fixture_db() as db_path:
            tmpdir = os.path.dirname(db_path)
            winner_id = _seed_video("skip7.mp4", filepath="/nas/archive/skip7.mp4", camera_name="CamA")
            loser_id = _seed_video("skip7.mp4", filepath=None, camera_name="CamA")
            for i in range(2):
                det_w = _seed_detection(winner_id, frame_number=i)
                _seed_species(det_w, "felis catus;;;cat")
            loser_crop_paths = []
            for i in range(2):
                det_l = _seed_detection(loser_id, frame_number=i)
                _seed_species(det_l, "felis catus;;;cat")
                crop_path = _seed_crop_file(tmpdir, f"skip7_loser_crop_{i}.jpg")
                _seed_crop(det_l, crop_path)
                loser_crop_paths.append(crop_path)

            existed_before = [os.path.exists(p) for p in loser_crop_paths]

            snapshot_dir = os.path.join(tmpdir, "snapshots")
            audit_log_path = os.path.join(tmpdir, "audit7.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            existed_after = [os.path.exists(p) for p in loser_crop_paths]

            ok = exit_code == 0 and all(existed_before) and all(existed_after)
            _check(
                case_id, ok,
                f"exit_code={exit_code}, existed_before={existed_before}, "
                f"existed_after={existed_after}",
            )
            if ok:
                passed += 1

        # 8. skip/no-crops-anywhere-not-skipped
        case_id = "skip/no-crops-anywhere-not-skipped"
        with _fixture_db():
            winner_id = _seed_video("skip8.mp4", filepath="/nas/archive/skip8.mp4", camera_name="CamA")
            loser_id = _seed_video("skip8.mp4", filepath=None, camera_name="CamA")
            det_w = _seed_detection(winner_id)
            _seed_species(det_w, "felis catus;;;cat")
            det_l = _seed_detection(loser_id)
            _seed_species(det_l, "felis catus;;;cat")

            with database.get_conn() as conn:
                plan = backfill_dedup_videos.plan_group(conn, "skip8.mp4", "CamA")
                backfill_dedup_videos.apply_group(conn, plan)

            with database.get_conn() as conn:
                loser_exists = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE id=?", (loser_id,)
                ).fetchone()[0]

            ok = plan.skipped_reason == "" and loser_exists == 0
            _check(
                case_id, ok,
                f"skipped_reason={plan.skipped_reason!r}, loser_exists={loser_exists}",
            )
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: reparent-and-skip/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


def suite_pairing_preservation():
    """Four cases exercising dual-lens paired_video_id preservation (rule
    7): incoming references to a loser re-pointed at the winner, the
    winner's own outgoing reference to a loser in a different group
    re-pointed at that group's winner, a symmetric worldwatch pair surviving
    consolidation, and the no-silent-null invariant that
    ON DELETE SET NULL is never relied on."""
    passed = 0
    total = 4
    try:
        # 1. pairing/repoint-incoming-reference
        case_id = "pairing/repoint-incoming-reference"
        with _fixture_db() as db_path:
            outside_id = _seed_video("standalone9.mp4", camera_name="CamA")
            winner_id = _seed_video(
                "pair9.mp4", filepath="/nas/archive/pair9.mp4", camera_name="CamA",
                paired_video_id=outside_id,
            )
            loser_id = _seed_video("pair9.mp4", filepath=None, camera_name="CamA")
            # Outside currently (wrongly) references the loser copy, not the winner.
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET paired_video_id=? WHERE id=?", (loser_id, outside_id)
                )

            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap9")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit9.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            with database.get_conn() as conn:
                outside_row = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (outside_id,)
                ).fetchone()
            broken = database.check_pairing_consistency()

            ok = (
                exit_code == 0
                and outside_row is not None
                and outside_row["paired_video_id"] == winner_id
                and broken == 0
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, outside_paired_video_id="
                f"{outside_row['paired_video_id'] if outside_row else None}, broken={broken}",
            )
            if ok:
                passed += 1

        # 2. pairing/repoint-winner-outgoing-reference
        case_id = "pairing/repoint-winner-outgoing-reference"
        with _fixture_db() as db_path:
            winner_b = _seed_video(
                "pairB10.mp4", filepath="/nas/archive/pairB10.mp4", camera_name="CamA"
            )
            loser_b = _seed_video("pairB10.mp4", filepath=None, camera_name="CamA")
            winner_a = _seed_video(
                "pairA10.mp4", filepath="/nas/archive/pairA10.mp4", camera_name="CamA",
                paired_video_id=loser_b,
            )
            _loser_a = _seed_video("pairA10.mp4", filepath=None, camera_name="CamA")
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET paired_video_id=? WHERE id=?", (winner_a, winner_b)
                )

            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap10")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit10.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            with database.get_conn() as conn:
                winner_a_row = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (winner_a,)
                ).fetchone()
            broken = database.check_pairing_consistency()

            ok = (
                exit_code == 0
                and winner_a_row is not None
                and winner_a_row["paired_video_id"] == winner_b
                and broken == 0
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, winner_a_paired="
                f"{winner_a_row['paired_video_id'] if winner_a_row else None}, broken={broken}",
            )
            if ok:
                passed += 1

        # 3. pairing/symmetric-after-apply
        case_id = "pairing/symmetric-after-apply"
        with _fixture_db() as db_path:
            winner_l1 = _seed_video(
                "worldwatch_lens1.mp4", filepath="/nas/archive/worldwatch_lens1.mp4",
                camera_name="worldwatch",
            )
            winner_l0 = _seed_video(
                "worldwatch_lens0.mp4", filepath="/nas/archive/worldwatch_lens0.mp4",
                camera_name="worldwatch", paired_video_id=winner_l1,
            )
            _loser_l0 = _seed_video("worldwatch_lens0.mp4", filepath=None, camera_name="worldwatch")
            _loser_l1 = _seed_video("worldwatch_lens1.mp4", filepath=None, camera_name="worldwatch")
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET paired_video_id=? WHERE id=?", (winner_l0, winner_l1)
                )

            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap11")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit11.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            with database.get_conn() as conn:
                remaining = [
                    r["id"] for r in conn.execute(
                        "SELECT id FROM videos WHERE camera_name='worldwatch'"
                    ).fetchall()
                ]
                rows = {
                    r["id"]: r["paired_video_id"] for r in conn.execute(
                        "SELECT id, paired_video_id FROM videos WHERE camera_name='worldwatch'"
                    ).fetchall()
                }
            broken = database.check_pairing_consistency()

            ok = (
                exit_code == 0
                and sorted(remaining) == sorted([winner_l0, winner_l1])
                and rows.get(winner_l0) == winner_l1
                and rows.get(winner_l1) == winner_l0
                and broken == 0
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, remaining={remaining}, rows={rows}, broken={broken}",
            )
            if ok:
                passed += 1

        # 4. pairing/no-silent-null
        case_id = "pairing/no-silent-null"
        with _fixture_db() as db_path:
            outside_id = _seed_video("standalone12.mp4", camera_name="CamA")
            winner_id = _seed_video(
                "pair12.mp4", filepath="/nas/archive/pair12.mp4", camera_name="CamA",
                paired_video_id=outside_id,
            )
            loser_id = _seed_video("pair12.mp4", filepath=None, camera_name="CamA")
            with database.get_conn() as conn:
                conn.execute(
                    "UPDATE videos SET paired_video_id=? WHERE id=?", (loser_id, outside_id)
                )

            snapshot_dir = os.path.join(os.path.dirname(db_path), "snap12")
            audit_log_path = os.path.join(os.path.dirname(db_path), "audit12.jsonl")
            exit_code = None
            try:
                with redirect_stdout(io.StringIO()):
                    backfill_dedup_videos.main([
                        "--db", db_path, "--consolidate", "--apply", "--confirm-irreversible",
                        "--snapshot-dir", snapshot_dir, "--audit-log", audit_log_path,
                    ])
            except SystemExit as exc:
                exit_code = exc.code

            with database.get_conn() as conn:
                outside_row = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (outside_id,)
                ).fetchone()
                winner_row = conn.execute(
                    "SELECT paired_video_id FROM videos WHERE id=?", (winner_id,)
                ).fetchone()

            ok = (
                exit_code == 0
                and outside_row is not None and outside_row["paired_video_id"] is not None
                and winner_row is not None and winner_row["paired_video_id"] is not None
            )
            _check(
                case_id, ok,
                f"exit_code={exit_code}, outside_paired="
                f"{outside_row['paired_video_id'] if outside_row else None}, "
                f"winner_paired={winner_row['paired_video_id'] if winner_row else None}",
            )
            if ok:
                passed += 1

    except AttributeError as exc:
        print(f"FAIL: pairing-preservation/function-missing — {exc}")
        return (passed, total)

    return (passed, total)


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
    parser.add_argument(
        "--suite",
        choices=[
            "grouping", "audit-readonly", "tiebreak", "consolidate-tracer",
            "fk-integrity", "correction-precedence", "reparent-and-skip",
            "pairing-preservation", "all",
        ],
        default="all",
    )
    args = parser.parse_args()

    suites = {
        "grouping": suite_grouping,
        "audit-readonly": suite_audit_readonly,
        "tiebreak": suite_tiebreak,
        "consolidate-tracer": suite_consolidate_tracer,
        "fk-integrity": suite_fk_integrity,
        "correction-precedence": suite_correction_precedence,
        "reparent-and-skip": suite_reparent_and_skip,
        "pairing-preservation": suite_pairing_preservation,
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
