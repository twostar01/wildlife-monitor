"""
verify_raw_cleanup.py — stdlib-only verification harness for recurring NAS
raw_recordings cleanup (CLEANUP-02, CLEANUP-03).

Suite `migration` proves the upgrade path against synthetic legacy databases
(a fresh database.init_db() call cannot exercise an ALTER-TABLE migration,
since CREATE-TABLE-IF-NOT-EXISTS is a no-op against an already-created
table) — each case hand-builds a pre-phase table shape with a raw sqlite3
connection, then calls database.init_db() and asserts the new columns land
and existing data survives.

Suite `invariant` proves get_raw_cleanup_candidates(), mark_raw_purged(),
record_raw_cleanup_stats() and the get_storage_stats() extension against a
database.init_db()-built fixture database.

06-03 extends this same file with `source` and `paths` suites — the suite
registry is a dict so adding entries is a one-line change.

Never touches the production database file. Every suite builds its own temp
database under tempfile.mkdtemp(), removed afterwards.

Usage:
    python scripts/verify_raw_cleanup.py --suite migration|invariant|all
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
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


@contextmanager
def _empty_sqlite_file():
    """Yield a path to a fresh, empty temp SQLite file (no schema at all)."""
    tmpdir = tempfile.mkdtemp()
    try:
        yield os.path.join(tmpdir, "legacy.db")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _iso(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _insert_video(
    filename,
    filepath=None,
    camera_name="CamA",
    recorded_at=None,
    processed_at="2026-07-29T00:00:00",
    file_purged_at=None,
    raw_purged_at=None,
    file_size_mb=100.0,
):
    """Insert a videos row via database.get_conn() with an explicit column
    list, giving each invariant case precise control over the fields that
    drive eligibility. Returns the new row id."""
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO videos (filename, filepath, camera_name, recorded_at, "
            "processed_at, file_purged_at, raw_purged_at, file_size_mb) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                filename, filepath, camera_name, recorded_at,
                processed_at, file_purged_at, raw_purged_at, file_size_mb,
            ),
        )
        return cur.lastrowid


# ── migration suite ──────────────────────────────────────────────────────

def _case_m1_runs_migration():
    """M1: a pre-phase runs table gains the three raw_cleanup_* columns and
    keeps its pre-existing row."""
    case_id = "migration/M1-runs-migration"
    with _empty_sqlite_file() as db_path:
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE runs (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time            TEXT NOT NULL,
                end_time              TEXT,
                status                TEXT,
                "trigger"             TEXT NOT NULL DEFAULT 'scheduled',
                videos_processed      INTEGER DEFAULT 0,
                detections_found      INTEGER DEFAULT 0,
                error_summary         TEXT,
                cameras_json          TEXT,
                offline_cameras_json  TEXT
            );
        """)
        original_start = "2026-06-01T08:00:00"
        cur = conn.execute(
            """INSERT INTO runs (start_time, "trigger") VALUES (?, ?)""",
            (original_start, "scheduled"),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()

        database.init_db(db_path)

        conn = sqlite3.connect(db_path)
        run_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        row = conn.execute(
            "SELECT start_time FROM runs WHERE id=?", (row_id,)
        ).fetchone()
        conn.close()

        expected = {"raw_cleanup_removed", "raw_cleanup_gb", "raw_cleanup_skipped"}
        ok = (
            expected.issubset(set(run_cols))
            and row is not None
            and row[0] == original_start
        )
        _check(case_id, ok, f"run_cols={run_cols}, row={row}")
        return ok


def _case_m2_videos_alter():
    """M2: a pre-phase videos table (no raw_purged_at, nullable filepath)
    gains raw_purged_at and keeps its pre-existing row."""
    case_id = "migration/M2-videos-alter"
    with _empty_sqlite_file() as db_path:
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE videos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                filepath        TEXT UNIQUE,
                camera_name     TEXT,
                file_size_mb    REAL,
                duration_secs   REAL,
                recorded_at     TEXT,
                processed_at    TEXT NOT NULL,
                has_animal      INTEGER DEFAULT 0,
                has_person      INTEGER DEFAULT 0,
                kept            INTEGER DEFAULT 0,
                thumbnail_path  TEXT,
                frame_count     INTEGER DEFAULT 0,
                file_purged_at  TEXT,
                lens_index      INTEGER,
                paired_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                needs_reprocess INTEGER DEFAULT 0
            );
        """)
        cur = conn.execute(
            "INSERT INTO videos (filename, filepath, processed_at) VALUES (?, ?, ?)",
            ("clip.mp4", "/nas/archive/clip.mp4", "2026-06-01T08:00:00"),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()

        database.init_db(db_path)

        conn = sqlite3.connect(db_path)
        vid_cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        row = conn.execute(
            "SELECT filename FROM videos WHERE id=?", (row_id,)
        ).fetchone()
        conn.close()

        ok = "raw_purged_at" in vid_cols and row is not None and row[0] == "clip.mp4"
        _check(case_id, ok, f"vid_cols={vid_cols}, row={row}")
        return ok


def _case_m3_legacy_rebuild():
    """M3: a legacy videos table still carrying filepath NOT NULL UNIQUE
    survives the videos_new rebuild with raw_purged_at added and the NOT
    NULL constraint dropped. init_db() is called twice to prove idempotence."""
    case_id = "migration/M3-legacy-rebuild"
    with _empty_sqlite_file() as db_path:
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE videos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                filename        TEXT NOT NULL,
                filepath        TEXT NOT NULL UNIQUE,
                camera_name     TEXT,
                file_size_mb    REAL,
                duration_secs   REAL,
                recorded_at     TEXT,
                processed_at    TEXT,
                has_animal      INTEGER DEFAULT 0,
                has_person      INTEGER DEFAULT 0,
                kept            INTEGER DEFAULT 0,
                thumbnail_path  TEXT,
                frame_count     INTEGER,
                file_purged_at  TEXT,
                lens_index      INTEGER,
                paired_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                needs_reprocess INTEGER DEFAULT 0
            );
        """)
        cur = conn.execute(
            "INSERT INTO videos (filename, filepath, processed_at) VALUES (?, ?, ?)",
            ("legacy.mp4", "/nas/archive/legacy.mp4", "2026-06-01T08:00:00"),
        )
        row_id = cur.lastrowid
        conn.commit()
        conn.close()

        database.init_db(db_path)
        database.init_db(db_path)  # second call proves idempotence

        conn = sqlite3.connect(db_path)
        vid_info = conn.execute("PRAGMA table_info(videos)").fetchall()
        vid_cols = [r[1] for r in vid_info]
        filepath_notnull = next((r[3] for r in vid_info if r[1] == "filepath"), None)
        row = conn.execute(
            "SELECT id, filename FROM videos WHERE id=?", (row_id,)
        ).fetchone()
        conn.close()

        ok = (
            "raw_purged_at" in vid_cols
            and filepath_notnull == 0
            and row is not None
            and row[0] == row_id
            and row[1] == "legacy.mp4"
        )
        _check(
            case_id, ok,
            f"vid_cols={vid_cols}, filepath_notnull={filepath_notnull}, row={row}",
        )
        return ok


def _case_m4_nullability():
    """M4: raw_cleanup_* columns carry no DEFAULT clause, and a freshly
    inserted runs row reads NULL — not 0 — for raw_cleanup_removed."""
    case_id = "migration/M4-nullability"
    with _fixture_db() as db_path:
        conn = sqlite3.connect(db_path)
        run_info = conn.execute("PRAGMA table_info(runs)").fetchall()
        dflt_values = {
            r[1]: r[4] for r in run_info
            if r[1] in ("raw_cleanup_removed", "raw_cleanup_gb", "raw_cleanup_skipped")
        }
        conn.close()

        run_id = database.record_run_start("manual")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT raw_cleanup_removed FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        conn.close()

        ok = (
            all(v is None for v in dflt_values.values())
            and len(dflt_values) == 3
            and row is not None
            and row[0] is None
        )
        _check(case_id, ok, f"dflt_values={dflt_values}, row={row}")
        return ok


def suite_migration():
    """Four cases proving the upgrade path against synthetic legacy databases."""
    total = 4
    passed = sum([
        _case_m1_runs_migration(),
        _case_m2_videos_alter(),
        _case_m3_legacy_rebuild(),
        _case_m4_nullability(),
    ])
    return (passed, total)


# ── invariant suite ──────────────────────────────────────────────────────

def suite_invariant():
    """Eleven cases proving get_raw_cleanup_candidates(), mark_raw_purged(),
    record_raw_cleanup_stats() and get_storage_stats() against a fixture DB."""
    passed = 0
    total = 11

    # I1: old row with non-NULL filepath is returned by get_raw_cleanup_candidates(14)
    case_id = "invariant/I1-old-row-returned"
    with _fixture_db():
        vid = _insert_video("old.mp4", filepath="/nas/archive/old.mp4", recorded_at=_iso(30))
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid in result_ids
        _check(case_id, ok, f"result_ids={result_ids}, expected={vid}")
        if ok:
            passed += 1

    # I2: recent row is not returned
    case_id = "invariant/I2-recent-row-excluded"
    with _fixture_db():
        vid = _insert_video("recent.mp4", filepath="/nas/archive/recent.mp4", recorded_at=_iso(3))
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid not in result_ids
        _check(case_id, ok, f"result_ids={result_ids}, unexpected={vid}")
        if ok:
            passed += 1

    # I3: NULL filepath is excluded even when old
    case_id = "invariant/I3-null-filepath-excluded"
    with _fixture_db():
        vid = _insert_video("nullpath.mp4", filepath=None, recorded_at=_iso(30))
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid not in result_ids
        _check(case_id, ok, f"result_ids={result_ids}, unexpected={vid}")
        if ok:
            passed += 1

    # I4: file_purged_at set is excluded even when old
    case_id = "invariant/I4-file-purged-excluded"
    with _fixture_db():
        vid = _insert_video(
            "purged.mp4", filepath="/nas/archive/purged.mp4",
            recorded_at=_iso(30), file_purged_at=_iso(1),
        )
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid not in result_ids
        _check(case_id, ok, f"result_ids={result_ids}, unexpected={vid}")
        if ok:
            passed += 1

    # I5: raw_purged_at already set is excluded even when old — never revisit
    case_id = "invariant/I5-raw-purged-excluded"
    with _fixture_db():
        vid = _insert_video(
            "already_raw_purged.mp4", filepath="/nas/archive/arp.mp4",
            recorded_at=_iso(30), raw_purged_at=_iso(1),
        )
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid not in result_ids
        _check(case_id, ok, f"result_ids={result_ids}, unexpected={vid}")
        if ok:
            passed += 1

    # I6: recorded_at NULL falls back to processed_at (IN-05); malformed recorded_at too
    case_id = "invariant/I6-processed-at-fallback"
    with _fixture_db():
        vid_null = _insert_video(
            "null_recorded.mp4", filepath="/nas/archive/nr.mp4",
            recorded_at=None, processed_at=_iso(30),
        )
        vid_malformed = _insert_video(
            "malformed_recorded.mp4", filepath="/nas/archive/mr.mp4",
            recorded_at="not-a-date", processed_at=_iso(30),
        )
        result_ids = {r["id"] for r in database.get_raw_cleanup_candidates(14)}
        ok = vid_null in result_ids and vid_malformed in result_ids
        _check(
            case_id, ok,
            f"result_ids={result_ids}, expected both {vid_null} and {vid_malformed}",
        )
        if ok:
            passed += 1

    # I7: retention_days=0 disables cleanup entirely, even when eligible rows exist
    case_id = "invariant/I7-zero-retention-disables"
    with _fixture_db():
        _insert_video("eligible.mp4", filepath="/nas/archive/eligible.mp4", recorded_at=_iso(30))
        result = database.get_raw_cleanup_candidates(0)
        ok = result == []
        _check(case_id, ok, f"result={result}")
        if ok:
            passed += 1

    # I8: mark_raw_purged stamps raw_purged_at, leaves filepath and file_purged_at alone
    case_id = "invariant/I8-mark-raw-purged"
    with _fixture_db():
        original_path = "/nas/archive/markme.mp4"
        vid = _insert_video("markme.mp4", filepath=original_path, recorded_at=_iso(30))
        database.mark_raw_purged(vid)
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT filepath, file_purged_at, raw_purged_at FROM videos WHERE id=?",
                (vid,),
            ).fetchone()
        raw_purged_parses = False
        try:
            datetime.fromisoformat(row["raw_purged_at"])
            raw_purged_parses = True
        except (ValueError, TypeError):
            pass
        ok = (
            raw_purged_parses
            and row["filepath"] == original_path
            and row["file_purged_at"] is None
        )
        _check(case_id, ok, f"row={dict(row)}")
        if ok:
            passed += 1

    # I9: record_raw_cleanup_stats touches only the named run row's three columns
    case_id = "invariant/I9-record-raw-cleanup-stats"
    with _fixture_db():
        other_run_id = database.record_run_start("scheduled")
        run_id = database.record_run_start("manual")
        database.record_raw_cleanup_stats(run_id, 7, 1.5, 2)
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT raw_cleanup_removed, raw_cleanup_gb, raw_cleanup_skipped, "
                "start_time, \"trigger\" FROM runs WHERE id=?", (run_id,),
            ).fetchone()
            other_row = conn.execute(
                "SELECT raw_cleanup_removed, raw_cleanup_gb, raw_cleanup_skipped "
                "FROM runs WHERE id=?", (other_run_id,),
            ).fetchone()
        ok = (
            row["raw_cleanup_removed"] == 7
            and row["raw_cleanup_gb"] == 1.5
            and row["raw_cleanup_skipped"] == 2
            and row["trigger"] == "manual"
            and row["start_time"] is not None
            and other_row["raw_cleanup_removed"] is None
            and other_row["raw_cleanup_gb"] is None
            and other_row["raw_cleanup_skipped"] is None
        )
        _check(case_id, ok, f"row={dict(row)}, other_row={dict(other_row)}")
        if ok:
            passed += 1

    # I10: get_storage_stats() reflects raw-purge marks without disturbing other keys
    case_id = "invariant/I10-storage-stats-raw-keys"
    with _fixture_db():
        vid_a = _insert_video(
            "raw_a.mp4", filepath="/nas/archive/raw_a.mp4",
            recorded_at=_iso(30), file_size_mb=200.0,
        )
        vid_b = _insert_video(
            "raw_b.mp4", filepath="/nas/archive/raw_b.mp4",
            recorded_at=_iso(30), file_size_mb=824.0,
        )
        _insert_video(
            "blank_c.mp4", filepath="/nas/archive/blanks/blank_c.mp4",
            recorded_at=_iso(30), file_size_mb=50.0,
        )
        before = database.get_storage_stats()
        database.mark_raw_purged(vid_a)
        database.mark_raw_purged(vid_b)
        after = database.get_storage_stats()

        expected_gb = round((200.0 + 824.0) / 1024, 2)
        ok = (
            "raw_purged_videos" in before and "raw_gb_reclaimed" in before
            and after["raw_purged_videos"] == 2
            and after["raw_gb_reclaimed"] == expected_gb
            and after["purged_videos"] == before["purged_videos"]
            and after["total_active_gb"] == before["total_active_gb"]
        )
        _check(case_id, ok, f"before={before}, after={after}, expected_gb={expected_gb}")
        if ok:
            passed += 1

    # I11: duplicate-filename siblings (D-02) — each candidate carries its OWN
    # filepath, and the NULL-filepath sibling is excluded by the ordinary filter.
    case_id = "invariant/I11-duplicate-siblings-own-filepath"
    with _fixture_db():
        stale_path = "/nas/archive/dup_stale.mp4"
        vid_stale = _insert_video(
            "dup.mp4", filepath=stale_path, camera_name="CamA", recorded_at=_iso(30),
        )
        vid_null = _insert_video(
            "dup.mp4", filepath=None, camera_name="CamA", recorded_at=_iso(30),
        )
        candidates = database.get_raw_cleanup_candidates(14)
        by_id = {c["id"]: c for c in candidates}
        ok = (
            vid_stale in by_id
            and by_id[vid_stale]["filepath"] == stale_path
            and vid_null not in by_id
        )
        _check(case_id, ok, f"candidates={candidates}")
        if ok:
            passed += 1

    return (passed, total)


SUITES = {
    "migration": suite_migration,
    "invariant": suite_invariant,
}


def main():
    parser = argparse.ArgumentParser(
        description="Recurring NAS raw_recordings cleanup verification harness (CLEANUP-02/03)"
    )
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()) + ["all"], default="all"
    )
    args = parser.parse_args()

    selected = SUITES.keys() if args.suite == "all" else [args.suite]

    all_passed = True
    for name in selected:
        passed, total = SUITES[name]()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            print(f"FAIL: {name} ({passed}/{total})")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
