"""
verify_phase16.py - stdlib-only verification harness for Phase 16 (Unified
Species Buckets & Reader Conversion).

Suites:
    audit_script - plan 16-01, the D-02 read-only audit
                   (scripts/audit_species_buckets.py) proven end to end on a
                   fixture with known collisions, in both pre-merge and merged
                   mode (AS1-AS7).
    (merge, unknown and resolver are added by plan 16-02; readers, frontend_src
    and live by plan 16-03.)

Fixture. _seed_phase16_fixture extends Phase 15's fixture (no fork) with rows
whose RAW labels differ but whose common names normalise to the same bucket:

    vid12 (1 day): n1 LBL_DOG2     "Domestic Dog" / "Canis lupus familiaris" q70
    vid5  (3 days, existing): n2 LBL_DOG2, same names, q65
    vid13 (2 days): m1 LBL_DEER "Mule Deer" q60, m2 LBL_DEER2 "Mule deer" q58
    vid14 (1 day): uu1 LBL_UNKNOWN named "Unknown species" (Phase 15 seeds it
                   with a NULL name; this is the other real-world shape) q40
    vid15 (1 day): z1 LBL_PSEUDO named "Unknown species" on a NON-Unknown
                   label (RESEARCH A1 residual case) q35
    vid16 (2 days): e1 LBL_NONAME with a NULL common name q50
    vid17 (1 day): q1 LBL_RACCOON2 "Northern raccoon " (trailing space) q62
    vid18 (3 days): no detections

Expected predicted buckets (8): domestic cat, northern raccoon, domestic dog,
wild boar, Unknown species, bobcat, mule deer and LBL_NONAME (a blank name
keeps its raw label as its key). Before the merge key ships there are 12
current keys; the four collisions are domestic dog, northern raccoon, mule deer
and Unknown species. Raw labels (LBL_*) and bucket keys (KEY_*) are separate
constants (D-05).

Usage:
    python scripts/verify_phase16.py --suite audit_script|all
    python scripts/verify_phase16.py --list
"""

import argparse
import contextlib
import io
import string
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import verify_phase15 as p15
import audit_species_buckets as audit


# -- constants ---------------------------------------------------------------

NO_SUCH_KEY = "zz-phase16-no-such-key"

# Raw SpeciesNet-shaped labels added on top of Phase 15's.
LBL_DOG2 = "22222222-0000-0000-0000-000000000002;mammalia;carnivora;canidae;canis;lupus familiaris;domestic dog"
LBL_DEER = "22222222-0000-0000-0000-000000000003;mammalia;cetartiodactyla;cervidae;odocoileus;hemionus;mule deer"
LBL_DEER2 = "22222222-0000-0000-0000-000000000004;mammalia;cetartiodactyla;cervidae;odocoileus;hemionus;mule deer"
LBL_RACCOON2 = "22222222-0000-0000-0000-000000000005;mammalia;carnivora;procyonidae;procyon;lotor;northern raccoon"
LBL_PSEUDO = "22222222-0000-0000-0000-000000000006;;;;;;"
LBL_NONAME = "22222222-0000-0000-0000-000000000007;mammalia;rodentia;;;;"

# Bucket keys (what the dropdowns, filters and drilldown accept), kept apart
# from the raw labels above (D-05).
KEY_CAT = "domestic cat"
KEY_DOG = "domestic dog"
KEY_BOAR = "wild boar"
KEY_RACCOON = "northern raccoon"
KEY_DEER = "mule deer"
KEY_UNKNOWN = "Unknown species"
KEY_BOBCAT = "bobcat"

_AUDIT_TIMING_NAMES = (
    "get_species_list",
    "get_stats",
    "get_timeline",
    "get_timeline(all)",
    "get_species_detail",
    "get_gallery(species)",
    "get_videos(species)",
    "get_videos(has_species=True)",
    "get_videos(has_species=False)",
    "get_videos(search)",
    "search",
)


# -- helpers -----------------------------------------------------------------

def _seed_phase16_fixture(path):
    """Phase 15's fixture plus the Phase 16 rows (see the module docstring).
    Returns the ids dict, extended with n1, n2, m1, m2, uu1, z1, e1, q1 and
    vid12..vid18."""
    ids = p15._seed_phase15_fixture(path)

    with database.get_conn() as conn:

        def _insert_video(name, days_ago):
            when = p15._utc_day_iso(days_ago)
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, 'World Watch', 1, ?, 0, ?)",
                (f"WorldWatch_16_{name}.mp4", when, when),
            )
            vid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            ids[name] = vid
            return vid

        def _add(det_name, video_id, label, common, scientific, quality):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, 0.9)",
                (det_id, label, common, scientific),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture16_crop_{det_id}.jpg", float(quality), p15._utc_day_iso(1)),
            )
            ids[det_name] = det_id

        v12 = _insert_video("vid12", 1)
        _add("n1", v12, LBL_DOG2, "Domestic Dog", "Canis lupus familiaris", 70)
        _add("n2", ids["vid5"], LBL_DOG2, "Domestic Dog", "Canis lupus familiaris", 65)
        v13 = _insert_video("vid13", 2)
        _add("m1", v13, LBL_DEER, "Mule Deer", "Odocoileus hemionus", 60)
        _add("m2", v13, LBL_DEER2, "Mule deer", "Odocoileus hemionus", 58)
        v14 = _insert_video("vid14", 1)
        _add("uu1", v14, p15.LBL_UNKNOWN, "Unknown species", None, 40)
        v15 = _insert_video("vid15", 1)
        _add("z1", v15, LBL_PSEUDO, "Unknown species", None, 35)
        v16 = _insert_video("vid16", 2)
        _add("e1", v16, LBL_NONAME, None, None, 50)
        v17 = _insert_video("vid17", 1)
        _add("q1", v17, LBL_RACCOON2, "Northern raccoon ", "Procyon lotor", 62)
        _insert_video("vid18", 3)

    return ids


def _ascii_norm(name):
    """Strip space/tab/CR/LF and lower-case ASCII letters only (the way the
    SQL key normaliser does). "" for None."""
    if name is None:
        return ""
    table = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
    return name.strip(" \t\r\n").translate(table)


def _shared_name_violations(rows):
    """One readable string per non-empty normalised common name that two or
    more get_species_list() rows carry (BUCKET-04)."""
    seen = {}
    for row in rows:
        norm = _ascii_norm(row.get("common_name"))
        if norm:
            seen.setdefault(norm, []).append(row["label"])
    return [
        f"{norm!r} shared by {len(keys)} rows {keys}"
        for norm, keys in sorted(seen.items())
        if len(keys) >= 2
    ]


def _run_audit(path, *extra):
    """(rc, text) of audit.main(["--db", path, *extra]) with stdout captured."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = audit.main(["--db", path, *extra])
    return rc, buf.getvalue()


def _line(text, prefix):
    """The first line of `text` that starts with `prefix`, else None."""
    for ln in text.splitlines():
        if ln.startswith(prefix):
            return ln
    return None


def _table_counts(path):
    database.set_db_path(path)
    with database.get_conn() as conn:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in audit._AUDIT_TABLES
        }


# -- audit_script suite ------------------------------------------------------

def suite_audit_script():
    """`audit_script` suite cases AS1-AS7 (7 total). Each case builds its own
    fixture in its own temp directory and restores the database path."""
    total = 7
    passed = 0
    original_db_path = database.get_db_path()

    def _fixture(tmp):
        path = str(Path(tmp) / "audit16.db")
        p15_ids = _seed_phase16_fixture(path)
        return path, p15_ids

    # AS1 - the audit passes and predicts 8 keys.
    case_id = "AS1"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            rc, text = _run_audit(path)
            database.set_db_path(path)
            current = len(database.get_species_list())
            merged = "INFO: mode merged" in text
            ok = (
                rc == 0
                and "RESULT: PASS" in text
                and "INFO: predicted_keys 8" in text
                and f"INFO: current_keys {current}" in text
                and (not merged or current == 8)
            )
            p15._check(case_id, ok, f"rc={rc} current={current} output:\n{text}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS2 - the four collisions, with the different-taxa flag.
    case_id = "AS2"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            rc, text = _run_audit(path)
            lines = [ln for ln in text.splitlines() if ln.startswith("COLLISION key=")]
            keys = sorted(
                ln[len("COLLISION key="):].rsplit(" labels=", 1)[0] for ln in lines
            )
            dog = _line(text, f"COLLISION key={KEY_DOG} ")
            deer = _line(text, f"COLLISION key={KEY_DEER} ")
            ok = (
                keys == sorted([KEY_DOG, KEY_RACCOON, KEY_DEER, KEY_UNKNOWN])
                and dog is not None and "different_taxa=yes" in dog
                and deer is not None and "different_taxa=no" in deer
            )
            p15._check(case_id, ok, f"collision lines={lines}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS3 - the MERGE block (pre-merge) or its absence (merged).
    case_id = "AS3"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            rc, text = _run_audit(path)
            merge_lines = [ln for ln in text.splitlines() if ln.startswith("MERGE key=")]
            if "INFO: mode merged" in text:
                ok = not merge_lines and "INFO: shared_display_names 0" in text
            else:
                dog = _line(text, f"MERGE key={KEY_DOG} ")
                ok = (
                    len(merge_lines) == 4
                    and dog is not None
                    and "detections=4 videos=3" in dog
                    and "INFO: shared_display_names 4" in text
                )
            p15._check(case_id, ok, f"merge lines={merge_lines}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS4 - A1, blank names, odd characters.
    case_id = "AS4"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            rc, text = _run_audit(path)
            ok = (
                "INFO: unknown_name_on_other_labels 1" in text
                and "INFO: blank_name_labels 1" in text
                and "INFO: odd_names 1" in text
                and "non-ascii" in audit._odd_name_reasons("Épervier")
                and {"whitespace", "control"} <= set(audit._odd_name_reasons(" Bobcat\t"))
                and audit._odd_name_reasons("Bobcat") == []
            )
            p15._check(case_id, ok, f"output:\n{text}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS5 - read-only: counts unchanged, _select rejects a write.
    case_id = "AS5"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            before = _table_counts(path)
            _run_audit(path)
            after = _table_counts(path)
            rejected = False
            with database.get_conn() as conn:
                try:
                    audit._select(conn, "DELETE FROM species")
                except AssertionError:
                    rejected = True
            ok = before == after and rejected
            p15._check(case_id, ok, f"before={before} after={after} delete rejected={rejected}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS6 - a missing database exits 2 and creates nothing; a wrong
    # expectation fails with a FAIL line.
    case_id = "AS6"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            missing = Path(tmp) / "missing.db"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                missing_rc = audit.main(["--db", str(missing)])
            rc, text = _run_audit(path, "--expect-predicted", "7")
            ok = (
                missing_rc == 2
                and "ERROR: no database" in buf.getvalue()
                and not missing.exists()
                and rc == 1
                and _line(text, "FAIL: predicted_keys") is not None
            )
            p15._check(
                case_id, ok,
                f"missing rc={missing_rc} exists={missing.exists()} "
                f"expect rc={rc} out={buf.getvalue()!r}",
            )
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    # AS7 - versions and every reader timing are printed.
    case_id = "AS7"
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path, _ids = _fixture(tmp)
            rc, text = _run_audit(path)
            missing_timings = [
                n for n in _AUDIT_TIMING_NAMES if _line(text, f"INFO: timing {n} ") is None
            ]
            n_timing = sum(1 for ln in text.splitlines() if ln.startswith("INFO: timing "))
            ok = (
                not missing_timings
                and n_timing == len(_AUDIT_TIMING_NAMES)
                and _line(text, "INFO: sqlite_version") is not None
                and _line(text, "INFO: python_version") is not None
            )
            p15._check(case_id, ok, f"missing timings={missing_timings} count={n_timing}")
            if ok:
                passed += 1
    finally:
        database.set_db_path(original_db_path)

    return (passed, total)


# -- registry / CLI ----------------------------------------------------------

SUITES = {
    "audit_script": (suite_audit_script, 7),
}

# Suites that only run when explicitly requested (never part of --suite all).
EXPLICIT_ONLY = set()


def main():
    parser = argparse.ArgumentParser(description="Phase 16 verification harness")
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()) + ["all"], default="all",
        help="which suite to run (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    args = parser.parse_args()

    if args.list:
        for name, (_fn, total) in SUITES.items():
            print(f"{name}: {total} cases")
        return 0

    names = ([n for n in SUITES if n not in EXPLICIT_ONLY]
             if args.suite == "all" else [args.suite])
    all_passed = True
    for name in names:
        fn, _total = SUITES[name]
        passed, total = fn()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            all_passed = False
            print(f"FAIL: {name} ({passed}/{total})")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
