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
    grouping           — LABEL-01/LABEL-02/LABEL-05, a corrected detection
                         leaves its raw-label bucket in every reader, the
                         readers agree with each other, and a bucket corrected
                         away vanishes everywhere (GR1-GR6).
    blacklist_suppress — D-07/D-08/D-09 and operator decision 1: a correction
                         overrides the blacklist (only a usable-name
                         correction does), a suppressed detection is excluded
                         from every reader, and the Unknown-sibling rule is
                         unchanged (BS1-BS7).
    edges              — normalisation merge, deterministic display name, the
                         scientific-name rule, D-03 separation, the equal-key
                         merge and an empty/populated smoke run (ED1-ED6).
    frontend_src       — LABEL-03/LABEL-04/LABEL-05 (plan 15-02), source-contract
                         checks on static/index.html: the stale-key guard in
                         openSpecies, sorted dropdowns that keep the active
                         filter, and the display-name gallery chip (FE1-FE9).
    audit              — read-only run of the same invariants plus per-reader
                         timing against a real database (AU1-AU4). SKIPs when
                         the database file is absent; GR6 self-tests it on
                         fixture data.

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
    python scripts/verify_phase15.py --suite lockstep|grouping|audit|blacklist_suppress|edges|frontend_src|all
    python scripts/verify_phase15.py --suite audit --db data/wildlife.db
    python scripts/verify_phase15.py --list
"""

import argparse
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import time
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


def _timeline_sums():
    """{key: summed count} over the all-time timeline."""
    rows = database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1])["rows"]
    out = {}
    for r in rows:
        out[r["label"]] = out.get(r["label"], 0) + r["count"]
    return out


def _activity_rows():
    """Activity rows that carry a label (empty days emit a NULL-label row)."""
    return [r for r in database.get_stats()["activity_7d_by_species"] if r.get("label")]


def _activity_sums():
    out = {}
    for r in _activity_rows():
        out[r["label"]] = out.get(r["label"], 0) + r["count"]
    return out


def _cross_reader_violations(strict_gallery=True, activity_complete=True):
    """Return (hard, soft): two lists of readable strings.

    Hard checks (a violation is a real disagreement between readers):
      - everything _lockstep_violations(strict_gallery) reports;
      - stats unique_species == len(get_species_list());
      - top_species labels == the first five list labels other than
        "Unknown species", in list order, and each cnt == the list
        detection_count;
      - all-time timeline: every label is a list key and each key's summed
        count == its list video_count (Unknown included on both sides);
      - activity rows with a label: the label is a list key, is not
        "Unknown species", and each key's summed count == video_count when
        activity_complete, else <= video_count.
    Soft checks (D-10 display consistency): top_species, timeline and
    activity names equal the list common_name for that key.
    """
    hard = list(_lockstep_violations(strict_gallery))
    soft = []
    rows = database.get_species_list()
    by_key = {r["label"]: r for r in rows}
    stats = database.get_stats()

    if stats["unique_species"] != len(rows):
        hard.append(f"unique_species {stats['unique_species']} != len(species list) {len(rows)}")

    expected_top = [r["label"] for r in rows if r["label"] != LBL_UNKNOWN][:5]
    top_labels = [t["label"] for t in stats["top_species"]]
    if top_labels != expected_top:
        hard.append(f"top_species labels {top_labels} != first five list labels {expected_top}")
    for t in stats["top_species"]:
        row = by_key.get(t["label"])
        if row is None:
            hard.append(f"top_species label {t['label']!r} is not a list key")
            continue
        if t["cnt"] != row["detection_count"]:
            hard.append(
                f"top_species {t['label']!r} cnt {t['cnt']} != detection_count {row['detection_count']}"
            )
        if t["common_name"] != row["common_name"]:
            soft.append(
                f"top_species {t['label']!r} name {t['common_name']!r} != list {row['common_name']!r}"
            )

    tl_rows = database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1])["rows"]
    tl_sums = {}
    for r in tl_rows:
        tl_sums[r["label"]] = tl_sums.get(r["label"], 0) + r["count"]
        row = by_key.get(r["label"])
        if row is None:
            hard.append(f"timeline label {r['label']!r} is not a list key")
        elif r["common_name"] != row["common_name"]:
            soft.append(
                f"timeline {r['label']!r} name {r['common_name']!r} != list {row['common_name']!r}"
            )
    for key, row in by_key.items():
        if tl_sums.get(key, 0) != row["video_count"]:
            hard.append(
                f"timeline sum for {key!r} {tl_sums.get(key, 0)} != video_count {row['video_count']}"
            )

    act_sums = {}
    for r in _activity_rows():
        key = r["label"]
        act_sums[key] = act_sums.get(key, 0) + r["count"]
        row = by_key.get(key)
        if key == LBL_UNKNOWN:
            hard.append("activity contains the Unknown species label")
        if row is None:
            hard.append(f"activity label {key!r} is not a list key")
        elif r["species"] != row["common_name"]:
            soft.append(
                f"activity {key!r} name {r['species']!r} != list {row['common_name']!r}"
            )
    for key, row in by_key.items():
        if key == LBL_UNKNOWN:
            continue
        got = act_sums.get(key, 0)
        if activity_complete and got != row["video_count"]:
            hard.append(f"activity sum for {key!r} {got} != video_count {row['video_count']}")
        elif got > row["video_count"]:
            hard.append(f"activity sum for {key!r} {got} > video_count {row['video_count']}")
    return hard, soft


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


# ── `grouping` suite ─────────────────────────────────────────────────────


def _apply_gr2_corrections(ids):
    """The mixed correction state GR2 (and the audit self-test) uses."""
    rcs = [
        database.correct_species(ids["c1"], "Northern Raccoon", "Procyon lotor"),
        database.correct_species(ids["g1"], "Coyote", "Canis latrans"),
        database.save_video_correction(ids["vid5"], LBL_DOG, "coyote_label", "coyote ", "Canis latrans"),
        # scientific-only: key unchanged
        database.correct_species(ids["c4"], "", "Felis silvestris"),
        # blank name: key unchanged
        database.save_video_correction(ids["vid6"], LBL_BOAR, "boar_label", "", ""),
        # corrected away from Unknown
        database.correct_species(ids["u1"], "Western Gray Squirrel", "Sciurus griseus"),
    ]
    return rcs


def suite_grouping():
    """`grouping` suite cases GR1-GR6 (6 total). See module docstring."""
    passed = 0
    total = 6

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        # GR1 (LABEL-01/02, leave-bucket) — correcting c1 away from CAT lowers
        # every CAT detection-count figure by exactly 1 and leaves every
        # video-count figure alone (VID1 still has c2 and c3).
        case_id = "GR1"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "gr1.db"))

        def _cat_figures():
            lst = {r["label"]: r for r in database.get_species_list()}
            top = {t["label"]: t for t in database.get_stats()["top_species"]}
            search_rows = {
                r["label"]: r for r in database.search("domestic cat")["species"]
            }
            return {
                "list_det": lst[LBL_CAT]["detection_count"],
                "top_cnt": top[LBL_CAT]["cnt"],
                "detail_det": database.get_species_detail(LBL_CAT)["info"]["total_detections"],
                "search_cnt": search_rows[LBL_CAT]["cnt"],
                "list_vid": lst[LBL_CAT]["video_count"],
                "timeline_vid": _timeline_sums().get(LBL_CAT, 0),
                "activity_vid": _activity_sums().get(LBL_CAT, 0),
            }

        before = _cat_figures()
        rc = database.correct_species(ids["c1"], "Northern Raccoon", "Procyon lotor")
        after = _cat_figures()
        det_keys = ("list_det", "top_cnt", "detail_det", "search_cnt")
        vid_keys = ("list_vid", "timeline_vid", "activity_vid")
        lst_labels = {r["label"] for r in database.get_species_list()}
        tl_labels = set(_timeline_sums())
        act_labels = set(_activity_sums())
        search_labels = {r["label"] for r in database.search("raccoon")["species"]}
        ok = (
            rc == 1
            and all(after[k] == before[k] - 1 for k in det_keys)
            and all(after[k] == before[k] for k in vid_keys)
            and before["list_det"] == 7
            and before["list_vid"] == 5
            and "northern raccoon" in lst_labels
            and "northern raccoon" in tl_labels
            and "northern raccoon" in act_labels
            and "northern raccoon" in search_labels
        )
        _check(case_id, ok, f"before={before}, after={after}, search_labels={search_labels}")
        if ok:
            passed += 1

        # GR2 (cross-reader agreement) — a mixed correction state across both
        # write paths, a scientific-only and a blank-name correction, and a
        # correction away from Unknown: every reader agrees.
        case_id = "GR2"
        gr2_path = os.path.join(tmpdir_obj.name, "gr2.db")
        ids = _seed_phase15_fixture(gr2_path)
        rcs = _apply_gr2_corrections(ids)
        hard, soft = _cross_reader_violations()
        ok = all(rc is not None for rc in rcs) and not hard and not soft
        _check(case_id, ok, f"rcs={rcs}, hard={hard}, soft={soft}")
        if ok:
            passed += 1

        # GR3 (LABEL-05, on GR2's state) — every DOG detection was corrected
        # away, so LBL_DOG is gone from every reader and its drilldown 404s.
        case_id = "GR3"
        stats = database.get_stats()
        ok = (
            LBL_DOG not in {r["label"] for r in database.get_species_list()}
            and LBL_DOG not in {t["label"] for t in stats["top_species"]}
            and LBL_DOG not in _timeline_sums()
            and LBL_DOG not in _activity_sums()
            and LBL_DOG not in {r["label"] for r in database.search("domestic dog")["species"]}
            and database.get_gallery(species_label=LBL_DOG)["total"] == 0
            and database.get_videos(species_label=LBL_DOG)["total"] == 0
            and database.get_species_detail(LBL_DOG)["info"] == {}
        )
        _check(case_id, ok, f"top={[t['label'] for t in stats['top_species']]}")
        if ok:
            passed += 1

        # GR4 (merge across write paths) — Gallery "Coyote" and video-player
        # "coyote " land in one bucket.
        case_id = "GR4"
        coyote = next((r for r in database.get_species_list() if r["label"] == "coyote"), None)
        ok = (
            coyote is not None
            and coyote["detection_count"] == 2
            and coyote["video_count"] == 2
        )
        _check(case_id, ok, f"coyote={coyote}")
        if ok:
            passed += 1

        # GR5 (search is drilldown-safe) — search hits are real buckets.
        case_id = "GR5"
        list_labels = {r["label"] for r in database.get_species_list()}
        coyote_hits = database.search("coyote")["species"]
        a_labels = [r["label"] for r in database.search("a")["species"]]
        raccoon_labels = [r["label"] for r in database.search("raccoon")["species"]]
        ok = (
            len(coyote_hits) == 1
            and coyote_hits[0]["label"] == "coyote"
            and coyote_hits[0]["cnt"] == 2
            and all(lbl in list_labels for lbl in a_labels + raccoon_labels)
            and LBL_DOG not in a_labels + raccoon_labels
        )
        _check(
            case_id, ok,
            f"coyote_hits={coyote_hits}, a_labels={a_labels}, raccoon_labels={raccoon_labels}",
        )
        if ok:
            passed += 1

        # GR6 (audit self-test) — the production audit passes on GR2's
        # fixture and does not SKIP.
        case_id = "GR6"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            audit_result = suite_audit(gr2_path)
        captured = buf.getvalue()
        ok = audit_result == (4, 4) and "SKIP" not in captured and "FAIL" not in captured
        _check(case_id, ok, f"audit_result={audit_result}, captured={captured!r}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── `blacklist_suppress` suite ───────────────────────────────────────────


def _video_det_ids(video_id):
    return {d["id"] for d in database.get_video_by_id(video_id)["detections"]}


def _list_labels():
    return {r["label"] for r in database.get_species_list()}


def _top_labels():
    return {t["label"] for t in database.get_stats()["top_species"]}


def _list_row(key):
    return next((r for r in database.get_species_list() if r["label"] == key), None)


def suite_blacklist_suppress():
    """`blacklist_suppress` suite cases BS1-BS7 (7 total). Each case seeds its
    own fresh fixture unless stated otherwise."""
    passed = 0
    total = 7

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        # BS1 (D-07) — a correction overrides the blacklist.
        case_id = "BS1"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "bs1.db"))
        database.add_to_blacklist(LBL_BOAR, "Wild Boar", "Sus scrofa", "")
        rc = database.correct_species(ids["b1"], "Northern Raccoon", "Procyon lotor")
        ok = (
            rc == 1
            and ids["b1"] in _detail_crop_ids("northern raccoon")
            and ids["b1"] in _gallery_ids(species_label="northern raccoon")
            and ids["b1"] in _video_det_ids(ids["vid6"])
            and LBL_BOAR not in _list_labels()
            and ids["b2"] not in _gallery_ids()
            and ids["b2"] not in _video_det_ids(ids["vid6"])
        )
        _check(case_id, ok, f"rc={rc}, vid6_dets={_video_det_ids(ids['vid6'])}")
        if ok:
            passed += 1

        # BS2 (Pitfall 3a, on BS1's DB) — a scientific-only correction does
        # not un-blacklist a detection.
        case_id = "BS2"
        rc = database.correct_species(ids["b3"], "", "Sus scrofa")
        hard, soft = _cross_reader_violations()
        ok = (
            rc == 1
            and ids["b3"] not in _gallery_ids()
            and ids["b3"] not in _video_det_ids(ids["vid6"])
            and LBL_BOAR not in _list_labels()
            and not hard
            and not soft
        )
        _check(case_id, ok, f"rc={rc}, hard={hard}, soft={soft}")
        if ok:
            passed += 1

        # BS3 (D-08 on a partial bucket) — suppressing one CAT detection
        # removes it from every reader and nowhere else.
        case_id = "BS3"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "bs3.db"))
        cat_before = _list_row(LBL_CAT)
        top_before = next(
            t["cnt"] for t in database.get_stats()["top_species"] if t["label"] == LBL_CAT
        )
        tl_before = _timeline_sums()[LBL_CAT]
        act_before = _activity_sums()[LBL_CAT]
        vids_before = database.get_videos(species_label=LBL_CAT)["total"]
        unique_before = database.get_stats()["unique_species"]
        applied = database.save_video_correction(ids["vid11"], LBL_CAT, None, None, None)
        vid11_item = next(
            (v for v in database.get_videos(per_page=100)["items"] if v["id"] == ids["vid11"]),
            None,
        )
        species_list = (vid11_item or {}).get("species_list") or ""
        cat_after = _list_row(LBL_CAT)
        top_after = next(
            t["cnt"] for t in database.get_stats()["top_species"] if t["label"] == LBL_CAT
        )
        hard, soft = _cross_reader_violations()
        ok = (
            applied == 1
            and (cat_before["detection_count"], cat_before["video_count"]) == (7, 5)
            and top_before == 7
            and (tl_before, act_before, vids_before) == (5, 5, 5)
            and ids["s1"] not in _gallery_ids()
            and ids["s1"] not in _gallery_ids(species_label=LBL_CAT)
            and ids["s1"] not in _detail_crop_ids(LBL_CAT)
            and ids["s1"] not in _video_det_ids(ids["vid11"])
            and cat_after["detection_count"] == 6
            and cat_after["video_count"] == 4
            and top_after == 6
            and _timeline_sums()[LBL_CAT] == 4
            and _activity_sums()[LBL_CAT] == 4
            and database.get_videos(species_label=LBL_CAT)["total"] == 4
            and vid11_item is not None
            and "Domestic Cat" not in species_list
            and "Northern Raccoon" in species_list
            and database.get_stats()["unique_species"] == unique_before
            and not hard
            and not soft
        )
        _check(
            case_id, ok,
            f"applied={applied}, cat_before={cat_before}, cat_after={cat_after}, "
            f"species_list={species_list!r}, hard={hard}, soft={soft}",
        )
        if ok:
            passed += 1

        # BS4 (D-08) — a suppressed-only bucket vanishes everywhere.
        case_id = "BS4"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "bs4.db"))
        unique_before = database.get_stats()["unique_species"]
        a4 = database.save_video_correction(ids["vid4"], LBL_DOG, None, None, None)
        a5 = database.save_video_correction(ids["vid5"], LBL_DOG, None, None, None)
        ok = (
            (a4, a5) == (1, 1)
            and LBL_DOG not in _list_labels()
            and LBL_DOG not in _top_labels()
            and LBL_DOG not in _timeline_sums()
            and LBL_DOG not in _activity_sums()
            and database.get_stats()["unique_species"] == unique_before - 1
            and database.get_species_detail(LBL_DOG)["info"] == {}
            and database.get_gallery(species_label=LBL_DOG)["total"] == 0
            and database.get_videos(species_label=LBL_DOG)["total"] == 0
        )
        _check(case_id, ok, f"a4={a4}, a5={a5}, unique_before={unique_before}")
        if ok:
            passed += 1

        # BS5 (D-09 unchanged) — the Unknown-sibling rule still tests the raw
        # label.
        case_id = "BS5"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "bs5.db"))
        unknown_before = _list_row(LBL_UNKNOWN)
        before_ok = (
            ids["u1"] not in _gallery_ids()
            and ids["u1"] not in _video_det_ids(ids["vid7"])
            and unknown_before is not None
            and unknown_before["detection_count"] == 2
        )
        database.correct_species(ids["u1"], "Western Gray Squirrel", "Sciurus griseus")
        squirrel = _list_row("western gray squirrel")
        after_u1_ok = (
            ids["u1"] in _gallery_ids()
            and ids["u1"] in _video_det_ids(ids["vid7"])
            and squirrel is not None
            and squirrel["detection_count"] == 1
        )
        database.correct_species(ids["u2"], "Some Animal", "Aliquid animalus")
        unknown_after = _list_row(LBL_UNKNOWN)
        after_u2_ok = (
            ids["u3"] in _video_det_ids(ids["vid8"])
            and ids["u3"] in _gallery_ids()
            and unknown_after is not None
            and unknown_after["detection_count"] == 1
        )
        ok = before_ok and after_u1_ok and after_u2_ok
        _check(
            case_id, ok,
            f"before_ok={before_ok}, after_u1_ok={after_u1_ok}, after_u2_ok={after_u2_ok}, "
            f"unknown_after={unknown_after}",
        )
        if ok:
            passed += 1

        # BS6 (Pitfall 6) — corrected-away-from-Unknown detections count under
        # their corrected key in stats; Unknown never appears in top/activity.
        case_id = "BS6"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "bs6.db"))
        for name in ("u1", "u2", "u3"):
            database.correct_species(ids[name], "Western Gray Squirrel", "Sciurus griseus")
        stats = database.get_stats()
        top = {t["label"]: t for t in stats["top_species"]}
        act_labels = {r["label"] for r in _activity_rows()}
        ok = (
            "western gray squirrel" in top
            and top["western gray squirrel"]["cnt"] == 3
            and LBL_UNKNOWN not in top
            and LBL_UNKNOWN not in act_labels
            and _activity_sums().get("western gray squirrel") == 2
        )
        _check(case_id, ok, f"top={top}, activity_sums={_activity_sums()}")
        if ok:
            passed += 1

        # BS7 (source; comment-stripped) — the filter's three ingredients.
        case_id = "BS7"
        db_text = _strip_hash_comment_lines(_read_text("database.py"))
        filter_src = _slice(db_text, "KNOWN_SPECIES_FILTER = (", "\n)")
        ok = (
            bool(filter_src)
            and "CORRECTED_KEY" in filter_src
            and "IS NOT NULL" in filter_src
            and "IS_SUPPRESSED_DETECTION" in filter_src
            and "HAS_UNIFIED_CORRECTION" not in filter_src
        )
        _check(case_id, ok, f"filter_src={filter_src!r}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── `edges` suite ────────────────────────────────────────────────────────


def _set_corrected_at(det_id, stamp):
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE species_corrections SET corrected_at = ? WHERE detection_id = ?",
            (stamp, det_id),
        )


def _display_names_for(key):
    """Every display name every reader shows for `key` (a set; one element
    means the readers agree)."""
    names = set()
    row = _list_row(key)
    if row is not None:
        names.add(row["common_name"])
    for t in database.get_stats()["top_species"]:
        if t["label"] == key:
            names.add(t["common_name"])
    info = database.get_species_detail(key).get("info") or {}
    if info:
        names.add(info["common_name"])
    for r in database.search(key)["species"]:
        if r["label"] == key:
            names.add(r["common_name"])
    for r in database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1])["rows"]:
        if r["label"] == key:
            names.add(r["common_name"])
    for r in _activity_rows():
        if r["label"] == key:
            names.add(r["species"])
    return names


def suite_edges():
    """`edges` suite cases ED1-ED6 (6 total)."""
    passed = 0
    total = 6

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        # ED1 (normalisation merge) — Gallery "Raccoon", a video-player
        # "raccoon<TAB>" and " RACCOON<CR><LF>" land in one bucket.
        case_id = "ED1"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "ed1.db"))
        database.correct_species(ids["c2"], "Raccoon", "Procyon lotor")
        database.save_video_correction(ids["vid2"], LBL_CAT, "raccoon_label", "raccoon\t", "Procyon lotor")
        database.save_video_correction(ids["vid7"], LBL_CAT, "raccoon_label", " RACCOON\r\n", "Procyon lotor")
        rows = database.get_species_list()
        raccoon_rows = [r for r in rows if r["label"] == "raccoon"]
        others = [
            r["label"] for r in rows
            if r["label"] != "raccoon" and r["label"].strip().lower() == "raccoon"
        ]
        ok = (
            len(raccoon_rows) == 1
            and raccoon_rows[0]["detection_count"] == 3
            and not others
        )
        _check(case_id, ok, f"raccoon_rows={raccoon_rows}, others={others}")
        if ok:
            passed += 1

        # ED2 (D-10 determinism, on ED1's DB) — the display name is the most
        # recent correction's trimmed name, identical across every reader.
        case_id = "ED2"
        _set_corrected_at(ids["c2"], "2000-01-01T00:00:00")
        _set_corrected_at(ids["c4"], "2000-01-03T00:00:00")
        _set_corrected_at(ids["k1"], "2000-01-02T00:00:00")
        names_c4 = _display_names_for("raccoon")
        _set_corrected_at(ids["k1"], "2000-01-04T00:00:00")
        names_k1 = _display_names_for("raccoon")
        for name in ("c2", "c4", "k1"):
            _set_corrected_at(ids[name], "2000-01-05T00:00:00")
        with database.get_conn() as conn:
            winner = conn.execute(
                "SELECT corrected_common FROM species_corrections WHERE detection_id IN (?, ?, ?) "
                "ORDER BY id DESC LIMIT 1",
                (ids["c2"], ids["c4"], ids["k1"]),
            ).fetchone()[0]
        names_tie = _display_names_for("raccoon")
        lists = [[r["common_name"] for r in database.get_species_list()] for _ in range(3)]
        ok = (
            names_c4 == {"raccoon"}
            and names_k1 == {"RACCOON"}
            and names_tie == {winner.strip()}
            and lists[0] == lists[1] == lists[2]
        )
        _check(
            case_id, ok,
            f"names_c4={names_c4}, names_k1={names_k1}, names_tie={names_tie}, winner={winner!r}",
        )
        if ok:
            passed += 1

        # ED3 (RESEARCH A5, on ED2's DB) — the latest correction has no
        # scientific name, so the bucket shows none rather than a raw one.
        case_id = "ED3"
        database.correct_species(ids["c2"], "Raccoon", "")
        row = _list_row("raccoon")
        info = database.get_species_detail("raccoon")["info"]
        ok = (
            row is not None
            and row["scientific_name"] is None
            and info.get("scientific_name") is None
            and row["common_name"] == "Raccoon"
        )
        _check(case_id, ok, f"row={row}, info={info}")
        if ok:
            passed += 1

        # ED4 (D-03) — a corrected bucket and a native SpeciesNet bucket with
        # the same display name stay separate.
        case_id = "ED4"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "ed4.db"))
        database.correct_species(ids["c1"], "Northern Raccoon", "Procyon lotor")
        same_name = {
            r["label"]: r for r in database.get_species_list()
            if r["common_name"] == "Northern Raccoon"
        }
        ok = (
            set(same_name) == {"northern raccoon", LBL_RACCOON}
            and same_name["northern raccoon"]["detection_count"] == 1
            and same_name[LBL_RACCOON]["detection_count"] == 2
            and _detail_crop_ids("northern raccoon") == {ids["c1"]}
            and _detail_crop_ids(LBL_RACCOON) == {ids["r1"], ids["s2"]}
        )
        _check(case_id, ok, f"same_name={sorted(same_name)}")
        if ok:
            passed += 1

        # ED5 (equal-key merge) — a raw label equal to the corrected key
        # merges into one bucket.
        case_id = "ED5"
        ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "ed5.db"))
        database.correct_species(ids["p2"], "Bobcat", "Lynx rufus")
        bobcat_rows = [r for r in database.get_species_list() if r["label"] == "bobcat"]
        ok = (
            len(bobcat_rows) == 1
            and bobcat_rows[0]["detection_count"] == 2
            and bobcat_rows[0]["common_name"] == "Bobcat"
            and database.get_gallery(species_label="bobcat")["total"] == 2
            and _detail_crop_ids("bobcat") == {ids["p1"], ids["p2"]}
        )
        _check(case_id, ok, f"bobcat_rows={bobcat_rows}")
        if ok:
            passed += 1

        # ED6 (smoke) — empty DB returns empty shapes; a populated DB runs
        # every reader without an OperationalError.
        case_id = "ED6"
        empty_path = os.path.join(tmpdir_obj.name, "ed6_empty.db")
        database.set_db_path(empty_path)
        database.init_db(empty_path)
        stats = database.get_stats()
        empty_ok = (
            len(stats["activity_7d_by_species"]) == 7
            and all(r["label"] is None and r["count"] == 0 for r in stats["activity_7d_by_species"])
            and stats["top_species"] == []
            and stats["unique_species"] == 0
            and database.get_species_list() == []
            and database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1])["rows"] == []
            and database.get_species_detail(NO_SUCH_KEY)["info"] == {}
            and database.search("a")["species"] == []
            and database.get_gallery(species_label=NO_SUCH_KEY)["total"] == 0
            and database.get_videos(species_label=NO_SUCH_KEY)["total"] == 0
        )
        smoke_error = None
        try:
            ids = _seed_phase15_fixture(os.path.join(tmpdir_obj.name, "ed6_full.db"))
            database.get_stats()
            database.get_species_list()
            database.get_timeline()
            database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1])
            database.get_species_detail(NO_SUCH_KEY)
            database.get_species_detail(LBL_CAT)
            database.search("a")
            database.get_gallery(species_label=NO_SUCH_KEY)
            database.get_videos(species_label=NO_SUCH_KEY)
            database.get_video_by_id(ids["vid1"])
        except sqlite3.OperationalError as exc:
            smoke_error = str(exc)
        ok = empty_ok and smoke_error is None
        _check(case_id, ok, f"empty_ok={empty_ok}, smoke_error={smoke_error}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── `audit` suite (read-only; SKIPs without a database) ──────────────────

_AUDIT_TABLES = ("videos", "detections", "species", "crops", "species_corrections", "blacklist")


def suite_audit(db_path="data/wildlife.db"):
    """`audit` suite cases AU1-AU4 (4 total). Read-only: every statement is a
    SELECT and the readers only SELECT. If `db_path` does not exist, prints
    `SKIP: audit (no database at <path>)` and returns (4, 4), the verify_phase10
    idiom."""
    total = 4
    if not Path(db_path).exists():
        print(f"SKIP: audit (no database at {db_path})")
        return (total, total)

    passed = 0
    original_db_path = database.get_db_path()

    def _select(conn, sql, params=()):
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            raise AssertionError("suite_audit attempted a non-SELECT statement")
        return conn.execute(sql, params)

    def _counts(conn):
        return {
            t: _select(conn, f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in _AUDIT_TABLES
        }

    try:
        database.set_db_path(db_path)
        with database.get_conn() as conn:
            has_corrections_table = (
                _select(
                    conn,
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='species_corrections'",
                ).fetchone()[0]
                == 1
            )
            counts_before = _counts(conn) if has_corrections_table else {}

            # AU1 — diagnostics (RESEARCH A2/A3, D-03, D-07).
            case_id = "AU1"
            print(f"INFO: sqlite_version {sqlite3.sqlite_version}")
            multiplicity = 0
            if has_corrections_table:
                by_supp = _select(
                    conn,
                    "SELECT suppressed, COUNT(*) FROM species_corrections GROUP BY suppressed",
                ).fetchall()
                print(f"INFO: species_corrections by suppressed {[tuple(r) for r in by_supp]}")
                kept0 = _select(
                    conn,
                    f"""SELECT COUNT(*) FROM species s
                        JOIN detections d ON s.detection_id = d.id
                        JOIN videos v ON d.video_id = v.id
                        WHERE {database.KNOWN_SPECIES_FILTER}
                          AND COALESCE(v.kept, 0) != 1""",
                ).fetchone()[0]
                print(f"INFO: known-filtered species rows on kept!=1 videos {kept0}")
                multiplicity = _select(
                    conn,
                    f"""SELECT COUNT(*) FROM species s
                        JOIN detections d ON s.detection_id = d.id
                        WHERE {database.KNOWN_SPECIES_FILTER}
                          AND (SELECT COUNT(*) FROM crops c WHERE c.detection_id = d.id) != 1""",
                ).fetchone()[0]
                print(f"INFO: known-filtered detections whose crop count is not 1 {multiplicity}")
                corrected_blacklisted = _select(
                    conn,
                    f"""SELECT COUNT(*) FROM species s
                        JOIN detections d ON s.detection_id = d.id
                        WHERE s.label IN (SELECT label FROM blacklist)
                          AND {database.CORRECTED_KEY} IS NOT NULL""",
                ).fetchone()[0]
                print(f"INFO: corrected detections whose raw label is blacklisted {corrected_blacklisted}")
        ok = has_corrections_table
        _check(case_id, ok, "species_corrections table is missing")
        if ok:
            passed += 1
        if not has_corrections_table:
            return (passed, total)

        rows = database.get_species_list()
        names = {}
        for r in rows:
            names[r["common_name"]] = names.get(r["common_name"], 0) + 1
        shared = sum(1 for n in names.values() if n >= 2)
        print(f"INFO: keys {len(rows)}")
        print(f"INFO: display names shared by two or more keys {shared}")

        # AU2 — the readers agree. activity_complete is False on real data:
        # the 7-day activity window can lag the all-time list.
        case_id = "AU2"
        hard, soft = _cross_reader_violations(
            strict_gallery=(multiplicity == 0), activity_complete=False
        )
        for line in soft:
            print(f"WARN: {line}")
        ok = not hard
        _check(case_id, ok, f"{len(hard)} violation(s): {hard[:20]}")
        if ok:
            passed += 1

        # AU3 (RESEARCH A4) — every reader stays inside the time budget.
        case_id = "AU3"
        timings = {}

        def _timed(name, fn):
            started = time.perf_counter()
            fn()
            timings[name] = time.perf_counter() - started

        _timed("get_species_list", database.get_species_list)
        _timed("get_stats", database.get_stats)
        _timed("get_timeline", database.get_timeline)
        _timed(
            "get_timeline(all)",
            lambda: database.get_timeline(date_from=TIMELINE_ALL[0], date_to=TIMELINE_ALL[1]),
        )
        if rows:
            big = max(rows, key=lambda r: r["detection_count"])["label"]
            _timed("get_species_detail", lambda: database.get_species_detail(big))
            _timed("get_gallery(species)", lambda: database.get_gallery(species_label=big))
            _timed("get_videos(species)", lambda: database.get_videos(species_label=big))
        _timed("search", lambda: database.search("raccoon"))
        for name, secs in timings.items():
            print(f"INFO: timing {name} {secs:.3f}")
        slow = {n: round(t, 3) for n, t in timings.items() if t >= READER_TIME_BUDGET_SECS}
        ok = not slow
        _check(case_id, ok, f"over the {READER_TIME_BUDGET_SECS}s budget: {slow}")
        if ok:
            passed += 1

        # AU4 — the audit changed nothing.
        case_id = "AU4"
        with database.get_conn() as conn:
            counts_after = _counts(conn)
        ok = counts_after == counts_before
        _check(case_id, ok, f"before={counts_before}, after={counts_after}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)

    return (passed, total)


# ── frontend_src (plan 15-02) ────────────────────────────────────────────

def _frontend_slices():
    """Comment-stripped slices of static/index.html used by the FE cases."""
    text = _read_text("static/index.html")
    return {
        "api": _strip_slash_comment_lines(
            _slice(text, "async function api(", "\nfunction cropUrl")),
        "open_species": _strip_slash_comment_lines(
            _slice(text, "async function openSpecies(", "\n// ── Gallery")),
        "populate": _strip_slash_comment_lines(
            _slice(text, "function populateSpeciesFilters(", "\n// ── Util")),
        "chips": _strip_slash_comment_lines(
            _slice(text, "function renderGalleryChips(", "\nfunction clearGalleryFilter(")),
        "load_species": _strip_slash_comment_lines(
            _slice(text, "async function loadSpecies(", "async function openSpecies(")),
        "search": _strip_slash_comment_lines(text),
    }


def suite_frontend_src():
    """FE1-FE3: a stale species key lands on a recoverable 'Species not
    found' modal. FE4-FE9: sorted dropdowns keep the active filter, the
    gallery chip names the species, and the key contract holds.

    The frontend is a single static file with no build step, so these are
    source-contract assertions over comment-stripped slices; behaviour in a
    browser is covered by the 15-03 operator checks."""
    passed = 0
    total = 9
    s = _frontend_slices()
    os_src = s["open_species"]

    # FE1 — guard shape: try < api call on /species/ < catch < first d.info,
    # and the catch block renders the not-found state and disarms both buttons.
    case_id = "FE1"
    i_try = os_src.find("try")
    i_api = os_src.find("/species/")
    i_catch = os_src.find("catch")
    i_info = os_src.find("d.info")
    ordered = -1 < i_try < i_api < i_catch < i_info
    block = os_src[i_catch:i_info] if ordered else ""
    needed = ["Species not found", "modalViewGalleryBtn", "modalViewVideosBtn",
              "= null", "return"]
    missing = [n for n in needed if n not in block]
    ok = ordered and not missing
    _check(case_id, ok,
           f"order try/api/catch/d.info = {(i_try, i_api, i_catch, i_info)}, "
           f"catch block missing {missing}")
    if ok:
        passed += 1

    # FE2 — the precondition the guard relies on: api() throws on non-2xx.
    case_id = "FE2"
    ok = "if (!r.ok) throw" in s["api"]
    _check(case_id, ok, "api() no longer throws on a non-2xx response")
    if ok:
        passed += 1

    # FE3 — the success path is intact.
    case_id = "FE3"
    needed = [
        "d.info.common_name || label",
        "modalViewGalleryBtn').onclick = () =>",
        "modalViewVideosBtn').onclick = () =>",
        "navigateToVideos({ species: label })",
    ]
    missing = [n for n in needed if n not in os_src]
    ok = not missing
    _check(case_id, ok, f"openSpecies success path missing {missing}")
    if ok:
        passed += 1

    pop = s["populate"]

    # FE4 — the key contract: option value and text expressions unchanged.
    case_id = "FE4"
    needed = ['value="${escHtml(s.label)}"', "${escHtml(s.common_name||s.label)}"]
    missing = [n for n in needed if n not in pop]
    ok = bool(pop) and not missing
    _check(case_id, ok, f"populateSpeciesFilters missing {missing}")
    if ok:
        passed += 1

    # FE5 — options are built from a sorted shallow copy.
    case_id = "FE5"
    needed = ["localeCompare", "sensitivity", "[...speciesList]"]
    missing = [n for n in needed if n not in pop]
    ok = not missing
    _check(case_id, ok, f"populateSpeciesFilters missing {missing}")
    if ok:
        passed += 1

    # FE6 — the caller's array is never sorted in place.
    case_id = "FE6"
    ok = bool(pop) and "speciesList.sort(" not in pop
    _check(case_id, ok, "populateSpeciesFilters sorts its speciesList parameter in place")
    if ok:
        passed += 1

    # FE7 — the active filters are re-applied after the options are rebuilt.
    case_id = "FE7"
    needed = ["state.gallerySpecies", "state.videoSpecies", ".value ="]
    missing = [n for n in needed if n not in pop]
    ok = not missing
    _check(case_id, ok, f"populateSpeciesFilters missing {missing}")
    if ok:
        passed += 1

    # FE8 — the gallery chip resolves a display name and no longer prints the
    # raw key directly.
    case_id = "FE8"
    chips = s["chips"]
    needed = ["state.speciesList", "chip("]
    missing = [n for n in needed if n not in chips]
    raw_key_template = "Species: ${state.gallerySpecies}"
    ok = bool(chips) and not missing and raw_key_template not in chips
    _check(case_id, ok,
           f"renderGalleryChips missing {missing}, raw-key template present: "
           f"{raw_key_template in chips}")
    if ok:
        passed += 1

    # FE9 — no species-card renderer reads ai_common_name (D-12), and a global
    # search hit still passes the key to openSpecies.
    case_id = "FE9"
    ok = (bool(s["load_species"])
          and "ai_common_name" not in s["load_species"]
          and 'data-search-species="${escHtml(s.label)}"' in s["search"])
    _check(case_id, ok,
           "loadSpecies reads ai_common_name, or the search hit lost its "
           "data-search-species key attribute")
    if ok:
        passed += 1

    return (passed, total)


# ── registry / CLI ───────────────────────────────────────────────────────

SUITES = {
    "lockstep": (suite_lockstep, 8),
    "grouping": (suite_grouping, 6),
    "audit": (suite_audit, 4),
    "blacklist_suppress": (suite_blacklist_suppress, 7),
    "edges": (suite_edges, 6),
    "frontend_src": (suite_frontend_src, 9),
}


def main():
    parser = argparse.ArgumentParser(description="Phase 15 verification harness")
    parser.add_argument(
        "--suite", choices=list(SUITES.keys()) + ["all"], default="all",
        help="which suite to run (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="list suites and exit")
    parser.add_argument(
        "--db", default="data/wildlife.db",
        help="database for the read-only audit suite (default: data/wildlife.db)",
    )
    args = parser.parse_args()

    if args.list:
        for name, (_fn, total) in SUITES.items():
            print(f"{name}: {total} cases")
        return 0

    names = list(SUITES.keys()) if args.suite == "all" else [args.suite]
    all_passed = True
    for name in names:
        fn, _total = SUITES[name]
        passed, total = fn(args.db) if name == "audit" else fn()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            all_passed = False
            print(f"FAIL: {name} ({passed}/{total})")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
