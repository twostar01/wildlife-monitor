"""
verify_phase12.py — stdlib-only verification harness for Phase 12
(Observability, UX & Monitoring Decisions), plan 12-01.

Suites:
    badge       — UI-05/D-05/D-06, the has_correction signal is accurate for
                  BOTH correction write paths (Gallery popover and
                  video-player editor) in get_gallery(), get_species_detail()
                  and get_species_list() (B1-B8).
    ui          — UI-05/D-03/D-04, confidenceBadge() renders the pencil
                  indicator instead of a stale confidence percentage on a
                  corrected tile, and the Gallery grid carries exactly one
                  corrected signal per tile (U1-U8), verified as pure source
                  assertions over static/index.html — no browser, no DOM.
    propagation — UI-05/D-05 (plan 12-03), display-name propagation from
                  species_corrections rows widened to get_gallery(),
                  get_species_detail(), get_videos() and get_video_by_id()
                  (P1-P10). P1, P2, P5 and P6 are RED before this plan's
                  task 2 (P1, P2, P5) and task 3 (P6) land. P3, P4, P7, P9
                  and P10 must pass both before and after — those five are
                  the guards that stop the fix being "achieved" by breaking
                  something else: P3 (the pre-existing Gallery-popover
                  path), P4 (an uncorrected crop), P7 (get_video_by_id()'s
                  species_corrections read path, which
                  this plan never touches and which already produced the
                  correct value before this plan existed), P9 (the
                  suppression sentinel and the blank-corrected-name guard),
                  and P10 (Phase 15, D-13: get_species_list(),
                  get_stats() and get_timeline() group on the effective
                  key, so corrected detections leave their raw-label bucket).
                  P8 (the Gallery-vs-video-player most-recent-write
                  precedence case) checks get_gallery() AND
                  get_video_by_id() together — the get_video_by_id() half is
                  green from the start (same reason as P7), so P8 only
                  turns fully green once task 2 fixes the get_gallery()
                  half.

Follows scripts/verify_phase10.py's structure: a `_check(case_id, condition,
detail)` helper, per-suite `(passed, total)` returns, a dict suite
registry, argparse `--suite` with an `all` choice, `--list`, `PASS:`/`FAIL:`
summary lines, and `sys.exit(0 if all_passed else 1)`.

Suite `badge` is written RED on purpose — every assertion targets the
post-fix state, so running this harness before database.py's HAS_CORRECTION
edit lands reports FAIL for cases B1, B4, B5 (the dA/video-player half) and
B6. B2, B3, B7 and B8 pass both before and after — they are the
no-regression anchors (the pre-existing Gallery-popover path, the
uncorrected case, the case/encoding pin, and the suppression sentinel).
That is the intended and required outcome — this task's database.py edit is
the change that turns cases B1/B4/B5/B6 green.

Usage:
    python scripts/verify_phase12.py --suite badge|ui|propagation|all
    python scripts/verify_phase12.py --list
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def _check(case_id, condition, detail=""):
    """Record a test assertion. Prints immediately on failure, silent on success."""
    if not condition:
        print(f"FAIL: {case_id} — {detail}")


def _repo_root():
    return Path(__file__).resolve().parents[1]


def _index_html_text():
    return (_repo_root() / "static" / "index.html").read_text(encoding="utf-8")


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


def _strip_comment_lines(text):
    """Return `text` with any line whose lstrip() begins with '//' removed —
    so a future explanatory comment can never accidentally satisfy or
    invalidate a source-assertion case."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


# ── `badge` suite fixture ────────────────────────────────────────────────


def _seed_fixture_db(path):
    """Build the shared badge/propagation fixture: one camera "World Watch".
      - video A / detection dA, species label "domestic cat" — a
        VIDEO-PLAYER-path correction (original_label="domestic cat",
        corrected_label="Northern raccoon") is seeded here via the real
        database.save_video_correction() write path (Phase 14 plan 14-02:
        the unified species_corrections table is the only one any write
        path targets — a raw INSERT would bypass the snapshot fan-out). The
        species row itself is never modified. Filename "WorldWatch_00_videoA.mp4" is already
        distinguishable from every other video in this fixture (substring
        search matches only this row) — plan 12-03's case P6 relies on
        that, no further change needed.
      - video B / detection dB, species label "domestic cat" — left
        UNCORRECTED by this helper; suite_badge() and suite_propagation()
        each apply the GALLERY POPOVER correction (via the real
        database.correct_species() write path) themselves, after any case
        that must observe dB as uncorrected has already run (B6 in
        particular — see suite_badge()).
      - video C / detection dC, species label "Northern raccoon", NO
        correction of either kind. species.confidence is 0.0, anchoring
        the zero-confidence display case downstream (task 2/U4).
      - video C additionally carries detection dD, species label
        "domestic cat", plus a video-player SUPPRESS action
        (database.save_video_correction() with corrected_label=None) —
        the species_corrections.suppressed column is the sentinel now
        (Phase 14 D-00's dedicated column; a NULL corrected_label is not
        the suppression signal).
      - video E / detection dE, species label "domestic cat" — carries
        BOTH a video-player correction (corrected_label="Northern
        raccoon", corrected_common="Northern Raccoon", written FIRST via
        database.save_video_correction()) AND a Gallery-popover correction
        ("Bobcat", via database.correct_species(), applied SECOND, below
        the INSERT block so it runs in its own committed transaction) —
        plan 12-03's case P8, the both-paths precedence collision. Under
        Phase 14's D-03 (plain recency wins, UNIQUE(detection_id) UPSERT),
        the LAST write — the Gallery correction — now wins the slot; see
        case P8's revised assertion below.
      - video F / detection dF, species label "domestic cat" — a
        video-player correction whose corrected_label is set (a real
        correction, not a suppress sentinel) but whose corrected_common is
        the empty string — plan 12-03's case P9, the blank-corrected-name
        guard.
    Returns a dict of the real autoincrement ids assigned to each row.

    Every video-player-path correction above is written through the real
    database.save_video_correction() write path, not a raw INSERT into
    species_corrections — each call opens its own
    get_conn(), so (per Phase 14 plan 14-02's task 3) every one of these
    calls runs AFTER the INSERT block below has committed, mirroring the
    same hazard this function's comment already documented for the
    Gallery-popover correct_species(d_e, ...) call.

    Adding video E/F to this shared fixture is a deliberate, plan-12-03-
    directed choice, not an oversight — see the corresponding <action> in
    12-03-PLAN.md task 1, which calls out fixture contamination as "the
    most likely failure mode here" and requires `--suite badge` to be
    re-confirmed 8/8 immediately after this edit. It is safe here because
    suite_badge()'s B1-B8 cases only look up specific ids by detection_id
    or a specific species label's crop list — none of them assert an exact
    row count or set membership that video E/F's presence would perturb,
    and B6's aggregate has_correction==1 check for "domestic cat" was
    already going to be 1 from dA's video-player correction alone, so dE's
    additional Gallery correction doesn't change B6's asserted
    value (verified empirically, not just by inspection, before this
    plan's task 1 commit).
    """
    database.set_db_path(path)
    database.init_db(path)
    now = "2026-08-16T04:00:00"

    with database.get_conn() as conn:

        def _insert_video(filename):
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, ?, 1, ?, 0, ?)",
                (filename, "World Watch", now, now),
            )
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        def _add_detection(video_id, label, common_name, confidence=0.9):
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_id,),
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
                (det_id, f"fixture12_crop_{det_id}.jpg", 80.0, now),
            )
            return det_id

        video_a = _insert_video("WorldWatch_00_videoA.mp4")
        video_b = _insert_video("WorldWatch_00_videoB.mp4")
        video_c = _insert_video("WorldWatch_00_videoC.mp4")
        video_e = _insert_video("WorldWatch_00_videoE.mp4")
        video_f = _insert_video("WorldWatch_00_videoF.mp4")

        d_a = _add_detection(video_a, "domestic cat", "Domestic Cat")
        d_b = _add_detection(video_b, "domestic cat", "Domestic Cat")
        d_c = _add_detection(video_c, "Northern raccoon", "Northern Raccoon", confidence=0.0)
        d_d = _add_detection(video_c, "domestic cat", "Domestic Cat")
        d_e = _add_detection(video_e, "domestic cat", "Domestic Cat")
        d_f = _add_detection(video_f, "domestic cat", "Domestic Cat")

    # Every video-player-path correction below is written through the real
    # database.save_video_correction() write path, deliberately run in ITS
    # OWN transaction, AFTER the INSERT block above has committed —
    # save_video_correction() opens its own get_conn(), and calling it
    # while the block above's connection is still open would be a second
    # writer against an uncommitted transaction on the same file (the same
    # hazard this function's docstring already documented for the
    # correct_species(d_e, ...) call below).

    # Video-player-path correction on video A (dA).
    database.save_video_correction(
        video_a, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor",
    )

    # Suppression sentinel on video C for dD's label — corrected_label=None
    # fans out as species_corrections.suppressed=1.
    database.save_video_correction(video_c, "domestic cat", None, None, None)

    # Video-player-path correction on video E (dE) — the precedence half
    # of case P8, written FIRST. The Gallery-popover correction ("Bobcat")
    # is applied via correct_species() below, SECOND — under Phase 14's
    # D-03 (plain recency wins), the Gallery write now wins the
    # UNIQUE(detection_id) slot.
    database.save_video_correction(
        video_e, "domestic cat", "Northern raccoon", "Northern Raccoon", "Procyon lotor",
    )

    # Blank-corrected-name case on video F (dF) — corrected_label is a
    # real (non-NULL, non-suppress) correction, but corrected_common is
    # the empty string. Case P9's second half.
    database.save_video_correction(video_f, "domestic cat", "Northern raccoon", "", "")

    # Gallery-popover correction on video E's species row, written SECOND
    # (after the video-player correction above) — the D-03 recency-wins
    # write order case P8 pins.
    database.correct_species(d_e, "Bobcat", "Lynx rufus")

    return {
        "video_a": video_a,
        "video_b": video_b,
        "video_c": video_c,
        "video_e": video_e,
        "video_f": video_f,
        "d_a": d_a,
        "d_b": d_b,
        "d_c": d_c,
        "d_d": d_d,
        "d_e": d_e,
        "d_f": d_f,
    }


def suite_badge():
    """Badge-suite cases B1-B8 (8 total), proving has_correction is
    accurate for BOTH correction write paths across get_gallery(),
    get_species_detail() and get_species_list(), that a suppressed detection
    is excluded from every reader (Phase 15 D-08; B8 — a suppress row was
    never a correction, and now it is not a visible species either), and
    that label matching is exact/BINARY.

    Cases run in a specific, deliberate order: B6 (the get_species_list()
    aggregate for the "domestic cat" group) is checked BEFORE dB's
    Gallery-popover correction is applied, so that B6 isolates the
    video-player path's (dA's) contribution via MAX() and is genuinely RED
    pre-fix. If dB were corrected first, its species_corrections row would
    already flip the group's has_correction to 1 under the OLD (pre-fix)
    get_species_list() SQL too — B6 would spuriously pass pre-fix and stop
    being a real RED case. B1/B3/B4 don't depend on this ordering (they
    read per-row state, not a cross-row aggregate) and are checked first
    for the same reason verify_phase10.py checks its "must hold before any
    mutation" cases early.

    Every case runs against a `tempfile.TemporaryDirectory
    (ignore_cleanup_errors=True)` and restores `database.set_db_path
    (original)` in a `finally` block, exactly as verify_phase10.py's
    suite_fix01 does.
    """
    passed = 0
    total = 8

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "badge.db")
        ids = _seed_fixture_db(db_path)
        video_c = ids["video_c"]
        d_a, d_b, d_c, d_d = ids["d_a"], ids["d_b"], ids["d_c"], ids["d_d"]

        def _gallery_item(det_id):
            items = database.get_gallery(per_page=100)["items"]
            return next((it for it in items if it.get("detection_id") == det_id), None)

        def _species_detail_crops(label):
            return database.get_species_detail(label)["crops"]

        def _crop_by_detection(crops, det_id):
            return next((c for c in crops if c.get("detection_id") == det_id), None)

        # B1 — get_gallery() has_correction==1 for dA's crop (video-player
        # path). RED before the fix.
        case_id = "B1"
        item_a = _gallery_item(d_a)
        ok = item_a is not None and item_a.get("has_correction") == 1
        _check(case_id, ok, f"item_a={item_a}")
        if ok:
            passed += 1

        # B3 — get_gallery() has_correction==0 for dC's crop (uncorrected).
        # Also anchors the 0.0-confidence display case downstream.
        case_id = "B3"
        item_c = _gallery_item(d_c)
        ok = item_c is not None and item_c.get("has_correction") == 0
        _check(case_id, ok, f"item_c={item_c}")
        if ok:
            passed += 1

        # B4 — get_species_detail("domestic cat")'s crops rows each carry a
        # has_correction KEY (D-06: the key itself is absent pre-fix). RED
        # before the fix.
        case_id = "B4"
        crops_cat = _species_detail_crops("domestic cat")
        ok = bool(crops_cat) and all("has_correction" in c for c in crops_cat)
        _check(case_id, ok, f"crops_cat={crops_cat}")
        if ok:
            passed += 1

        # B6 — get_species_list() returns has_correction==1 for the
        # "northern raccoon" row, driven solely by dA's video-player
        # correction at this point (dB is still uncorrected). Revised for
        # Phase 15 (D-13): the list now groups on the EFFECTIVE key, so dA
        # has left the "domestic cat" bucket and sits in the corrected
        # "northern raccoon" bucket. Asserting on "domestic cat" would now
        # pass only through dF's blank-name correction (has_correction=1,
        # key unchanged) — the wrong reason. See the ordering note in this
        # function's docstring. Revised for Phase 16 (D-01): the native d_c
        # row (label "Northern raccoon", common_name "Northern Raccoon") now
        # joins the corrected bucket, so detection_count is 2 (dA + d_c).
        case_id = "B6"
        species_rows = database.get_species_list()
        raccoon_row = next(
            (r for r in species_rows if r.get("label") == "northern raccoon"), None
        )
        ok = (
            raccoon_row is not None
            and raccoon_row.get("has_correction") == 1
            and raccoon_row.get("detection_count") == 2
        )
        _check(case_id, ok, f"raccoon_row={raccoon_row}")
        if ok:
            passed += 1

        # Apply the Gallery-popover correction to dB now, through the real
        # write path (database.correct_species()) — everything after this
        # point may observe dB as corrected.
        database.correct_species(d_b, "Northern Raccoon", "Procyon lotor")

        # B2 — get_gallery() has_correction==1 for dB's crop (gallery-
        # popover path). Green before AND after — the no-regression anchor.
        case_id = "B2"
        item_b = _gallery_item(d_b)
        ok = item_b is not None and item_b.get("has_correction") == 1
        _check(case_id, ok, f"item_b={item_b}")
        if ok:
            passed += 1

        # B5 — get_species_detail("northern raccoon") returns
        # has_correction==1 for BOTH dA's crop and dB's crop. Revised for
        # Phase 15 (D-13): both detections were corrected to Northern
        # Raccoon, so the drilldown key is now the corrected bucket
        # "northern raccoon", not their raw "domestic cat" label.
        case_id = "B5"
        crops_raccoon = _species_detail_crops("northern raccoon")
        crop_a = _crop_by_detection(crops_raccoon, d_a)
        crop_b = _crop_by_detection(crops_raccoon, d_b)
        ok = (
            crop_a is not None
            and crop_a.get("has_correction") == 1
            and crop_b is not None
            and crop_b.get("has_correction") == 1
        )
        _check(case_id, ok, f"crop_a={crop_a}, crop_b={crop_b}")
        if ok:
            passed += 1

        # B7 — exact, case-sensitive matching through the live
        # video-player write path: save_video_correction() with an
        # original_label differing only by case from the actual species
        # label ("Domestic Cat" vs "domestic cat") fans out to 0
        # detections (_fanout_detection_ids compares s.label = ? exactly),
        # writes no species_corrections row, and so does NOT flip
        # has_correction for the otherwise-uncorrected crop. Built against
        # a fourth video whose species label is lowercase. Must pass
        # before and after.
        case_id = "B7"
        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO videos (filename, camera_name, kept, recorded_at, lens_index, processed_at) "
                "VALUES (?, ?, 1, ?, 0, ?)",
                ("WorldWatch_00_videoD.mp4", "World Watch", "2026-08-16T04:00:00", "2026-08-16T04:00:00"),
            )
            video_d = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO detections (video_id, category, confidence) VALUES (?, 'animal', 0.9)",
                (video_d,),
            )
            d_g = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO species (detection_id, label, common_name, scientific_name, confidence) "
                "VALUES (?, 'domestic cat', 'Domestic Cat', 'Domestic Cat', 0.9)",
                (d_g,),
            )
            conn.execute(
                "INSERT INTO crops (detection_id, crop_path, quality_score, created_at) "
                "VALUES (?, ?, ?, ?)",
                (d_g, f"fixture12_crop_{d_g}.jpg", 80.0, "2026-08-16T04:00:00"),
            )
        # Runs after the fixture's write transaction has closed:
        # save_video_correction() opens its own connection. Differs only
        # by case from the real label "domestic cat".
        applied = database.save_video_correction(video_d, "Domestic Cat", "Something Else", "Something Else", "Aliquid alienum")
        with database.get_conn() as conn:
            sc_rows = conn.execute(
                "SELECT COUNT(*) FROM species_corrections WHERE detection_id=?", (d_g,)
            ).fetchone()[0]
        item_g = _gallery_item(d_g)
        ok = (
            applied == 0
            and sc_rows == 0
            and item_g is not None
            and item_g.get("has_correction") == 0
        )
        _check(case_id, ok, f"applied={applied!r}, sc_rows={sc_rows}, item_g={item_g}")
        if ok:
            passed += 1

        # B8 — suppression sentinel: dD's detection carries a suppress row
        # (corrected_label NULL, suppressed=1), which is not a correction.
        # Revised for Phase 15 (D-08, D-13): a suppressed detection is now
        # excluded from every reader, so its crop is ABSENT from the
        # Gallery and from the "domestic cat" drilldown rather than shown
        # with has_correction==0. The suppress-is-not-a-correction
        # distinction itself (no has_correction flip, no name change) stays
        # pinned by verify_phase14's suppress suite.
        case_id = "B8"
        item_d = _gallery_item(d_d)
        cat_crops_b8 = _species_detail_crops("domestic cat")
        ok = item_d is None and _crop_by_detection(cat_crops_b8, d_d) is None
        _check(case_id, ok, f"item_d={item_d}, video_c={video_c}")
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


# ── `ui` suite (source assertions over static/index.html) ───────────────


def suite_ui():
    """UI-suite cases U1-U8 (8 total), pure source assertions over
    static/index.html — no browser, no DOM, no new dependency. Every
    assertion is scoped to the region it is about via `_slice()`, and every
    slice has comment lines (lstrip() starting with '//') stripped before
    testing, so an unrelated occurrence or explanatory comment elsewhere in
    this 3000-line single-file frontend can neither satisfy nor break a
    case.
    """
    passed = 0
    total = 8

    text = _index_html_text()

    badge_body = _slice(text, "function confidenceBadge(", "function escHtml(")
    badge_body_stripped = _strip_comment_lines(badge_body)

    gallery_slice = _slice(
        text, "grid.innerHTML = data.items.map(item =>", "renderPagination('galleryPagination'"
    )
    gallery_slice_stripped = _strip_comment_lines(gallery_slice)

    modal_slice = _slice(
        text, "document.getElementById('modalGallery').innerHTML", "// Videos"
    )
    modal_slice_stripped = _strip_comment_lines(modal_slice)

    apply_detection_slice = _slice(
        text, "async function applyDetectionCorrection(", "\nfunction "
    )
    if not apply_detection_slice:
        apply_detection_slice = _slice(text, "async function applyDetectionCorrection(", None)
    apply_detection_slice_stripped = _strip_comment_lines(apply_detection_slice)

    # U1 — the function confidenceBadge( declaration line names two parameters.
    case_id = "U1"
    decl_line = next(
        (line for line in text.splitlines() if "function confidenceBadge(" in line), ""
    )
    ok = decl_line.strip() == "function confidenceBadge(value, hasCorrection) {"
    _check(case_id, ok, f"decl_line={decl_line!r}")
    if ok:
        passed += 1

    # U2 — within the confidenceBadge body slice, a badge-corrected
    # reference appears BEFORE the === null guard.
    case_id = "U2"
    corrected_idx = badge_body_stripped.find("badge-corrected")
    null_guard_idx = badge_body_stripped.find("=== null")
    ok = corrected_idx != -1 and null_guard_idx != -1 and corrected_idx < null_guard_idx
    _check(case_id, ok, f"corrected_idx={corrected_idx}, null_guard_idx={null_guard_idx}")
    if ok:
        passed += 1

    # U3 — within that same slice, Math.round(value * 100) still appears —
    # the uncorrected branch was preserved rather than rewritten.
    case_id = "U3"
    ok = "Math.round(value * 100)" in badge_body_stripped
    _check(case_id, ok, "")
    if ok:
        passed += 1

    # U4 — within that same slice, value === null still appears — the
    # explicit guard survived.
    case_id = "U4"
    ok = "value === null" in badge_body_stripped
    _check(case_id, ok, "")
    if ok:
        passed += 1

    # U5 — within the gallery-grid slice, the confidenceBadge( call is
    # followed by item.has_correction before its closing paren.
    case_id = "U5"
    call_idx = gallery_slice_stripped.find("confidenceBadge(")
    ok = False
    if call_idx != -1:
        close_idx = gallery_slice_stripped.find(")", call_idx)
        call_text = gallery_slice_stripped[call_idx:close_idx]
        ok = "item.has_correction" in call_text
    _check(case_id, ok, f"call_idx={call_idx}")
    if ok:
        passed += 1

    # U6 — within the modal-crops slice, the confidenceBadge( call is
    # followed by c.has_correction before its closing paren.
    case_id = "U6"
    call_idx = modal_slice_stripped.find("confidenceBadge(")
    ok = False
    if call_idx != -1:
        close_idx = modal_slice_stripped.find(")", call_idx)
        call_text = modal_slice_stripped[call_idx:close_idx]
        ok = "c.has_correction" in call_text
    _check(case_id, ok, f"call_idx={call_idx}")
    if ok:
        passed += 1

    # U7 — within the gallery-grid slice, the identifier correctedBadge does
    # not occur (D-04: the text tag was removed, subsumed by the badge-slot
    # indicator). Region-scoped and comment-stripped so an explanatory
    # comment naming the removed identifier can't fail this case.
    case_id = "U7"
    ok = "correctedBadge" not in gallery_slice_stripped
    _check(case_id, ok, "")
    if ok:
        passed += 1

    # U8 — within the applyDetectionCorrection slice, a badge-pair
    # reference occurs AND a confidenceBadge( call occurs — the in-place
    # DOM patch reaches the badge slot, not only the name element.
    case_id = "U8"
    ok = (
        "badge-pair" in apply_detection_slice_stripped
        and "confidenceBadge(" in apply_detection_slice_stripped
    )
    _check(case_id, ok, "")
    if ok:
        passed += 1

    return (passed, total)


# ── `propagation` suite (plan 12-03) ─────────────────────────────────────


def suite_propagation():
    """Propagation-suite cases P1-P10 (10 total), UI-05/D-05 (plan 12-03):
    a species corrected through the video player's per-crop editor
    (species_corrections) now shows its corrected common/scientific name in
    the Gallery grid, the species-detail modal, the Videos tab and filename
    search — not only inside that one video's own detail view. Since Phase 15
    (D-13) the readers that group or filter on a species (get_species_list(),
    get_stats(), get_timeline(), search()'s species half, and the
    species-filter predicates in get_gallery()/get_videos()) key on the
    EFFECTIVE label, not the raw one; case P10 pins that grouping.

    Reuses `_seed_fixture_db()` as-is, then applies the Gallery-popover
    correction to dB itself (mirroring `suite_badge()`'s own post-seed
    correction) so case P3's no-regression anchor has something to anchor
    against. Unlike `suite_badge()`, there is no cross-case aggregate
    ordering constraint here (P10's aggregate checks are grouping-key pins,
    not corrected-vs-uncorrected counts), so the correction can be applied
    once, up front, before any case runs.

    Every case runs against a `tempfile.TemporaryDirectory
    (ignore_cleanup_errors=True)` and restores `database.set_db_path
    (original)` in a `finally` block, exactly as `suite_badge()` does.
    """
    passed = 0
    total = 10

    original_db_path = database.get_db_path()
    tmpdir_obj = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        db_path = os.path.join(tmpdir_obj.name, "propagation.db")
        ids = _seed_fixture_db(db_path)
        video_a = ids["video_a"]
        video_e = ids["video_e"]
        d_a, d_b, d_c, d_d = ids["d_a"], ids["d_b"], ids["d_c"], ids["d_d"]
        d_e, d_f = ids["d_e"], ids["d_f"]

        # Gallery-popover correction on video B — case P3's anchor.
        database.correct_species(d_b, "Northern Raccoon", "Procyon lotor")

        def _gallery_item(det_id):
            items = database.get_gallery(per_page=100)["items"]
            return next((it for it in items if it.get("detection_id") == det_id), None)

        def _species_detail_crops(label):
            return database.get_species_detail(label)["crops"]

        def _crop_by_detection(crops, det_id):
            return next((c for c in crops if c.get("detection_id") == det_id), None)

        # P1 — get_gallery() returns common_name "Northern Raccoon" for
        # dA's crop (video-player path). RED before task 2.
        case_id = "P1"
        item_a = _gallery_item(d_a)
        ok = item_a is not None and item_a.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"item_a={item_a}")
        if ok:
            passed += 1

        # P2 — get_gallery() returns scientific_name "Procyon lotor" for
        # dA's crop. RED before task 2.
        case_id = "P2"
        ok = item_a is not None and item_a.get("scientific_name") == "Procyon lotor"
        _check(case_id, ok, f"item_a={item_a}")
        if ok:
            passed += 1

        # P3 — get_gallery() returns the gallery-popover-corrected name for
        # dB's crop. Green before AND after — no-regression anchor.
        case_id = "P3"
        item_b = _gallery_item(d_b)
        ok = item_b is not None and item_b.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"item_b={item_b}")
        if ok:
            passed += 1

        # P4 — get_gallery() returns the raw SpeciesNet common_name for
        # dC's uncorrected crop.
        case_id = "P4"
        item_c = _gallery_item(d_c)
        ok = item_c is not None and item_c.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"item_c={item_c}")
        if ok:
            passed += 1

        # P5 — get_species_detail("northern raccoon")'s crops row for dA
        # returns common_name "Northern Raccoon". Revised for Phase 15
        # (D-13): dA's effective bucket is its corrected name, no longer
        # its raw "domestic cat" label.
        case_id = "P5"
        crops_raccoon = _species_detail_crops("northern raccoon")
        crop_a = _crop_by_detection(crops_raccoon, d_a)
        ok = crop_a is not None and crop_a.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"crop_a={crop_a}")
        if ok:
            passed += 1

        # P6 — get_videos(search=<video A's filename>)'s single item has a
        # species_list containing "Northern Raccoon" and NOT containing the
        # original AI common name for "domestic cat". RED before task 3 —
        # the exact symptom the folded todo reported.
        case_id = "P6"
        video_a_filename = database.get_video_by_id(video_a)["video"]["filename"]
        videos_result = database.get_videos(search=video_a_filename, per_page=100)
        video_a_item = next(
            (v for v in videos_result["items"] if v.get("id") == video_a), None
        )
        species_list = (video_a_item or {}).get("species_list") or ""
        ok = (
            video_a_item is not None
            and "Northern Raccoon" in species_list
            and "Domestic Cat" not in species_list
        )
        _check(case_id, ok, f"video_a_item={video_a_item}")
        if ok:
            passed += 1

        # P7 — get_video_by_id(A)'s detections still report "Northern
        # Raccoon" — the SQL result and get_video_by_id()'s
        # read path agree, because both read the same species_corrections
        # row. Green before AND after: get_video_by_id() already produced
        # this value before this plan touched any SQL.
        case_id = "P7"
        detail_a = database.get_video_by_id(video_a)
        det_a = next((d for d in detail_a["detections"] if d.get("id") == d_a), None)
        ok = det_a is not None and det_a.get("common_name") == "Northern Raccoon"
        _check(case_id, ok, f"det_a={det_a}")
        if ok:
            passed += 1

        # P8 — precedence, revised for Phase 14's D-03 (plain recency
        # wins, UNIQUE(detection_id) UPSERT): this case previously pinned
        # the opposite ordering — the video-player value ("Northern
        # Raccoon") beating the Gallery-popover value ("Bobcat")
        # unconditionally, regardless of write order, which is what the
        # pre-Phase-14 read-time "video always wins" COALESCE chain
        # produced. Phase 14 replaced that with write-time recency: in
        # this fixture the video-player correction is written FIRST and
        # the Gallery correction SECOND (see _seed_fixture_db()'s comment),
        # so under D-03 the LAST write — Gallery's "Bobcat" — now wins.
        # get_gallery() and get_video_by_id() must agree; that guarantee is
        # exactly what this combined case still exists to pin.
        case_id = "P8"
        item_e = _gallery_item(d_e)
        detail_e = database.get_video_by_id(video_e)
        det_e = next((d for d in detail_e["detections"] if d.get("id") == d_e), None)
        ok = (
            item_e is not None
            and item_e.get("common_name") == "Bobcat"
            and det_e is not None
            and det_e.get("common_name") == "Bobcat"
        )
        _check(case_id, ok, f"item_e={item_e}, det_e={det_e}")
        if ok:
            passed += 1

        # P9 — suppression and blank handling. Revised for Phase 15 (D-08,
        # D-13): dD (suppressed) is now ABSENT from the "domestic cat"
        # drilldown and from the Gallery — a suppressed detection has no
        # effective species — while dF (corrected_label set, corrected_common
        # empty string: the blank-name guard) is still present under its raw
        # bucket, returning its original common name rather than a blank.
        case_id = "P9"
        crops_cat = _species_detail_crops("domestic cat")
        crop_d = _crop_by_detection(crops_cat, d_d)
        crop_f = _crop_by_detection(crops_cat, d_f)
        ok = (
            crop_d is None
            and _gallery_item(d_d) is None
            and crop_f is not None
            and crop_f.get("common_name") == "Domestic Cat"
        )
        _check(case_id, ok, f"crop_d={crop_d}, crop_f={crop_f}")
        if ok:
            passed += 1

        # P10 — effective-key grouping (Phase 15, D-13; formerly the
        # "deliberate non-goal" pin of raw-label grouping). dB is corrected
        # up front, and dA carries a video-player correction, so the corrected
        # bucket "northern raccoon" holds exactly dA and dB. Every reader that
        # groups on the effective key must agree: get_species_list() has one
        # "northern raccoon" row (detection_count 2, video_count 2, display
        # name "Northern Raccoon", no ai_common_name key anywhere),
        # get_stats()["top_species"] reports it with cnt 2, get_timeline()
        # rows labelled "northern raccoon" sum to 2, and dA/dB have left the
        # "domestic cat" drilldown. Phase 15's key decision (the normalised
        # corrected common name, raw label for uncorrected detections) is
        # what made the old non-goal comment obsolete. Revised for Phase 16
        # (D-01): the native d_c row (label "Northern raccoon", common_name
        # "Northern Raccoon") now joins the corrected bucket, so the bucket
        # holds dA, dB and d_c: detection_count 3, video_count 3, stats cnt 3,
        # timeline sum 3. The display name stays "Northern Raccoon" (d_c's
        # native common_name wins, native-first D-04).
        case_id = "P10"
        species_rows = database.get_species_list()
        raccoon_row = next(
            (r for r in species_rows if r.get("label") == "northern raccoon"), None
        )
        stats = database.get_stats()
        stats_raccoon_row = next(
            (r for r in stats["top_species"] if r.get("label") == "northern raccoon"), None
        )
        # Explicit window around the fixture's fixed 2026-08-16 dates: the
        # default 30-day lookback ages the fixture out of range as real time passes.
        timeline = database.get_timeline(date_from="2026-08-01", date_to="2026-08-31")
        timeline_raccoon_sum = sum(
            r.get("count", 0) for r in timeline["rows"] if r.get("label") == "northern raccoon"
        )
        cat_detail_ids = {
            c.get("detection_id") for c in database.get_species_detail("domestic cat")["crops"]
        }
        ok = (
            raccoon_row is not None
            and raccoon_row.get("detection_count") == 3
            and raccoon_row.get("video_count") == 3
            and raccoon_row.get("common_name") == "Northern Raccoon"
            and all("ai_common_name" not in r for r in species_rows)
            and stats_raccoon_row is not None
            and stats_raccoon_row.get("cnt") == 3
            and stats_raccoon_row.get("common_name") == "Northern Raccoon"
            and timeline_raccoon_sum == 3
            and d_a not in cat_detail_ids
            and d_b not in cat_detail_ids
        )
        _check(
            case_id,
            ok,
            "effective-key grouping (Phase 15 D-13): corrected detections dA/dB "
            "must group under 'northern raccoon' in the species list, stats "
            f"top_species and timeline; raccoon_row={raccoon_row}, "
            f"stats_raccoon_row={stats_raccoon_row}, "
            f"timeline_raccoon_sum={timeline_raccoon_sum}, "
            f"cat_detail_ids={cat_detail_ids}",
        )
        if ok:
            passed += 1
    finally:
        database.set_db_path(original_db_path)
        tmpdir_obj.cleanup()

    return (passed, total)


SUITES = {
    "badge": (suite_badge, 8),
    "ui": (suite_ui, 8),
    "propagation": (suite_propagation, 10),
}


def main():
    parser = argparse.ArgumentParser(
        description="Phase 12 verification harness (Observability, UX & Monitoring Decisions)"
    )
    parser.add_argument(
        "--suite",
        choices=list(SUITES.keys()) + ["all"],
        default="all",
    )
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for name, (_, total) in SUITES.items():
            print(f"{name}: {total} cases")
        sys.exit(0)

    selected = list(SUITES.keys()) if args.suite == "all" else [args.suite]

    all_passed = True
    for name in selected:
        fn, total = SUITES[name]
        passed, total = fn()
        if passed == total:
            print(f"PASS: {name} ({passed}/{total})")
        else:
            print(f"FAIL: {name} ({passed}/{total})")
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
