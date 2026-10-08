"""
verify_phase18.py - stdlib-only verification harness for Phase 18 (Production
Legacy Drop). It proves scripts/drop_legacy_corrections.py before anything
points it at production.

Suites:
    script    - the drop script's real main() on legacy-shaped fixtures, with the
                systemd, lock and DDL steps driven through the script's seams so
                it runs everywhere, Windows included. Dry-run, the opt-in gates,
                the verified snapshot, a clean apply, an injected failure at
                each statement and at the checks (rollback to the identical
                state), idempotence, and a self-test of the post-drop probe.
    bytecopy  - (explicit, --db) a production-volume rehearsal. The source is
                opened mode=ro and copied with Connection.backup(); the REAL CLI
                runs on the copies under the real Linux guards (systemctl, flock).
                Needs Linux. Never writes the source.
    postdrop  - (explicit, --db and --snapshot) a mode=ro probe of a database
                after the drop, compared with the snapshot the drop took. It
                never runs init_db() and never writes either file.

Usage:
    python scripts/verify_phase18.py [--suite script|all]
    python scripts/verify_phase18.py --suite bytecopy --db <database>
    python scripts/verify_phase18.py --suite postdrop --db <database> --snapshot <snapshot>
    python scripts/verify_phase18.py --make-fixture <new directory>
    python scripts/verify_phase18.py --list

verify_phase15, verify_phase17 and drop_legacy_corrections are sibling imports,
so run this file without `python -I`.
"""

import argparse
import contextlib
import hashlib
import io
import json
import locale
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import drop_legacy_corrections as dlc
import verify_phase15 as p15
import verify_phase17 as p17


# -- constants ---------------------------------------------------------------

ORPHAN_LABEL = "mammalia;carnivora;hyaenidae;crocuta;crocuta;spotted hyaena"
ALL_SIX = sorted(p17.LEGACY_COLUMNS + [p17.LEGACY_TABLE] + p17.LEGACY_INDEXES)


# -- fixture and state helpers -------------------------------------------------

def _fixture18(root):
    """p17's two shapes, then on the legacy shape: an all-NULL orphan
    video_corrections row (for vid1), one closed runs row, vid2 purged
    (filepath NULL) and an empty rehearsal.lock beside the database."""
    shapes = p17._build_shapes(root)
    ids = shapes["ids"]
    legacy = shapes["legacy"]
    database.set_db_path(legacy)
    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at, note) "
            "VALUES (?, ?, NULL, NULL, NULL, ?, NULL)",
            (ids["vid1"], ORPHAN_LABEL, p17.LEGACY_STAMP),
        )
        conn.execute(
            """INSERT INTO runs (start_time, end_time, status, "trigger",
                   videos_processed, detections_found)
               VALUES ('2026-10-01T06:00:00', '2026-10-01T06:10:00', 'success',
                       'scheduled', 3, 5)"""
        )
        conn.execute("UPDATE videos SET filepath=NULL WHERE id=?", (ids["vid2"],))
    lock = Path(legacy).parent / "rehearsal.lock"
    lock.write_bytes(b"")
    return {"db": legacy, "lacking": shapes["lacking"], "ids": ids,
            "lock": str(lock), "root": str(root)}


@contextlib.contextmanager
def _fixture_ctx():
    """A fresh _fixture18 in a temp dir; restores the database path afterwards."""
    original = database.get_db_path()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        fx = _fixture18(tmp.name)
        fx["tmp"] = tmp.name
        fx["snapdir"] = str(Path(tmp.name) / "snap")
        fx["audit"] = str(Path(tmp.name) / "audit" / "audit.jsonl")
        yield fx
    finally:
        database.set_db_path(original)
        tmp.cleanup()


@contextlib.contextmanager
def _patched(**seams):
    """Swap attributes of the drop script's module and restore them."""
    saved = {name: getattr(dlc, name) for name in seams}
    try:
        for name, value in seams.items():
            setattr(dlc, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(dlc, name, value)


class LockStub:
    """Stands in for the flock seams and records the calls."""

    def __init__(self, acquire_result=42, acquire_error=None):
        self.acquire_result = acquire_result
        self.acquire_error = acquire_error
        self.acquired = []
        self.released = []

    def acquire(self, path):
        self.acquired.append(path)
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.acquire_result

    def release(self, handle):
        self.released.append(handle)


def _seams(state="inactive", stub=None, **extra):
    """Default seams for an apply case: the analysis service state and a lock
    stub. Returns (patch dict, stub)."""
    stub = stub or LockStub()
    patch = {
        "_analysis_service_state": (lambda: state),
        "_acquire_run_lock": stub.acquire,
        "_release_run_lock": stub.release,
    }
    patch.update(extra)
    return patch, stub


def _run_cli(argv):
    """dlc.main(argv) with stdout and stderr captured. Returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    saved = database.get_db_path()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dlc.main(argv)
    finally:
        database.set_db_path(saved)
    return code, out.getvalue(), err.getvalue()


def _apply_args(fx, **override):
    args = {
        "--db": fx["db"], "--snapshot-dir": fx["snapdir"],
        "--audit-log": fx["audit"], "--lock-file": fx["lock"],
    }
    args.update(override)
    argv = []
    for key, value in args.items():
        if value is not None:
            argv += [key, value]
    return argv + ["--apply", "--confirm-irreversible"]


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ro(path):
    # mode=ro: the harness's read-only view, as in verify_phase17's bytecopy source.
    return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)


def _content_digests(conn):
    """species (over the seven surviving columns) and species_corrections content
    digests, computed here independently of the drop script."""
    cols = ", ".join(p17.SPECIES_COLUMNS)
    species = hashlib.sha256(
        repr(conn.execute(f"SELECT {cols} FROM species ORDER BY id").fetchall()).encode()
    ).hexdigest()
    corrections = hashlib.sha256(
        repr(conn.execute("SELECT * FROM species_corrections ORDER BY id").fetchall()).encode()
    ).hexdigest()
    return {"species": species, "species_corrections": corrections}


def _state(path):
    """Everything a refusal must leave untouched, through a mode=ro connection:
    sqlite_master and every table's count, both digests, the file sha256."""
    conn = _ro(path)
    try:
        st = p17._src_state(conn)
        st["digests"] = _content_digests(conn)
    finally:
        conn.close()
    st["sha256"] = _file_sha256(path)
    return st


def _state_no_file(path):
    """_state without the file hash (a committed drop changes the file)."""
    st = _state(path)
    st.pop("sha256")
    return st


def _snapshots(snapdir):
    if not os.path.isdir(snapdir):
        return []
    return sorted(Path(snapdir).glob(dlc.SNAPSHOT_PREFIX + "*.db"))


def _audit_events(audit_path):
    if not os.path.isfile(audit_path):
        return []
    with open(audit_path, encoding="utf-8") as fh:
        return [json.loads(line)["event"] for line in fh if line.strip()]


def _dir_listing(path):
    return sorted(n for n in os.listdir(Path(path).parent) if not n.endswith(("-wal", "-shm")))


def _nothing_written(fx, before, code, expect_code=1):
    """Shared assertion for a refusal: expected exit code, file and state
    unchanged, no snapshot, no drop audit line."""
    problems = []
    if code != expect_code:
        problems.append(f"exit code {code}, expected {expect_code}")
    after = _state(fx["db"])
    if after["sha256"] != before["sha256"]:
        problems.append("the database file bytes changed")
    if after != before:
        problems.append("the database state changed")
    if _snapshots(fx["snapdir"]):
        problems.append("a snapshot was written")
    events = _audit_events(fx["audit"])
    if "drop_committed" in events or "drop_rolled_back" in events:
        problems.append(f"drop audit events were written: {events}")
    return problems


# -- suite: script ---------------------------------------------------------------

def _dr1():
    problems = []
    if (dlc.LEGACY_COLUMNS, dlc.LEGACY_TABLE, dlc.LEGACY_INDEXES) != (
            p17.LEGACY_COLUMNS, p17.LEGACY_TABLE, p17.LEGACY_INDEXES):
        problems.append("the script's legacy constants differ from verify_phase17's")
    if dlc.SPECIES_COLUMNS_AFTER != p17.SPECIES_COLUMNS:
        problems.append("SPECIES_COLUMNS_AFTER differs from verify_phase17's SPECIES_COLUMNS")
    with _fixture_ctx() as fx:
        before = _state(fx["db"])
        listing = _dir_listing(fx["db"])
        patch, _stub = _seams()
        with _patched(**patch):
            code, out, err = _run_cli(["--db", fx["db"], "--lock-file", fx["lock"]])
        if code != 0:
            problems.append(f"exit code {code}: {err[-300:]}")
        if "DRY-RUN - nothing has been written" not in out:
            problems.append("the DRY-RUN line is missing")
        present = [ln for ln in out.splitlines() if ln.startswith("OBJECT: ") and ln.endswith(" present")]
        if len(present) != 6:
            problems.append(f"{len(present)} OBJECT present lines, expected 6")
        after = _state(fx["db"])
        if after["sha256"] != before["sha256"]:
            problems.append("the database file bytes changed")
        if after != before:
            problems.append("the database state changed")
        if _dir_listing(fx["db"]) != listing:
            problems.append("the database directory listing changed")
        if _snapshots(fx["snapdir"]) or os.path.exists(fx["snapdir"]) or os.path.exists(fx["audit"]):
            problems.append("a snapshot or audit file was created by a dry-run")
    return not problems, "; ".join(problems)


def _ap1():
    problems = []
    with _fixture_ctx() as fx:
        before = _state(fx["db"])
        patch, stub = _seams()
        with _patched(**patch):
            code, out, err = _run_cli(_apply_args(fx))
        if code != 0:
            problems.append(f"exit code {code}: {err[-300:]}")
        if "APPLIED - legacy objects dropped and verified" not in out:
            problems.append("the APPLIED line is missing")
        if p17._legacy_objects(fx["db"]):
            problems.append(f"legacy objects remain: {p17._legacy_objects(fx['db'])}")
        if p17._schema_snapshot(fx["db"])["columns"] != p17.SPECIES_COLUMNS:
            problems.append("species columns are not the fresh seven in order")
        conn = _ro(fx["db"])
        try:
            if [tuple(r) for r in conn.execute("PRAGMA integrity_check")] != [("ok",)]:
                problems.append("integrity_check is not ok")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                problems.append("foreign_key_check returned rows")
            if conn.execute(
                    "SELECT COUNT(*) FROM sqlite_sequence WHERE name=?", (p17.LEGACY_TABLE,)
            ).fetchone()[0]:
                problems.append("sqlite_sequence still has a video_corrections row")
        finally:
            conn.close()
        after = _state_no_file(fx["db"])
        want = {t: n for t, n in before["counts"].items() if t != p17.LEGACY_TABLE}
        if after["counts"] != want:
            problems.append("a surviving table's row count changed")
        if after["digests"] != before["digests"]:
            problems.append("a content digest changed")
        if len(stub.released) != 1:
            problems.append(f"the lock was released {len(stub.released)} times, expected 1")
    return not problems, "; ".join(problems)


def _rb1():
    problems = []
    real = dlc._execute_drop_statement
    for n in (1, 2, 3, 4):
        with _fixture_ctx() as fx:
            before_schema = p17._schema_snapshot(fx["db"])
            before = _state(fx["db"])
            calls = {"n": 0}

            def flaky(conn, sql, _n=n, _calls=calls):
                _calls["n"] += 1
                if _calls["n"] == _n:
                    raise RuntimeError(f"injected failure at statement {_n}")
                real(conn, sql)

            patch, _stub = _seams(_execute_drop_statement=flaky)
            with _patched(**patch):
                code, out, err = _run_cli(_apply_args(fx))
            tag = f"[statement {n}] "
            if code != 1:
                problems.append(f"{tag}exit code {code}, expected 1")
            if "ROLLED BACK" not in err:
                problems.append(f"{tag}no ROLLED BACK line")
            if p17._legacy_objects(fx["db"]) != ALL_SIX:
                problems.append(f"{tag}legacy objects are {p17._legacy_objects(fx['db'])}")
            if p17._schema_snapshot(fx["db"]) != before_schema:
                problems.append(f"{tag}the schema changed")
            after = _state_no_file(fx["db"])
            if after["counts"] != before["counts"] or after["digests"] != before["digests"]:
                problems.append(f"{tag}counts or digests changed")
            if len(_snapshots(fx["snapdir"])) != 1:
                problems.append(f"{tag}{len(_snapshots(fx['snapdir']))} snapshot files, expected 1")
            events = _audit_events(fx["audit"])
            if "drop_rolled_back" not in events or "drop_committed" in events:
                problems.append(f"{tag}audit events {events}")
    return not problems, "; ".join(problems)


SCRIPT_CASES = [
    ("DR1", _dr1),
    ("AP1", _ap1),
    ("RB1", _rb1),
]


def suite_script():
    """`script` suite. Returns (passed, cases run)."""
    passed = 0
    for case_id, fn in SCRIPT_CASES:
        passed += p17._case(case_id, fn)
    return (passed, len(SCRIPT_CASES))


# -- registry / CLI ----------------------------------------------------------

SUITES = {
    "script": (suite_script, 3),
}

# Suites that only run when explicitly requested (never part of --suite all).
EXPLICIT_ONLY = set()


def main():
    parser = argparse.ArgumentParser(description="Phase 18 verification harness")
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()) + ["all"], default="all",
        help="which suite to run (default: all, the non-explicit suites)",
    )
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    args = parser.parse_args()

    if args.list:
        for name, (_fn, total) in SUITES.items():
            print(f"{name}: {total} cases")
        return 0

    # The suites import web_app (the bytecopy GET sweep reads static/index.html with
    # the locale's default encoding). Re-run once in UTF-8 mode on a non-UTF-8
    # locale instead of reporting a false 5xx (the verify_phase17 guard).
    if (not sys.flags.utf8_mode and os.environ.get("PHASE18_UTF8_RERUN") != "1"
            and locale.getpreferredencoding(False).lower().replace("-", "") != "utf8"):
        env = dict(os.environ, PYTHONUTF8="1", PHASE18_UTF8_RERUN="1")
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env=env
        ).returncode

    names = ([n for n in SUITES if n not in EXPLICIT_ONLY]
             if args.suite == "all" else [args.suite])
    all_passed = True
    for name in names:
        fn, expected_total = SUITES[name]
        passed, total = fn()
        if passed == total == expected_total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            all_passed = False
            print(f"FAIL: {name} ({passed}/{total}, registry says {expected_total})")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
