"""
verify_phase17.py - stdlib-only verification harness for Phase 17 (Legacy
Correction Decoupling).

Phase 14 made species_corrections the sole correction store and froze three
columns on `species` plus one table (see LEGACY_COLUMNS / LEGACY_TABLE below).
Phase 17 removes every runtime dependency on those objects so Phase 18 can drop
them from production without crashing anything. This harness proves it by
building the app's database in TWO shapes from one seed and running the same
exerciser on both:

    lacking - what Phase 18 leaves behind: a database that physically has none
              of the legacy objects (a fresh init_db() of the decoupled code).
    legacy  - production's shape today: the lacking shape plus the legacy DDL,
              with non-NULL frozen values seeded in it, so "ignored" is proven
              rather than assumed.

Suites:
    schema    - SC1-SC4: init_db() on an empty file creates exactly the seven
                species columns and no legacy table or index; init_db() twice
                is a no-op on both shapes; a species table that predates
                top_candidates_json still gets that column and no legacy one.
    decoupled - DC1-DC6: video detail, every reader, both correction write
                paths, the reprocess write helper and a REAL
                `wildlife_processor.py --reprocess-flagged` run (ML modules
                stubbed) behave identically on both shapes, and a frozen
                legacy value is never surfaced, written or cleared.
    guard     - GD1-GD6: a permanent static scan of the non-harness sources.
                No file may name the legacy objects, comments included; the
                live API field `user_common_name` is allowed only inside an
                explicit allowlist of regions. GD1 is a negative control that
                proves the scanner can fail; GD5 proves it is not vacuous;
                GD6 keeps the four retired one-shot scripts gone. The scan is
                data-driven (SCAN_GLOBS, FORBIDDEN_*, BARE_ALLOW,
                GUARD_EXEMPT), so Phase 18 extends it with one line.
    endpoints - EP1-EP6: every GET route of web_app.app through
                fastapi.testclient.TestClient on both shapes (no 5xx, identical
                DB-backed output), the live `user_common_name` API field end to
                end, and the video-player correction endpoints. EP1 fails when a
                GET route is not mapped in GET_URLS / SKIP_ROUTES, so a route
                added later cannot escape the proof. Needs fastapi and httpx:
                skipped under `--suite all` when they are missing, a FAIL when
                `--suite endpoints` is asked for explicitly.
    bytecopy  - BC1-BC5 (explicit, --db): a read-only rehearsal on a real
                database. The source is opened mode=ro and copied with
                Connection.backup(). Copy A keeps the legacy objects: init_db()
                must change nothing. Copy B has them dropped (SQLite >= 3.35):
                integrity and foreign-key checks clean, init_db() re-adds
                nothing. Every GET route answers below 500 on both and matches,
                the read and write exerciser runs clean on both, and the source's
                schema and row counts are unchanged afterwards (D-03).
    live      - LV1-LV5 (explicit, --base-url): GET only against a running
                service. Against the pre-Phase-17 service exactly LV2 fails (the
                old code still serves the corrections key); the restart turns
                it green. It never sends a write.

Fixture. Both shapes are built from scripts/verify_phase15.py's fixture plus a
dual-lens pair and a flagged video with two crops on disk. The legacy DDL below
lives ONLY in this file: database.py no longer declares those objects, so the
harness re-creates them with raw statements on a copy.

Usage:
    python scripts/verify_phase17.py --suite schema|decoupled|guard|endpoints|all
    python scripts/verify_phase17.py --suite bytecopy --db <path to a database>
    python scripts/verify_phase17.py --suite live --base-url http://127.0.0.1:8080
    python scripts/verify_phase17.py --list

verify_phase15 is a sibling import, so run this file without `python -I`.
"""

import argparse
import contextlib
import fnmatch
import json
import locale
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import verify_phase15 as p15


# -- constants ---------------------------------------------------------------

# species columns a fresh init_db() must create, in this order.
SPECIES_COLUMNS = [
    "id", "detection_id", "label", "common_name", "scientific_name",
    "confidence", "top_candidates_json",
]

# The three frozen columns on `species`, the frozen table and its two indexes.
LEGACY_COLUMNS = ["user_common_name", "user_scientific_name", "corrected_at"]
LEGACY_TABLE = "video_corrections"
LEGACY_INDEXES = ["idx_corrections_video", "idx_corrections_label"]

# Production's legacy DDL, copied from database.py's SCHEMA and migration as they
# stood before Phase 17 removed them. Applied to a fixture copy only.
LEGACY_DDL = [
    "ALTER TABLE species ADD COLUMN user_common_name TEXT",
    "ALTER TABLE species ADD COLUMN user_scientific_name TEXT",
    "ALTER TABLE species ADD COLUMN corrected_at TEXT",
    """CREATE TABLE video_corrections (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id             INTEGER NOT NULL REFERENCES videos(id),
        original_label       TEXT NOT NULL,
        corrected_label      TEXT,
        corrected_common     TEXT,
        corrected_scientific TEXT,
        corrected_at         TEXT NOT NULL,
        note                 TEXT
    )""",
    "CREATE INDEX idx_corrections_video ON video_corrections(video_id)",
    "CREATE INDEX idx_corrections_label ON video_corrections(original_label)",
]

# Frozen values seeded on the legacy shape. No reader may ever surface them.
LEGACY_SEED_NAME = "Legacy Frozen Gallery Name"
LEGACY_SEED_SCIENTIFIC = "Legacius frozenii"
LEGACY_VIDEO_NAME = "Legacy Frozen Video Name"
LEGACY_STAMP = "2026-01-02T03:04:05"

# What the stubbed SpeciesNet returns for every crop in the reprocess run.
STUB_LABEL = "33333333-0000-0000-0000-000000000017;mammalia;carnivora;canidae;canis;latrans;coyote"
STUB_COMMON = "Coyote"
STUB_SCIENTIFIC = "Canis latrans"

SHAPES = ("lacking", "legacy")


# -- helpers -----------------------------------------------------------------

def _repo_root():
    return p15._repo_root()


def _legacy_objects(path):
    """Sorted names of the legacy objects present in the database at `path`."""
    database.set_db_path(path)
    found = []
    with database.get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(species)").fetchall()]
        found += [c for c in LEGACY_COLUMNS if c in cols]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master").fetchall()}
        found += [n for n in [LEGACY_TABLE] + LEGACY_INDEXES if n in names]
    return sorted(found)


def _schema_snapshot(path):
    """Everything init_db() could add or drop: the species column list, every
    sqlite_master row, and (when the frozen objects exist) their row counts."""
    database.set_db_path(path)
    with database.get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(species)").fetchall()]
        master = sorted(
            tuple(r) for r in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            ).fetchall()
        )
        table_rows = None
        if any(m[1] == LEGACY_TABLE for m in master):
            table_rows = conn.execute(f"SELECT COUNT(*) FROM {LEGACY_TABLE}").fetchone()[0]
        frozen_values = None
        if all(c in cols for c in LEGACY_COLUMNS):
            frozen_values = conn.execute(
                "SELECT COUNT(*) FROM species WHERE user_common_name IS NOT NULL "
                "OR user_scientific_name IS NOT NULL OR corrected_at IS NOT NULL"
            ).fetchone()[0]
    return {"columns": cols, "master": master,
            "table_rows": table_rows, "frozen_values": frozen_values}


def _frozen_rows(path):
    """The frozen legacy values per detection (legacy shape only)."""
    database.set_db_path(path)
    with database.get_conn() as conn:
        return [
            tuple(r) for r in conn.execute(
                "SELECT detection_id, user_common_name, user_scientific_name, corrected_at "
                "FROM species WHERE user_common_name IS NOT NULL "
                "OR user_scientific_name IS NOT NULL OR corrected_at IS NOT NULL "
                "ORDER BY detection_id"
            ).fetchall()
        ]


def _seed_phase17(path, files_dir):
    """Phase 15's fixture plus a dual-lens pair and a flagged video, then one
    correction through each write path. Returns the ids dict (Phase 15's keys
    plus pair0/pair1 video ids, pair_det0/pair_det1, rp_vid, rp_det1, rp_det2
    and rp_label2)."""
    ids = p15._seed_phase15_fixture(path)
    files = Path(files_dir)
    files.mkdir(parents=True, exist_ok=True)

    def _file(name):
        p = files / name
        p.write_bytes(b"x")
        return str(p)

    pair_names = ("World Watch_00_20261001060000.mp4", "World Watch_01_20261001060000.mp4")
    when = p15._utc_day_iso(1)

    with database.get_conn() as conn:

        def _video(filename, filepath, flagged=0):
            cur = conn.execute(
                "INSERT INTO videos (filename, filepath, camera_name, kept, recorded_at, "
                "processed_at, needs_reprocess) VALUES (?, ?, 'World Watch', 1, ?, ?, ?)",
                (filename, filepath, when, when, flagged),
            )
            return cur.lastrowid

        def _detect(video_id, label, quality, crop_path):
            common, sci = p15._NAMES[label]
            cur = conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
            )
            det_id = cur.lastrowid
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, 0.9)",
                (det_id, label, common, sci),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, crop_path, float(quality), when),
            )
            return det_id

        ids["pair0"] = _video(pair_names[0], _file("pair0.mp4"))
        ids["pair1"] = _video(pair_names[1], _file("pair1.mp4"))
        ids["pair_det0"] = _detect(ids["pair0"], p15.LBL_CAT, 80, _file("pair_crop0.jpg"))
        ids["pair_det1"] = _detect(ids["pair1"], p15.LBL_DOG, 70, _file("pair_crop1.jpg"))

        ids["rp_vid"] = _video("WorldWatch_17_reprocess.mp4", _file("reprocess.mp4"), flagged=1)
        ids["rp_det1"] = _detect(ids["rp_vid"], p15.LBL_CAT, 80, _file("rp_crop1.jpg"))
        ids["rp_det2"] = _detect(ids["rp_vid"], p15.LBL_RACCOON, 70, _file("rp_crop2.jpg"))
        ids["rp_label2"] = p15.LBL_RACCOON
    ids["pair_names"] = pair_names

    # Connections closed: link the pair, then write one correction per path.
    database.link_lens_pair(ids["pair0"], pair_names[0])
    database.correct_species(ids["rp_det1"], "Bobcat", "Lynx rufus")
    database.save_video_correction(
        ids["rp_vid"], ids["rp_label2"], "corr_label", "Red Fox", "Vulpes vulpes"
    )
    return ids


def _build_shapes(root):
    """Seed the lacking shape, copy it, add the legacy objects and frozen
    values to the copy. Raises AssertionError when init_db() still creates a
    legacy object (this is what turns every legacy-free case RED)."""
    root = Path(root)
    lacking = root / "lacking" / "wildlife.db"
    legacy = root / "legacy" / "wildlife.db"
    lacking.parent.mkdir(parents=True, exist_ok=True)
    legacy.parent.mkdir(parents=True, exist_ok=True)

    ids = _seed_phase17(str(lacking), str(root / "files"))
    present = _legacy_objects(str(lacking))
    if present:
        raise AssertionError(f"fresh init_db() still creates legacy objects: {present}")

    # Consistent snapshot of the lacking file (it is a WAL database). The source
    # opens through get_conn; the destination is a plain connection on a fixture
    # file that get_conn cannot address while the source is open.
    database.set_db_path(str(lacking))
    with database.get_conn() as src:
        dst = sqlite3.connect(str(legacy))
        try:
            src.backup(dst)
        finally:
            dst.close()

    database.set_db_path(str(legacy))
    with database.get_conn() as conn:
        for stmt in LEGACY_DDL:
            conn.execute(stmt)
        for det in (ids["c2"], ids["rp_det1"]):
            conn.execute(
                "UPDATE species SET user_common_name=?, user_scientific_name=?, corrected_at=? "
                "WHERE detection_id=?",
                (LEGACY_SEED_NAME, LEGACY_SEED_SCIENTIFIC, LEGACY_STAMP, det),
            )
        raw_label = conn.execute(
            "SELECT s.label FROM species s WHERE s.detection_id=?", (ids["pair_det0"],)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at, note) "
            "VALUES (?, ?, 'frozen_label', ?, 'Frozenus videoi', ?, NULL)",
            (ids["pair0"], raw_label, LEGACY_VIDEO_NAME, LEGACY_STAMP),
        )
    return {"lacking": str(lacking), "legacy": str(legacy), "ids": ids}


@contextlib.contextmanager
def _fixture():
    """Fresh shapes in a temp dir; restores the DB path afterwards."""
    original = database.get_db_path()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        yield _build_shapes(tmp.name)
    finally:
        database.set_db_path(original)
        tmp.cleanup()


def _case(case_id, fn):
    """Run one case body returning (ok, detail). An exception is a FAIL, not an
    abort, so the suite still reports a count. Returns 1 or 0."""
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a harness reports, never aborts
        ok, detail = False, f"raised {type(exc).__name__}: {exc}"
    p15._check(case_id, ok, detail)
    return 1 if ok else 0


def _each_shape(shapes, check):
    """Run check(name, path, ids) -> (ok, detail) on both shapes."""
    for name in SHAPES:
        ok, detail = check(name, shapes[name], shapes["ids"])
        if not ok:
            return False, f"[{name}] {detail}"
    return True, ""


def _dump(value):
    return json.dumps(value, sort_keys=True, default=str)


def _reader_outputs(path, ids, timings=None):
    """JSON of every reader the dashboard drives, against the database at
    `path`. Each result must be identical between the two shapes. When
    `timings` is a dict, the seconds each reader took are recorded in it."""
    database.set_db_path(path)
    calls = {
        "get_stats": lambda: database.get_stats(),
        "get_species_list": lambda: database.get_species_list(),
        "get_species_detail": lambda: database.get_species_detail(p15.KEY_CAT),
        "get_gallery": lambda: database.get_gallery(),
        "get_gallery(species)": lambda: database.get_gallery(species_label=p15.KEY_CAT),
        "get_cameras": lambda: database.get_cameras(),
        "get_videos": lambda: database.get_videos(),
        "get_videos(has_species)": lambda: database.get_videos(has_species=True),
        "get_videos(no_species)": lambda: database.get_videos(has_species=False),
        "get_videos(search)": lambda: database.get_videos(search="raccoon"),
        "get_timeline": lambda: database.get_timeline(),
        "search": lambda: database.search("raccoon"),
        "get_blank_videos": lambda: database.get_blank_videos(),
        "get_blacklist": lambda: database.get_blacklist(),
        "get_corrections": lambda: database.get_corrections(),
        "get_corrections(pair)": lambda: database.get_corrections(ids["pair0"]),
        "get_corrections(reprocess)": lambda: database.get_corrections(ids["rp_vid"]),
        "get_reprocess_queue": lambda: database.get_reprocess_queue(),
        "get_storage_stats": lambda: database.get_storage_stats(),
        "get_recent_runs": lambda: database.get_recent_runs(),
        "get_last_run": lambda: database.get_last_run(),
        "get_video_by_id(paired)": lambda: database.get_video_by_id(ids["pair0"]),
        "get_video_by_id(unpaired)": lambda: database.get_video_by_id(ids["vid1"]),
    }
    out = {}
    for name, fn in calls.items():
        started = time.monotonic()
        out[name] = _dump(fn())
        if timings is not None:
            timings[name] = time.monotonic() - started
    return out


# The processor run. A child process stubs the ML modules (so no model loads and
# the box needs no cv2 or speciesnet) and runs the real wildlife_processor.py as
# __main__ with --reprocess-flagged. This is the only way to execute the
# reprocess branch, which sits under `if __name__ == "__main__":` behind
# `import cv2`. The processor's own sys.exit(0) ends the child.
_BOOTSTRAP_TEMPLATE = '''
import importlib.util
import os
import runpy
import sys
import types

repo, data_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)

STUB_LABEL = __STUB_LABEL__


class SpeciesNet:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, **kw):
        return {"predictions": [
            {"classifications": {"classes": [STUB_LABEL], "scores": [0.95]}}
            for _ in kw["filepaths"]
        ]}


stub = types.ModuleType("speciesnet")
stub.SpeciesNet = SpeciesNet
stub.DEFAULT_MODEL = "stub"
sys.modules["speciesnet"] = stub

for name in ("cv2", "numpy"):
    if importlib.util.find_spec(name) is None:
        sys.modules[name] = types.ModuleType(name)

sys.argv = ["wildlife_processor.py", "--video-dir", data_dir,
            "--data-dir", data_dir, "--reprocess-flagged"]
runpy.run_path(os.path.join(repo, "wildlife_processor.py"), run_name="__main__")
'''
_REPROCESS_BOOTSTRAP = _BOOTSTRAP_TEMPLATE.replace("__STUB_LABEL__", repr(STUB_LABEL))


def _run_reprocess(path):
    """Run the processor's --reprocess-flagged branch against the database at
    `path` (its directory is the data dir). Returns the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-c", _REPROCESS_BOOTSTRAP, str(_repo_root()), str(Path(path).parent)],
        cwd=str(_repo_root()), capture_output=True, text=True, timeout=300,
    )


def _species_row(path, det_id):
    database.set_db_path(path)
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM species WHERE detection_id=?", (det_id,)).fetchone()
        return tuple(row) if row else None


def _correction_row(path, det_id):
    database.set_db_path(path)
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT corrected_common, suppressed, source FROM species_corrections "
            "WHERE detection_id=?", (det_id,)
        ).fetchone()
        return tuple(row) if row else None


# -- schema suite ------------------------------------------------------------

def suite_schema():
    """`schema` suite cases SC1-SC4 (4 total)."""
    total = 4
    passed = 0

    # SC1 - a fresh init_db() on an empty file.
    def sc1():
        original = database.get_db_path()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            path = str(Path(tmp.name) / "fresh.db")
            database.init_db(path)
            snap = _schema_snapshot(path)
            names = {m[1] for m in snap["master"]}
            problems = []
            if snap["columns"] != SPECIES_COLUMNS:
                problems.append(f"species columns {snap['columns']}")
            if LEGACY_TABLE in names:
                problems.append(f"table {LEGACY_TABLE} exists")
            problems += [f"index {i} exists" for i in LEGACY_INDEXES if i in names]
            if "species_corrections" not in names:
                problems.append("species_corrections missing")
            return not problems, "; ".join(problems)
        finally:
            database.set_db_path(original)
            tmp.cleanup()

    passed += _case("SC1", sc1)

    # SC2 - init_db() twice on production's shape changes nothing.
    def sc2():
        with _fixture() as shapes:
            path = shapes["legacy"]
            before = _schema_snapshot(path)
            database.init_db(path)
            once = _schema_snapshot(path)
            database.init_db(path)
            twice = _schema_snapshot(path)
            ok = before == once == twice
            return ok, "init_db() changed the legacy-present schema or its row counts"

    passed += _case("SC2", sc2)

    # SC3 - init_db() twice on the legacy-free shape changes nothing and adds
    # nothing legacy.
    def sc3():
        with _fixture() as shapes:
            path = shapes["lacking"]
            before = _schema_snapshot(path)
            database.init_db(path)
            database.init_db(path)
            after = _schema_snapshot(path)
            present = _legacy_objects(path)
            ok = before == after and not present
            return ok, f"snapshot changed={before != after}; legacy objects now present: {present}"

    passed += _case("SC3", sc3)

    # SC4 - a species table that predates top_candidates_json.
    def sc4():
        original = database.get_db_path()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            path = str(Path(tmp.name) / "old.db")
            database.set_db_path(path)
            with database.get_conn() as conn:
                conn.execute(
                    "CREATE TABLE species (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "detection_id INTEGER NOT NULL, label TEXT NOT NULL, common_name TEXT, "
                    "scientific_name TEXT, confidence REAL)"
                )
            database.init_db(path)
            cols = _schema_snapshot(path)["columns"]
            legacy = [c for c in LEGACY_COLUMNS if c in cols]
            ok = cols == SPECIES_COLUMNS and not legacy
            return ok, f"columns {cols}; legacy columns {legacy}"
        finally:
            database.set_db_path(original)
            tmp.cleanup()

    passed += _case("SC4", sc4)

    return (passed, total)


# -- decoupled suite ---------------------------------------------------------

def suite_decoupled():
    """`decoupled` suite cases DC1-DC6 (6 total)."""
    total = 6
    passed = 0

    # DC1 - video detail on both shapes: exact keys, pair, unpaired, missing.
    def dc1():
        def check(name, path, ids):
            database.set_db_path(path)
            r = database.get_video_by_id(ids["pair0"])
            want = {"video", "detections", "paired", "pair_detections"}
            if set(r) != want:
                return False, f"paired keys {sorted(r)}"
            if not r["paired"] or r["paired"]["id"] != ids["pair1"]:
                return False, f"paired={r['paired'] and r['paired'].get('id')}, want {ids['pair1']}"
            if not r["pair_detections"]:
                return False, "pair_detections empty"
            u = database.get_video_by_id(ids["vid1"])
            if set(u) != want or u["paired"] is not None or u["pair_detections"] != []:
                return False, f"unpaired result {sorted(u)} paired={u.get('paired')}"
            if database.get_video_by_id(999999) != {}:
                return False, "a missing id must give {}"
            return True, ""

        with _fixture() as shapes:
            return _each_shape(shapes, check)

    passed += _case("DC1", dc1)

    # DC2 - every reader is byte-identical between the shapes.
    def dc2():
        with _fixture() as shapes:
            a = _reader_outputs(shapes["lacking"], shapes["ids"])
            b = _reader_outputs(shapes["legacy"], shapes["ids"])
            diff = [k for k in a if a[k] != b[k]]
            return not diff, f"readers differ between shapes: {diff}"

    passed += _case("DC2", dc2)

    # DC3 - no reader surfaces a frozen legacy value.
    def dc3():
        with _fixture() as shapes:
            out = _reader_outputs(shapes["legacy"], shapes["ids"])
            leaked = [
                k for k, v in out.items()
                if LEGACY_SEED_NAME in v or LEGACY_VIDEO_NAME in v or LEGACY_SEED_SCIENTIFIC in v
            ]
            return not leaked, f"frozen legacy values surfaced by: {leaked}"

    passed += _case("DC3", dc3)

    # DC4 - both write paths touch only species_corrections.
    def dc4():
        def check(name, path, ids):
            database.set_db_path(path)
            before_snap = _schema_snapshot(path)
            frozen_before = _frozen_rows(path) if name == "legacy" else []

            # Gallery path: r1 (raccoon, vid3).
            det = ids["r1"]
            row_before = _species_row(path, det)
            n = database.correct_species(
                detection_id=det, user_common_name="Ringtail",
                user_scientific_name="Bassariscus astutus",
            )
            if n != 1:
                return False, f"correct_species returned {n}"
            if _species_row(path, det) != row_before:
                return False, "correct_species changed the species row"
            sc = _correction_row(path, det)
            if not sc or sc[0] != "Ringtail" or sc[2] != "gallery":
                return False, f"species_corrections row after correct_species: {sc}"
            items = database.get_gallery(per_page=100)["items"]
            names = {it["detection_id"]: it.get("common_name") for it in items}
            if names.get(det) != "Ringtail":
                return False, f"gallery shows {names.get(det)!r} for the corrected detection"
            detail = database.get_video_by_id(ids["vid3"])
            shown = {d["id"]: d["common_name"] for d in detail["detections"]}
            if shown.get(det) != "Ringtail":
                return False, f"video detail shows {shown.get(det)!r}"

            # Video-player path: the dog on vid4.
            dog_det = ids["g1"]
            dog_before = _species_row(path, dog_det)
            count = database.save_video_correction(
                ids["vid4"], p15.LBL_DOG, "corr_dog", "Coyote Hybrid", "Canis hybridus"
            )
            if count != 1:
                return False, f"save_video_correction fanned out to {count}"
            if _species_row(path, dog_det) != dog_before:
                return False, "save_video_correction changed the species row"
            sc2 = _correction_row(path, dog_det)
            if not sc2 or sc2[0] != "Coyote Hybrid" or sc2[2] != "video_player":
                return False, f"species_corrections row after save_video_correction: {sc2}"
            detail = database.get_video_by_id(ids["vid4"])
            shown = {d["id"]: d["common_name"] for d in detail["detections"]}
            if shown.get(dog_det) != "Coyote Hybrid":
                return False, f"video detail shows {shown.get(dog_det)!r} after the video-player save"

            # delete_correction removes the row.
            database.set_db_path(path)
            with database.get_conn() as conn:
                cid = conn.execute(
                    "SELECT id FROM species_corrections WHERE detection_id=?", (det,)
                ).fetchone()[0]
            if database.delete_correction(cid) != 1 or _correction_row(path, det) is not None:
                return False, "delete_correction did not remove the row"

            after_snap = _schema_snapshot(path)
            if after_snap["table_rows"] != before_snap["table_rows"]:
                return False, "the old table's row count changed"
            if after_snap["frozen_values"] != before_snap["frozen_values"]:
                return False, "the count of frozen species values changed"
            if name == "legacy" and _frozen_rows(path) != frozen_before:
                return False, "a frozen legacy value changed"
            return True, ""

        with _fixture() as shapes:
            return _each_shape(shapes, check)

    passed += _case("DC4", dc4)

    # DC5 - the reprocess write helper.
    def dc5():
        def check(name, path, ids):
            frozen_before = _frozen_rows(path) if name == "legacy" else []
            other_before = _correction_row(path, ids["rp_det2"])
            if _correction_row(path, ids["rp_det1"]) is None or other_before is None:
                return False, "fixture lost a reprocess-video correction"
            database.set_db_path(path)
            with database.get_conn() as conn:
                database.rewrite_species_for_reprocess(
                    conn, ids["rp_det1"], STUB_LABEL, STUB_COMMON, STUB_SCIENTIFIC,
                    0.95, '[{"label": "x"}]',
                )
            row = _species_row(path, ids["rp_det1"])
            # id, detection_id, label, common_name, scientific_name, confidence, top_candidates_json
            if row[2:7] != (STUB_LABEL, STUB_COMMON, STUB_SCIENTIFIC, 0.95, '[{"label": "x"}]'):
                return False, f"species row after the helper: {row}"
            if _correction_row(path, ids["rp_det1"]) is not None:
                return False, "the detection's species_corrections row survived"
            if _correction_row(path, ids["rp_det2"]) != other_before:
                return False, "another detection's correction was touched"
            if name == "legacy" and _frozen_rows(path) != frozen_before:
                return False, "a frozen legacy value changed"
            return True, ""

        with _fixture() as shapes:
            return _each_shape(shapes, check)

    passed += _case("DC5", dc5)

    # DC6 - a real --reprocess-flagged run on both shapes.
    def dc6():
        def check(name, path, ids):
            frozen_before = _frozen_rows(path) if name == "legacy" else []
            proc = _run_reprocess(path)
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-600:]
                return False, f"processor exited {proc.returncode}: {tail}"
            for key in ("rp_det1", "rp_det2"):
                row = _species_row(path, ids[key])
                if not row or row[2] != STUB_LABEL or row[3] != STUB_COMMON:
                    return False, f"{key} species row after the run: {row}"
                if _correction_row(path, ids[key]) is not None:
                    return False, f"{key} still has a species_corrections row"
            database.set_db_path(path)
            with database.get_conn() as conn:
                flag = conn.execute(
                    "SELECT needs_reprocess FROM videos WHERE id=?", (ids["rp_vid"],)
                ).fetchone()[0]
            if flag != 0:
                return False, f"needs_reprocess is {flag}"
            detail = database.get_video_by_id(ids["rp_vid"])
            shown = sorted(d["common_name"] for d in detail["detections"])
            if shown != [STUB_COMMON, STUB_COMMON]:
                return False, f"video detail shows {shown}"
            if name == "legacy" and _frozen_rows(path) != frozen_before:
                return False, "a frozen legacy value changed"
            return True, ""

        with _fixture() as shapes:
            return _each_shape(shapes, check)

    passed += _case("DC6", dc6)

    return (passed, total)


# -- guard suite -------------------------------------------------------------
#
# A permanent static guard: no non-harness source may name the legacy objects.
# It is driven by the data structures below, so Phase 18 can extend it with one
# line. The patterns are data HERE, which is why scripts/verify_*.py (this file
# included) is excluded from the scan: the harness fixtures legitimately build
# the legacy shape. BUILDLOG.md, CHANGELOG.md, .planning and .gsd are simply not
# matched by SCAN_GLOBS.

# Relative to the repo root.
SCAN_GLOBS = [
    "*.py", "*.sh", "static/*.html", "systemd/*", "README.md",
    "scripts/*.py", "scripts/*.sh", "scripts/*.ps1",
]
SCAN_EXCLUDE_GLOBS = ["scripts/verify_*.py"]

# Whole-file exemptions: path -> reason. Empty in Phase 17. Phase 18's
# standalone production drop script is expected to be added here with one line
# and a reason, because it must name the objects it drops.
GUARD_EXEMPT = {}

# Unambiguous names: forbidden everywhere scanned, comments included (this is
# what forces the stale-comment cleanup).
FORBIDDEN_ANYWHERE = [
    r"\bvideo_corrections\b",
    r"\bMIGRATION_ADD_CORRECTIONS\b",
    r"\bHAS_VIDEO_CORRECTION\b",
    r"\bVIDEO_CORRECTION_(?:COMMON|SCIENTIFIC)\b",
    r"\bDISPLAY_(?:COMMON|SCIENTIFIC)\b",
    r"\bget_video_corrections\b",
    r"\bapply_corrections_to_species\b",
    r"\bidx_corrections_(?:video|label)\b",
]

# Names that are ambiguous on their own (corrected_at lives on
# species_corrections): forbidden only in a SQL-shaped context.
FORBIDDEN_SQLISH = [
    r"\b(?:s|s2|species)\.(?:user_common_name|user_scientific_name|corrected_at)\b",
    r"\b(?:user_common_name|user_scientific_name|corrected_at)\s*=\s*(?i:null)\b",
    r"(?i:add\s+column)\s+(?:user_common_name|user_scientific_name|corrected_at)\b",
    r"NULLIF\(\s*(?:\w+\.)?user_(?:common|scientific)_name",
]

# The live API field name. Allowed only inside the regions below.
BARE_TOKEN = r"\buser_(?:common|scientific)_name\b"

# path -> (start marker, end marker, reason). The region runs from the first
# start marker to the next end marker after it.
BARE_ALLOW = {
    "database.py": (
        "def correct_species(", "\ndef ", "live API field: correct_species parameters",
    ),
    "web_app.py": (
        "class SpeciesCorrectionRequest(", '@app.get("/api/videos")',
        "live API request model and handler",
    ),
    "static/index.html": (
        "async function applyDetectionCorrection(", "\n}", "Gallery correction POST body",
    ),
}

# The four one-shot scripts retired in 17-01 (D-01): they stay absent.
RETIRED_SCRIPTS = [
    "scripts/backfill_dedup_videos.py",
    "scripts/backfill_species_corrections.py",
    "scripts/verify_dedup_backfill.py",
    "scripts/verify_backfill_species_corrections.py",
]

# Core files the guard must be scanning (GD5), so a mistyped glob cannot make it
# pass vacuously.
REQUIRED_SCANNED = [
    "database.py", "web_app.py", "wildlife_processor.py", "notifications.py",
    "image_quality.py", "static/index.html", "README.md", "nas_sync.sh",
    "nas_connect.sh", "setup.sh",
    "systemd/wildlife-analysis.service", "systemd/wildlife-analysis.timer",
    "systemd/wildlife-monitor.service",
    "scripts/migrate_stale_paths.py", "scripts/audit_species_buckets.py",
]


def _scan_files():
    """Sorted repo-relative posix paths the guard covers."""
    root = _repo_root()
    found = set()
    for pattern in SCAN_GLOBS:
        for p in root.glob(pattern):
            if p.is_file():
                found.add(p.relative_to(root).as_posix())
    found = {
        f for f in found
        if not any(fnmatch.fnmatch(f, ex) for ex in SCAN_EXCLUDE_GLOBS)
        and f not in GUARD_EXEMPT
    }
    return sorted(found)


def _read_scanned(relpath):
    """LF-normalised text; undecodable bytes are replaced rather than raised, so
    one odd file cannot abort the whole scan. Comments are NOT stripped."""
    raw = (_repo_root() / relpath).read_bytes().decode("utf-8", errors="replace")
    return raw.replace("\r\n", "\n")


def _allowed_region(relpath, text):
    """(start, end) character offsets of the file's BARE_ALLOW region, or None
    when the file has no entry or its start marker is absent."""
    entry = BARE_ALLOW.get(relpath)
    if not entry:
        return None
    start_marker, end_marker, _reason = entry
    start = text.find(start_marker)
    if start == -1:
        return None
    end = text.find(end_marker, start + len(start_marker))
    return (start, len(text) if end == -1 else end)


def _scan_text(relpath, text):
    """Violations in `text` as (relpath, line number, pattern, line text). The
    text is scanned raw: the guard deliberately covers stale comments."""
    out = []

    def _add(match, pattern):
        line_no = text.count("\n", 0, match.start()) + 1
        line = text.splitlines()[line_no - 1] if text else ""
        out.append((relpath, line_no, pattern, line.strip()))

    for pattern in FORBIDDEN_ANYWHERE + FORBIDDEN_SQLISH:
        for m in re.finditer(pattern, text):
            _add(m, pattern)
    region = _allowed_region(relpath, text)
    for m in re.finditer(BARE_TOKEN, text):
        if region and region[0] <= m.start() < region[1]:
            continue
        _add(m, BARE_TOKEN)
    return out


def _scan_repo():
    """All violations across the scanned set, plus the {path: text} read."""
    texts = {f: _read_scanned(f) for f in _scan_files()}
    violations = []
    for rel, text in texts.items():
        violations += _scan_text(rel, text)
    return violations, texts


def _show(violations):
    """Print one `path:line: pattern` per violation and return a short detail."""
    for rel, line_no, pattern, line in violations:
        print(f"  {rel}:{line_no}: {pattern}   {line[:100]}")
    return f"{len(violations)} violation(s), listed above"


def suite_guard():
    """`guard` suite cases GD1-GD6 (6 total)."""
    total = 6
    passed = 0

    # GD1 - negative control: the scanner can fail, and does not over-fire.
    def gd1():
        always = [
            "FROM video_corrections vc", "MIGRATION_ADD_CORRECTIONS", "HAS_VIDEO_CORRECTION",
            "VIDEO_CORRECTION_COMMON", "VIDEO_CORRECTION_SCIENTIFIC", "DISPLAY_COMMON",
            "DISPLAY_SCIENTIFIC", "get_video_corrections(", "apply_corrections_to_species(",
            "idx_corrections_video", "idx_corrections_label",
        ]
        sqlish = [
            "s.user_common_name", "s2.user_scientific_name", "species.corrected_at",
            "user_common_name = NULL", "corrected_at=null", "ADD COLUMN user_common_name",
            "add  column corrected_at", "NULLIF(s.user_common_name,'')",
            "NULLIF( user_scientific_name,'')",
        ]
        missed = []
        for sample in always + sqlish:
            if not _scan_text("synthetic.py", f"x = 1\n{sample}\n"):
                missed.append(sample)
        if not _scan_text("synthetic.py", "payload = user_common_name\n"):
            missed.append("bare token outside any region")
        if not _scan_text("database.py", "def other():\n    return user_common_name\n"):
            missed.append("bare token in database.py outside correct_species")
        if not _scan_text(
            "database.py", "def correct_species(\n    user_common_name: str,\n)\ndef other():\n"
            "    return user_scientific_name\n"
        ):
            missed.append("bare token after the correct_species region")
        if missed:
            return False, f"the scanner missed: {missed}"

        # The allowlisted regions and the live look-alikes stay clean.
        inside = (
            "def correct_species(\n    user_common_name: str,\n    user_scientific_name: str,\n)\n"
            "def other():\n    pass\n"
        )
        web = (
            "class SpeciesCorrectionRequest(BaseModel):\n    user_common_name: str\n"
            "def f(body):\n    return body.user_common_name\n"
            '@app.get("/api/videos")\n'
        )
        clean = (
            "SELECT species_corrections.corrected_at, sc.corrected_at FROM species_corrections sc\n"
            "ON CONFLICT(detection_id) DO UPDATE SET corrected_at=excluded.corrected_at\n"
            "ORDER BY sc.corrected_at DESC\n"
            "k = KEY_DISPLAY_CTE + NATIVE_DISPLAY_CTE\n"
            "CREATE INDEX idx_species_corrections_label ON species_corrections(corrected_label)\n"
            "display_common = save_video_correction()\n"
        )
        noisy = (
            _scan_text("database.py", inside)
            + _scan_text("web_app.py", web)
            + _scan_text("synthetic.py", clean)
        )
        if noisy:
            return False, f"look-alikes were flagged: {noisy}"
        return True, ""

    passed += _case("GD1", gd1)

    violations, texts = _scan_repo()
    forbidden = set(FORBIDDEN_ANYWHERE)
    sqlish = set(FORBIDDEN_SQLISH)

    # GD2 - no always-forbidden token anywhere, comments included.
    def gd2():
        hits = [v for v in violations if v[2] in forbidden]
        return (not hits), (_show(hits) if hits else "")

    passed += _case("GD2", gd2)

    # GD3 - no SQL-shaped legacy pattern.
    def gd3():
        hits = [v for v in violations if v[2] in sqlish]
        return (not hits), (_show(hits) if hits else "")

    passed += _case("GD3", gd3)

    # GD4 - bare tokens only inside the allowlisted regions, and no stale entry.
    def gd4():
        problems = []
        hits = [v for v in violations if v[2] == BARE_TOKEN]
        for rel, (start_marker, _end, _reason) in BARE_ALLOW.items():
            text = texts.get(rel)
            if text is None:
                problems.append(f"{rel}: allowlisted but not scanned")
                continue
            region = _allowed_region(rel, text)
            if region is None:
                problems.append(f"{rel}: start marker {start_marker!r} not found (stale allowlist)")
            elif not re.search(BARE_TOKEN, text[region[0]:region[1]]):
                problems.append(f"{rel}: region holds no live-field token (stale allowlist)")
        if hits:
            problems.append(_show(hits))
        return (not problems), "; ".join(problems)

    passed += _case("GD4", gd4)

    # GD5 - coverage: the core files are scanned, no harness is, none was empty.
    def gd5():
        scanned = set(texts)
        missing = [f for f in REQUIRED_SCANNED if f not in scanned]
        harnesses = [f for f in scanned if fnmatch.fnmatch(f, "scripts/verify_*.py")]
        empty = [f for f, t in texts.items() if not t.strip()]
        ok = not (missing or harnesses or empty)
        return ok, f"not scanned: {missing}; harness files scanned: {harnesses}; empty reads: {empty}"

    passed += _case("GD5", gd5)

    # GD6 - the four retired scripts stay absent and unreferenced.
    def gd6():
        root = _repo_root()
        back = [r for r in RETIRED_SCRIPTS if (root / r).exists()]
        stems = [Path(r).stem for r in RETIRED_SCRIPTS]
        others = [
            p.relative_to(root).as_posix() for p in (root / "scripts").glob("verify_*.py")
            if p.name != "verify_phase17.py"
        ]
        refs = []
        for rel in sorted(set(texts) | set(others)):
            text = texts.get(rel) or _read_scanned(rel)
            for stem in stems:
                if stem in text:
                    refs.append(f"{rel} -> {stem}")
        ok = not (back or refs)
        return ok, f"retired scripts present: {back}; references: {refs}"

    passed += _case("GD6", gd6)

    return (passed, total)


# -- web layer helpers (endpoints suite) ---------------------------------------
#
# Every GET route of web_app.app is mapped below to a URL template. EP1 compares
# the set of declared GET routes with these maps, so a route added to web_app.py
# later fails the harness until somebody maps it here (or skips it with a reason).

# route.path -> reason it is not requested.
SKIP_ROUTES = {"/api/updates": "contacts PyPI over the network"}

# Exact route.path string -> URL template. Placeholders: {paired_video_id},
# {unpaired_video_id}, {playable_video_id}, {purged_video_id}, {species_key},
# {run_id}, {crop_name}, {thumb_name}, {search_q}. Values that go into a path or
# query are percent-quoted before they are substituted.
GET_URLS = {
    "/openapi.json": "/openapi.json",
    "/": "/",
    "/media/crops/{filename}": "/media/crops/{crop_name}",
    "/media/thumbnails/{filename}": "/media/thumbnails/{thumb_name}",
    "/media/video/{video_id}": "/media/video/{playable_video_id}",
    "/api/stats": "/api/stats",
    "/api/species": "/api/species",
    "/api/species/search": "/api/species/search?q=fox",
    "/api/species/{label:path}": "/api/species/{species_key}",
    "/api/gallery": "/api/gallery",
    "/api/cameras": "/api/cameras",
    "/api/videos": "/api/videos",
    "/api/videos/{video_id}": "/api/videos/{paired_video_id}",
    "/api/timeline": "/api/timeline",
    "/api/blanks": "/api/blanks",
    "/api/system": "/api/system",
    "/api/blacklist": "/api/blacklist",
    "/api/corrections": "/api/corrections",
    "/api/maintenance/reprocess_queue": "/api/maintenance/reprocess_queue",
    "/api/search": "/api/search?q={search_q}",
    "/api/runs": "/api/runs",
    "/api/runs/last": "/api/runs/last",
    "/api/runs/{run_id}": "/api/runs/{run_id}",
    "/api/settings": "/api/settings",
    "/api/schedule/next-run": "/api/schedule/next-run",
    "/api/run/status": "/api/run/status",
    "/api/maintenance/storage": "/api/maintenance/storage",
}

# Query variants that reach different SQL, or different branches of one route.
EXTRA_GET_URLS = [
    "/api/videos?has_species=true",
    "/api/videos?has_species=false",
    "/api/videos?search={search_q}",
    "/api/videos?species={species_key}",
    "/api/gallery?species={species_key}",
    "/api/corrections?video_id={paired_video_id}",
    "/api/videos/{unpaired_video_id}",
    "/media/video/{purged_video_id}",
]

# The DB-backed URLs compared between the two shapes. Left out: the schema and
# index page (not DB-backed), every /media/ URL (file serving), and the routes
# that read the host rather than the database (/api/system, /api/run/status,
# /api/schedule/next-run, /api/settings). If a compared response ever carries an
# inherently shape-dependent field, exclude that field by name with a comment
# giving the reason; never exclude a whole route.
_NOT_COMPARED_PREFIXES = (
    "/openapi.json", "/media/", "/api/system", "/api/run/status",
    "/api/schedule/next-run", "/api/settings",
)
COMPARE_URLS = [
    t for t in list(GET_URLS.values()) + EXTRA_GET_URLS
    if t != "/" and not t.startswith(_NOT_COMPARED_PREFIXES)
]

# Bytes written at the playable video's filepath.
TINY_VIDEO_BYTES = b"phase17-tiny-video"


def _import_web():
    """(web_app, TestClient). Raises ImportError with a one-line reason when
    fastapi, httpx or web_app's own imports are unavailable."""
    try:
        import web_app
        from fastapi.testclient import TestClient
    except Exception as exc:  # noqa: BLE001 - any import-time failure is a skip reason
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())
        raise ImportError(reason) from exc
    return web_app, TestClient


@contextlib.contextmanager
def _web_context(web_app, db_path, data_dir):
    """Point web_app and database at a fixture database and a temporary data
    directory, and restore all three afterwards. The harness never runs against
    ./data (T-17-16)."""
    saved_db = database.get_db_path()
    saved_data = web_app.DATA_DIR
    saved_settings = web_app.SETTINGS_FILE
    database.set_db_path(str(db_path))
    web_app.DATA_DIR = str(data_dir)
    web_app.SETTINGS_FILE = str(Path(data_dir) / "settings.json")
    try:
        yield
    finally:
        database.set_db_path(saved_db)
        web_app.DATA_DIR = saved_data
        web_app.SETTINGS_FILE = saved_settings


def _all_templates():
    return list(GET_URLS.values()) + list(EXTRA_GET_URLS)


def _get_all(client, params, timings=None):
    """Request every GET_URLS and EXTRA_GET_URLS entry with the placeholders in
    `params` filled in. Returns {url: (status, parsed JSON or text)}. When
    `timings` is a dict, the seconds each request took are recorded in it."""
    out = {}
    for template in _all_templates():
        url = template.format(**params)
        started = time.monotonic()
        resp = client.get(url)
        if timings is not None:
            timings[url] = time.monotonic() - started
        body = None
        if "json" in resp.headers.get("content-type", ""):
            try:
                body = resp.json()
            except ValueError:
                body = None
        if body is None:
            body = resp.text
        out[url] = (resp.status_code, body)
    return out


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


@contextlib.contextmanager
def _web_fixture():
    """Fresh shapes plus everything the endpoint cases need: a tiny file at the
    lens-0 paired video's filepath, one purged video (filepath NULL), one crop
    and one thumbnail in a temporary data directory, and one runs row with fixed
    values. Yields {"shapes", "ids", "params", "data_dir"}."""
    original = database.get_db_path()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        shapes = _build_shapes(tmp.name)
        ids = shapes["ids"]
        data_dir = Path(tmp.name) / "data"
        (data_dir / "crops").mkdir(parents=True)
        (data_dir / "thumbnails").mkdir(parents=True)
        (data_dir / "crops" / "ep_crop.jpg").write_bytes(b"crop")
        (data_dir / "thumbnails" / "ep_thumb.jpg").write_bytes(b"thumb")

        run_id = None
        for name in SHAPES:
            database.set_db_path(shapes[name])
            with database.get_conn() as conn:
                playable_path = conn.execute(
                    "SELECT filepath FROM videos WHERE id=?", (ids["pair0"],)
                ).fetchone()[0]
                conn.execute("UPDATE videos SET filepath=NULL WHERE id=?", (ids["vid2"],))
                cur = conn.execute(
                    """INSERT INTO runs (start_time, end_time, status, "trigger",
                           videos_processed, detections_found)
                       VALUES ('2026-10-01T06:00:00', '2026-10-01T06:10:00', 'success',
                               'scheduled', 3, 5)"""
                )
                run_id = cur.lastrowid
        Path(playable_path).write_bytes(TINY_VIDEO_BYTES)

        params = {
            "paired_video_id": ids["pair0"],
            "unpaired_video_id": ids["vid1"],
            "playable_video_id": ids["pair0"],
            "purged_video_id": ids["vid2"],
            "species_key": _quote(p15.KEY_CAT),
            "run_id": run_id,
            "crop_name": "ep_crop.jpg",
            "thumb_name": "ep_thumb.jpg",
            "search_q": _quote("raccoon"),
        }
        yield {"shapes": shapes, "ids": ids, "params": params, "data_dir": data_dir}
    finally:
        database.set_db_path(original)
        tmp.cleanup()


def _collect(web_app, TestClient, fx, name):
    """Every GET response on one shape, plus the playable video's bytes."""
    with _web_context(web_app, fx["shapes"][name], fx["data_dir"]):
        client = TestClient(web_app.app, raise_server_exceptions=False)
        got = _get_all(client, fx["params"])
        media = client.get("/media/video/{playable_video_id}".format(**fx["params"]))
        return got, media.content


def _ep_problems(got, media_bytes, params):
    """Assertions shared by EP2 and EP3 for one shape's responses."""
    problems = []
    bad = {u: s for u, (s, _b) in got.items() if s >= 500}
    if bad:
        problems.append(f"5xx responses: {bad}")
    detail_url = "/api/videos/{paired_video_id}".format(**params)
    status, body = got[detail_url]
    if status != 200:
        problems.append(f"{detail_url} -> {status}")
    elif not isinstance(body, dict) or set(body) != {"video", "detections", "paired", "pair_detections"}:
        problems.append(f"{detail_url} keys {sorted(body) if isinstance(body, dict) else type(body)}")
    elif not body["pair_detections"]:
        problems.append(f"{detail_url} pair_detections is empty")
    playable = "/media/video/{playable_video_id}".format(**params)
    if got[playable][0] != 200 or media_bytes != TINY_VIDEO_BYTES:
        problems.append(f"{playable} -> {got[playable][0]}, {len(media_bytes)} bytes")
    purged = "/media/video/{purged_video_id}".format(**params)
    if got[purged][0] != 404:
        problems.append(f"{purged} -> {got[purged][0]}, want 404")
    return problems


# -- endpoints suite -----------------------------------------------------------

def suite_endpoints():
    """`endpoints` suite cases EP1-EP6 (6 total). Returns (passed, total), or
    (None, reason) when fastapi/httpx are unavailable (a skip, decided by main)."""
    total = 6
    passed = 0
    try:
        web_app, TestClient = _import_web()
    except ImportError as exc:
        return (None, str(exc))

    # EP1 - every GET route is mapped, and nothing mapped is stale.
    def ep1():
        declared = {
            r.path for r in web_app.app.routes
            if "GET" in (getattr(r, "methods", None) or ())
        }
        mapped = set(GET_URLS) | set(SKIP_ROUTES)
        problems = []
        if declared - mapped:
            problems.append(f"unmapped GET routes: {sorted(declared - mapped)}")
        if mapped - declared:
            problems.append(f"stale map entries: {sorted(mapped - declared)}")
        overlap = set(GET_URLS) & set(SKIP_ROUTES)
        if overlap:
            problems.append(f"both requested and skipped: {sorted(overlap)}")
        return not problems, "; ".join(problems)

    passed += _case("EP1", ep1)

    def _shape_case(name):
        def run():
            with _web_fixture() as fx:
                got, media = _collect(web_app, TestClient, fx, name)
                problems = _ep_problems(got, media, fx["params"])
                return not problems, "; ".join(problems)
        return run

    # EP2 / EP3 - every GET route on each shape.
    passed += _case("EP2", _shape_case("lacking"))
    passed += _case("EP3", _shape_case("legacy"))

    # EP4 - the DB-backed responses are identical between the shapes, and no
    # frozen legacy value appears anywhere.
    def ep4():
        with _web_fixture() as fx:
            a, _ = _collect(web_app, TestClient, fx, "lacking")
            b, _ = _collect(web_app, TestClient, fx, "legacy")
            problems = []
            for template in COMPARE_URLS:
                url = template.format(**fx["params"])
                if a[url] != b[url]:
                    problems.append(f"{url} differs between the shapes")
            for label, got in (("lacking", a), ("legacy", b)):
                for url, (_s, body) in got.items():
                    text = body if isinstance(body, str) else _dump(body)
                    if LEGACY_SEED_NAME in text or LEGACY_VIDEO_NAME in text:
                        problems.append(f"[{label}] {url} carries a frozen legacy value")
            return not problems, "; ".join(problems)

    passed += _case("EP4", ep4)

    # EP5 - the live API field user_common_name, end to end, on both shapes.
    def ep5():
        with _web_fixture() as fx:
            det = fx["ids"]["r1"]
            for name in SHAPES:
                path = fx["shapes"][name]
                with _web_context(web_app, path, fx["data_dir"]):
                    client = TestClient(web_app.app, raise_server_exceptions=False)
                    before = _species_row(path, det)
                    resp = client.post("/api/species/correct", json={
                        "detection_id": det, "user_common_name": "Bobcat",
                        "user_scientific_name": "Lynx rufus",
                    })
                    if resp.status_code != 200 or resp.json() != {"ok": True}:
                        return False, f"[{name}] POST -> {resp.status_code} {resp.text[:120]}"
                    sc = _correction_row(path, det)
                    if not sc or sc[0] != "Bobcat" or sc[2] != "gallery":
                        return False, f"[{name}] species_corrections row {sc}"
                    items = client.get("/api/gallery?per_page=100").json()["items"]
                    item = next((i for i in items if i["detection_id"] == det), None)
                    if not item or item["common_name"] != "Bobcat" or item["has_correction"] != 1:
                        return False, f"[{name}] gallery item {item and (item['common_name'], item['has_correction'])}"
                    if _species_row(path, det) != before:
                        return False, f"[{name}] the species row changed"
                    unknown = client.post("/api/species/correct", json={
                        "detection_id": 99999999, "user_common_name": "Bobcat",
                        "user_scientific_name": "Lynx rufus",
                    })
                    if unknown.status_code != 404:
                        return False, f"[{name}] unknown detection -> {unknown.status_code}"
                    ctrl = client.post("/api/species/correct", json={
                        "detection_id": det, "user_common_name": "Bo\x07b",
                        "user_scientific_name": "Lynx rufus",
                    })
                    if ctrl.status_code != 422:
                        return False, f"[{name}] control character -> {ctrl.status_code}"
            return True, ""

    passed += _case("EP5", ep5)

    # EP6 - the video-player endpoints, on both shapes.
    def ep6():
        with _web_fixture() as fx:
            ids = fx["ids"]
            for name in SHAPES:
                path = fx["shapes"][name]
                with _web_context(web_app, path, fx["data_dir"]):
                    client = TestClient(web_app.app, raise_server_exceptions=False)
                    database.set_db_path(path)
                    with database.get_conn() as conn:
                        raw = conn.execute(
                            "SELECT label FROM species WHERE detection_id=?", (ids["pair_det0"],)
                        ).fetchone()[0]
                    resp = client.post("/api/corrections", json={
                        "video_id": ids["pair0"], "original_label": raw,
                        "corrected_label": "corr_label", "corrected_common": "Red Fox",
                        "corrected_scientific": "Vulpes vulpes", "note": "",
                    })
                    if resp.status_code != 200 or resp.json().get("detections", 0) < 1:
                        return False, f"[{name}] POST /api/corrections -> {resp.status_code} {resp.text[:120]}"
                    listed = client.get(f"/api/corrections?video_id={ids['pair0']}").json()
                    rows = [r for r in listed if r["detection_id"] == ids["pair_det0"]]
                    if not rows:
                        return False, f"[{name}] GET /api/corrections lists {[r['detection_id'] for r in listed]}"
                    detail = client.get(f"/api/videos/{ids['pair0']}").json()
                    if "corrections" in detail:
                        return False, f"[{name}] video detail still has a corrections key"
                    mine = [d for d in detail["detections"] if d["id"] == ids["pair_det0"]]
                    if not mine or mine[0]["corrected"] != 1:
                        return False, f"[{name}] video detail does not reflect the correction: {mine}"
                    cid = rows[0]["id"]
                    first = client.delete(f"/api/corrections/{cid}")
                    second = client.delete(f"/api/corrections/{cid}")
                    if first.status_code != 200 or second.status_code != 404:
                        return False, f"[{name}] DELETE -> {first.status_code}, then {second.status_code}"
            return True, ""

    passed += _case("EP6", ep6)

    return (passed, total)


# -- bytecopy suite ------------------------------------------------------------
#
# A rehearsal on a real database. The source is opened read-only (mode=ro) and
# copied with Connection.backup() into a TemporaryDirectory; everything else
# (init_db, the DROP statements, the endpoint requests, the write paths) runs on
# the copies through database.set_db_path() and get_conn(). The source is never
# written (D-03), and BC5 proves its schema and row counts are unchanged.

def _src_state(src):
    """sqlite_master rows and COUNT(*) of every table, through `src`."""
    master = sorted(
        tuple(r) for r in src.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")
    )
    counts = {}
    names = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for (name,) in names:
        counts[name] = src.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0]
    return {"master": master, "counts": counts}


def _backup_copy(src, dest_path):
    """Connection.backup() of `src` into a new file. Returns the seconds taken."""
    started = time.monotonic()
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
    return time.monotonic() - started


def _pick_params():
    """Placeholder values chosen by SQL on the database the caller has already
    pointed database.py at. The species and search values come from the API."""
    with database.get_conn() as conn:
        def one(sql):
            row = conn.execute(sql).fetchone()
            return row[0] if row else None

        paired = one(
            "SELECT v.id FROM videos v WHERE v.paired_video_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM detections d WHERE d.video_id = v.id) "
            "ORDER BY v.id DESC LIMIT 1"
        ) or one("SELECT id FROM videos WHERE paired_video_id IS NOT NULL ORDER BY id DESC LIMIT 1")
        unpaired = one("SELECT id FROM videos WHERE paired_video_id IS NULL ORDER BY id DESC LIMIT 1")
        purged = one("SELECT id FROM videos WHERE filepath IS NULL ORDER BY id DESC LIMIT 1")
        run_id = one("SELECT MAX(id) FROM runs") or 1
        crop = one("SELECT crop_path FROM crops ORDER BY id DESC LIMIT 1")
        thumb = one(
            "SELECT thumbnail_path FROM videos WHERE thumbnail_path IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        )
    missing = [n for n, v in (("paired", paired), ("unpaired", unpaired), ("purged", purged)) if v is None]
    if missing:
        raise AssertionError(f"the database has no {missing} video to drive the endpoints with")
    return {
        "paired_video_id": paired,
        "unpaired_video_id": unpaired,
        # Never request /media/video for a video with a real filepath: TestClient
        # would read the whole NAS file into memory. A purged id still drives
        # get_video_by_id and must answer 404.
        "playable_video_id": purged,
        "purged_video_id": purged,
        "run_id": run_id,
        "crop_name": _quote(Path(crop).name if crop else "none.jpg"),
        "thumb_name": _quote(Path(thumb).name if thumb else "none.jpg"),
    }


def _species_fields(path, det_id):
    """The columns a reprocess rewrites, by name (production's column order
    differs from a fresh database's)."""
    database.set_db_path(path)
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT label, common_name, scientific_name, confidence, top_candidates_json "
            "FROM species WHERE detection_id=?", (det_id,)
        ).fetchone()
        return tuple(row) if row else None


def suite_bytecopy(db_path):
    """`bytecopy` suite cases BC1-BC5 (5 total) against the database at
    `db_path`, opened read-only. Explicit only: needs --db."""
    total = 5
    passed = 0
    src_file = Path(db_path).resolve()
    original = database.get_db_path()
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    st = {}

    # The one raw connection in this suite. get_conn() opens read-write, so it
    # cannot open the production file read-only; the file is therefore opened
    # with a mode=ro URI here and nowhere else.
    src = sqlite3.connect(src_file.as_uri() + "?mode=ro", uri=True)

    def need(key):
        if key not in st:
            raise RuntimeError(f"copy {key.upper()} is unavailable (an earlier case failed)")
        return st[key]

    try:
        start = _src_state(src)
        print(f"INFO: python {sys.version.split()[0]}")
        print(f"INFO: sqlite {sqlite3.sqlite_version}")
        print(f"INFO: source_bytes {src_file.stat().st_size}")

        # BC1 - production's shape: init_db() is inert.
        def bc1():
            cols = [r[1] for r in src.execute("PRAGMA table_info(species)")]
            names = {r[0] for r in src.execute("SELECT name FROM sqlite_master")}
            present = [c for c in LEGACY_COLUMNS if c in cols]
            present += [n for n in [LEGACY_TABLE] + LEGACY_INDEXES if n in names]
            if len(present) != len(LEGACY_COLUMNS) + 1 + len(LEGACY_INDEXES):
                return False, (
                    "source already lacks the legacy objects; this rehearsal expects the "
                    f"pre-Phase-18 production shape (present: {sorted(present)})"
                )
            path_a = Path(tmp.name) / "copy_a.db"
            secs = _backup_copy(src, path_a)
            st["a"] = str(path_a)
            print(f"INFO: backup_seconds_a {secs:.2f}")
            before = _schema_snapshot(st["a"])
            database.init_db(st["a"])
            database.init_db(st["a"])
            after = _schema_snapshot(st["a"])
            database.set_db_path(st["a"])
            with database.get_conn() as conn:
                nonblank = conn.execute(
                    "SELECT COUNT(*) FROM species WHERE NULLIF(user_common_name,'') IS NOT NULL"
                ).fetchone()[0]
            print(f"INFO: legacy_table_rows {before['table_rows']}")
            print(f"INFO: species_rows_with_frozen_value {before['frozen_values']}")
            print(f"INFO: species_rows_with_nonblank_frozen_name {nonblank}")
            print(f"INFO: species_corrections_rows {start['counts'].get('species_corrections')}")
            if before == after:
                return True, ""
            added = sorted(set(map(str, after["master"])) - set(map(str, before["master"])))
            dropped = sorted(set(map(str, before["master"])) - set(map(str, after["master"])))
            return False, (
                f"init_db() changed the production-shaped copy: columns {before['columns']} -> "
                f"{after['columns']}; added {added}; dropped {dropped}; "
                f"rows {before['table_rows']} -> {after['table_rows']}"
            )

        passed += _case("BC1", bc1)

        # BC2 - a copy with the legacy columns and table dropped.
        def bc2():
            if sqlite3.sqlite_version_info < (3, 35, 0):
                return False, f"SQLite {sqlite3.sqlite_version} is older than 3.35: no DROP COLUMN"
            path_b = Path(tmp.name) / "copy_b.db"
            secs = _backup_copy(src, path_b)
            st["b"] = str(path_b)
            print(f"INFO: backup_seconds_b {secs:.2f}")
            database.set_db_path(st["b"])
            started = time.monotonic()
            with database.get_conn() as conn:
                for column in LEGACY_COLUMNS:
                    conn.execute(f"ALTER TABLE species DROP COLUMN {column}")
                conn.execute(f"DROP TABLE {LEGACY_TABLE}")
            print(f"INFO: drop_seconds {time.monotonic() - started:.2f}")
            with database.get_conn() as conn:
                integrity = [tuple(r) for r in conn.execute("PRAGMA integrity_check").fetchall()]
                fk = [tuple(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]
            problems = []
            if integrity != [("ok",)]:
                problems.append(f"integrity_check {integrity[:3]}")
            if fk:
                problems.append(f"foreign_key_check returned {len(fk)} row(s), first {fk[:3]}")
            database.init_db(st["b"])
            database.init_db(st["b"])
            present = _legacy_objects(st["b"])
            cols = _schema_snapshot(st["b"])["columns"]
            if present:
                problems.append(f"init_db() re-added {present}")
            if set(cols) != set(SPECIES_COLUMNS):
                problems.append(f"species columns {cols}")
            return not problems, "; ".join(problems)

        passed += _case("BC2", bc2)

        # BC3 - every GET route on both copies, before any write.
        def bc3():
            web_app, TestClient = _import_web()
            data_dir = Path(tmp.name) / "data"
            data_dir.mkdir(exist_ok=True)
            got, params, slowest = {}, {}, ("", 0.0)
            for key in ("a", "b"):
                with _web_context(web_app, need(key), data_dir):
                    p = _pick_params()
                    client = TestClient(web_app.app, raise_server_exceptions=False)
                    species = client.get("/api/species").json()
                    if not species:
                        return False, f"[{key}] /api/species is empty"
                    p["species_key"] = _quote(species[0]["label"])
                    p["search_q"] = _quote(species[0].get("common_name") or species[0]["label"])
                    timings = {}
                    got[key] = _get_all(client, p, timings)
                    params[key] = p
                    worst = max(timings.items(), key=lambda kv: kv[1])
                    if worst[1] > slowest[1]:
                        slowest = worst
            st["params"] = params["a"]
            print(f"INFO: slowest_get_seconds {slowest[1]:.2f} {slowest[0]}")
            problems = []
            if params["a"] != {**params["b"]}:
                problems.append("the two copies picked different placeholder values")
            for key in ("a", "b"):
                bad = {u: s for u, (s, _b) in got[key].items() if s >= 500}
                if bad:
                    problems.append(f"[{key}] 5xx responses: {bad}")
                detail = got[key]["/api/videos/{paired_video_id}".format(**params[key])]
                if detail[0] != 200 or not isinstance(detail[1], dict):
                    problems.append(f"[{key}] video detail -> {detail[0]}")
                elif set(detail[1]) != {"video", "detections", "paired", "pair_detections"}:
                    problems.append(f"[{key}] video detail keys {sorted(detail[1])}")
                purged = got[key]["/media/video/{purged_video_id}".format(**params[key])]
                if purged[0] != 404 or "purged" not in _dump(purged[1]):
                    problems.append(f"[{key}] purged video -> {purged[0]} {str(purged[1])[:80]}")
            for template in COMPARE_URLS:
                url = template.format(**params["a"])
                if got["a"][url] != got["b"][url]:
                    problems.append(f"{url} differs between the undropped and the dropped copy")
            return not problems, "; ".join(problems)

        passed += _case("BC3", bc3)

        # BC4 - the read and write exerciser on both copies (copies only).
        def bc4():
            params = st.get("params")
            if not params:
                return False, "BC3 did not run far enough to choose real ids"
            rids = {
                "pair0": params["paired_video_id"],
                "rp_vid": params["paired_video_id"],
                "vid1": params["unpaired_video_id"],
            }
            database.set_db_path(need("a"))
            with database.get_conn() as conn:
                pick = conn.execute(
                    "SELECT d.id, d.video_id, s.label FROM detections d "
                    "JOIN species s ON s.detection_id = d.id "
                    "WHERE NOT EXISTS (SELECT 1 FROM species_corrections sc WHERE sc.detection_id = d.id) "
                    "ORDER BY d.id DESC LIMIT 1"
                ).fetchone()
            if not pick:
                return False, "no uncorrected detection to write against"
            det, video_id, label = pick[0], pick[1], pick[2]
            reader_secs = {}
            snaps = {}
            for key in ("a", "b"):
                path = need(key)
                timings = {}
                snaps[key] = {"before": _reader_outputs(path, rids, timings)}
                reader_secs.update({f"{key}:{n}": s for n, s in timings.items()})
                if key == "a":
                    legacy_before = (_schema_snapshot(path), _frozen_rows(path))

                species_before = _species_row(path, det)
                database.set_db_path(path)
                if database.correct_species(det, "Bobcat", "Lynx rufus") != 1:
                    return False, f"[{key}] correct_species did not return 1"
                if _species_row(path, det) != species_before:
                    return False, f"[{key}] correct_species changed the species row"
                sc = _correction_row(path, det)
                if not sc or sc[0] != "Bobcat" or sc[2] != "gallery":
                    return False, f"[{key}] species_corrections after correct_species: {sc}"

                count = database.save_video_correction(video_id, label, "corr_x", "Red Fox", "Vulpes vulpes")
                if not count or count < 1:
                    return False, f"[{key}] save_video_correction fanned out to {count}"
                sc = _correction_row(path, det)
                if not sc or sc[0] != "Red Fox" or sc[2] != "video_player":
                    return False, f"[{key}] species_corrections after save_video_correction: {sc}"
                if _species_row(path, det) != species_before:
                    return False, f"[{key}] save_video_correction changed the species row"

                with database.get_conn() as conn:
                    cid = conn.execute(
                        "SELECT id FROM species_corrections WHERE detection_id=?", (det,)
                    ).fetchone()[0]
                if database.delete_correction(cid) != 1 or _correction_row(path, det) is not None:
                    return False, f"[{key}] delete_correction did not remove the row"

                database.correct_species(det, "Bobcat", "Lynx rufus")
                with database.get_conn() as conn:
                    database.rewrite_species_for_reprocess(
                        conn, det, STUB_LABEL, STUB_COMMON, STUB_SCIENTIFIC, 0.95, '[{"label": "x"}]'
                    )
                fields = _species_fields(path, det)
                if fields != (STUB_LABEL, STUB_COMMON, STUB_SCIENTIFIC, 0.95, '[{"label": "x"}]'):
                    return False, f"[{key}] species row after the reprocess helper: {fields}"
                if _correction_row(path, det) is not None:
                    return False, f"[{key}] the reprocess helper left the species_corrections row"

                snaps[key]["after"] = _reader_outputs(path, rids)
            diff = [k for k in snaps["a"]["before"] if snaps["a"]["before"][k] != snaps["b"]["before"][k]]
            diff += [f"after:{k}" for k in snaps["a"]["after"] if snaps["a"]["after"][k] != snaps["b"]["after"][k]]
            if diff:
                return False, f"readers differ between the undropped and dropped copy: {diff}"
            legacy_after = (_schema_snapshot(st["a"]), _frozen_rows(st["a"]))
            if legacy_after != legacy_before:
                return False, "the decoupled code changed a legacy object on the production-shaped copy"
            if _legacy_objects(st["b"]):
                return False, f"legacy objects appeared on the dropped copy: {_legacy_objects(st['b'])}"
            slowest = max(reader_secs.items(), key=lambda kv: kv[1])
            print(f"INFO: slowest_reader_seconds {slowest[1]:.2f} {slowest[0]}")
            return True, ""

        passed += _case("BC4", bc4)

        # BC5 - the source file is untouched.
        def bc5():
            end = _src_state(src)
            problems = []
            if end["master"] != start["master"]:
                problems.append("sqlite_master differs")
            changed = {t: (start["counts"].get(t), n) for t, n in end["counts"].items()
                       if start["counts"].get(t) != n}
            if changed or set(end["counts"]) != set(start["counts"]):
                problems.append(f"row counts changed: {changed}")
            return not problems, "; ".join(problems)

        passed += _case("BC5", bc5)
    finally:
        src.close()
        database.set_db_path(original)
        tmp.cleanup()

    return (passed, total)


# -- live suite ---------------------------------------------------------------
#
# HTTP GET only, against a running service. It never writes: the write paths are
# proven on fixtures and copies (EP5, EP6, BC4). 17-03 runs it against the
# pre-Phase-17 service, where exactly LV2 fails (the old code still serves the
# corrections key); 17-04 turns it green with the restart.

def _live_head_bytes(base_url, path, n=1024):
    """Ranged GET returning (status, at most n bytes). Never raises: a transport
    failure comes back as (None, reason bytes). For /media/video, so a whole NAS
    file is never downloaded."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(
        base_url.rstrip("/") + path, method="GET", headers={"Range": f"bytes=0-{n - 1}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=p15.LIVE_TIMEOUT_SECS) as resp:
            return resp.status, resp.read(n)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:  # noqa: BLE001 - URLError, timeout, refused, ...
        return None, f"{type(exc).__name__}: {exc}".encode()


def _live_find_videos(base_url):
    """(paired id, unpaired id, playable id, purged id, thumbnail basename) read
    from the API itself; any of them may be None."""
    paired = unpaired = playable = purged = thumb = None
    st, listing, _d = p15._live_json(base_url, "/api/videos?per_page=100")
    items = listing.get("items", []) if isinstance(listing, dict) else []
    for item in items:
        if paired is None and item.get("paired_video_id"):
            paired = item["id"]
        if unpaired is None and not item.get("paired_video_id"):
            unpaired = item["id"]
        if thumb is None and item.get("thumbnail_path"):
            thumb = Path(item["thumbnail_path"]).name
    for item in items[:15]:
        st, detail, _d = p15._live_json(base_url, f"/api/videos/{item['id']}")
        if st == 200 and isinstance(detail, dict) and (detail.get("video") or {}).get("filepath"):
            playable = item["id"]
            break
    # Purged files: blank videos carry filepath in the listing, oldest page first.
    st, blanks, _d = p15._live_json(base_url, "/api/blanks?per_page=100")
    if st == 200 and isinstance(blanks, dict):
        for page in (blanks.get("pages", 1), 1):
            st, body, _d = p15._live_json(base_url, f"/api/blanks?per_page=100&page={page}")
            rows = body.get("videos", []) if isinstance(body, dict) else []
            hit = next((r for r in rows if not r.get("filepath")), None)
            if hit:
                purged = hit["id"]
                break
    return paired, unpaired, playable, purged, thumb


def suite_live(base_url="http://localhost:8080"):
    """`live` suite cases LV1-LV5 (5 total). Explicit only (--base-url)."""
    total = 5
    passed = 0

    st, species, _d = p15._live_json(base_url, "/api/species")
    species = species if st == 200 and isinstance(species, list) and species else []
    paired, unpaired, playable, purged, thumb = _live_find_videos(base_url)
    st, last_run, _d = p15._live_json(base_url, "/api/runs/last")
    first = species[0] if species else {}
    key = _quote(first.get("label", ""))
    params = {
        "paired_video_id": paired, "unpaired_video_id": unpaired,
        "playable_video_id": playable, "purged_video_id": purged,
        "species_key": key,
        "run_id": (last_run or {}).get("id", 1) if isinstance(last_run, dict) else 1,
        # Production file names carry spaces ("Back Wall_00_..."): percent-quote.
        "crop_name": _quote(first.get("best_crop") or "none.jpg"),
        "thumb_name": _quote(thumb or "none.jpg"),
        "search_q": _quote(first.get("common_name") or first.get("label") or "a"),
    }

    # LV1 - every GET route answers below 500.
    def lv1():
        problems = []
        if not species:
            problems.append("/api/species gave no rows to resolve placeholders from")
        for name in ("paired_video_id", "unpaired_video_id", "purged_video_id"):
            if params[name] is None:
                problems.append(f"could not find a value for {{{name}}}")
        if problems:
            return False, "; ".join(problems)
        for template in _all_templates():
            if template.startswith("/media/video/"):
                continue  # LV3 owns these, with a ranged read
            url = template.format(**params)
            status, body = p15._live_get(base_url, url)
            if status is None or status >= 500:
                problems.append(f"{url} -> {status if status is not None else body}")
        try:
            web_app, _client = _import_web()
        except ImportError:
            web_app = None
        if web_app is not None:
            declared = {r.path for r in web_app.app.routes
                        if "GET" in (getattr(r, "methods", None) or ())}
            if declared != set(GET_URLS) | set(SKIP_ROUTES):
                problems.append("GET route coverage differs from GET_URLS / SKIP_ROUTES")
        return not problems, "; ".join(problems)

    passed += _case("LV1", lv1)

    # LV2 - video detail has exactly four keys, and no corrections key.
    def lv2():
        st, detail, why = p15._live_json(base_url, f"/api/videos/{params['paired_video_id']}")
        if st != 200 or not isinstance(detail, dict):
            return False, f"/api/videos/{params['paired_video_id']} -> {why or st}"
        want = {"video", "detections", "paired", "pair_detections"}
        return set(detail) == want and "corrections" not in detail, (
            f"keys {sorted(detail)}; want exactly {sorted(want)}"
        )

    passed += _case("LV2", lv2)

    # LV3 - a playable file streams (ranged), a purged one answers 404.
    def lv3():
        if params["playable_video_id"] is None:
            return False, "no video with a file on disk was found"
        status, body = _live_head_bytes(base_url, f"/media/video/{params['playable_video_id']}")
        if status not in (200, 206):
            return False, f"playable video -> {status} {body[:80]!r}"
        status, _body = p15._live_get(base_url, f"/media/video/{params['purged_video_id']}")
        return status == 404, f"purged video -> {status}, want 404"

    passed += _case("LV3", lv3)

    # LV4 - the index page, the corrections list and a species detail; plus a
    # source assertion that this suite can never send a write.
    def lv4():
        import inspect
        status, page = p15._live_get(base_url, "/")
        if status != 200 or "applyDetectionCorrection" not in page:
            return False, f"GET / -> {status}, or applyDetectionCorrection is missing"
        status, listed, why = p15._live_json(base_url, "/api/corrections")
        if status != 200 or not isinstance(listed, list):
            return False, f"/api/corrections -> {why or status}"
        status, _body = p15._live_get(base_url, f"/api/species/{params['species_key']}")
        if status != 200:
            return False, f"/api/species/<key> -> {status}"
        source = inspect.getsource(suite_live)
        write_markers = ["method=" + '"POST"', "da" + "ta=", "method=" + '"PUT"', "method=" + '"DELETE"']
        found = [m for m in write_markers if m in source]
        return not found, f"suite_live source contains a write marker: {found}"

    passed += _case("LV4", lv4)

    # LV5 - INFO lines 17-04 diffs before and after the restart.
    def lv5():
        info = {}
        status, stats, _w = p15._live_json(base_url, "/api/stats")
        if status == 200 and isinstance(stats, dict):
            scalars = {k: v for k, v in sorted(stats.items()) if isinstance(v, (int, float))}
            info["stats"] = json.dumps(scalars, sort_keys=True)
        info["species_count"] = len(species) if species else None
        status, listed, _w = p15._live_json(base_url, "/api/corrections")
        info["corrections_count"] = len(listed) if status == 200 and isinstance(listed, list) else None
        status, videos, _w = p15._live_json(base_url, "/api/videos?per_page=1")
        info["videos_total"] = videos.get("total") if status == 200 and isinstance(videos, dict) else None
        status, gallery, _w = p15._live_json(base_url, "/api/gallery?per_page=1")
        info["gallery_total"] = gallery.get("total") if status == 200 and isinstance(gallery, dict) else None
        info["last_run_id"] = last_run.get("id") if isinstance(last_run, dict) else None
        for name, value in info.items():
            print(f"INFO: {name} {value}")
        unreadable = [n for n, v in info.items() if v is None]
        return not unreadable, f"unreadable: {unreadable}"

    passed += _case("LV5", lv5)

    return (passed, total)


# -- registry / CLI ----------------------------------------------------------

SUITES = {
    "schema": (suite_schema, 4),
    "decoupled": (suite_decoupled, 6),
    "guard": (suite_guard, 6),
    "endpoints": (suite_endpoints, 6),
    "bytecopy": (suite_bytecopy, 5),
    "live": (suite_live, 5),
}

# Suites that only run when explicitly requested (never part of --suite all).
EXPLICIT_ONLY = {"bytecopy", "live"}


def main():
    parser = argparse.ArgumentParser(description="Phase 17 verification harness")
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()) + ["all"], default="all",
        help="which suite to run (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    parser.add_argument(
        "--db", default=None,
        help="database for the bytecopy suite (required for it, no default). It is "
             "opened read-only (mode=ro) and copied with Connection.backup(); it is "
             "never written",
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8080",
        help="running service for the live suite, GET only (default: http://localhost:8080)",
    )
    args = parser.parse_args()

    if args.list:
        for name, (_fn, total) in SUITES.items():
            print(f"{name}: {total} cases")
        return 0

    if args.suite == "bytecopy":
        if not args.db:
            print("ERROR: --db is required for the bytecopy suite")
            return 2
        # sqlite3.connect creates a missing file, so check before any connect
        # (the audit_species_buckets pattern).
        if not os.path.isfile(args.db):
            print(f"ERROR: no database at {args.db}")
            return 2

    # web_app.root() reads static/index.html with the locale's default encoding.
    # On a UTF-8 box (the Linux target) that is right; on a cp1252 dev box GET /
    # raises UnicodeDecodeError, which would be a false 5xx here. Re-run once in
    # UTF-8 mode instead of weakening the "no 5xx on any GET route" proof.
    if (not sys.flags.utf8_mode and os.environ.get("PHASE17_UTF8_RERUN") != "1"
            and locale.getpreferredencoding(False).lower().replace("-", "") != "utf8"):
        env = dict(os.environ, PYTHONUTF8="1", PHASE17_UTF8_RERUN="1")
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], env=env
        ).returncode

    names = ([n for n in SUITES if n not in EXPLICIT_ONLY]
             if args.suite == "all" else [args.suite])
    all_passed = True
    for name in names:
        fn, _total = SUITES[name]
        if name == "bytecopy":
            passed, total = fn(args.db)
        elif name == "live":
            passed, total = fn(args.base_url)
        else:
            passed, total = fn()
        if passed is None:
            # A suite that cannot run returns (None, reason). Under `all` that is a
            # skip (never a PASS); asked for by name it is a FAIL, so the phase
            # gate cannot be satisfied by a skip.
            if args.suite == name:
                all_passed = False
                print(f"FAIL: {name} (fastapi/httpx unavailable: {total})")
            else:
                print(f"SKIP: {name} ({total})")
            continue
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            all_passed = False
            print(f"FAIL: {name} ({passed}/{total})")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
