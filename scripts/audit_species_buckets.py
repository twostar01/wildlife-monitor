"""
audit_species_buckets.py - read-only production audit for the Phase 16 merge
key (D-02 hard gate).

Phase 16 changes the species bucket key (Design B, D-01): a native detection is
keyed on its NORMALISED common name instead of its raw SpeciesNet label, so a
native "Northern Raccoon" bucket and an operator-corrected "northern raccoon"
bucket become one. That key shape flows into every reader, so before it ships
this script predicts what it does to the REAL data: current vs predicted key
counts, every label collision per normalised common name (with a different-taxa
flag, D-03 merges them anyway), blank and odd-character names, the number of
non-Unknown labels named 'Unknown species' (RESEARCH A1), SQLite/Python
versions and per-reader timings.

Run it on a byte-copy, never on the live database:

    python scripts/audit_species_buckets.py --db /tmp/wildlife-16-audit.db --expect-current 51 --expect-predicted 47

Two modes, picked from the code in database.py:
    pre-merge - database.py has no NATIVE_KEY yet. The predicted key is the
                audit's own copy (_PREDICTED_NATIVE_KEY) and the MERGE block
                lists the current keys that Design B will fold together.
    merged    - database.py defines NATIVE_KEY (Phase 16 plan 02 onwards). The
                audit then proves the shipped key: current_keys must equal
                predicted_keys and no two list rows may share a display name.

Read-only by construction: every statement goes through _select (SELECT/WITH
only), --db is required (no default, so data/wildlife.db is never opened by
accident), a missing path exits 2 before database.set_db_path (sqlite3.connect
would CREATE the file), and the row counts of six tables are compared before
and after (a difference is a FAIL).

Exit status: 0 on RESULT: PASS, 1 on RESULT: FAIL, 2 when the database file is
missing. Stdlib only; all DB access goes through database.get_conn().
"""

import argparse
import platform
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


# -- key expressions ---------------------------------------------------------

# D-01's native key: the normalised common name, falling back to the raw label
# when the name is blank. The literal guard keeps the Unknown pseudo-label and
# any native row whose common name is 'Unknown species' in the single Unknown
# bucket (D-05, RESEARCH Pitfall 1 and residual case A1). 16-02 defines
# database.NATIVE_KEY with the same text and pins the two whitespace-
# insensitively equal, so this copy cannot drift. Built only from
# database._normalize_key_sql: never a literal case-folding call here.
_PREDICTED_NATIVE_KEY = f"""CASE WHEN s.label = 'Unknown species'
              OR {database._normalize_key_sql('s.common_name')} = 'unknown species'
             THEN 'Unknown species'
         ELSE COALESCE({database._normalize_key_sql('s.common_name')}, s.label) END"""

NATIVE_KEY_SQL = getattr(database, "NATIVE_KEY", _PREDICTED_NATIVE_KEY)
MODE = "merged" if hasattr(database, "NATIVE_KEY") else "pre-merge"
PREDICTED_EFFECTIVE_KEY = f"COALESCE({database.CORRECTED_KEY}, {NATIVE_KEY_SQL})"

READER_TIME_BUDGET_SECS = 5.0
_AUDIT_TABLES = ("videos", "detections", "species", "crops", "species_corrections", "blacklist")
TIMELINE_ALL = ("2000-01-01", "2100-12-31")

_FROM_VISIBLE = """FROM species s
            JOIN detections d ON s.detection_id = d.id"""


# -- helpers -----------------------------------------------------------------

def _select(conn, sql, params=()):
    """The only way this script touches the database: SELECT/WITH statements."""
    if not sql.strip().upper().startswith(("SELECT", "WITH")):
        raise AssertionError("audit_species_buckets attempted a non-SELECT statement")
    return conn.execute(sql, params)


def _odd_name_reasons(name):
    """Why a common name is odd: surrounding whitespace, control characters,
    non-ASCII characters (SQLite LOWER folds ASCII only). [] for a clean name."""
    reasons = []
    if name != name.strip(" \t\r\n"):
        reasons.append("whitespace")
    if any(ord(ch) < 32 for ch in name):
        reasons.append("control")
    if any(ord(ch) > 127 for ch in name):
        reasons.append("non-ascii")
    return reasons


def _ascii_lower(text):
    """Lower-case ASCII letters only, the way SQLite LOWER does."""
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


def _table_counts(conn):
    """Row counts of the audited tables (None for a table that is absent)."""
    counts = {}
    for table in _AUDIT_TABLES:
        exists = _select(
            conn,
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        counts[table] = (
            _select(conn, f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else None
        )
    return counts


def _collisions(conn):
    """Native keys (visible, uncorrected rows) carried by two or more labels.
    Returns {key: {"labels": {label: {...}}, "scientific": set}}."""
    rows = _select(
        conn,
        f"""SELECT {NATIVE_KEY_SQL} AS nk, s.label AS lbl,
                   s.common_name AS cn, s.scientific_name AS sn, COUNT(*) AS n
            {_FROM_VISIBLE}
            WHERE {database.KNOWN_SPECIES_FILTER}
              AND {database.CORRECTED_KEY} IS NULL
            GROUP BY {NATIVE_KEY_SQL}, s.label, s.common_name, s.scientific_name""",
    ).fetchall()
    groups = {}
    for r in rows:
        group = groups.setdefault(r["nk"], {"labels": {}, "scientific": set()})
        member = group["labels"].setdefault(
            r["lbl"], {"common": None, "scientific": None, "rows": 0}
        )
        member["rows"] += r["n"]
        if r["cn"] is not None and (member["common"] is None or r["cn"] > member["common"]):
            member["common"] = r["cn"]
        if r["sn"] is not None and (member["scientific"] is None or r["sn"] > member["scientific"]):
            member["scientific"] = r["sn"]
        sci = (r["sn"] or "").strip().lower()
        if sci:
            group["scientific"].add(sci)
    return {k: g for k, g in groups.items() if len(g["labels"]) >= 2}


def _merges(conn):
    """Predicted keys whose visible rows carry two or more current keys.
    Returns {predicted key: {"detections", "videos", "current": {key: (n, v)}}}."""
    per_pair = _select(
        conn,
        f"""SELECT {PREDICTED_EFFECTIVE_KEY} AS pk, {database.EFFECTIVE_KEY} AS ck,
                   COUNT(*) AS n, COUNT(DISTINCT d.video_id) AS v
            {_FROM_VISIBLE}
            WHERE {database.KNOWN_SPECIES_FILTER}
            GROUP BY {PREDICTED_EFFECTIVE_KEY}, {database.EFFECTIVE_KEY}""",
    ).fetchall()
    per_key = _select(
        conn,
        f"""SELECT {PREDICTED_EFFECTIVE_KEY} AS pk,
                   COUNT(*) AS n, COUNT(DISTINCT d.video_id) AS v
            {_FROM_VISIBLE}
            WHERE {database.KNOWN_SPECIES_FILTER}
            GROUP BY {PREDICTED_EFFECTIVE_KEY}""",
    ).fetchall()
    by_pk = {}
    for r in per_pair:
        by_pk.setdefault(r["pk"], {})[r["ck"]] = (r["n"], r["v"])
    merges = {}
    for r in per_key:
        current = by_pk.get(r["pk"], {})
        if len(current) >= 2:
            merges[r["pk"]] = {"detections": r["n"], "videos": r["v"], "current": current}
    return merges


def _timed(timings, name, fn):
    started = time.perf_counter()
    fn()
    timings[name] = time.perf_counter() - started


# -- the audit ---------------------------------------------------------------

def run_audit(db_path, expect_current=None, expect_predicted=None, search_term="raccoon"):
    """Print the audit for the database at `db_path`; return the number of FAIL
    lines printed. The caller owns the existence check (see main)."""
    failures = 0

    def fail(message):
        nonlocal failures
        failures += 1
        print(f"FAIL: {message}")

    original_db_path = database.get_db_path()
    try:
        database.set_db_path(db_path)

        print(f"INFO: mode {MODE}")
        print(f"INFO: sqlite_version {sqlite3.sqlite_version}")
        print(f"INFO: python_version {platform.python_version()}")

        with database.get_conn() as conn:
            has_corrections = _select(
                conn,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='species_corrections'",
            ).fetchone()[0] == 1
            if not has_corrections:
                fail("species_corrections table is missing")
                print(f"RESULT: FAIL ({failures} failure(s))")
                return failures

            counts_before = _table_counts(conn)

            current_keys = _select(
                conn,
                f"""SELECT COUNT(DISTINCT {database.EFFECTIVE_KEY})
                    {_FROM_VISIBLE}
                    WHERE {database.KNOWN_SPECIES_FILTER}""",
            ).fetchone()[0]
            predicted_keys = _select(
                conn,
                f"""SELECT COUNT(DISTINCT {PREDICTED_EFFECTIVE_KEY})
                    {_FROM_VISIBLE}
                    WHERE {database.KNOWN_SPECIES_FILTER}""",
            ).fetchone()[0]
            print(f"INFO: current_keys {current_keys}")
            print(f"INFO: predicted_keys {predicted_keys}")

            # COLLISION block (D-02, D-03): different taxa are reported and
            # merged anyway.
            collisions = _collisions(conn)
            for key in sorted(collisions):
                group = collisions[key]
                taxa = "yes" if len(group["scientific"]) > 1 else "no"
                print(
                    f"COLLISION key={key} labels={len(group['labels'])} different_taxa={taxa}"
                )
                for label in sorted(group["labels"]):
                    m = group["labels"][label]
                    print(
                        f"  label={label} common={m['common']!r} "
                        f"scientific={m['scientific']!r} rows={m['rows']}"
                    )
            print(f"INFO: collisions {len(collisions)}")

            # MERGE block: the current -> predicted key list (Pitfall 3 figures:
            # detections add, videos are a distinct union).
            merges = _merges(conn)
            for key in sorted(merges):
                m = merges[key]
                print(
                    f"MERGE key={key} from={len(m['current'])} "
                    f"detections={m['detections']} videos={m['videos']}"
                )
                for ck in sorted(m["current"]):
                    n, v = m["current"][ck]
                    print(f"  current={ck} detections={n} videos={v}")
            print(f"INFO: merges {len(merges)}")

            # Blank common names: stay their own buckets (D-01).
            blank = _select(
                conn,
                f"""SELECT s.label AS lbl, COUNT(*) AS n
                    FROM species s
                    WHERE {database._normalize_key_sql('s.common_name')} IS NULL
                      AND s.label != 'Unknown species'
                      AND s.label NOT LIKE '%;;;;;;blank'
                    GROUP BY s.label
                    ORDER BY s.label""",
            ).fetchall()
            print(f"INFO: blank_name_labels {len(blank)}")
            for r in blank:
                print(f"  label={r['lbl']} rows={r['n']}")

            # Odd characters in common names.
            names = _select(
                conn,
                """SELECT s.common_name AS cn, COUNT(*) AS n
                   FROM species s
                   WHERE s.common_name IS NOT NULL
                   GROUP BY s.common_name
                   ORDER BY s.common_name""",
            ).fetchall()
            odd = [(r["cn"], _odd_name_reasons(r["cn"]), r["n"]) for r in names]
            odd = [o for o in odd if o[1]]
            print(f"INFO: odd_names {len(odd)}")
            for name, reasons, n in odd:
                print(f"  name={name!r} reasons={','.join(reasons)} rows={n}")

            # Names equal under full Unicode casefold but not under SQLite's
            # ASCII-only LOWER: they stay separate buckets.
            folded = {}
            for r in names:
                trimmed = r["cn"].strip(" \t\r\n")
                if trimmed:
                    folded.setdefault(trimmed.casefold(), set()).add(trimmed)
            variants = []
            for variant_set in folded.values():
                if len({_ascii_lower(v) for v in variant_set}) > 1:
                    variants.append(sorted(variant_set))
            variants.sort()
            print(f"INFO: unicode_case_variants {len(variants)}")
            for variant in variants:
                print(f"  names={variant!r}")

            # RESEARCH A1: non-Unknown labels named 'Unknown species'.
            unknown_named = _select(
                conn,
                f"""SELECT COUNT(*) FROM species s
                    WHERE {database._normalize_key_sql('s.common_name')} = 'unknown species'
                      AND s.label != 'Unknown species'""",
            ).fetchone()[0]
            print(f"INFO: unknown_name_on_other_labels {unknown_named}")
            if unknown_named > 0:
                print(
                    f"WARN: {unknown_named} rows on other labels are named 'Unknown species'; "
                    "NATIVE_KEY keys them into the Unknown bucket "
                    "(excluded from top species and the 7-day chart)"
                )

        # Display names shared by two or more list rows (BUCKET-04).
        species_list = database.get_species_list()
        by_name = {}
        for row in species_list:
            norm = _ascii_lower((row["common_name"] or "").strip(" \t\r\n"))
            if norm:
                by_name.setdefault(norm, []).append(row["label"])
        shared = {n: keys for n, keys in by_name.items() if len(keys) >= 2}
        print(f"INFO: shared_display_names {len(shared)}")
        for name in sorted(shared):
            print(f"  name={name} keys={shared[name]}")
        if MODE == "merged" and shared:
            fail(f"shared_display_names {len(shared)}")

        # Expectation checks.
        if expect_current is not None and expect_current != current_keys:
            fail(f"current_keys {current_keys} != expected {expect_current}")
        if expect_predicted is not None and expect_predicted != predicted_keys:
            fail(f"predicted_keys {predicted_keys} != expected {expect_predicted}")
        if MODE == "merged" and current_keys != predicted_keys:
            fail(f"merged mode: current_keys {current_keys} != predicted_keys {predicted_keys}")

        # Reader timings.
        timings = {}
        _timed(timings, "get_species_list", database.get_species_list)
        _timed(timings, "get_stats", database.get_stats)
        _timed(timings, "get_timeline", database.get_timeline)
        _timed(
            timings,
            "get_timeline(all)",
            lambda: database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1]),
        )
        if species_list:
            big = max(species_list, key=lambda r: r["detection_count"])["label"]
            _timed(timings, "get_species_detail", lambda: database.get_species_detail(big))
            _timed(
                timings,
                "get_gallery(species)",
                lambda: database.get_gallery(species_label=big, per_page=100),
            )
            _timed(
                timings,
                "get_videos(species)",
                lambda: database.get_videos(species_label=big, per_page=100),
            )
        _timed(
            timings,
            "get_videos(has_species=True)",
            lambda: database.get_videos(has_species=True, per_page=10),
        )
        _timed(
            timings,
            "get_videos(has_species=False)",
            lambda: database.get_videos(has_species=False, per_page=10),
        )
        _timed(
            timings,
            "get_videos(search)",
            lambda: database.get_videos(search=search_term, per_page=10),
        )
        _timed(timings, "search", lambda: database.search(search_term))
        for name, secs in timings.items():
            print(f"INFO: timing {name} {secs:.3f}")
        slow = {n: round(t, 3) for n, t in timings.items() if t >= READER_TIME_BUDGET_SECS}
        if slow:
            fail(f"over the {READER_TIME_BUDGET_SECS}s budget: {slow}")

        # The audit changed nothing.
        with database.get_conn() as conn:
            counts_after = _table_counts(conn)
        if counts_after != counts_before:
            fail(f"audit changed row counts before={counts_before} after={counts_after}")
    finally:
        database.set_db_path(original_db_path)

    if failures:
        print(f"RESULT: FAIL ({failures} failure(s))")
    else:
        print("RESULT: PASS")
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only audit of the Phase 16 unified-species-bucket key (D-02 gate). "
        "Run it on a byte-copy of the production database."
    )
    parser.add_argument(
        "--db", required=True,
        help="path to a BYTE-COPY of the database (required: there is no default)",
    )
    parser.add_argument("--expect-current", type=int, help="expected current key count")
    parser.add_argument("--expect-predicted", type=int, help="expected predicted key count")
    parser.add_argument(
        "--search-term", default="raccoon",
        help="term timed through get_videos(search=) and search() (default: raccoon)",
    )
    args = parser.parse_args(argv)

    # sqlite3.connect creates a missing file, so check before set_db_path.
    if not Path(args.db).exists():
        print(f"ERROR: no database at {args.db}")
        return 2

    failures = run_audit(
        args.db,
        expect_current=args.expect_current,
        expect_predicted=args.expect_predicted,
        search_term=args.search_term,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
