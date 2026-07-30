"""
verify_archive_collision.py — stdlib-only verification harness for nas_sync.sh's
archive-collision reconciliation (DEDUP-02).

Parses nas_sync.sh as text to assert the shipped source has all 4 collision-clearing
UPDATE statements paired with a file_purged_at timestamp and collapsed to a single
transaction (suite `source`), then extracts those statements' SQL text out of the
script and executes them against a temp SQLite fixture DB built via database.init_db()
to prove the older row ends up with filepath NULL / file_purged_at set, the current
row is untouched, and the reconciled row is excluded from file_purged_at IS NULL
queries including database.get_purgeable_videos (suite `invariant`). Extracting the
statement text rather than re-typing it means the invariant suite cannot drift from
the SQL nas_sync.sh actually ships. Never touches the production database file.

Usage:
    python scripts/verify_archive_collision.py --suite source|invariant|all
"""

import argparse
import sqlite3
import sys
import tempfile
import os
import shutil
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


def _script_text():
    """Return nas_sync.sh's lines as a list of strings (no trailing newlines)."""
    script_path = Path(__file__).resolve().parents[1] / "nas_sync.sh"
    return script_path.read_text(encoding="utf-8").splitlines()


def _noncomment_lines(lines):
    """Drop lines whose lstripped form starts with '#'.

    The inline Python blocks in nas_sync.sh are commented in places, and an
    uncommented-only view is what makes the negative source assertion meaningful
    rather than self-invalidating.
    """
    return [line for line in lines if not line.lstrip().startswith("#")]


def _extract_collision_updates():
    """Extract the collision-reconciliation UPDATE SQL strings from nas_sync.sh.

    Scans the script text for lines containing both 'UPDATE videos' and
    'file_purged_at', then takes the text between the first and last double-quote
    on each matching line. Extracting rather than re-typing the statement means
    this suite cannot drift from the SQL the script actually ships — if someone
    edits nas_sync.sh, the invariant suite runs the edited statement.
    """
    statements = []
    for line in _script_text():
        if "UPDATE videos" in line and "file_purged_at" in line:
            first = line.find('"')
            last = line.rfind('"')
            if first != -1 and last != -1 and first != last:
                statements.append(line[first + 1:last])
    return statements


def suite_source():
    """Five cases parsing nas_sync.sh as text — see PLAN.md task 1 for spec."""
    passed = 0
    total = 5
    lines = _script_text()
    noncomment = _noncomment_lines(lines)

    # 1. source/four-corrected-collision-updates
    case_id = "source/four-corrected-collision-updates"
    corrected = [l for l in lines if "SET filepath=NULL, file_purged_at=?" in l]
    ok = len(corrected) == 4
    _check(case_id, ok, f"found {len(corrected)} corrected line(s), expected 4")
    if ok:
        passed += 1

    # 2. source/no-unpaired-filepath-nulling
    case_id = "source/no-unpaired-filepath-nulling"
    nulling_lines = [l for l in noncomment if "SET filepath=NULL" in l]
    unpaired = [l for l in nulling_lines if "file_purged_at" not in l]
    ok = len(unpaired) == 0
    _check(
        case_id, ok,
        f"{len(unpaired)} non-comment line(s) null filepath without file_purged_at",
    )
    if ok:
        passed += 1

    # 3. source/commit-count-halved
    case_id = "source/commit-count-halved"
    commit_count = sum(l.count("conn.commit()") for l in lines)
    ok = commit_count == 4
    _check(case_id, ok, f"found {commit_count} conn.commit() call(s), expected 4")
    if ok:
        passed += 1

    # 4. source/counter-initialised-both-blocks
    case_id = "source/counter-initialised-both-blocks"
    init_lines = [
        l for l in lines
        if "duplicate_recognized" in l and "archived" in l and l.rstrip().endswith("= 0")
    ]
    ok = len(init_lines) == 2
    _check(case_id, ok, f"found {len(init_lines)} counter-init line(s), expected 2")
    if ok:
        passed += 1

    # 5. source/counter-in-both-summaries
    case_id = "source/counter-in-both-summaries"
    summary_lines = [l for l in lines if "Duplicates reconciled:" in l]
    ok = len(summary_lines) == 2
    _check(
        case_id, ok,
        f"found {len(summary_lines)} summary line(s) referencing the counter, expected 2",
    )
    if ok:
        passed += 1

    return (passed, total)


def suite_invariant():
    """Four cases executing SQL extracted from nas_sync.sh against a fixture DB."""
    passed = 0
    total = 4

    statements = _extract_collision_updates()

    # 1. invariant/extracted-statements-found
    case_id = "invariant/extracted-statements-found"
    ok = len(statements) == 4
    _check(case_id, ok, f"found {len(statements)} extracted statement(s), expected 4")
    if ok:
        passed += 1

    if len(statements) < 4:
        # Degrade cleanly rather than crash — nas_sync.sh has not been fixed yet.
        detail = f"only {len(statements)} statement(s) extracted; nas_sync.sh not yet fixed"
        for remaining_id in (
            "invariant/older-row-marked-purged",
            "invariant/current-row-untouched",
            "invariant/reconciled-row-not-pending",
        ):
            _check(remaining_id, False, detail)
        return (passed, total)

    statement = statements[0]
    dest_path = "/mnt/nas/wildlife_archive/frontdoor/2026/07/20/clip.mp4"
    local_staging_path = "/local_videos/frontdoor/2026/07/20/clip.mp4"

    with _fixture_db() as db_path:
        with database.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO videos (filename, filepath, camera_name, processed_at, "
                "recorded_at, kept, has_animal, has_person, file_purged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                ("clip.mp4", dest_path, "frontdoor", "2026-07-19T10:00:00",
                 "2026-07-19T10:00:00", 0, 0, 0),
            )
            older_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO videos (filename, filepath, camera_name, processed_at, "
                "recorded_at, kept, has_animal, has_person, file_purged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                ("clip.mp4", local_staging_path, "frontdoor", "2026-07-19T10:00:01",
                 "2026-07-19T10:00:01", 1, 0, 0),
            )
            current_id = cur.lastrowid

        # Execute the extracted statement the way nas_sync.sh itself does — a plain
        # sqlite3.connect(), not database.get_conn() — mirroring the script's
        # deliberate out-of-process connection style.
        timestamp = "2026-07-29T12:00:00"
        raw_conn = sqlite3.connect(db_path)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute(statement, (timestamp, dest_path, current_id))
        raw_conn.commit()
        raw_conn.close()

        with database.get_conn() as conn:
            older_row = conn.execute(
                "SELECT filepath, file_purged_at FROM videos WHERE id=?", (older_id,)
            ).fetchone()
            current_row = conn.execute(
                "SELECT filepath, file_purged_at FROM videos WHERE id=?", (current_id,)
            ).fetchone()

        # 2. invariant/older-row-marked-purged
        case_id = "invariant/older-row-marked-purged"
        ok = older_row["filepath"] is None and older_row["file_purged_at"] is not None
        _check(case_id, ok, f"older_row={dict(older_row)}")
        if ok:
            passed += 1

        # 3. invariant/current-row-untouched
        case_id = "invariant/current-row-untouched"
        ok = (
            current_row["filepath"] == local_staging_path
            and current_row["file_purged_at"] is None
        )
        _check(case_id, ok, f"current_row={dict(current_row)}")
        if ok:
            passed += 1

        # 4. invariant/reconciled-row-not-pending
        case_id = "invariant/reconciled-row-not-pending"
        with database.get_conn() as conn:
            pending = conn.execute(
                "SELECT id FROM videos WHERE filepath IS NOT NULL AND file_purged_at IS NULL"
            ).fetchall()
        pending_ids = {r["id"] for r in pending}
        purgeable = database.get_purgeable_videos(
            blank_days=1, blank_gb=None, kept_days=1, kept_gb=None, grace_days=0
        )
        purgeable_ids = {v["id"] for v in purgeable["blank"]} | {
            v["id"] for v in purgeable["kept"]
        }
        ok = older_id not in pending_ids and older_id not in purgeable_ids
        _check(
            case_id, ok,
            f"older_id={older_id}, pending_ids={pending_ids}, purgeable_ids={purgeable_ids}",
        )
        if ok:
            passed += 1

    return (passed, total)


def main():
    parser = argparse.ArgumentParser(
        description="Archive-collision reconciliation verification harness (DEDUP-02)"
    )
    parser.add_argument(
        "--suite", choices=["source", "invariant", "all"], default="all"
    )
    args = parser.parse_args()

    suites = {
        "source": suite_source,
        "invariant": suite_invariant,
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
