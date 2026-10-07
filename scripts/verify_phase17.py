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

Fixture. Both shapes are built from scripts/verify_phase15.py's fixture plus a
dual-lens pair and a flagged video with two crops on disk. The legacy DDL below
lives ONLY in this file: database.py no longer declares those objects, so the
harness re-creates them with raw statements on a copy.

Usage:
    python scripts/verify_phase17.py --suite schema|decoupled|all
    python scripts/verify_phase17.py --list

verify_phase15 is a sibling import, so run this file without `python -I`.
"""

import argparse
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
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


def _reader_outputs(path, ids):
    """JSON of every reader the dashboard drives, against the database at
    `path`. Each result must be identical between the two shapes."""
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
    return {name: _dump(fn()) for name, fn in calls.items()}


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


# -- registry / CLI ----------------------------------------------------------

SUITES = {
    "schema": (suite_schema, 4),
    "decoupled": (suite_decoupled, 6),
}

# Suites that only run when explicitly requested (never part of --suite all).
EXPLICIT_ONLY = set()


def main():
    parser = argparse.ArgumentParser(description="Phase 17 verification harness")
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
