"""
verify_phase16.py - stdlib-only verification harness for Phase 16 (Unified
Species Buckets & Reader Conversion).

Suites:
    audit_script - plan 16-01, the D-02 read-only audit
                   (scripts/audit_species_buckets.py) proven end to end on a
                   fixture with known collisions, in both pre-merge and merged
                   mode (AS1-AS7).
    merge        - plan 16-02, BUCKET-01..04: same-name natives and corrections
                   are ONE bucket, native-first display name, the corrected
                   flag, the no-shared-name invariant and the single
                   definitions (MG1-MG9; MG1 same-name natives, MG2 a
                   correction joins them, MG3 adversarial display order, MG4
                   most frequent native name, MG5 correction-only bucket, MG6
                   re-correction, MG7 shared-name invariant, MG8 corrected
                   badge flag, MG9 source pins).
    unknown      - plan 16-02, BUCKET-02/D-05: both stored shapes of Unknown
                   are one bucket and stay out of top_species and the 7-day
                   chart (UN1-UN6).
    resolver     - plan 16-02, D-06/BUCKET-05: an old raw label, a name in any
                   casing or the key resolves to the same bucket at
                   get_species_detail, get_gallery and get_videos (RS1-RS6).
    (readers, frontend_src and live are added by plan 16-03.)

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
    python scripts/verify_phase16.py --suite audit_script|merge|unknown|resolver|all
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


# -- shared helpers for the merge / unknown / resolver suites ----------------

BADGE_LINE = (
    "${s.has_correction ? ' <span class=\"badge-corrected\">✏ corrected</span>' : ''}"
)


@contextlib.contextmanager
def _fixture_db(name):
    """A fresh Phase 16 fixture in its own temp dir; restores the DB path."""
    original = database.get_db_path()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        yield _seed_phase16_fixture(str(Path(tmp.name) / f"{name}.db"))
    finally:
        database.set_db_path(original)
        tmp.cleanup()


def _case(case_id, fn):
    """Run one case body returning (ok, detail). An exception is a FAIL, not
    an abort, so the suite still reports a count. Returns 1 or 0."""
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a harness reports, never aborts
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    p15._check(case_id, ok, detail)
    return 1 if ok else 0


def _row(key):
    return p15._list_row(key)


def _src_db():
    """database.py with Python comment lines stripped."""
    return p15._strip_hash_comment_lines(p15._read_text("database.py"))


# -- merge suite -------------------------------------------------------------

def suite_merge():
    """`merge` suite cases MG1-MG9 (9 total): BUCKET-01..04, D-01, D-04, D-07."""
    total = 9
    passed = 0

    # MG1 - same-name natives merge; videos are a distinct union.
    def mg1():
        with _fixture_db("mg1") as ids:
            dog = _row(KEY_DOG)
            labels = p15._list_labels()
            detail = database.get_species_detail(KEY_DOG)
            ok = (
                dog is not None
                and dog["detection_count"] == 4
                and dog["video_count"] == 3
                and database.get_gallery(species_label=KEY_DOG)["total"] == 4
                and database.get_videos(species_label=KEY_DOG)["total"] == 3
                and detail["info"].get("total_detections") == 4
                and p15._detail_crop_ids(KEY_DOG) == {ids["g1"], ids["g2"], ids["n1"], ids["n2"]}
                and p15.LBL_DOG not in labels
                and LBL_DOG2 not in labels
                and LBL_NONAME in labels
            )
            return ok, f"dog={dog}, labels={sorted(labels)}"

    passed += _case("MG1", mg1)

    # MG2 + MG6 share one DB: a correction joins the native bucket, then is
    # re-pointed at another bucket.
    with _fixture_db("mg2") as ids:
        def mg2():
            rc = database.correct_species(ids["c1"], "domestic dog", "Canis domesticus")
            dog, cat = _row(KEY_DOG), _row(KEY_CAT)
            ok = (
                rc == 1
                and dog is not None
                and dog["detection_count"] == 5
                and dog["video_count"] == 4
                and dog["has_correction"] == 1
                and dog["common_name"] == "Domestic Dog"
                and dog["scientific_name"] == "Canis lupus familiaris"
                and cat is not None
                and cat["detection_count"] == 6
                and ids["c1"] in p15._gallery_ids(species_label=KEY_DOG)
                and ids["c1"] in p15._detail_crop_ids(KEY_DOG)
            )
            return ok, f"rc={rc}, dog={dog}, cat={cat}"

        passed += _case("MG2", mg2)

        def mg6():
            rc = database.correct_species(ids["c1"], "Domestic Cat", "Felis catus")
            dog, cat = _row(KEY_DOG), _row(KEY_CAT)
            ok = (
                rc == 1
                and cat is not None
                and cat["detection_count"] == 7
                and cat["video_count"] == 5
                and cat["has_correction"] == 1
                and cat["common_name"] == "Domestic Cat"
                and dog is not None
                and dog["detection_count"] == 4
                and dog["video_count"] == 3
            )
            return ok, f"rc={rc}, dog={dog}, cat={cat}"

        passed += _case("MG6", mg6)

    # MG3 - adversarial order: the corrected member's wrong AI name ("Wild
    # Boar") sorts after the bucket name and a lowercase correction text sorts
    # after it too; the native tie breaks by name.
    def mg3():
        with _fixture_db("mg3") as ids:
            rc = database.correct_species(ids["b1"], "mule deer", "Cervus fakeus")
            names = p15._display_names_for(KEY_DEER)
            deer = _row(KEY_DEER)
            ok = (
                rc == 1
                and names == {"Mule Deer"}
                and deer is not None
                and deer["scientific_name"] == "Odocoileus hemionus"
            )
            return ok, f"rc={rc}, names={names}, deer={deer}"

    passed += _case("MG3", mg3)

    # MG4 - the most frequent native name wins, deterministically.
    def mg4():
        with _fixture_db("mg4"):
            rac = _row(KEY_RACCOON)
            lists = [
                [(r["label"], r["common_name"]) for r in database.get_species_list()]
                for _ in range(3)
            ]
            ok = (
                rac is not None
                and rac["common_name"] == "Northern Raccoon"
                and rac["scientific_name"] == "Procyon lotor"
                and rac["detection_count"] == 3
                and rac["video_count"] == 3
                and lists[0] == lists[1] == lists[2]
            )
            return ok, f"rac={rac}"

    passed += _case("MG4", mg4)

    # MG5 - a correction-only bucket keeps its key_display name.
    def mg5():
        with _fixture_db("mg5") as ids:
            rc = database.correct_species(ids["p1"], "Coyote ", "Canis latrans")
            coyote = _row("coyote")
            ok = (
                rc == 1
                and coyote is not None
                and coyote["common_name"] == "Coyote"
                and coyote["scientific_name"] == "Canis latrans"
                and KEY_BOBCAT not in p15._list_labels()
                and p15._display_names_for("coyote") == {"Coyote"}
            )
            return ok, f"rc={rc}, coyote={coyote}"

    passed += _case("MG5", mg5)

    # MG7 - BUCKET-04 hard invariant after a mixed correction state.
    def mg7():
        with _fixture_db("mg7") as ids:
            rcs = [
                database.correct_species(ids["c1"], "domestic dog", "Canis lupus familiaris"),
                database.correct_species(ids["b1"], "mule deer", "Odocoileus hemionus"),
                database.correct_species(ids["p1"], "Coyote ", "Canis latrans"),
                database.correct_species(ids["u1"], "Western Gray Squirrel", "Sciurus griseus"),
                database.save_video_correction(
                    ids["vid5"], LBL_DOG2, "raccoon_label", "Northern Raccoon", "Procyon lotor"
                ),
            ]
            shared = _shared_name_violations(database.get_species_list())
            hard, soft = p15._cross_reader_violations()
            ok = all(rc is not None for rc in rcs) and shared == [] and (hard, soft) == ([], [])
            return ok, f"rcs={rcs}, shared={shared}, hard={hard}, soft={soft}"

    passed += _case("MG7", mg7)

    # MG8 - BUCKET-03 / D-07: a merged bucket carries the existing boolean flag.
    def mg8():
        with _fixture_db("mg8") as ids:
            before = _row(KEY_DOG)
            rc = database.correct_species(ids["n1"], "Domestic Dog", "Canis lupus familiaris")
            after = _row(KEY_DOG)
            html = p15._read_text("static/index.html")
            ok = (
                rc == 1
                and before is not None
                and before["has_correction"] == 0
                and after is not None
                and after["has_correction"] == 1
                and after["detection_count"] == 4
                and html.count(BADGE_LINE) == 1
            )
            return ok, f"before={before}, after={after}, badge_count={html.count(BADGE_LINE)}"

    passed += _case("MG8", mg8)

    # MG9 - single definitions in the comment-stripped source.
    def mg9():
        db_text = _src_db()
        lines = db_text.splitlines()
        n_nat = sum(1 for ln in lines if ln.startswith("NATIVE_KEY = "))
        n_cte = sum(1 for ln in lines if ln.startswith("NATIVE_DISPLAY_CTE = "))
        cte_region = p15._slice(db_text, "NATIVE_DISPLAY_CTE = ", "\n\n")
        n_lower = db_text.count("LOWER(")
        n_join = db_text.count("LEFT JOIN native_display nd ON nd.k = ")
        predicted = " ".join(audit._PREDICTED_NATIVE_KEY.split())
        shipped = " ".join(database.NATIVE_KEY.split())
        ok = (
            n_nat == 1
            and n_cte == 1
            and "{NATIVE_KEY}" in cte_region
            and n_lower == 1
            and n_join == 6
            and predicted == shipped
        )
        return ok, (
            f"n_nat={n_nat}, n_cte={n_cte}, cte_uses_key={'{NATIVE_KEY}' in cte_region}, "
            f"n_lower={n_lower}, n_join={n_join}, audit_key_equal={predicted == shipped}"
        )

    passed += _case("MG9", mg9)

    return (passed, total)


# -- unknown suite -----------------------------------------------------------

def suite_unknown():
    """`unknown` suite cases UN1-UN6 (6 total): BUCKET-02, D-05."""
    total = 6
    passed = 0

    # UN1 - both stored shapes of Unknown land in ONE bucket.
    def un1():
        with _fixture_db("un1"):
            with database.get_conn() as conn:
                named = conn.execute(
                    "SELECT COUNT(*) FROM species WHERE label = 'Unknown species' "
                    "AND common_name = 'Unknown species'"
                ).fetchone()[0]
                nulled = conn.execute(
                    "SELECT COUNT(*) FROM species WHERE label = 'Unknown species' "
                    "AND common_name IS NULL"
                ).fetchone()[0]
            rows = database.get_species_list()
            unknown_labels = [r["label"] for r in rows if _ascii_norm(r["label"]) == "unknown species"]
            row = _row(KEY_UNKNOWN)
            ok = (
                named >= 1
                and nulled >= 1
                and unknown_labels == [KEY_UNKNOWN]
                and row is not None
                and row["detection_count"] == 4
                and LBL_PSEUDO not in {r["label"] for r in rows}
            )
            return ok, f"named={named}, nulled={nulled}, unknown_labels={unknown_labels}, row={row}"

    passed += _case("UN1", un1)

    # UN2 - still excluded from top_species and the 7-day chart.
    def un2():
        with _fixture_db("un2"):
            stats = database.get_stats()
            bad_top = [
                t for t in stats["top_species"]
                if "unknown species" in (_ascii_norm(t["label"]), _ascii_norm(t["common_name"]))
            ]
            bad_act = [
                r for r in p15._activity_rows()
                if "unknown species" in (_ascii_norm(r["label"]), _ascii_norm(r["species"]))
            ]
            ok = not bad_top and not bad_act
            return ok, f"bad_top={bad_top}, bad_act={bad_act}"

    passed += _case("UN2", un2)

    # UN3 - still present in the timeline and drilldown.
    def un3():
        with _fixture_db("un3") as ids:
            row = _row(KEY_UNKNOWN)
            tl = p15._timeline_sums().get(KEY_UNKNOWN)
            detail = database.get_species_detail(KEY_UNKNOWN)
            ok = (
                row is not None
                and row["video_count"] == 3
                and tl == row["video_count"]
                and detail["info"].get("total_detections") == 4
                and ids["z1"] in p15._detail_crop_ids(KEY_UNKNOWN)
            )
            return ok, f"row={row}, timeline={tl}, detail_info={detail['info']}"

    passed += _case("UN3", un3)

    # UN4 - old raw labels resolve to the Unknown bucket.
    def un4():
        with _fixture_db("un4"):
            ok = (
                database.resolve_species_key(LBL_PSEUDO) == KEY_UNKNOWN
                and database.get_gallery(species_label=LBL_PSEUDO)["total"] == 4
                and database.get_gallery(species_label=p15.LBL_UNKNOWN)["total"] == 4
            )
            return ok, (
                f"resolved={database.resolve_species_key(LBL_PSEUDO)!r}, "
                f"pseudo_total={database.get_gallery(species_label=LBL_PSEUDO)['total']}"
            )

    passed += _case("UN4", un4)

    # UN5 - a detection corrected away from Unknown counts under its new key.
    def un5():
        with _fixture_db("un5") as ids:
            rc = database.correct_species(ids["uu1"], "Western Gray Squirrel", "Sciurus griseus")
            squirrel = _row("western gray squirrel")
            unknown = _row(KEY_UNKNOWN)
            act = p15._activity_sums().get("western gray squirrel")
            stats = database.get_stats()
            ok = (
                rc == 1
                and squirrel is not None
                and squirrel["detection_count"] == 1
                and unknown is not None
                and unknown["detection_count"] == 3
                and act == 1
                and stats["unique_species"] == len(database.get_species_list())
            )
            return ok, f"rc={rc}, squirrel={squirrel}, unknown={unknown}, activity={act}"

    passed += _case("UN5", un5)

    # UN6 - source: the Unknown literal guard and the two get_stats exclusions.
    def un6():
        db_text = _src_db()
        native = p15._slice(db_text, "NATIVE_KEY = ", "\nEFFECTIVE_KEY = ")
        stats_fn = p15._slice(db_text, "def get_stats(", "\ndef ")
        n_excl = stats_fn.count("{EFFECTIVE_KEY} != 'Unknown species'")
        ok = "'Unknown species'" in native and "'unknown species'" in native and n_excl == 2
        return ok, f"n_excl={n_excl}, native={native!r}"

    passed += _case("UN6", un6)

    return (passed, total)


# -- resolver suite ----------------------------------------------------------

def suite_resolver():
    """`resolver` suite cases RS1-RS6 (6 total): D-06, BUCKET-05."""
    total = 6
    passed = 0

    # RS1 - every spelling of the dog bucket lands on it at all three entry points.
    def rs1():
        with _fixture_db("rs1"):
            bad = []
            for v in (p15.LBL_DOG, LBL_DOG2, "Domestic Dog", "  DOMESTIC DOG\t", KEY_DOG):
                detail = database.get_species_detail(v)
                got = (
                    database.resolve_species_key(v),
                    detail["label"],
                    detail["info"].get("total_detections"),
                    database.get_gallery(species_label=v)["total"],
                    database.get_videos(species_label=v)["total"],
                )
                if got != (KEY_DOG, KEY_DOG, 4, 4, 3):
                    bad.append((v, got))
            return not bad, f"bad={bad}"

    passed += _case("RS1", rs1)

    # RS2 - an unknown value still yields nothing (the route's 404).
    def rs2():
        with _fixture_db("rs2"):
            ok = (
                database.get_species_detail(NO_SUCH_KEY)["info"] == {}
                and database.get_gallery(species_label=NO_SUCH_KEY)["total"] == 0
                and database.get_videos(species_label=NO_SUCH_KEY)["total"] == 0
            )
            return ok, ""

    passed += _case("RS2", rs2)

    # RS3 - a blank-name native and the Unknown key resolve to themselves.
    def rs3():
        with _fixture_db("rs3"):
            got = (
                database.resolve_species_key(LBL_NONAME),
                database.resolve_species_key(KEY_UNKNOWN),
            )
            return got == (LBL_NONAME, KEY_UNKNOWN), f"got={got}"

    passed += _case("RS3", rs3)

    # RS4 - empty input passes through unchanged and filters nothing.
    def rs4():
        with _fixture_db("rs4"):
            ok = (
                database.resolve_species_key("") == ""
                and database.resolve_species_key(None) is None
                and database.get_gallery(species_label="")["total"] == database.get_gallery()["total"]
            )
            return ok, ""

    passed += _case("RS4", rs4)

    # RS5 - an old link to a bucket that was corrected away still 404s.
    def rs5():
        with _fixture_db("rs5") as ids:
            rcs = [
                database.correct_species(ids["m1"], "Elk", "Cervus canadensis"),
                database.correct_species(ids["m2"], "Elk", "Cervus canadensis"),
            ]
            detail = database.get_species_detail(LBL_DEER)
            elk = _row("elk")
            ok = (
                rcs == [1, 1]
                and detail["info"] == {}
                and detail["label"] == KEY_DEER
                and elk is not None
                and elk["detection_count"] == 2
            )
            return ok, f"rcs={rcs}, info={detail['info']}, label={detail['label']!r}, elk={elk}"

    passed += _case("RS5", rs5)

    # RS6 - source (comment-stripped): the value is bound, never interpolated,
    # and each entry point resolves once.
    def rs6():
        db_text = _src_db()
        resolver = p15._slice(db_text, "def resolve_species_key(", "\ndef ")
        detail_fn = p15._slice(db_text, "def get_species_detail(", "\ndef ")
        gallery_fn = p15._slice(db_text, "def get_gallery(", "\ndef ")
        videos_fn = p15._slice(db_text, "def get_videos(", "\ndef ")
        web_text = p15._strip_hash_comment_lines(p15._read_text("web_app.py"))
        route = p15._slice(web_text, '@app.get("/api/species/{label:path}")', "@app.")
        ok = (
            "s.label = ?" in resolver
            and "_normalize_key_sql('?')" in resolver
            and "{value}" not in resolver
            and "label = resolve_species_key(label)" in detail_fn
            and gallery_fn.count("resolve_species_key(species_label)") == 1
            and videos_fn.count("resolve_species_key(species_label)") == 1
            and "db.get_species_detail(label)" in route
        )
        return ok, (
            f"resolver_ok={'s.label = ?' in resolver}, "
            f"gallery_n={gallery_fn.count('resolve_species_key(species_label)')}, "
            f"videos_n={videos_fn.count('resolve_species_key(species_label)')}, route={route!r}"
        )

    passed += _case("RS6", rs6)

    return (passed, total)


# -- registry / CLI ----------------------------------------------------------

SUITES = {
    "audit_script": (suite_audit_script, 7),
    "merge": (suite_merge, 9),
    "unknown": (suite_unknown, 6),
    "resolver": (suite_resolver, 6),
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
