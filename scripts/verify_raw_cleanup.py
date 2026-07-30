"""
verify_raw_cleanup.py — stdlib-only verification harness for recurring NAS
raw_recordings cleanup (CLEANUP-01, CLEANUP-02, CLEANUP-03).

Suite `migration` proves the upgrade path against synthetic legacy databases
(a fresh database.init_db() call cannot exercise an ALTER-TABLE migration,
since CREATE-TABLE-IF-NOT-EXISTS is a no-op against an already-created
table) — each case hand-builds a pre-phase table shape with a raw sqlite3
connection, then calls database.init_db() and asserts the new columns land
and existing data survives.

Suite `invariant` proves get_raw_cleanup_candidates(), mark_raw_purged(),
record_raw_cleanup_stats() and the get_storage_stats() extension against a
database.init_db()-built fixture database.

Suite `source` parses nas_sync.sh as text to prove the shipped raw-cleanup
block sits in the D-05-mandated position, accesses the database only via
named database.py imports (never a raw sqlite3.connect()), delimits its two
pure decision functions with extraction sentinels containing no shell
interpolation, only calls unlink() after every guard has run, never raises
or calls sys.exit (so it can never fail the nightly systemd unit), writes
record_raw_cleanup_stats() exactly once and only after the per-row loop
completes, and falls back to a retention default of 0 (inert), not 14.

Suite `paths` extracts raw_path_for() and verify_raw_candidate() straight
out of nas_sync.sh (the text between the `# >>> raw-cleanup-verify-fns` /
`# <<< raw-cleanup-verify-fns` sentinels), execs that text into a fresh
namespace, and exercises both happy paths plus every skip reason against
real temp filesystem fixtures — proving the shipped decision logic (not a
re-typed copy of it) never deletes a file it shouldn't.

Never touches the production database file. Every suite builds its own temp
database/filesystem under tempfile.mkdtemp(), removed afterwards.

Usage:
    python scripts/verify_raw_cleanup.py --suite migration|invariant|source|paths|all
"""

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import unittest.mock as mock
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


# ── source suite ─────────────────────────────────────────────────────────

def _script_text():
    """Return nas_sync.sh's lines as a list of strings (no trailing newlines)."""
    script_path = Path(__file__).resolve().parents[1] / "nas_sync.sh"
    return script_path.read_text(encoding="utf-8").splitlines()


def _noncomment_lines(lines):
    """Drop lines whose lstripped form starts with '#'.

    Mirrors verify_archive_collision.py's helper of the same name so a
    commented-out line can never satisfy or invalidate a source assertion.
    """
    return [line for line in lines if not line.lstrip().startswith("#")]


def _find_index(lines, predicate, start=0):
    """Return the first index >= start where predicate(line) is True, or -1."""
    for i in range(start, len(lines)):
        if predicate(lines[i]):
            return i
    return -1


def _raw_cleanup_block_extent(lines):
    """Return (start, end) indices (inclusive) of the raw-cleanup block, or
    (-1, -1) if the block's divider comment or heredoc terminator can't be
    found."""
    start = _find_index(
        lines, lambda l: "Raw recordings cleanup (verify-then-delete, D-05)" in l
    )
    if start == -1:
        return (-1, -1)
    end = _find_index(lines, lambda l: l.strip() == "PYEOF", start=start)
    return (start, end) if end != -1 else (-1, -1)


def suite_source():
    """Seven cases parsing nas_sync.sh as text — see PLAN.md task 2 for spec."""
    passed = 0
    total = 7
    lines = _script_text()

    # SRC1 (D-05 ordering, load-bearing): the raw-cleanup block sits strictly
    # after the blank-archive block's heredoc terminator and strictly before
    # the retention-purge block's announcement message.
    case_id = "source/SRC1-d05-ordering"
    blank_summary_idx = _find_index(lines, lambda l: "Blank archived:" in l)
    blank_terminator_idx = (
        _find_index(lines, lambda l: l.strip() == "PYEOF", start=blank_summary_idx)
        if blank_summary_idx != -1
        else -1
    )
    raw_block_idx, raw_block_end_idx = _raw_cleanup_block_extent(lines)
    retention_idx = _find_index(lines, lambda l: "Running retention policy purge" in l)
    ok = (
        blank_terminator_idx != -1
        and raw_block_idx != -1
        and retention_idx != -1
        and blank_terminator_idx < raw_block_idx < retention_idx
    )
    _check(
        case_id, ok,
        f"blank_terminator_idx={blank_terminator_idx}, raw_block_idx={raw_block_idx}, "
        f"retention_idx={retention_idx}",
    )
    if ok:
        passed += 1

    block_lines = (
        lines[raw_block_idx:raw_block_end_idx + 1]
        if raw_block_idx != -1 and raw_block_end_idx != -1
        else []
    )
    block_noncomment = _noncomment_lines(block_lines)

    # SRC2 (DB access style): no direct sqlite3.connect() inside the block's
    # extent, and the four named functions are imported from database.
    case_id = "source/SRC2-db-access-style"
    no_direct_connect = not any("sqlite3.connect(" in l for l in block_noncomment)
    has_named_imports = any(
        "from database import" in l
        and "get_last_run" in l
        and "get_raw_cleanup_candidates" in l
        and "mark_raw_purged" in l
        and "record_raw_cleanup_stats" in l
        for l in block_lines
    )
    ok = bool(block_lines) and no_direct_connect and has_named_imports
    _check(case_id, ok, f"no_direct_connect={no_direct_connect}, has_named_imports={has_named_imports}")
    if ok:
        passed += 1

    # SRC3 (sentinels): both occur exactly once, opening precedes closing,
    # and no '$' character appears strictly between them.
    case_id = "source/SRC3-sentinels"
    open_idxs = [i for i, l in enumerate(lines) if l.strip() == "# >>> raw-cleanup-verify-fns"]
    close_idxs = [i for i, l in enumerate(lines) if l.strip() == "# <<< raw-cleanup-verify-fns"]
    ok = (
        len(open_idxs) == 1
        and len(close_idxs) == 1
        and open_idxs[0] < close_idxs[0]
        and not any("$" in l for l in lines[open_idxs[0] + 1:close_idxs[0]])
    )
    _check(case_id, ok, f"open_idxs={open_idxs}, close_idxs={close_idxs}")
    if ok:
        passed += 1

    # SRC4 (guard ordering): unlink( only appears after every guard check.
    case_id = "source/SRC4-guard-ordering"
    containment_idx = _find_index(lines, lambda l: "is_relative_to(video_root_resolved)" in l)
    same_file_idx = _find_index(lines, lambda l: "os.path.samefile(raw_path, archive_path)" in l)
    archive_exists_idx = _find_index(lines, lambda l: "if not archive_path.exists():" in l)
    size_cmp_idx = _find_index(
        lines, lambda l: "raw_path.stat().st_size != archive_path.stat().st_size" in l
    )
    unlink_idx = _find_index(lines, lambda l: "raw_path.unlink()" in l)
    guard_idxs = [containment_idx, same_file_idx, archive_exists_idx, size_cmp_idx, unlink_idx]
    ok = (
        min(guard_idxs) != -1
        and unlink_idx > containment_idx
        and unlink_idx > same_file_idx
        and unlink_idx > archive_exists_idx
        and unlink_idx > size_cmp_idx
    )
    _check(
        case_id, ok,
        f"containment={containment_idx}, same_file={same_file_idx}, "
        f"archive_exists={archive_exists_idx}, size_cmp={size_cmp_idx}, unlink={unlink_idx}",
    )
    if ok:
        passed += 1

    # SRC5 (no hard failure): no sys.exit inside the block's extent, and if a
    # RAW_CLEANUP_EXIT shell guard exists it must never call `exit` on
    # failure — it should warn and fall through to the retention-purge
    # block, unlike ARCHIVE_EXIT/BLANK_ARCHIVE_EXIT which do `exit 1`
    # (WR-01 fix: the guard now inspects the heredoc's exit code so a crash
    # inside the block is surfaced with a warning instead of silently
    # aborting under set -e, but it still can never fail the nightly
    # systemd unit).
    case_id = "source/SRC5-no-hard-failure"
    no_sys_exit = not any("sys.exit" in l for l in block_lines)
    exit_guard_idxs = [i for i, l in enumerate(lines) if "RAW_CLEANUP_EXIT=" in l]
    if exit_guard_idxs:
        guard_start = exit_guard_idxs[0]
        guard_end = _find_index(lines, lambda l: l.strip() == "", start=guard_start)
        guard_end = guard_end if guard_end != -1 else len(lines)
        guard_block = lines[guard_start:guard_end]
        guard_never_exits = not any("exit" in l for l in guard_block)
    else:
        guard_never_exits = True
    ok = bool(block_lines) and no_sys_exit and guard_never_exits
    _check(case_id, ok, f"no_sys_exit={no_sys_exit}, guard_never_exits={guard_never_exits}")
    if ok:
        passed += 1

    # SRC6 (stats write is single and post-loop): record_raw_cleanup_stats
    # appears exactly once, at a line index past the per-row for loop's body
    # (determined by dedenting back to the loop's own indentation level).
    case_id = "source/SRC6-stats-single-post-loop"
    stats_call_idxs = [i for i, l in enumerate(block_lines) if "record_raw_cleanup_stats(" in l]
    for_idx = _find_index(block_lines, lambda l: l.strip().startswith("for row in candidates:"))
    loop_indent = (
        len(block_lines[for_idx]) - len(block_lines[for_idx].lstrip()) if for_idx != -1 else -1
    )
    loop_end_idx = -1
    if for_idx != -1:
        for i in range(for_idx + 1, len(block_lines)):
            stripped = block_lines[i].strip()
            if not stripped:
                continue
            indent = len(block_lines[i]) - len(block_lines[i].lstrip())
            if indent <= loop_indent:
                loop_end_idx = i
                break
    ok = (
        len(stats_call_idxs) == 1
        and for_idx != -1
        and loop_end_idx != -1
        and stats_call_idxs[0] > loop_end_idx
    )
    _check(
        case_id, ok,
        f"stats_call_idxs={stats_call_idxs}, for_idx={for_idx}, loop_end_idx={loop_end_idx}",
    )
    if ok:
        passed += 1

    # SRC7 (absent-key fallback): the retention setting falls back to 0, not
    # 14 — the block stays inert until explicitly configured.
    case_id = "source/SRC7-fallback-zero"
    ok = any(
        'raw_days = settings.get("raw_recordings_retention_days") or 0' in l
        for l in block_lines
    )
    _check(case_id, ok, "fallback-to-zero line not found in the raw-cleanup block")
    if ok:
        passed += 1

    return (passed, total)


# ── paths suite ──────────────────────────────────────────────────────────

def _extract_verify_fns_source():
    """Return the dedented text sliced from nas_sync.sh between the two
    `raw-cleanup-verify-fns` sentinel comments, or None if not found."""
    lines = _script_text()
    start = _find_index(lines, lambda l: l.strip() == "# >>> raw-cleanup-verify-fns")
    end = _find_index(lines, lambda l: l.strip() == "# <<< raw-cleanup-verify-fns")
    if start == -1 or end == -1 or end <= start:
        return None
    body = "\n".join(lines[start + 1:end])
    return textwrap.dedent(body)


def _load_verify_fns():
    """exec() the extracted sentinel-delimited text into a fresh namespace
    and return (raw_path_for, verify_raw_candidate), or (None, None) if the
    functions can't be found — never a re-typed copy of either body."""
    src = _extract_verify_fns_source()
    if not src:
        return (None, None)
    namespace = {"Path": Path, "os": os}
    exec(compile(src, "<raw-cleanup-verify-fns>", "exec"), namespace)
    return (namespace.get("raw_path_for"), namespace.get("verify_raw_candidate"))


def _build_nas_layout(tmpdir):
    """Lay out a miniature NAS under tmpdir: a video root, an archive root,
    and a blanks root nested inside the archive root. Returns the three
    Path objects, all pre-created."""
    video_root = Path(tmpdir) / "nas_video"
    archive_root = Path(tmpdir) / "wildlife_archive"
    blank_root = archive_root / "blanks"
    video_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    blank_root.mkdir(parents=True, exist_ok=True)
    return video_root, archive_root, blank_root


def _write_pair(video_root, root, rel, raw_content=b"hello", archive_content=None):
    """Write matching (or deliberately mismatched) raw/archive files at the
    same relative path under video_root and root. Returns (raw_path, archive_path)."""
    if archive_content is None:
        archive_content = raw_content
    archive_path = root / rel
    raw_path = video_root / rel
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(archive_content)
    raw_path.write_bytes(raw_content)
    return raw_path, archive_path


def _case_p1_happy_kept():
    """P1: identical-content raw/archive files at mirrored paths yield ok."""
    case_id = "paths/P1-happy-kept"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "01" / "clip.mp4"
        raw_path, archive_path = _write_pair(video_root, archive_root, rel)
        result_path, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root)
        )
        expected = (video_root / rel).resolve()
        ok = reason == "ok" and result_path is not None and result_path.resolve() == expected
        _check(case_id, ok, f"reason={reason}, result_path={result_path}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p2_happy_blank():
    """P2: same for a filepath under the blanks root — matched before the
    archive root, with no leading 'blanks' segment retained in the suffix."""
    case_id = "paths/P2-happy-blank"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "02" / "blank.mp4"
        raw_path, archive_path = _write_pair(video_root, blank_root, rel)
        result_path, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root)
        )
        expected = (video_root / rel).resolve()
        no_blanks_prefix = result_path is not None and "blanks" not in result_path.parts
        ok = (
            reason == "ok"
            and result_path is not None
            and result_path.resolve() == expected
            and no_blanks_prefix
        )
        _check(case_id, ok, f"reason={reason}, result_path={result_path}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p3_missing_archive():
    """P3 (D-04): raw exists but archive doesn't yields no_archive, never ok."""
    case_id = "paths/P3-missing-archive"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "03" / "clip.mp4"
        raw_path = video_root / rel
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(b"hello")
        archive_path = archive_root / rel  # deliberately never created
        _, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root)
        )
        ok = reason == "no_archive"
        _check(case_id, ok, f"reason={reason}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p4_size_mismatch():
    """P4 (D-04): raw and archive exist but differ in byte length."""
    case_id = "paths/P4-size-mismatch"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "04" / "clip.mp4"
        _write_pair(
            video_root, archive_root, rel,
            raw_content=b"hello", archive_content=b"hello world",
        )
        archive_path = archive_root / rel
        _, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root)
        )
        ok = reason == "size_mismatch"
        _check(case_id, ok, f"reason={reason}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p5_missing_raw():
    """P5: archive exists but raw doesn't yields no_raw."""
    case_id = "paths/P5-missing-raw"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "05" / "clip.mp4"
        archive_path = archive_root / rel
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(b"hello")
        # raw file intentionally not created
        _, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root)
        )
        ok = reason == "no_raw"
        _check(case_id, ok, f"reason={reason}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p6_unmapped():
    """P6 (Pitfall 3 / D-02): a filepath under neither root yields unmapped,
    and the extracted function contains no duplicate-row-specific branch."""
    case_id = "paths/P6-unmapped"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        other_root = Path(tmpdir) / "unrelated"
        other_root.mkdir(parents=True, exist_ok=True)
        stray_path = other_root / "dup_stale.mp4"
        stray_path.write_bytes(b"hello")
        _, reason = verify_raw_candidate(
            str(stray_path), str(archive_root), str(blank_root), str(video_root)
        )
        src = _extract_verify_fns_source() or ""
        no_dup_branch = "duplicate" not in src.lower()
        ok = reason == "unmapped" and no_dup_branch
        _check(case_id, ok, f"reason={reason}, no_dup_branch={no_dup_branch}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p7_traversal():
    """P7 (T-06-01): a relative suffix containing parent-directory segments
    that would re-compose outside the video root yields escapes_root, even
    when a real file exists at the escaped location."""
    case_id = "paths/P7-traversal"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        escaped_rel = "sub/../../escaped/secret.mp4"
        crafted_filepath = str(archive_root) + "/" + escaped_rel

        # A real file at the escaped location, so the case would genuinely
        # pass (wrongly) without the containment guard.
        escaped_target = video_root.parent / "escaped" / "secret.mp4"
        escaped_target.parent.mkdir(parents=True, exist_ok=True)
        escaped_target.write_bytes(b"hello")

        Path(crafted_filepath).parent.mkdir(parents=True, exist_ok=True)
        Path(crafted_filepath).write_bytes(b"hello")

        _, reason = verify_raw_candidate(
            crafted_filepath, str(archive_root), str(blank_root), str(video_root)
        )
        ok = reason == "escapes_root"
        _check(case_id, ok, f"reason={reason}, crafted_filepath={crafted_filepath}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p8_same_file():
    """P8 (T-06-02, critical): video root and archive root are the same
    directory, so the reconstruction resolves onto the archive file itself —
    must yield same_file, never ok."""
    case_id = "paths/P8-same-file"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        shared_root = Path(tmpdir) / "shared"
        shared_root.mkdir(parents=True, exist_ok=True)
        blank_root = Path(tmpdir) / "blanks_unused"  # never matches
        rel = Path("CamA") / "clip.mp4"
        archive_path = shared_root / rel
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(b"hello")

        _, reason = verify_raw_candidate(
            str(archive_path), str(shared_root), str(blank_root), str(shared_root)
        )
        ok = reason == "same_file"
        _check(case_id, ok, f"reason={reason}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p9_symlink_root():
    """P9: a symlinked video root still resolves a legitimate candidate to
    ok — the containment guard must compare resolved paths on both sides."""
    case_id = "paths/P9-symlinked-root"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        real_video_dir = Path(tmpdir) / "real_nas_video"
        real_video_dir.mkdir(parents=True, exist_ok=True)
        video_root_link = Path(tmpdir) / "nas_video_symlink"
        try:
            os.symlink(real_video_dir, video_root_link, target_is_directory=True)
        except (OSError, NotImplementedError) as e:
            print(f"SKIP: {case_id} — platform does not permit symlink creation: {e}")
            return True

        archive_root = Path(tmpdir) / "wildlife_archive"
        blank_root = archive_root / "blanks"
        archive_root.mkdir(parents=True, exist_ok=True)
        blank_root.mkdir(parents=True, exist_ok=True)

        rel = Path("CamA") / "2026" / "07" / "09" / "clip.mp4"
        raw_path, archive_path = _write_pair(real_video_dir, archive_root, rel)

        _, reason = verify_raw_candidate(
            str(archive_path), str(archive_root), str(blank_root), str(video_root_link)
        )
        ok = reason == "ok"
        _check(case_id, ok, f"reason={reason}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p10_io_error():
    """P10 (T-06-16): a stat() failure on the raw file (simulating a
    transient NAS mount hiccup) is caught and converted to a non-ok reason,
    never ok — deterministic via a Path.stat() patch rather than a
    platform-specific permission trick."""
    case_id = "paths/P10-io-error"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel = Path("CamA") / "2026" / "07" / "10" / "clip.mp4"
        raw_path, archive_path = _write_pair(video_root, archive_root, rel)

        original_stat = Path.stat

        def flaky_stat(self, *args, **kwargs):
            if self == raw_path:
                raise OSError("simulated NAS mount hiccup")
            return original_stat(self, *args, **kwargs)

        try:
            with mock.patch.object(Path, "stat", flaky_stat):
                _, reason = verify_raw_candidate(
                    str(archive_path), str(archive_root), str(blank_root), str(video_root)
                )
        except Exception as e:
            _check(case_id, False, f"platform could not produce the condition: {e}")
            return False

        ok = reason != "ok"
        _check(case_id, ok, f"reason={reason}, expected a non-ok reason (io_error)")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_p11_row_isolation():
    """P11 (Pitfall 3): two rows whose filepath differs only in camera
    segment reconstruct to two different raw paths — never each other's."""
    case_id = "paths/P11-row-isolation"
    raw_path_for, verify_raw_candidate = _load_verify_fns()
    if raw_path_for is None:
        _check(case_id, False, "verify-fns not found in nas_sync.sh")
        return False
    tmpdir = tempfile.mkdtemp()
    try:
        video_root, archive_root, blank_root = _build_nas_layout(tmpdir)
        rel_a = Path("CamA") / "2026" / "07" / "11" / "dup.mp4"
        rel_b = Path("CamB") / "2026" / "07" / "11" / "dup.mp4"
        raw_a, archive_a = _write_pair(video_root, archive_root, rel_a, raw_content=b"AAAA")
        raw_b, archive_b = _write_pair(video_root, archive_root, rel_b, raw_content=b"BBBB")

        result_a = raw_path_for(str(archive_a), str(archive_root), str(blank_root), str(video_root))
        result_b = raw_path_for(str(archive_b), str(archive_root), str(blank_root), str(video_root))

        ok = (
            result_a is not None
            and result_b is not None
            and result_a.resolve() == raw_a.resolve()
            and result_b.resolve() == raw_b.resolve()
            and result_a.resolve() != result_b.resolve()
        )
        _check(case_id, ok, f"result_a={result_a}, result_b={result_b}")
        return ok
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def suite_paths():
    """Eleven cases exercising the extracted decision logic against real
    temp filesystems — every skip reason plus both happy paths."""
    total = 11
    passed = sum([
        _case_p1_happy_kept(),
        _case_p2_happy_blank(),
        _case_p3_missing_archive(),
        _case_p4_size_mismatch(),
        _case_p5_missing_raw(),
        _case_p6_unmapped(),
        _case_p7_traversal(),
        _case_p8_same_file(),
        _case_p9_symlink_root(),
        _case_p10_io_error(),
        _case_p11_row_isolation(),
    ])
    return (passed, total)


SUITES = {
    "migration": suite_migration,
    "invariant": suite_invariant,
    "source": suite_source,
    "paths": suite_paths,
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
