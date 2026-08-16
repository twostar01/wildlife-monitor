"""
migrate_stale_paths.py — one-time path-prefix migration for crops.crop_path
and videos.thumbnail_path.

Rewrites the stale `/home/nash/...` path prefix left behind by a
pre-existing, unrelated home-directory rename to the current
`/home/twostar/...` prefix. Pure string-prefix rewrite — no row deletion,
no data loss. Confirmed scope (2026-08-15): 4,274 of 17,298
crops.crop_path rows and 10,080 of 47,457 videos.thumbnail_path rows are
nash-prefixed; videos.filepath is confirmed out of scope (0 nash-prefixed
rows).

Unlike PATTERNS.md's illustrative single-statement description, this
script computes the rewritten path in Python via a leading-prefix slice
(rewrite_path()) rather than delegating to SQLite's built-in
string-substitution function, which rewrites every occurrence in a value,
not just the leading one — a stored path such as
`/home/nash/wildlife-monitor/data/crops/home/nash/x.jpg` would otherwise be
corrupted in its tail. See 11-01-PLAN.md's <planning_note> for the full
rationale.

Usage:
    python scripts/migrate_stale_paths.py --db data/wildlife.db
    python scripts/migrate_stale_paths.py --db data/wildlife.db \
        --apply --confirm-irreversible --snapshot-dir /tmp/snap --audit-log /tmp/audit.jsonl
"""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from sqlite3 import connect as sqlite_connect
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


STALE_PREFIX = "/home/nash/"
CURRENT_PREFIX = "/home/twostar/"
TARGETS = [("crops", "crop_path"), ("videos", "thumbnail_path")]


@dataclasses.dataclass
class StaleRow:
    table: str
    column: str
    row_id: int
    old_path: str
    new_path: str


def rewrite_path(path):
    """Leading-prefix slice: rewrite only a path that starts with
    STALE_PREFIX at position 0. Never a whole-string substitution — see
    the module docstring for why that would be wrong here (a non-leading
    occurrence must survive untouched)."""
    if isinstance(path, str) and path.startswith(STALE_PREFIX):
        return CURRENT_PREFIX + path[len(STALE_PREFIX):]
    return path


def select_stale_rows(conn):
    """Return every StaleRow across both TARGETS, sorted by (table, column,
    row_id) ascending. The LIKE predicate anchors at position 0 with no
    leading wildcard, so an embedded (non-leading) occurrence is never
    selected. NULL columns are excluded automatically: SQL LIKE against a
    NULL column yields NULL, not TRUE, so no separate NULL branch is
    needed."""
    rows = []
    for table, column in TARGETS:
        cursor = conn.execute(
            f"SELECT id, {column} FROM {table} WHERE {column} LIKE ? ORDER BY id ASC",
            (STALE_PREFIX + "%",),
        )
        for row_id, old_path in cursor.fetchall():
            rows.append(StaleRow(
                table=table,
                column=column,
                row_id=row_id,
                old_path=old_path,
                new_path=rewrite_path(old_path),
            ))
    return rows


def count_stale_rows(conn):
    """Return per-column and total counts of nash-prefixed rows, derived
    from the same LIKE predicate select_stale_rows() uses."""
    counts = {}
    total = 0
    for table, column in TARGETS:
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?",
            (STALE_PREFIX + "%",),
        ).fetchone()[0]
        counts[f"{table}.{column}"] = n
        total += n
    counts["total"] = total
    return counts


def find_collisions(conn, rows):
    """Return the subset of rows whose new_path already exists in
    crops.crop_path — the UNIQUE constraint guard. crops.crop_path is the
    only UNIQUE target column (database.py SCHEMA); a collision here would
    make the UPDATE fail at the database layer, so callers can use this to
    report a clear pre-write diagnostic instead of a raw IntegrityError."""
    collisions = []
    for row in rows:
        existing = conn.execute(
            "SELECT COUNT(*) FROM crops WHERE crop_path = ?", (row.new_path,)
        ).fetchone()[0]
        if existing:
            collisions.append(row)
    return collisions


def suffix_violations(rows):
    """Return the StaleRows where the text after STALE_PREFIX in old_path
    differs from the text after CURRENT_PREFIX in new_path. Must always be
    empty -- a non-empty result means rewrite_path() has been broken and
    the run must not proceed."""
    violations = []
    for row in rows:
        if row.old_path[len(STALE_PREFIX):] != row.new_path[len(CURRENT_PREFIX):]:
            violations.append(row)
    return violations


def non_path_digest(conn, table, exclude_column):
    """SHA-256 hex digest over every row's every column except
    exclude_column, ordered by id -- the "nothing else changed"
    fingerprint (ROADMAP Success Criterion 4's verification hook, used by
    plan 11-04). Each value is serialised via json.dumps(sort_keys=True) so
    NULL and the empty string hash distinctly, then joined by a NUL
    separator; rows are separated by a double-NUL. Stable across runs and
    independent of row insertion order because the SELECT is ordered by
    id."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    cols = [c for c in cols if c != exclude_column]
    col_list = ", ".join(cols)
    digest = hashlib.sha256()
    for row in conn.execute(f"SELECT {col_list} FROM {table} ORDER BY id ASC").fetchall():
        serialized = "\x00".join(json.dumps(v, sort_keys=True) for v in row)
        digest.update(serialized.encode("utf-8"))
        digest.update(b"\x00\x00")
    return digest.hexdigest()


def snapshot_db(src_path, dest_path):
    """Take an online backup of the database at src_path into dest_path
    using sqlite3.Connection.backup(). Copied verbatim (mechanism only)
    from backfill_dedup_videos.py: a plain file copy is wrong here because
    get_conn() sets PRAGMA journal_mode=WAL, and copying the main database
    file alone can miss data still sitting in the -wal file. Callers must
    treat a raised exception here as a hard abort before any write pass —
    the snapshot is the only rollback path for the database half of the
    operation."""
    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    src_conn = sqlite_connect(src_path)
    try:
        dest_conn = sqlite_connect(dest_path)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def open_audit_log(path):
    """Open a JSON-lines file for append. Raises OSError (uncaught) if the
    path can't be opened — callers in apply mode must treat that as a hard
    abort before any write: an unlogged irreversible operation cannot be
    reconstructed from the snapshot."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(path, "a", encoding="utf-8")


def write_audit_line(handle, payload):
    """Write one compact JSON object per line and flush immediately, so a
    crash mid-run still leaves every completed row's line durable on
    disk."""
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
    handle.flush()


def render_plan_report(counts, rows, applied, digests=None, as_json=False):
    """Print a Go/No-Go summary. applied=False describes what the run
    would do (future tense); applied=True describes what the run did
    (past tense) — never the reverse, per the FIX-03 / P-02 honesty lesson
    (backfill_dedup_videos.py's args.apply-branched summary). digests, if
    given, is a {table: hex_digest} mapping from non_path_digest() -- the
    "nothing else changed" fingerprint for each target table."""
    if as_json:
        payload = dict(counts)
        payload["applied"] = applied
        if digests:
            payload["non_path_digests"] = dict(digests)
        print(json.dumps(payload, sort_keys=True))
        return

    print("Stale Path Migration -- Go/No-Go Summary")
    print("=" * 44)
    for table, column in TARGETS:
        print(f"{table}.{column}: {counts.get(f'{table}.{column}', 0)}")
    print(f"Total: {counts.get('total', 0)}")
    if applied:
        print(f"Rows rewritten this run: {len(rows)}")
        print("Status: APPLIED -- rows were rewritten as counted above.")
    else:
        print(f"Rows that would be rewritten: {len(rows)}")
        print(
            "Status: DRY-RUN -- rows would be rewritten as counted above; "
            "nothing has been written."
        )
    if digests:
        label = "verified unchanged" if applied else "before write"
        for table, _column in TARGETS:
            print(f"non_path_digest[{table}] ({label}): {digests.get(table, '')}")
    print(f"RUNVAR: report_generated_at={datetime.now().isoformat()}")


def apply_rewrite(conn, rows, audit_handle):
    """Inside a single transaction on the passed connection, issue one
    parameterised UPDATE per row and write one audit line per row. Table
    and column names come only from the fixed TARGETS allowlist, never
    from user input; every value is a bound parameter. Returns the total
    rows changed."""
    changed = 0
    for row in rows:
        conn.execute(
            f"UPDATE {row.table} SET {row.column} = ? WHERE id = ?",
            (row.new_path, row.row_id),
        )
        changed += 1
        write_audit_line(audit_handle, {
            "table": row.table,
            "column": row.column,
            "row_id": row.row_id,
            "old_path": row.old_path,
            "new_path": row.new_path,
        })
    return changed


def verify_post_conditions(conn):
    """Return remaining_stale (total from count_stale_rows), fk_violations
    (PRAGMA foreign_key_check row count -- touches nothing this migration
    modifies, included because every prior production write in this
    project runs it, per PATTERNS.md), crops_rowcount, and
    videos_rowcount."""
    counts = count_stale_rows(conn)
    fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    crops_rowcount = conn.execute("SELECT COUNT(*) FROM crops").fetchone()[0]
    videos_rowcount = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    return {
        "remaining_stale": counts["total"],
        "fk_violations": fk_violations,
        "crops_rowcount": crops_rowcount,
        "videos_rowcount": videos_rowcount,
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="One-time /home/nash/ -> /home/twostar/ path prefix migration"
    )
    parser.add_argument("--db", default="data/wildlife.db", help="Path to the SQLite database")
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the real write -- requires --confirm-irreversible, "
             "--snapshot-dir and --audit-log too (default: dry-run plan only, "
             "nothing written)",
    )
    parser.add_argument(
        "--confirm-irreversible", action="store_true",
        help="Required together with --apply to actually rewrite rows. Two "
             "independent flags so no single mistyped or shell-history-recalled "
             "argument can start an irreversible pass.",
    )
    parser.add_argument(
        "--snapshot-dir", default=None,
        help="Directory for the pre-apply online DB backup (required for --apply)",
    )
    parser.add_argument(
        "--audit-log", default=None,
        help="Path to the JSONL audit log written during --apply (required for --apply)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    database.set_db_path(args.db)

    with database.get_conn() as conn:
        rows = select_stale_rows(conn)
        counts = count_stale_rows(conn)
        before_digests = {
            table: non_path_digest(conn, table, column) for table, column in TARGETS
        }

    violations = suffix_violations(rows)
    if violations:
        print(
            "ERROR: suffix_violations found -- rewrite_path() produced a value "
            "whose suffix does not match the original suffix. Aborting before "
            "any write.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v.table}.{v.column} id={v.row_id}: {v.old_path!r} -> {v.new_path!r}", file=sys.stderr)
        sys.exit(1)

    render_plan_report(counts, rows, applied=False, digests=before_digests, as_json=args.json)

    if not args.apply:
        sys.exit(0)

    if not args.confirm_irreversible:
        print(
            "ERROR: --apply requires --confirm-irreversible. This migration "
            "rewrites database rows; both flags must be passed deliberately so "
            "no single mistyped or shell-history-recalled argument can start "
            "an irreversible pass over production.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not args.snapshot_dir:
        print("ERROR: --apply requires --snapshot-dir; aborting before any write.", file=sys.stderr)
        sys.exit(1)
    if not args.audit_log:
        print("ERROR: --apply requires --audit-log; aborting before any write.", file=sys.stderr)
        sys.exit(1)

    snapshot_path = os.path.join(
        args.snapshot_dir, f"stale-paths-snapshot-{datetime.now():%Y%m%dT%H%M%S%f}.db"
    )
    try:
        snapshot_db(args.db, snapshot_path)
    except Exception as exc:
        print(f"ERROR: pre-apply snapshot failed, aborting before any write: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        audit_handle = open_audit_log(args.audit_log)
    except OSError as exc:
        print(
            f"ERROR: could not open audit log for writing, aborting before any write: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with database.get_conn() as conn:
            changed = apply_rewrite(conn, rows, audit_handle)
    finally:
        audit_handle.close()

    with database.get_conn() as conn:
        verification = verify_post_conditions(conn)
        after_digests = {
            table: non_path_digest(conn, table, column) for table, column in TARGETS
        }

    mismatches = {
        table: (before_digests[table], after_digests[table])
        for table, _column in TARGETS
        if before_digests[table] != after_digests[table]
    }
    if mismatches:
        print(
            "ERROR: non_path_digest mismatch after apply -- a non-path column "
            "changed unexpectedly. Aborting.",
            file=sys.stderr,
        )
        for table, (before, after) in mismatches.items():
            print(f"  {table}: before={before} after={after}", file=sys.stderr)
        sys.exit(1)

    render_plan_report(counts, rows, applied=True, digests=after_digests, as_json=args.json)
    if not args.json:
        print(f"Rows changed: {changed}")
        print(f"RUNVAR: snapshot_path={snapshot_path}")
        print(f"RUNVAR: audit_log_path={args.audit_log}")
        print("Post-run verification:")
        print(json.dumps(verification, sort_keys=True))

    if verification["remaining_stale"] != 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
