"""
drop_legacy_corrections.py - Phase 18 one-shot: physically remove the legacy
correction objects from a wildlife database.

What it drops, and why
    species.user_common_name, species.user_scientific_name, species.corrected_at,
    and the video_corrections table with its two indexes
    (idx_corrections_video, idx_corrections_label). Phase 14 (D-06/D-07) made
    species_corrections the only correction store and froze these objects;
    Phase 17 removed every runtime dependency on them; this script, run once
    against production, closes D-07. It is idempotent: on a database that no
    longer holds any of the six objects it prints NOTHING TO DROP and exits 0.

Usage
    python scripts/drop_legacy_corrections.py --db /abs/path/wildlife.db
        Dry-run (the default). Opens the database mode=ro, reports what would
        be dropped, writes nothing and creates nothing.
    python scripts/drop_legacy_corrections.py --db /abs/path/wildlife.db \
        --apply --confirm-irreversible \
        --snapshot-dir /abs/dir --audit-log /abs/audit.jsonl [--lock-file /abs/.nas_sync.lock]
        Applies: guards, a verified Connection.backup() snapshot, then one
        BEGIN IMMEDIATE transaction that drops everything and checks the result
        before it commits.

Exit codes: 0 dry-run / applied and verified / nothing to drop; 1 refused by a
gate, rolled back, or a post-commit check failed; 2 usage error (a relative
path, or no database file).

Safety notes
    * Every DDL identifier comes only from the module constants below, never
      from arguments or database content. The read-only COUNT(*) loop quotes
      table names it reads from sqlite_master of the database being inspected
      (it runs no DDL and no write); every value in a query is a bound
      parameter or a literal.
    * The read-only inspection uses a mode=ro URI connection. That is the one
      documented exception to the project rule "all DB access via
      get_conn()": it mirrors the verify_phase17 bytecopy source open, and
      get_conn() cannot open a file read-only.
    * Every write goes through database.set_db_path() plus database.get_conn().
      Python's sqlite3 opens no transaction before DDL, so the apply issues an
      explicit BEGIN IMMEDIATE first; get_conn()'s except/rollback then undoes
      the whole set if any statement or check fails.
    * The audit file is counts-only JSONL: counts, object names, paths,
      versions and timings, never a row's contents (the orphan
      video_corrections row survives only in the snapshot).
    * The script never deletes or prunes a snapshot, and never VACUUMs.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database  # noqa: E402


# -- constants ---------------------------------------------------------------

LEGACY_COLUMNS = ["user_common_name", "user_scientific_name", "corrected_at"]
LEGACY_TABLE = "video_corrections"
LEGACY_INDEXES = ["idx_corrections_video", "idx_corrections_label"]
# The six objects, in report order.
LEGACY_OBJECTS = LEGACY_COLUMNS + [LEGACY_TABLE] + LEGACY_INDEXES

# species columns after the drop, in order (a fresh init_db() creates exactly these).
SPECIES_COLUMNS_AFTER = [
    "id", "detection_id", "label", "common_name", "scientific_name",
    "confidence", "top_candidates_json",
]

MIN_SQLITE = (3, 35, 0)
RUNNING_STATES = {"active", "activating", "deactivating", "reloading"}
ANALYSIS_UNIT = "wildlife-analysis.service"
DEFAULT_LOCK_FILE = str(Path(__file__).resolve().parents[1] / ".nas_sync.lock")
SNAPSHOT_PREFIX = "legacy-drop-snapshot-"

LEGACY_ROW_COLUMNS = [
    "id", "video_id", "original_label", "corrected_label", "corrected_common",
    "corrected_scientific", "corrected_at", "note",
]


# -- seams (the harness monkeypatches these; main() looks them up at call time) --

def _sqlite_version():
    return sqlite3.sqlite_version_info


def _analysis_service_state():
    """ActiveState of the analysis unit. Raises on any error (fail closed)."""
    out = subprocess.run(
        ["systemctl", "show", ANALYSIS_UNIT, "--property=ActiveState", "--value"],
        capture_output=True, text=True, timeout=10, check=True,
    )
    return out.stdout.strip()


def _acquire_run_lock(lock_path):
    """Non-blocking exclusive flock on nas_sync.sh's lock file. Returns the fd,
    or None when another process holds it. Any other error raises. The file is
    opened O_RDONLY without O_CREAT: a missing lock file is never created."""
    import fcntl  # Linux only; the Windows harness patches this seam

    fd = os.open(lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release_run_lock(handle):
    os.close(handle)


def _execute_drop_statement(conn, sql):
    """The single DDL seam, one statement per call. A harness monkeypatches it
    to raise on the Nth call (the migrate_stale_paths convention)."""
    conn.execute(sql)


def _in_txn_checks(conn, expected):
    """Run inside the open drop transaction, after every statement. Raises
    RuntimeError naming every mismatch; the caller's rollback undoes the drop."""
    problems = []
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign_key_check returned {len(fk)} row(s)")
    integrity = [tuple(r) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    if integrity != [("ok",)]:
        problems.append(f"integrity_check returned {integrity[:3]}")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(species)").fetchall()]
    if cols != SPECIES_COLUMNS_AFTER:
        problems.append(f"species columns are {cols}, expected {SPECIES_COLUMNS_AFTER}")
    remaining = _present_objects(conn)
    if remaining:
        problems.append(f"legacy objects remain: {remaining}")
    counts = _table_counts(conn)
    for label, reference in (("transaction start", expected["counts"]),
                             ("snapshot", expected["snapshot_counts"])):
        want = {t: n for t, n in reference.items() if t != LEGACY_TABLE}
        if counts != want:
            diff = sorted(
                t for t in set(counts) | set(want) if counts.get(t) != want.get(t)
            )
            problems.append(f"row counts differ from {label} for {diff}")
    if _species_digest(conn) != expected["species_digest"]:
        problems.append("species content digest changed")
    if _corrections_digest(conn) != expected["corrections_digest"]:
        problems.append("species_corrections content digest changed")
    if problems:
        raise RuntimeError("; ".join(problems))


def _verify_snapshot(snapshot_path, expected):
    """Open the snapshot mode=ro. Raises RuntimeError on any mismatch with the
    live file as it was when the snapshot finished: integrity, the same legacy
    objects present, identical per-table row counts."""
    conn = sqlite3.connect(Path(snapshot_path).as_uri() + "?mode=ro", uri=True)
    try:
        integrity = [tuple(r) for r in conn.execute("PRAGMA integrity_check").fetchall()]
        problems = []
        if integrity != [("ok",)]:
            problems.append(f"snapshot integrity_check returned {integrity[:3]}")
        present = _present_objects(conn)
        if present != sorted(expected["present"]):
            problems.append(
                f"snapshot holds legacy objects {present}, live file held {sorted(expected['present'])}"
            )
        counts = _table_counts(conn)
        if counts != expected["counts"]:
            diff = sorted(
                t for t in set(counts) | set(expected["counts"])
                if counts.get(t) != expected["counts"].get(t)
            )
            problems.append(f"snapshot row counts differ from the live file for {diff}")
    finally:
        conn.close()
    if problems:
        raise RuntimeError("; ".join(problems))


# -- read helpers (work on any connection) -------------------------------------

def _present_objects(conn):
    """Sorted names of the six legacy objects present on `conn`."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(species)").fetchall()]
    found = [c for c in LEGACY_COLUMNS if c in cols]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    if LEGACY_TABLE in tables:
        found.append(LEGACY_TABLE)
    found += [i for i in LEGACY_INDEXES if i in indexes]
    return sorted(found)


def _table_counts(conn):
    names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND substr(name, 1, 7) != 'sqlite_' ORDER BY name"
        ).fetchall()
    ]
    return {
        n: conn.execute('SELECT COUNT(*) FROM "%s"' % n.replace('"', '""')).fetchone()[0]
        for n in names
    }


def _digest(rows):
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(list(row), default=str).encode("utf-8"))
        h.update(b"\x00\x00")
    return h.hexdigest()


def _species_digest(conn):
    cols = ", ".join(SPECIES_COLUMNS_AFTER)
    return _digest(conn.execute(f"SELECT {cols} FROM species ORDER BY id").fetchall())


def _corrections_digest(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "species_corrections" not in tables:
        return None
    return _digest(conn.execute("SELECT * FROM species_corrections ORDER BY id").fetchall())


def _open_ro(path):
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)


def _inspect(path):
    """Everything the report and the guards need, through a mode=ro connection."""
    conn = _open_ro(path)
    try:
        present = _present_objects(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        existing_legacy_cols = [c for c in LEGACY_COLUMNS if c in present]
        frozen = None
        if existing_legacy_cols:
            where = " OR ".join(f"{c} IS NOT NULL" for c in existing_legacy_cols)
            frozen = conn.execute(f"SELECT COUNT(*) FROM species WHERE {where}").fetchone()[0]
        rows = []
        if LEGACY_TABLE in present:
            cols = ", ".join(LEGACY_ROW_COLUMNS)
            rows = [tuple(r) for r in conn.execute(
                f"SELECT {cols} FROM {LEGACY_TABLE} ORDER BY id"
            ).fetchall()]
        open_runs = 0
        if "runs" in tables:
            open_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE end_time IS NULL").fetchone()[0]
        return {
            "present": present,
            "counts": _table_counts(conn),
            "frozen": frozen,
            "rows": rows,
            "open_runs": open_runs,
            "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
            "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
            "species_columns": [r[1] for r in conn.execute("PRAGMA table_info(species)")],
        }
    finally:
        conn.close()


def _post_checks(path):
    conn = _open_ro(path)
    try:
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity = [tuple(r) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()
    return len(fk), integrity


# -- output helpers --------------------------------------------------------------

def _version_text(version):
    return ".".join(str(p) for p in version)


def _err(prefix, message):
    print(f"{prefix}: {message}", file=sys.stderr)


def _refuse(message):
    _err("REFUSED", message)
    return 1


def _audit(handle, event, **fields):
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat()}
    payload.update(fields)
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
    handle.flush()


def _lock_report(lock_file, probe):
    """Report-only lock state. A dry-run probes it (acquire and release at once);
    an apply does not, because it takes the lock for real a few steps later and
    must hold it exactly once."""
    if not os.path.isfile(lock_file):
        return "absent"
    if not probe:
        return "checked-at-apply"
    try:
        handle = _acquire_run_lock(lock_file)
    except Exception as exc:  # noqa: BLE001 - a report never aborts
        return f"unavailable ({type(exc).__name__}: {exc})"
    if handle is None:
        return "busy"
    try:
        _release_run_lock(handle)
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({type(exc).__name__}: {exc})"
    return "free"


def _service_report():
    try:
        state = _analysis_service_state()
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({type(exc).__name__}: {exc})"
    return state if state else "unknown (empty state)"


def _print_report(args, info):
    print(f"PLAN: {args.db}")
    print(f"VERSION: python {sys.version.split()[0]} sqlite {_version_text(_sqlite_version())}")
    for name in LEGACY_OBJECTS:
        print(f"OBJECT: {name} {'present' if name in info['present'] else 'absent'}")
    for table, n in sorted(info["counts"].items()):
        print(f"COUNT: {table} {n}")
    if info["frozen"] is not None:
        print(f"FROZEN: {info['frozen']}")
    for row in info["rows"]:
        fields = " ".join(f"{k}={v!r}" for k, v in zip(LEGACY_ROW_COLUMNS, row))
        print(f"LEGACY ROW: {fields}")
    too_old = _sqlite_version() < MIN_SQLITE
    print(f"GUARD: sqlite {_version_text(_sqlite_version())} {'too-old' if too_old else 'ok'}")
    print(f"GUARD: analysis_service {_service_report()}")
    print(f"GUARD: run_lock {args.lock_file} {_lock_report(args.lock_file, not args.apply)}")
    print(f"GUARD: open_runs {info['open_runs']}")


# -- snapshot ------------------------------------------------------------------

def _take_snapshot(db_path, snapshot_dir):
    """Online Connection.backup() of the live file into a new timestamped file
    under snapshot_dir. Returns the path. Source through get_conn(); the
    destination is a plain connection on the new file."""
    os.makedirs(snapshot_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    path = os.path.join(snapshot_dir, f"{SNAPSHOT_PREFIX}{stamp}.db")
    database.set_db_path(db_path)
    with database.get_conn() as src:
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    return path


# -- main ------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Phase 18 one-shot: drop the legacy correction columns, table and indexes"
    )
    parser.add_argument("--db", required=True, help="Absolute path of the database")
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the drop. Also needs --confirm-irreversible, --snapshot-dir and "
             "--audit-log (default: dry-run, nothing written)",
    )
    parser.add_argument(
        "--confirm-irreversible", action="store_true",
        help="Second opt-in flag required with --apply",
    )
    parser.add_argument("--snapshot-dir", default=None, help="Absolute directory for the snapshot")
    parser.add_argument("--audit-log", default=None, help="Absolute path of the counts-only JSONL audit")
    parser.add_argument(
        "--lock-file", default=DEFAULT_LOCK_FILE,
        help="Absolute path of nas_sync.sh's lock file (default: the repo's .nas_sync.lock)",
    )
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    # a. absolute paths
    for flag in ("db", "snapshot_dir", "audit_log", "lock_file"):
        value = getattr(args, flag)
        if value is not None and not os.path.isabs(value):
            _err("ERROR", f"--{flag.replace('_', '-')} must be an absolute path: {value}")
            return 2

    # b. sqlite3 creates a missing file on connect, so check before any connect.
    if not os.path.isfile(args.db):
        _err("ERROR", f"no database at {args.db}")
        return 2

    saved_db_path = database.get_db_path()
    try:
        return _run(args)
    finally:
        database.set_db_path(saved_db_path)


def _run(args):
    # c. read-only inspection and report
    info = _inspect(args.db)
    _print_report(args, info)

    # d. idempotent no-op
    if not info["present"]:
        print("NOTHING TO DROP - none of the six legacy objects exist")
        return 0

    # e. dry-run
    if not args.apply:
        if args.snapshot_dir:
            print(f"WOULD WRITE: snapshot {os.path.join(args.snapshot_dir, SNAPSHOT_PREFIX + '<timestamp>.db')}")
        print("DRY-RUN - nothing has been written")
        return 0

    # f. both opt-in flags and both paths
    missing = [flag for flag, ok in (
        ("--confirm-irreversible", args.confirm_irreversible),
        ("--snapshot-dir", bool(args.snapshot_dir)),
        ("--audit-log", bool(args.audit_log)),
    ) if not ok]
    if missing:
        return _refuse(f"--apply requires {', '.join(missing)}")

    # g. guards, each failing closed
    version = _sqlite_version()
    if version < MIN_SQLITE:
        return _refuse(
            f"SQLite {_version_text(version)} is older than {_version_text(MIN_SQLITE)} "
            "(ALTER TABLE DROP COLUMN needs 3.35+)"
        )
    try:
        state = _analysis_service_state()
    except Exception as exc:  # noqa: BLE001 - fail closed on any systemd error
        return _refuse(
            f"cannot read the state of {ANALYSIS_UNIT} ({type(exc).__name__}: {exc}); failing closed"
        )
    if not state:
        return _refuse(f"{ANALYSIS_UNIT} reported an empty state; failing closed")
    if state in RUNNING_STATES:
        return _refuse(f"{ANALYSIS_UNIT} is {state}: a run is in progress")
    if not os.path.isfile(args.lock_file):
        return _refuse(f"run lock {args.lock_file} does not exist; it is never created here")
    try:
        lock = _acquire_run_lock(args.lock_file)
    except Exception as exc:  # noqa: BLE001
        return _refuse(f"cannot take run lock {args.lock_file} ({type(exc).__name__}: {exc})")
    if lock is None:
        return _refuse(f"run lock {args.lock_file} is busy: a run is in progress")

    # The lock is held from here to the end, released on every path.
    try:
        return _apply(args, info, version)
    finally:
        try:
            _release_run_lock(lock)
        except Exception as exc:  # noqa: BLE001
            _err("ERROR", f"could not release run lock: {type(exc).__name__}: {exc}")


def _apply(args, info, version):
    open_runs = _inspect(args.db)["open_runs"]
    if open_runs > 0:
        return _refuse(f"{open_runs} open run(s) in the runs table (end_time IS NULL)")

    # h. audit log opened before anything is written
    try:
        parent = os.path.dirname(args.audit_log)
        if parent:
            os.makedirs(parent, exist_ok=True)
        audit = open(args.audit_log, "a", encoding="utf-8")
    except OSError as exc:
        return _refuse(f"cannot open the audit log {args.audit_log} ({exc})")
    try:
        return _apply_with_audit(args, info, version, audit)
    finally:
        audit.close()


def _apply_with_audit(args, info, version, audit):
    py_version = sys.version.split()[0]
    sql_version = _version_text(version)

    # i. snapshot (D-01), verified before any DDL
    started = time.monotonic()
    snapshot = None
    try:
        snapshot = _take_snapshot(args.db, args.snapshot_dir)
        live_now = _inspect(args.db)
        _verify_snapshot(snapshot, {
            "present": live_now["present"], "counts": live_now["counts"],
        })
        snapshot_counts = _inspect(snapshot)["counts"]
    except Exception as exc:  # noqa: BLE001 - nothing has been dropped on this path
        where = f" (partial file left at {snapshot})" if snapshot else ""
        return _refuse(
            f"snapshot failed or did not verify: {type(exc).__name__}: {exc}{where}; "
            "nothing was dropped"
        )
    snapshot_seconds = time.monotonic() - started
    print(f"SNAPSHOT: {snapshot} integrity ok")
    _audit(audit, "snapshot_taken", db=args.db, snapshot=snapshot, counts=live_now["counts"],
           present=live_now["present"], seconds=round(snapshot_seconds, 3),
           python=py_version, sqlite=sql_version)

    # j. the drop: one BEGIN IMMEDIATE transaction through get_conn()
    checks_seconds = 0.0
    dropped = []
    try:
        drop_started = time.monotonic()
        database.set_db_path(args.db)
        with database.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before_counts = _table_counts(conn)
            expected = {
                "counts": before_counts,
                "snapshot_counts": snapshot_counts,
                "species_digest": _species_digest(conn),
                "corrections_digest": _corrections_digest(conn),
            }
            present = _present_objects(conn)
            for column in LEGACY_COLUMNS:
                if column in present:
                    _execute_drop_statement(conn, f"ALTER TABLE species DROP COLUMN {column}")
                    dropped.append(column)
            if LEGACY_TABLE in present:
                _execute_drop_statement(conn, f"DROP TABLE IF EXISTS {LEGACY_TABLE}")
                dropped.append(LEGACY_TABLE)
                dropped += [i for i in LEGACY_INDEXES if i in present]
            checks_started = time.monotonic()
            _in_txn_checks(conn, expected)
            checks_seconds = time.monotonic() - checks_started
        drop_seconds = time.monotonic() - drop_started - checks_seconds
    except Exception as exc:  # noqa: BLE001 - get_conn has already rolled back
        reason = f"{type(exc).__name__}: {exc}"
        _audit(audit, "drop_rolled_back", db=args.db, snapshot=snapshot, error=reason,
               counts=info["counts"], present=info["present"],
               python=py_version, sqlite=sql_version)
        _err("ROLLED BACK", f"{reason} - nothing was dropped; snapshot kept at {snapshot}")
        return 1

    # k. post-commit re-inspection through a fresh read-only connection
    after = _inspect(args.db)
    fk_rows, integrity = _post_checks(args.db)
    want = {t: n for t, n in before_counts.items() if t != LEGACY_TABLE}
    problems = []
    if after["present"]:
        problems.append(f"legacy objects remain: {after['present']}")
    if after["species_columns"] != SPECIES_COLUMNS_AFTER:
        problems.append(f"species columns are {after['species_columns']}")
    if after["counts"] != want:
        problems.append("row counts differ from the pre-drop counts")
    if fk_rows or integrity != [("ok",)]:
        problems.append(f"foreign_key_check {fk_rows} row(s), integrity_check {integrity[:3]}")
    _audit(audit, "drop_committed", db=args.db, snapshot=snapshot, dropped=dropped,
           counts_before=before_counts, counts_after=after["counts"],
           page_count=after["page_count"], freelist_count=after["freelist_count"],
           snapshot_seconds=round(snapshot_seconds, 3), drop_seconds=round(drop_seconds, 3),
           checks_seconds=round(checks_seconds, 3), post_commit_ok=not problems,
           python=py_version, sqlite=sql_version)
    if problems:
        return _refuse(
            "post-commit check failed - the drop IS committed; restore from "
            f"{snapshot} ({'; '.join(problems)})"
        )

    for table in sorted(before_counts):
        if table == LEGACY_TABLE:
            print(f"COUNTS: {table} {before_counts[table]} -> dropped")
        else:
            print(f"COUNTS: {table} {before_counts[table]} -> {after['counts'][table]}")
    print("CHECK: foreign_key_check 0 rows; integrity_check ok")
    print(f"INFO: page_count {after['page_count']} freelist_count {after['freelist_count']}")
    print(
        f"TIMING: snapshot_seconds {snapshot_seconds:.2f} drop_seconds {drop_seconds:.2f} "
        f"checks_seconds {checks_seconds:.2f}"
    )
    print("APPLIED - legacy objects dropped and verified")
    print(f"RUNVAR: snapshot_path={snapshot}")
    print(f"RUNVAR: audit_log_path={args.audit_log}")
    print(f"RUNVAR: lock_file={args.lock_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
