"""
verify_backfill_species_corrections.py — stdlib-only verification harness
for scripts/backfill_species_corrections.py (Phase 14 plan 03: the
species_corrections backfill migration).

Suites:
    plan  — P1-P7: dry-run correctness against a legacy-only fixture this
            harness builds itself. Asserts the real corrected-detection
            count, the fan-out expansion count, per-source counts, the
            unmatched/orphan count, the duplicate count, the NULL-
            corrected_at count, and that the report's crops total is
            printed as context (never the write target) while a dry-run
            leaves species_corrections empty.
    apply — A1-A8: the real --apply write, exercised directly on a fixture
            copy. Every planned row lands with the right values and
            source; both D-03 precedence directions resolve correctly; a
            suppress row lands suppressed=1; an orphan legacy row
            contributes nothing; a second --apply run is a no-op; a
            pre-existing species_corrections row with a NEWER corrected_at
            survives untouched; the audit-trail digest is identical before
            and after; PRAGMA foreign_key_check is empty and the post-apply
            row count equals the dry-run's planned count.
    gates — G1-G5: the four-independent-flag apply gate. --apply alone,
            --apply --confirm-irreversible without --snapshot-dir, and the
            same without --audit-log each exit non-zero and write nothing.
            A full-flag run creates a snapshot file and a non-empty JSONL
            audit log whose final line names the snapshot path.

Follows scripts/verify_phase12.py's structure: a `_check(case_id,
condition, detail)` helper, per-suite `(passed, total)` returns, a `SUITES`
dict, argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:`
summary lines, and `sys.exit(0 if all_passed else 1)`.

RED-before-GREEN, by task: this harness is written BEFORE the migration
script it drives exists (task 1). Until scripts/backfill_species_
corrections.py is written, ALL THREE suites fail loudly, with every case's
detail naming the missing module rather than raising a traceback. The
`plan` suite turns green in task 2 (the dry-run half lands); `apply` and
`gates` turn green in task 3 (the four-flag write path, snapshot, audit
log and reconciliation land).

This harness never touches a real production database — every suite
builds its own throw-away legacy-only fixture inside a
`tempfile.TemporaryDirectory` and always passes an explicit `--db <fixture
path>` to the migration under test, so no invocation can ever fall back to
the migration's own default database path.

Usage:
    python scripts/verify_backfill_species_corrections.py --suite plan|apply|gates|all
    python scripts/verify_backfill_species_corrections.py --list
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


PLAN_CASE_IDS = ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]
APPLY_CASE_IDS = ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"]
GATE_CASE_IDS = ["G1", "G2", "G3", "G4", "G5"]


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")
    return bool(condition)


def _load_backfill():
    """Lazily load scripts/backfill_species_corrections.py by file path
    (not sys.path import) so this works regardless of invocation cwd.
    Returns None — never raises — before the module exists (task 1's
    intended RED state) or if it fails to import, so every case can record
    a clear FAIL detail naming the missing/broken module instead of the
    whole suite crashing with a traceback."""
    path = Path(__file__).resolve().parent / "backfill_species_corrections.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("backfill_species_corrections", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _run_backfill(mod, argv):
    """Invoke backfill_species_corrections.main(argv), capturing
    (exit_code, stdout_text). main() always exits via sys.exit(); a
    SystemExit with code None means an implicit 0 (matches every other
    verify_*.py harness's convention in this project)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mod.main(argv)
        code = 0
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    return code, buf.getvalue()


# ── Legacy-only fixture ──────────────────────────────────────────────────


def _seed_legacy_fixture(path):
    """Build a database whose corrections exist ONLY in the two legacy
    locations (species.user_common_name/user_scientific_name/corrected_at
    and video_corrections) — raw SQL inserts, deliberately NOT through
    correct_species()/save_video_correction(), because this simulates
    genuine pre-migration production state, not the new unified write
    path. species_corrections itself is created (empty) by init_db(), same
    as any other table in SCHEMA.

    Seeds one case per <behavior> requirement:
      - V1: fan-out — dV1/dV2 share raw label "domestic cat", covered by
        ONE video-level correction row that must expand to both; dN
        carries a DIFFERENT label on the same video and must receive
        nothing.
      - V2: dG — a Gallery-only correction (no video-level row at all).
      - V3: dB — BOTH legacy sources correct this detection; the
        video-level correction's corrected_at is LATER than the Gallery
        correction's — video wins (D-03).
      - V4: dB2 — the mirror of dB: BOTH legacy sources again, but this
        time the Gallery correction's corrected_at is LATER — Gallery
        wins.
      - V5: dS — a legacy suppress row (video-level corrected_label IS
        NULL) — must backfill as suppressed=1, not as a correction.
      - V6: dQ — a Gallery correction whose corrected_at is NULL (a real
        historical data-quality case) — must not crash precedence
        resolution and must still be migrated.
      - V7: an orphan legacy video-level row whose (video_id,
        original_label) matches ZERO current detections (its own
        detection carries a different label) — contributes nothing, must
        be counted and reported, never silently dropped.
      - V8: a duplicate pair of legacy video-level rows for the SAME
        (video_id, original_label) with different corrected_at — the
        later one wins, the duplicate is counted and reported.
      - V9: dU — a detection with no correction of either kind at all —
        must never appear in species_corrections.

    Every detection also gets exactly one crops row, so the fixture's
    crops total (11) is deliberately different from the planned
    species_corrections row count (8) — P7 pins that the dry-run report
    never conflates the two.

    Returns a dict of every id a suite needs to look up.
    """
    database.set_db_path(path)
    database.init_db(path)
    now = datetime.now()

    def _t(offset_seconds):
        return (now + timedelta(seconds=offset_seconds)).isoformat()

    with database.get_conn() as conn:

        def _insert_video(filename):
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, "
                "lens_index, processed_at) VALUES (?, 'World Watch', 1, ?, 0, ?)",
                (filename, _t(0), _t(0)),
            )
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection(video_id, label, common_name="Domestic Cat", confidence=0.9):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', ?)",
                (video_id, confidence),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (det_id, label, common_name, common_name, confidence),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture14bf_crop_{det_id}.jpg", 80.0, _t(0)),
            )
            return det_id

        # V1 — fan-out: dV1/dV2 share "domestic cat"; dN carries a
        # different label and must receive nothing from the same row.
        v1 = _insert_video("WorldWatch_00_v1.mp4")
        d_v1a = _add_detection(v1, "domestic cat")
        d_v1b = _add_detection(v1, "domestic cat")
        d_n = _add_detection(v1, "coyote")
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v1, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor", _t(0)),
        )

        # V2 — dG: Gallery-only correction, no video-level row at all.
        v2 = _insert_video("WorldWatch_00_v2.mp4")
        d_g = _add_detection(v2, "domestic cat")
        conn.execute(
            "UPDATE species SET user_common_name=?, user_scientific_name=?, corrected_at=? "
            "WHERE detection_id=?",
            ("Bobcat", "Lynx rufus", _t(0), d_g),
        )

        # V3 — dB: BOTH legacy sources; video-level correction is LATER —
        # video wins (D-03 precedence, direction 1).
        v3 = _insert_video("WorldWatch_00_v3.mp4")
        d_b = _add_detection(v3, "domestic cat")
        conn.execute(
            "UPDATE species SET user_common_name=?, user_scientific_name=?, corrected_at=? "
            "WHERE detection_id=?",
            ("Bobcat", "Lynx rufus", _t(-100), d_b),
        )
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v3, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor", _t(-50)),
        )

        # V4 — dB2: the mirror of dB — BOTH legacy sources again, but the
        # Gallery correction is LATER — Gallery wins (D-03, direction 2).
        v4 = _insert_video("WorldWatch_00_v4.mp4")
        d_b2 = _add_detection(v4, "domestic cat")
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v4, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor", _t(-50)),
        )
        conn.execute(
            "UPDATE species SET user_common_name=?, user_scientific_name=?, corrected_at=? "
            "WHERE detection_id=?",
            ("Bobcat", "Lynx rufus", _t(-10), d_b2),
        )

        # V5 — dS: legacy suppress row (corrected_label IS NULL).
        v5 = _insert_video("WorldWatch_00_v5.mp4")
        d_s = _add_detection(v5, "domestic cat")
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,NULL,NULL,NULL,?)",
            (v5, "domestic cat", _t(0)),
        )

        # V6 — dQ: Gallery correction with a NULL corrected_at (data-
        # quality case) — must not crash precedence resolution.
        v6 = _insert_video("WorldWatch_00_v6.mp4")
        d_q = _add_detection(v6, "domestic cat")
        conn.execute(
            "UPDATE species SET user_common_name=?, user_scientific_name=? WHERE detection_id=?",
            ("Bobcat", "Lynx rufus", d_q),
        )

        # V7 — orphan: a legacy video-level row whose original_label
        # matches NO detection on this video (the video's own detection
        # carries a different label) — must contribute nothing and be
        # reported by name, never silently dropped.
        v7 = _insert_video("WorldWatch_00_v7.mp4")
        d_orphan_host = _add_detection(v7, "domestic cat")
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v7, "grizzly bear", "Black bear", "Black Bear", "Ursus americanus", _t(0)),
        )

        # V8 — duplicate: two legacy video-level rows for the SAME
        # (video_id, original_label) with different corrected_at — the
        # later one ("Red Fox") must win, the earlier is a discarded
        # duplicate.
        v8 = _insert_video("WorldWatch_00_v8.mp4")
        d_dup = _add_detection(v8, "domestic cat")
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v8, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor", _t(-100)),
        )
        conn.execute(
            "INSERT INTO video_corrections (video_id, original_label, corrected_label, "
            "corrected_common, corrected_scientific, corrected_at) VALUES (?,?,?,?,?,?)",
            (v8, "domestic cat", "Red fox", "Red Fox", "Vulpes vulpes", _t(-50)),
        )

        # V9 — dU: no correction of either kind — must never appear in
        # species_corrections, i.e. a genuinely uncorrected detection.
        v9 = _insert_video("WorldWatch_00_v9.mp4")
        d_u = _add_detection(v9, "domestic cat")

    return {
        "v1": v1, "d_v1a": d_v1a, "d_v1b": d_v1b, "d_n": d_n,
        "v2": v2, "d_g": d_g,
        "v3": v3, "d_b": d_b,
        "v4": v4, "d_b2": d_b2,
        "v5": v5, "d_s": d_s,
        "v6": v6, "d_q": d_q,
        "v7": v7, "d_orphan_host": d_orphan_host,
        "v8": v8, "d_dup": d_dup,
        "v9": v9, "d_u": d_u,
    }


# ── plan suite (P1-P7) ───────────────────────────────────────────────────


def suite_plan():
    total = 7
    mod = _load_backfill()
    if mod is None:
        for case_id in PLAN_CASE_IDS:
            _check(case_id, False, "scripts/backfill_species_corrections.py not found — write it (task 2)")
        return 0, total

    passed = 0
    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "plan_fixture.db")
        _seed_legacy_fixture(db_path)

        code, out_json = _run_backfill(mod, ["--db", db_path, "--json"])
        json_lines = [line for line in out_json.strip().splitlines() if line.strip()]
        payload = json.loads(json_lines[-1]) if json_lines else {}

        if _check(
            "P1", code == 0 and payload.get("planned_row_count") == 8,
            f"expected exit 0 & planned_row_count=8, got exit={code} payload={payload}",
        ):
            passed += 1

        if _check(
            "P2", payload.get("fanout_expansion_count") == 6,
            f"expected fanout_expansion_count=6, got {payload.get('fanout_expansion_count')}",
        ):
            passed += 1

        if _check(
            "P3",
            payload.get("gallery_source_rows") == 4 and payload.get("video_source_rows_before_fanout") == 6,
            f"expected gallery_source_rows=4 & video_source_rows_before_fanout=6, got {payload}",
        ):
            passed += 1

        if _check(
            "P4", payload.get("unmatched_count") == 1,
            f"expected unmatched_count=1, got {payload.get('unmatched_count')}",
        ):
            passed += 1

        if _check(
            "P5", payload.get("duplicate_count") == 1,
            f"expected duplicate_count=1, got {payload.get('duplicate_count')}",
        ):
            passed += 1

        if _check(
            "P6", payload.get("null_corrected_at_gallery_count") == 1,
            f"expected null_corrected_at_gallery_count=1, got {payload.get('null_corrected_at_gallery_count')}",
        ):
            passed += 1

        # P7: the text report labels crops as context (never the write
        # target), the two numbers differ, and a dry-run writes nothing.
        code_text, out_text = _run_backfill(mod, ["--db", db_path])
        crops_line_ok = any("context" in line and "11" in line for line in out_text.splitlines())
        database.set_db_path(db_path)
        with database.get_conn() as conn:
            sc_count = conn.execute("SELECT COUNT(*) FROM species_corrections").fetchone()[0]
        p7_ok = (
            code_text == 0
            and crops_line_ok
            and sc_count == 0
            and payload.get("crops_total_context_only") != payload.get("planned_row_count")
        )
        if _check(
            "P7", p7_ok,
            f"expected crops labelled context (11≠8) & dry-run writes nothing, "
            f"got sc_count={sc_count} crops_line_ok={crops_line_ok}",
        ):
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return passed, total


# ── apply suite (A1-A8) ──────────────────────────────────────────────────


def suite_apply():
    total = 8
    mod = _load_backfill()
    if mod is None:
        for case_id in APPLY_CASE_IDS:
            _check(case_id, False, "scripts/backfill_species_corrections.py not found — write it (task 3)")
        return 0, total

    passed = 0
    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        base_dir = tmpdir_obj.name

        # ---- Primary run: A1-A5, A8 ----
        db1 = os.path.join(base_dir, "apply1.db")
        ids = _seed_legacy_fixture(db1)
        code, out = _run_backfill(mod, [
            "--db", db1, "--apply", "--confirm-irreversible",
            "--snapshot-dir", os.path.join(base_dir, "snap1"),
            "--audit-log", os.path.join(base_dir, "audit1.jsonl"),
            "--json",
        ])
        run_ok = code == 0
        json_lines = [line for line in out.strip().splitlines() if line.strip()]
        payload = json.loads(json_lines[-1]) if json_lines else {}

        database.set_db_path(db1)

        def _sc_row(det_id):
            with database.get_conn() as conn:
                row = conn.execute(
                    "SELECT corrected_label, corrected_common, corrected_scientific, "
                    "suppressed, source, corrected_at FROM species_corrections WHERE detection_id=?",
                    (det_id,),
                ).fetchone()
            return dict(row) if row else None

        row_v1a = _sc_row(ids["d_v1a"])
        row_v1b = _sc_row(ids["d_v1b"])
        row_g = _sc_row(ids["d_g"])
        row_dup = _sc_row(ids["d_dup"])

        a1_ok = (
            run_ok
            and row_v1a and row_v1a["source"] == "video_player" and row_v1a["corrected_common"] == "Northern Raccoon"
            and row_v1b and row_v1b["source"] == "video_player" and row_v1b["corrected_common"] == "Northern Raccoon"
            and row_g and row_g["source"] == "gallery" and row_g["corrected_common"] == "Bobcat"
            and row_g["corrected_label"] is None
            and row_dup and row_dup["corrected_common"] == "Red Fox" and row_dup["corrected_scientific"] == "Vulpes vulpes"
        )
        if _check("A1", a1_ok, f"row values mismatch: v1a={row_v1a} v1b={row_v1b} g={row_g} dup={row_dup}"):
            passed += 1

        row_b = _sc_row(ids["d_b"])
        a2_ok = bool(row_b) and row_b["source"] == "video_player" and row_b["corrected_common"] == "Northern Raccoon"
        if _check("A2", a2_ok, f"dB precedence: expected video_player wins, got {row_b}"):
            passed += 1

        row_b2 = _sc_row(ids["d_b2"])
        a3_ok = bool(row_b2) and row_b2["source"] == "gallery" and row_b2["corrected_common"] == "Bobcat"
        if _check("A3", a3_ok, f"dB2 precedence: expected gallery wins, got {row_b2}"):
            passed += 1

        row_s = _sc_row(ids["d_s"])
        a4_ok = bool(row_s) and row_s["suppressed"] == 1 and row_s["corrected_label"] is None and row_s["source"] == "video_player"
        if _check("A4", a4_ok, f"dS suppress row mismatch: {row_s}"):
            passed += 1

        row_orphan_host = _sc_row(ids["d_orphan_host"])
        a5_ok = row_orphan_host is None
        if _check("A5", a5_ok, f"orphan-host detection should have no species_corrections row, got {row_orphan_host}"):
            passed += 1

        with database.get_conn() as conn:
            fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            row_count = conn.execute("SELECT COUNT(*) FROM species_corrections").fetchone()[0]
        a8_ok = (
            run_ok and fk_violations == 0
            and row_count == payload.get("planned_row_count")
            and bool(payload.get("audit_trail_digests"))
        )
        if _check("A8", a8_ok, f"post-conditions: fk={fk_violations} row_count={row_count} payload={payload}"):
            passed += 1

        # ---- A6: idempotent second apply ----
        db2 = os.path.join(base_dir, "apply2.db")
        _seed_legacy_fixture(db2)
        snap_dir2 = os.path.join(base_dir, "snap2")
        code_first, _ = _run_backfill(mod, [
            "--db", db2, "--apply", "--confirm-irreversible",
            "--snapshot-dir", snap_dir2, "--audit-log", os.path.join(base_dir, "audit2a.jsonl"),
        ])
        database.set_db_path(db2)
        with database.get_conn() as conn:
            snapshot_before = sorted(
                (dict(r) for r in conn.execute(
                    "SELECT detection_id, corrected_label, corrected_common, corrected_scientific, "
                    "suppressed, source, corrected_at FROM species_corrections ORDER BY detection_id"
                ).fetchall()),
                key=lambda d: d["detection_id"],
            )
        code_second, _ = _run_backfill(mod, [
            "--db", db2, "--apply", "--confirm-irreversible",
            "--snapshot-dir", snap_dir2, "--audit-log", os.path.join(base_dir, "audit2b.jsonl"),
        ])
        database.set_db_path(db2)
        with database.get_conn() as conn:
            snapshot_after = sorted(
                (dict(r) for r in conn.execute(
                    "SELECT detection_id, corrected_label, corrected_common, corrected_scientific, "
                    "suppressed, source, corrected_at FROM species_corrections ORDER BY detection_id"
                ).fetchall()),
                key=lambda d: d["detection_id"],
            )
        a6_ok = code_first == 0 and code_second == 0 and snapshot_before == snapshot_after
        if _check("A6", a6_ok, f"second apply changed state: before={snapshot_before} after={snapshot_after}"):
            passed += 1

        # ---- A7: pre-existing NEWER row survives unchanged ----
        db3 = os.path.join(base_dir, "apply3.db")
        ids3 = _seed_legacy_fixture(db3)
        database.set_db_path(db3)
        future_ts = (datetime.now() + timedelta(days=1)).isoformat()
        with database.get_conn() as conn:
            database._upsert_species_correction(
                conn, ids3["d_g"], corrected_label=None,
                corrected_common="PRE-EXISTING LIVE CORRECTION",
                corrected_scientific="Live Sci", suppressed=0,
                source="gallery", note=None,
            )
            # _upsert_species_correction() always stamps "now" — overwrite
            # corrected_at directly to an unambiguous future timestamp so
            # this case does not depend on clock resolution.
            conn.execute(
                "UPDATE species_corrections SET corrected_at=? WHERE detection_id=?",
                (future_ts, ids3["d_g"]),
            )
        code3, _ = _run_backfill(mod, [
            "--db", db3, "--apply", "--confirm-irreversible",
            "--snapshot-dir", os.path.join(base_dir, "snap3"),
            "--audit-log", os.path.join(base_dir, "audit3.jsonl"),
        ])
        database.set_db_path(db3)
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT corrected_common, corrected_at FROM species_corrections WHERE detection_id=?",
                (ids3["d_g"],),
            ).fetchone()
        a7_ok = (
            code3 == 0 and row
            and row["corrected_common"] == "PRE-EXISTING LIVE CORRECTION"
            and row["corrected_at"] == future_ts
        )
        if _check("A7", a7_ok, f"pre-existing newer row was overwritten: {dict(row) if row else None}"):
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return passed, total


# ── gates suite (G1-G5) ──────────────────────────────────────────────────


def suite_gates():
    total = 5
    mod = _load_backfill()
    if mod is None:
        for case_id in GATE_CASE_IDS:
            _check(case_id, False, "scripts/backfill_species_corrections.py not found — write it (task 3)")
        return 0, total

    passed = 0
    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        base_dir = tmpdir_obj.name

        def _sc_count(db_path):
            database.set_db_path(db_path)
            with database.get_conn() as conn:
                return conn.execute("SELECT COUNT(*) FROM species_corrections").fetchone()[0]

        db1 = os.path.join(base_dir, "g1.db")
        _seed_legacy_fixture(db1)
        code1, _ = _run_backfill(mod, ["--db", db1, "--apply"])
        g1_ok = code1 != 0 and _sc_count(db1) == 0
        if _check("G1", g1_ok, f"--apply alone should exit non-zero and write nothing, got code={code1}"):
            passed += 1

        db2 = os.path.join(base_dir, "g2.db")
        _seed_legacy_fixture(db2)
        code2, _ = _run_backfill(mod, ["--db", db2, "--apply", "--confirm-irreversible"])
        g2_ok = code2 != 0 and _sc_count(db2) == 0
        if _check("G2", g2_ok, f"missing --snapshot-dir should exit non-zero and write nothing, got code={code2}"):
            passed += 1

        db3 = os.path.join(base_dir, "g3.db")
        _seed_legacy_fixture(db3)
        code3, _ = _run_backfill(mod, [
            "--db", db3, "--apply", "--confirm-irreversible",
            "--snapshot-dir", os.path.join(base_dir, "snap3"),
        ])
        g3_ok = code3 != 0 and _sc_count(db3) == 0
        if _check("G3", g3_ok, f"missing --audit-log should exit non-zero and write nothing, got code={code3}"):
            passed += 1

        db4 = os.path.join(base_dir, "g4.db")
        _seed_legacy_fixture(db4)
        snap_dir4 = os.path.join(base_dir, "snap4")
        audit_log4 = os.path.join(base_dir, "audit4.jsonl")
        code4, _ = _run_backfill(mod, [
            "--db", db4, "--apply", "--confirm-irreversible",
            "--snapshot-dir", snap_dir4, "--audit-log", audit_log4,
        ])
        snapshot_files = list(Path(snap_dir4).glob("*.db")) if os.path.isdir(snap_dir4) else []
        g4_ok = code4 == 0 and len(snapshot_files) == 1 and os.path.exists(audit_log4) and os.path.getsize(audit_log4) > 0
        if _check(
            "G4", g4_ok,
            f"full-flag run should create one snapshot file and a non-empty audit log, "
            f"got snapshot_files={snapshot_files} exists={os.path.exists(audit_log4)}",
        ):
            passed += 1

        last_line_ok = False
        if snapshot_files and os.path.exists(audit_log4):
            with open(audit_log4, encoding="utf-8") as fh:
                lines = [line for line in fh.read().splitlines() if line.strip()]
            if lines:
                try:
                    last_obj = json.loads(lines[-1])
                    last_line_ok = (
                        last_obj.get("event") == "summary"
                        and "snapshot_path" in last_obj
                        and os.path.basename(last_obj["snapshot_path"]) == os.path.basename(str(snapshot_files[0]))
                    )
                except json.JSONDecodeError:
                    last_line_ok = False
        if _check("G5", last_line_ok, "audit log's final line must parse as JSON and name the snapshot path"):
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return passed, total


SUITES = {
    "plan": (suite_plan, 7),
    "apply": (suite_apply, 8),
    "gates": (suite_gates, 5),
}


def main():
    parser = argparse.ArgumentParser(
        description="Verification harness for scripts/backfill_species_corrections.py "
                     "(Phase 14 plan 03) — drives the migration end-to-end against a "
                     "legacy-only fixture database this harness builds itself; never "
                     "touches a real database."
    )
    parser.add_argument("--suite", choices=list(SUITES.keys()) + ["all"], default="all")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, (_func, total) in SUITES.items():
            print(f"{name}: {total} cases")
        return

    suite_names = list(SUITES.keys()) if args.suite == "all" else [args.suite]
    all_passed = True
    for name in suite_names:
        func, total = SUITES[name]
        passed, total = func()
        status = "PASS" if passed == total else "FAIL"
        if passed != total:
            all_passed = False
        print(f"{status}: {name} ({passed}/{total})")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
