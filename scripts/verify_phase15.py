"""
verify_phase15.py — stdlib-only verification harness for Phase 15
(Effective-Label Grouping & Filtering), plan 15-01.

Every species reader in database.py groups and filters on ONE effective key
(EFFECTIVE_KEY): the normalised corrected common name for a corrected
detection, the raw SpeciesNet label for an uncorrected one. This harness
proves the readers agree with each other.

Suites:
    lockstep           — LABEL-03/LABEL-04/D-05, the dropdown key, the gallery
                         and video filter predicates and the drilldown all
                         resolve the same key for every get_species_list() row
                         (LS1-LS8).

Fixture data uses SpeciesNet-shaped raw labels (uuid;class;order;family;genus;
species;common) for native rows so D-03 (a corrected bucket and a native
bucket stay separate) is exercised realistically, plus one deliberately
non-SpeciesNet label ("bobcat") for the equal-key merge case. Fixture dates
are relative to SQLite's UTC DATE('now'): fixed dates rot.

Follows scripts/verify_phase14.py's structure: a `_check(case_id, condition,
detail)` helper, per-suite `(passed, total)` returns, a dict suite registry,
argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:` summary
lines, and `sys.exit(0 if all_passed else 1)`.

Never call a database.* function ad hoc without database.set_db_path(<temp
file>) first: sqlite3.connect would otherwise create data/wildlife.db.

Usage:
    python scripts/verify_phase15.py --suite lockstep|all
    python scripts/verify_phase15.py --list
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


# ── helpers ──────────────────────────────────────────────────────────────

NO_SUCH_KEY = "zz-phase15-no-such-key"
TIMELINE_ALL = ("2000-01-01", "2100-12-31")
READER_TIME_BUDGET_SECS = 5.0

# SpeciesNet-shaped raw labels: a fixed uuid-like first field, then
# class;order;family;genus;species;common (RESEARCH Pitfall 9).
LBL_CAT = "11111111-0000-0000-0000-000000000001;mammalia;carnivora;felidae;felis;catus;domestic cat"
LBL_DOG = "11111111-0000-0000-0000-000000000002;mammalia;carnivora;canidae;canis;familiaris;domestic dog"
LBL_BOAR = "11111111-0000-0000-0000-000000000003;mammalia;cetartiodactyla;suidae;sus;scrofa;wild boar"
LBL_RACCOON = "11111111-0000-0000-0000-000000000004;mammalia;carnivora;procyonidae;procyon;lotor;northern raccoon"
LBL_UNKNOWN = "Unknown species"
LBL_BLANK = "11111111-0000-0000-0000-000000000005;;;;;;blank"
# Deliberately NOT SpeciesNet-shaped: its raw label equals the key a
# correction to "Bobcat" produces, so the two merge (ED5).
LBL_BOBCAT_PLAIN = "bobcat"

_NAMES = {
    LBL_CAT: ("Domestic Cat", "Felis catus"),
    LBL_DOG: ("Domestic Dog", "Canis familiaris"),
    LBL_BOAR: ("Wild Boar", "Sus scrofa"),
    LBL_RACCOON: ("Northern Raccoon", "Procyon lotor"),
    LBL_BOBCAT_PLAIN: ("Bobcat", "Lynx rufus"),
    LBL_UNKNOWN: (None, None),
    LBL_BLANK: (None, None),
}


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _read_text(relpath):
    """Read a repo file with newlines normalised to LF (the repo mixes CRLF
    and LF working-tree files)."""
    text = (_repo_root() / relpath).read_text(encoding="utf-8")
    return text.replace("\r\n", "\n")


def _slice(text, start, end):
    """Return the substring from the first occurrence of `start` up to the
    next occurrence of `end` after that point (or end of text if `end` is
    None or not found). Returns "" when `start` is absent."""
    start_idx = text.find(start)
    if start_idx == -1:
        return ""
    if end is None:
        return text[start_idx:]
    end_idx = text.find(end, start_idx + len(start))
    if end_idx == -1:
        return text[start_idx:]
    return text[start_idx:end_idx]


def _strip_hash_comment_lines(text):
    """Drop every line whose lstrip() begins with '#' (Python comments), so a
    future explanatory comment can never satisfy or invalidate a source
    assertion."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _strip_slash_comment_lines(text):
    """Drop every line whose lstrip() begins with '//' (JS comments)."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def _utc_day_iso(days_ago):
    """`YYYY-MM-DDT12:00:00` for the UTC date `days_ago` days before now.
    Fixtures sit relative to SQLite's UTC DATE('now'); fixed dates rot."""
    day = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return f"{day.isoformat()}T12:00:00"


def _seed_phase15_fixture(path):
    """Build the Phase 15 fixture in a fresh DB at `path` and return a dict of
    ids. One camera "World Watch", every video kept=1, one crop per detection
    (crop_path `fixture15_crop_<det_id>.jpg`).

    video (days ago): detection = label quality
        vid1 (1): c1 CAT 90, c2 CAT 50, c3 CAT 70
        vid2 (2): c4 CAT 60
        vid3 (2): r1 RACCOON 80
        vid4 (3): g1 DOG 80
        vid5 (3): g2 DOG 75
        vid6 (1): b1 BOAR 80, b2 BOAR 70, b3 BOAR 60
        vid7 (1): u1 UNKNOWN 40, k1 CAT 55
        vid8 (2): u2 UNKNOWN 40, u3 UNKNOWN 35
        vid9 (1): x1 BLANK 30
        vid10 (2): p1 BOBCAT_PLAIN 65, p2 CAT 45
        vid11 (1): s1 CAT 52, s2 RACCOON 66

    Before any correction the CAT bucket is 7 detections on 5 videos with best
    crop c1.
    """
    database.set_db_path(path)
    database.init_db(path)
    ids = {}

    with database.get_conn() as conn:

        def _insert_video(name, days_ago):
            when = _utc_day_iso(days_ago)
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, ?, 1, ?, 0, ?)",
                (f"WorldWatch_15_{name}.mp4", "World Watch", when, when),
            )
            vid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            ids[name] = vid
            return vid

        def _add(det_name, video_id, label, quality):
            common, sci = _NAMES[label]
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
            )
            det_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, ?, ?, ?, 0.9)",
                (det_id, label, common, sci),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (det_id, f"fixture15_crop_{det_id}.jpg", float(quality), _utc_day_iso(1)),
            )
            ids[det_name] = det_id

        v1 = _insert_video("vid1", 1)
        _add("c1", v1, LBL_CAT, 90)
        _add("c2", v1, LBL_CAT, 50)
        _add("c3", v1, LBL_CAT, 70)
        v2 = _insert_video("vid2", 2)
        _add("c4", v2, LBL_CAT, 60)
        v3 = _insert_video("vid3", 2)
        _add("r1", v3, LBL_RACCOON, 80)
        v4 = _insert_video("vid4", 3)
        _add("g1", v4, LBL_DOG, 80)
        v5 = _insert_video("vid5", 3)
        _add("g2", v5, LBL_DOG, 75)
        v6 = _insert_video("vid6", 1)
        _add("b1", v6, LBL_BOAR, 80)
        _add("b2", v6, LBL_BOAR, 70)
        _add("b3", v6, LBL_BOAR, 60)
        v7 = _insert_video("vid7", 1)
        _add("u1", v7, LBL_UNKNOWN, 40)
        _add("k1", v7, LBL_CAT, 55)
        v8 = _insert_video("vid8", 2)
        _add("u2", v8, LBL_UNKNOWN, 40)
        _add("u3", v8, LBL_UNKNOWN, 35)
        v9 = _insert_video("vid9", 1)
        _add("x1", v9, LBL_BLANK, 30)
        v10 = _insert_video("vid10", 2)
        _add("p1", v10, LBL_BOBCAT_PLAIN, 65)
        _add("p2", v10, LBL_CAT, 45)
        v11 = _insert_video("vid11", 1)
        _add("s1", v11, LBL_CAT, 52)
        _add("s2", v11, LBL_RACCOON, 66)

    return ids


def _crop_path(ids, det_name):
    return f"fixture15_crop_{ids[det_name]}.jpg"


def _gallery_ids(**kwargs):
    """Detection ids in get_gallery(per_page=100, **kwargs)."""
    return {
        it["detection_id"]
        for it in database.get_gallery(per_page=100, **kwargs)["items"]
    }


def _detail_crop_ids(key):
    return {c["detection_id"] for c in database.get_species_detail(key)["crops"]}


def _lockstep_violations(strict_gallery=True):
    """Readable violation strings for the dropdown/filter/drilldown lockstep.
    For every get_species_list() row, with key = row["label"]:
      - labels are unique;
      - get_videos(species_label=key) total == video_count;
      - get_species_detail(key): info.total_detections == detection_count,
        sum of trend counts == detection_count, len(videos) ==
        min(video_count, 20);
      - strict_gallery: get_gallery(species_label=key) total ==
        detection_count;
      - always: the sum over keys of filtered gallery totals ==
        get_gallery() total (every visible crop sits in exactly one bucket).
    """
    out = []
    rows = database.get_species_list()
    labels = [r["label"] for r in rows]
    if len(labels) != len(set(labels)):
        out.append(f"duplicate list labels: {labels}")
    gallery_sum = 0
    for r in rows:
        key = r["label"]
        dc, vc = r["detection_count"], r["video_count"]
        vt = database.get_videos(species_label=key, per_page=1)["total"]
        if vt != vc:
            out.append(f"{key!r}: get_videos total {vt} != video_count {vc}")
        detail = database.get_species_detail(key)
        info = detail.get("info") or {}
        if info.get("total_detections") != dc:
            out.append(
                f"{key!r}: detail total_detections {info.get('total_detections')} != detection_count {dc}"
            )
        trend_sum = sum(t["count"] for t in detail["trend"])
        if trend_sum != dc:
            out.append(f"{key!r}: detail trend sum {trend_sum} != detection_count {dc}")
        if len(detail["videos"]) != min(vc, 20):
            out.append(
                f"{key!r}: detail videos {len(detail['videos'])} != min(video_count, 20) {min(vc, 20)}"
            )
        gt = database.get_gallery(species_label=key, per_page=1)["total"]
        gallery_sum += gt
        if strict_gallery and gt != dc:
            out.append(f"{key!r}: get_gallery total {gt} != detection_count {dc}")
    total = database.get_gallery(per_page=1)["total"]
    if gallery_sum != total:
        out.append(f"sum of filtered gallery totals {gallery_sum} != get_gallery total {total}")
    return out


# ── `lockstep` suite ─────────────────────────────────────────────────────


def suite_lockstep():
    """`lockstep` suite cases LS1-LS8 (8 total). See module docstring."""
    passed = 0
    total = 8

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "lockstep.db"))

        def _list_row(key):
            return next((r for r in database.get_species_list() if r["label"] == key), None)

        # LS1 — fresh fixture, no corrections: lockstep holds and the CAT
        # bucket has its expected figures.
        case_id = "LS1"
        violations = _lockstep_violations()
        cat = _list_row(LBL_CAT)
        ok = (
            not violations
            and cat is not None
            and cat["detection_count"] == 7
            and cat["video_count"] == 5
            and cat["best_crop"] == _crop_path(ids, "c1")
        )
        _check(case_id, ok, f"violations={violations}, cat={cat}")
        if ok:
            passed += 1

        # LS2 — a Gallery correction moves a detection out of its raw bucket
        # and into the corrected-name bucket, in every reader.
        case_id = "LS2"
        rc = database.correct_species(ids["c1"], "Northern Raccoon", "Procyon lotor")
        rac = _list_row("northern raccoon")
        cat = _list_row(LBL_CAT)
        ok = (
            rc == 1
            and rac is not None
            and rac["detection_count"] == 1
            and rac["common_name"] == "Northern Raccoon"
            and rac["scientific_name"] == "Procyon lotor"
            and rac["has_correction"] == 1
            and cat is not None
            and cat["detection_count"] == 6
            and _gallery_ids(species_label="northern raccoon") == {ids["c1"]}
            and ids["c1"] not in _gallery_ids(species_label=LBL_CAT)
            and ids["c1"] in _detail_crop_ids("northern raccoon")
            and ids["c1"] not in _detail_crop_ids(LBL_CAT)
        )
        _check(case_id, ok, f"rc={rc}, rac={rac}, cat={cat}")
        if ok:
            passed += 1

        # LS3 — lockstep still holds after the correction.
        case_id = "LS3"
        violations = _lockstep_violations()
        ok = not violations
        _check(case_id, ok, f"violations={violations}")
        if ok:
            passed += 1

        # LS4 (D-11) — best crop is chosen inside the effective bucket.
        case_id = "LS4"
        rac = _list_row("northern raccoon")
        cat = _list_row(LBL_CAT)
        ok = (
            rac is not None
            and rac["best_crop"] == _crop_path(ids, "c1")
            and cat is not None
            and cat["best_crop"] == _crop_path(ids, "c3")
        )
        _check(case_id, ok, f"rac={rac}, cat={cat}")
        if ok:
            passed += 1

        # LS5 (D-12) — ai_common_name is gone from the species list, while
        # get_gallery() items still carry it.
        case_id = "LS5"
        list_rows = database.get_species_list()
        gallery_items = database.get_gallery(per_page=5)["items"]
        ok = (
            bool(list_rows)
            and all("ai_common_name" not in r for r in list_rows)
            and bool(gallery_items)
            and all("ai_common_name" in it for it in gallery_items)
        )
        _check(case_id, ok, f"list_keys={sorted(list_rows[0]) if list_rows else None}")
        if ok:
            passed += 1

        # LS6 — an unknown key yields info == {} and web_app's 404 branch.
        case_id = "LS6"
        web_text = _strip_hash_comment_lines(_read_text("web_app.py"))
        route = _slice(web_text, '@app.get("/api/species/{label:path}")', "@app.")
        ok = (
            database.get_species_detail(NO_SUCH_KEY)["info"] == {}
            and "db.get_species_detail(label)" in route
            and 'if not detail.get("info")' in route
            and "HTTPException(404" in route
        )
        _check(case_id, ok, f"route={route!r}")
        if ok:
            passed += 1

        # LS7 (D-05) — source assertions: the key travels as `label`, and
        # every predicate site interpolates EFFECTIVE_KEY as a bound `?`.
        case_id = "LS7"
        web_text = _strip_hash_comment_lines(_read_text("web_app.py"))
        gallery_route = _slice(web_text, '@app.get("/api/gallery")', "@app.")
        videos_route = _slice(web_text, '@app.get("/api/videos")', "@app.")
        html = _strip_slash_comment_lines(_read_text("static/index.html"))
        populate = _slice(html, "function populateSpeciesFilters(", "\n}")
        db_text = _strip_hash_comment_lines(_read_text("database.py"))
        gallery_fn = _slice(db_text, "def get_gallery(", "\ndef ")
        gallery_pred = _slice(gallery_fn, "if species_label:", "if camera_name:")
        videos_fn = _slice(db_text, "def get_videos(", "\ndef ")
        videos_pred = _slice(videos_fn, "if species_label:", "if has_person")
        detail_fn = _slice(db_text, "def get_species_detail(", "\ndef ")
        n_bound = detail_fn.count("= ?")
        n_key_bound = detail_fn.count("{EFFECTIVE_KEY} = ?")
        option_value = 'value="${escHtml(s.label)}"'
        ok = (
            "species_label=species" in gallery_route
            and "species_label=species" in videos_route
            and option_value in populate
            and "{EFFECTIVE_KEY} = ?" in gallery_pred
            and "s.label" not in gallery_pred
            and "{EFFECTIVE_KEY} = ?" in videos_pred
            and "{KNOWN_SPECIES_FILTER}" in videos_pred
            and "s.label" not in videos_pred
            and n_bound == n_key_bound == 4
        )
        _check(
            case_id,
            ok,
            f"gallery_route_ok={'species_label=species' in gallery_route}, "
            f"videos_route_ok={'species_label=species' in videos_route}, "
            f"populate_ok={option_value in populate}, "
            f"gallery_pred={gallery_pred!r}, videos_pred={videos_pred!r}, "
            f"n_bound={n_bound}, n_key_bound={n_key_bound}",
        )
        if ok:
            passed += 1

        # LS8 (D-04) — the key is defined once, in SQL, via the helpers.
        case_id = "LS8"
        lines = db_text.splitlines()
        n_eff = sum(1 for ln in lines if ln.startswith("EFFECTIVE_KEY = "))
        n_cor = sum(1 for ln in lines if ln.startswith("CORRECTED_KEY = "))
        n_cte = sum(1 for ln in lines if ln.startswith("KEY_DISPLAY_CTE = "))
        n_lower = db_text.count("LOWER(")
        normalize_fn = _slice(db_text, "def _normalize_key_sql(", "\nCORRECTED_KEY = ")
        cte_region = _slice(db_text, "KEY_DISPLAY_CTE = ", "\n\n")
        ok = (
            n_eff == 1
            and n_cor == 1
            and n_cte == 1
            and n_lower == 1
            and "LOWER(" in normalize_fn
            and "_normalize_key_sql(" in cte_region
        )
        _check(
            case_id,
            ok,
            f"n_eff={n_eff}, n_cor={n_cor}, n_cte={n_cte}, n_lower={n_lower}, "
            f"in_normalize={'LOWER(' in normalize_fn}, "
            f"cte_calls_helper={'_normalize_key_sql(' in cte_region}",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── registry / CLI ───────────────────────────────────────────────────────

SUITES = {
    "lockstep": (suite_lockstep, 8),
}


def main():
    parser = argparse.ArgumentParser(description="Phase 15 verification harness")
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

    names = list(SUITES.keys()) if args.suite == "all" else [args.suite]
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
