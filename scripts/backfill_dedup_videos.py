"""
backfill_dedup_videos.py — historical dedup backfill for the `videos` table.

Consolidates the ~19,291 duplicate (filename, camera_name) `videos` row
identities left behind by the pre-v1.1 archive-collision bug (04-DEFERRED.md)
down to one authoritative row per physical file. Supports two independent
modes:

  --audit       Read-only shape audit (PITFALLS.md Pitfall 1). No
                row-mutating statement, no filesystem-removal call.
  --consolidate The tracer's write path: plan every duplicate group, print
                the plan, and — only with --apply --confirm-irreversible —
                delete loser rows' full child graph and clean up orphaned
                crop/thumbnail files, in one transaction per group. Dry-run
                (plan-and-print, no writes) is the default whenever --apply
                is absent. This slice's select_winner()/plan_group() apply
                only the default tie-break with no correction precedence,
                re-parenting, or pairing repoint — see 09-02-PLAN.md's
                <scope_boundary>; 09-03 extends both functions.

Usage:
    python scripts/backfill_dedup_videos.py --db data/wildlife.db --audit [--json]
    python scripts/backfill_dedup_videos.py --db data/wildlife.db --consolidate
    python scripts/backfill_dedup_videos.py --db data/wildlife.db --consolidate \
        --apply --confirm-irreversible --snapshot-dir /tmp/snap --audit-log /tmp/audit.jsonl
"""

import argparse
import dataclasses
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from sqlite3 import connect as sqlite_connect
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


def find_duplicate_groups(conn):
    """Return one entry per duplicate (filename, camera_name) identity, in a
    stable (filename, camera_name) order.

    SQLite's GROUP BY treats all NULL values in a grouped column as one
    group, which matches the NULL-safe `IS` semantics
    database._find_existing_video_row() uses elsewhere — a NULL camera_name
    groups only with other NULL camera_name rows, never with a non-NULL one.

    The explicit ORDER BY exists so --limit's "process only the first N
    groups" is deterministic across runs and machines, rather than relying
    on GROUP BY's incidental (and unspecified) result order.
    """
    rows = conn.execute(
        "SELECT filename, camera_name, COUNT(*) AS n FROM videos "
        "GROUP BY filename, camera_name HAVING COUNT(*) > 1 "
        "ORDER BY filename, camera_name"
    ).fetchall()
    return [
        {"filename": r["filename"], "camera_name": r["camera_name"], "n": r["n"]}
        for r in rows
    ]


def group_member_ids(conn, filename, camera_name):
    """Return a duplicate group's member ids in tie-break order.

    Character-identical to database._find_existing_video_row()'s ORDER BY
    expression — do not re-derive or "improve" this ordering; PROJECT.md
    records "prefer live-filepath row, then lowest id" as confirmed correct
    against production data in Phase 5. `camera_name IS ?` is SQLite's
    NULL-safe equality; `filename = ?` is exact equality, never LIKE, so a
    percent sign or underscore inside a filename cannot behave as a
    wildcard character.
    """
    rows = conn.execute(
        "SELECT id FROM videos WHERE filename = ? AND camera_name IS ? "
        "ORDER BY (filepath IS NULL), id",
        (filename, camera_name),
    ).fetchall()
    return [r["id"] for r in rows]


def child_stats(conn, video_id):
    """Return detections/species/crops/correction counts for one video.

    `video_corrections` is counted directly by video_id; `corrected_species`
    is a separate count of that video's species rows carrying a non-NULL
    corrected_at, since an operator correction can live in either place.
    """
    detections = conn.execute(
        "SELECT COUNT(*) FROM detections WHERE video_id = ?", (video_id,)
    ).fetchone()[0]
    species = conn.execute(
        "SELECT COUNT(*) FROM species s JOIN detections d ON s.detection_id = d.id "
        "WHERE d.video_id = ?", (video_id,)
    ).fetchone()[0]
    crops = conn.execute(
        "SELECT COUNT(*) FROM crops c JOIN detections d ON c.detection_id = d.id "
        "WHERE d.video_id = ?", (video_id,)
    ).fetchone()[0]
    video_corrections = conn.execute(
        "SELECT COUNT(*) FROM video_corrections WHERE video_id = ?", (video_id,)
    ).fetchone()[0]
    corrected_species = conn.execute(
        "SELECT COUNT(*) FROM species s JOIN detections d ON s.detection_id = d.id "
        "WHERE d.video_id = ? AND s.corrected_at IS NOT NULL", (video_id,)
    ).fetchone()[0]
    return {
        "detections": detections,
        "species": species,
        "crops": crops,
        "video_corrections": video_corrections,
        "corrected_species": corrected_species,
    }


def correction_signal(conn, video_id, cache=None):
    """Return True when this video carries an operator correction signal
    from either write path: a species row (joined via detections) with
    non-NULL corrected_at, or any video_corrections row for this video_id.
    Both sources must be consulted — database.correct_species() writes the
    first, the video-correction endpoint writes the second, and checking
    only one silently misses half of the operator's manual labeling.

    When `cache` (a _bulk_prefetch() dict) is supplied, this is an O(1)
    dict lookup instead of two fresh queries — required at production scale
    (~19,291 groups), where the per-call query version issues on the order
    of tens of thousands of statements just for this one check."""
    if cache is not None:
        return _cached_correction_signal(cache, video_id)
    species_row = conn.execute(
        "SELECT 1 FROM species s JOIN detections d ON s.detection_id = d.id "
        "WHERE d.video_id = ? AND s.corrected_at IS NOT NULL LIMIT 1",
        (video_id,),
    ).fetchone()
    if species_row is not None:
        return True
    correction_row = conn.execute(
        "SELECT 1 FROM video_corrections WHERE video_id = ? LIMIT 1",
        (video_id,),
    ).fetchone()
    return correction_row is not None


def group_correction_holders(conn, member_ids, cache=None):
    """Return the subset of member_ids for which correction_signal() is
    True, preserving member_ids' original order."""
    return [vid for vid in member_ids if correction_signal(conn, vid, cache=cache)]


@dataclasses.dataclass
class GroupPlan:
    """One duplicate group's consolidation plan. skipped_reason is always
    empty, reparent_from is always None, and pairing_repoints is always []
    in this tracer slice — 09-03 populates all three (correction
    precedence, the zero-detections re-parent rule, and dual-lens
    paired_video_id repointing). Declaring the fields now lets 09-03 extend
    behavior without reshaping this record."""
    filename: str
    camera_name: Optional[str]
    winner_id: int
    loser_ids: list
    rule: str
    skipped_reason: str
    reparent_from: Optional[int]
    detections: int
    species: int
    crops: int
    corrections: int
    candidate_files: list
    pairing_repoints: list


def select_winner(conn, member_ids, cache=None):
    """Return (winner_id, rule, skipped_reason) for a duplicate group's
    already tie-break-ordered member ids, applying D-01/D-02's correction
    precedence ahead of the default tie-break:

    - Exactly one member carries a correction signal (species.corrected_at
      or a video_corrections row, per correction_signal()) -> that member
      wins outright, rule "correction-precedence", no skip. This overrides
      the ordering result even when the holder sorts last.
    - Two or more distinct members carry a correction signal -> per D-02
      this is not a machine's call to make: the group is not auto-resolved.
      The winner still falls back to the default tie-break pick so a plan
      exists to report, but skipped_reason is set to
      "conflicting-corrections" and apply_group() must leave the group
      exactly as found.
    - Zero holders -> the default tie-break, member_ids[0]. member_ids
      arrives ordered by group_member_ids()'s `ORDER BY (filepath IS NULL),
      id` expression, so the winner is simply its first element — this
      function does not re-sort and does not introduce a second ordering
      expression. PROJECT.md records this tie-break as confirmed correct
      against production data in Phase 5. D-01 requires no extra code here:
      siblings disagreeing on species label or crop quality with no
      correction present fall straight into this branch as normal ML
      scoring variance, not a signal requiring special handling.
    """
    holders = group_correction_holders(conn, member_ids, cache=cache)
    if len(holders) == 1:
        return holders[0], "correction-precedence", ""
    if len(holders) >= 2:
        return member_ids[0], "default-tiebreak", "conflicting-corrections"
    return member_ids[0], "default-tiebreak", ""


def collect_candidate_files(conn, loser_ids, cache=None):
    """Return the distinct, sorted set of file paths the given losers
    reference: every crop_path from crops joined through those videos'
    detections, plus every non-NULL thumbnail_path on the loser videos rows
    themselves. Collected before deletion, because once the rows are gone
    the paths are unrecoverable.

    When `cache` (a _bulk_prefetch() dict) is supplied, this reads
    crop_paths_by_video/thumbnail_by_video instead of issuing two fresh
    queries per group — required at production scale, where the per-group
    query version issues on the order of tens of thousands of statements."""
    if not loser_ids:
        return []
    if cache is not None:
        paths = set()
        for lid in loser_ids:
            paths.update(cache["crop_paths_by_video"].get(lid, []))
            thumb = cache["thumbnail_by_video"].get(lid)
            if thumb is not None:
                paths.add(thumb)
        return sorted(paths)
    placeholders = ",".join("?" for _ in loser_ids)
    paths = set()
    for row in conn.execute(
        f"SELECT c.crop_path FROM crops c "
        f"JOIN detections d ON c.detection_id = d.id "
        f"WHERE d.video_id IN ({placeholders})",
        tuple(loser_ids),
    ):
        paths.add(row["crop_path"])
    for row in conn.execute(
        f"SELECT thumbnail_path FROM videos "
        f"WHERE id IN ({placeholders}) AND thumbnail_path IS NOT NULL",
        tuple(loser_ids),
    ):
        paths.add(row["thumbnail_path"])
    return sorted(paths)


def choose_reparent_source(conn, winner_id, loser_ids, cache=None):
    """Return the loser id to adopt detections from (rule 4), or None.

    Returns None unless the winner's detection count is zero and at least
    one loser's is greater than zero — a winner that already holds any
    detections is never re-parented onto, even if a loser holds more.
    Among qualifying losers, returns the one with the highest detection
    count, breaking ties by lowest id so repeated planning is
    deterministic.

    When `cache` (a _bulk_prefetch() dict) is supplied, detection counts
    come from its detections_count dict instead of one query per member —
    required at production scale, where the per-member query version
    issues on the order of tens of thousands of statements."""
    if cache is not None:
        winner_detections = cache["detections_count"].get(winner_id, 0)
    else:
        winner_detections = conn.execute(
            "SELECT COUNT(*) FROM detections WHERE video_id=?", (winner_id,)
        ).fetchone()[0]
    if winner_detections != 0:
        return None

    candidates = []
    for loser_id in loser_ids:
        if cache is not None:
            count = cache["detections_count"].get(loser_id, 0)
        else:
            count = conn.execute(
                "SELECT COUNT(*) FROM detections WHERE video_id=?", (loser_id,)
            ).fetchone()[0]
        if count > 0:
            candidates.append((count, loser_id))
    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][1]


def reparent_detections(conn, from_video_id, to_video_id):
    """Move every detection from one video onto another via a single
    UPDATE. Does not touch species or crops: both reference detection_id,
    not video_id, so they follow the moved detections automatically without
    being touched. Does not move video_corrections either — that table
    references video_id directly and records the operator's judgment about
    a specific row; a loser carrying a video_corrections row would already
    have been caught by correction_signal() and either won the group
    outright or caused a conflicting-corrections skip, so a re-parent
    source is guaranteed not to carry one."""
    conn.execute(
        "UPDATE detections SET video_id=? WHERE video_id=?",
        (to_video_id, from_video_id),
    )


def collect_pairing_repoints(conn, winner_id, loser_ids, cache=None):
    """Return (row_id, new_target) pairs needed to preserve every
    paired_video_id relationship a group's losers participate in (rule 7),
    checked in both directions per Pitfall 5:

    - any row elsewhere in the table whose paired_video_id references a
      loser is re-pointed at the winner
    - the winner's own paired_video_id, if it references a loser in this
      same group, is cleared to NULL rather than re-pointed at itself — a
      self-reference is meaningless

    Never relies on the paired_video_id column's ON DELETE SET NULL
    default, which would silently break a live pairing with no error.

    When `cache` (a _bulk_prefetch() dict) is supplied, both directions
    are resolved from its paired_referrers/paired_by_video dicts instead
    of two fresh queries per group — required at production scale, where
    the per-group query version issues on the order of tens of thousands
    of statements, and production audit measured groups_with_pairing at 0
    but that reflects today's data shape, not a guarantee for every run."""
    if not loser_ids:
        return []

    exclude_ids = set(loser_ids) | {winner_id}

    if cache is not None:
        repoints = []
        for loser_id in loser_ids:
            for referrer_id in cache["paired_referrers"].get(loser_id, []):
                if referrer_id not in exclude_ids:
                    repoints.append((referrer_id, winner_id))
        winner_target = cache["paired_by_video"].get(winner_id)
        if winner_target in loser_ids:
            repoints.append((winner_id, None))
        return repoints

    placeholders = ",".join("?" for _ in loser_ids)
    exclude_placeholders = ",".join("?" for _ in exclude_ids)

    repoints = []
    for row in conn.execute(
        f"SELECT id FROM videos WHERE paired_video_id IN ({placeholders}) "
        f"AND id NOT IN ({exclude_placeholders})",
        tuple(loser_ids) + tuple(exclude_ids),
    ):
        repoints.append((row["id"], winner_id))

    winner_row = conn.execute(
        "SELECT paired_video_id FROM videos WHERE id=?", (winner_id,)
    ).fetchone()
    if winner_row is not None and winner_row["paired_video_id"] in loser_ids:
        repoints.append((winner_id, None))

    return repoints


def apply_pairing_repoints(conn, repoints):
    """Apply the (row_id, new_target) pairs collect_pairing_repoints()
    computed, one UPDATE per pair. Must run inside the group's transaction
    before any loser row is deleted."""
    for row_id, new_target in repoints:
        conn.execute(
            "UPDATE videos SET paired_video_id=? WHERE id=?", (new_target, row_id)
        )


def plan_group(conn, filename, camera_name, cache=None):
    """Build and return a GroupPlan with no side effects at all — safe to
    call in dry-run mode against production. Gathers member ids, selects the
    winner, applies the child-handling rules (4/5/6) and the pairing-repoint
    rule (7) when no skip is already set, sums each loser's child_stats()
    into the count fields, and collects candidate_files via
    collect_candidate_files().

    loser_ids is every member id that is not the winner — computed by
    exclusion rather than member_ids[1:], because correction-precedence
    (D-02) can select a winner that does not sort first in tie-break order,
    and member_ids[1:] would then wrongly still count the true default-first
    member as a loser to delete while leaving the real loser undeleted.

    When `cache` (a _bulk_prefetch() dict, built once by the caller) is
    supplied, every per-video lookup below reads from it instead of issuing
    a fresh query, and the rule-5 check's stats are reused for the final
    per-loser summary loop rather than recomputed. This is required at
    production scale: called once per duplicate group (~19,291 in
    production), the uncached version issues on the order of hundreds of
    thousands of individual SQL statements across select_winner(),
    choose_reparent_source(), the two child_stats() call sites, and
    collect_pairing_repoints() — the same N+1 shape 09-01 found and fixed
    in run_audit(), but in this function, only ever exercised against small
    fixtures before the 09-04 rehearsal ran it at real production scale."""
    member_ids = group_member_ids(conn, filename, camera_name)
    winner_id, rule, skipped_reason = select_winner(conn, member_ids, cache=cache)
    loser_ids = [vid for vid in member_ids if vid != winner_id]

    def _stats(vid):
        return _cached_child_stats(cache, vid) if cache is not None else child_stats(conn, vid)

    reparent_from = None
    loser_stats_by_id = {}
    if not skipped_reason:
        reparent_from = choose_reparent_source(conn, winner_id, loser_ids, cache=cache)
        if reparent_from is not None:
            # Rule 4: the winner holds zero detections but the richest
            # loser holds some — adopt that loser's detections onto the
            # winner rather than discarding the only detection data the
            # physical file has.
            rule = f"{rule}+reparent"
        else:
            winner_stats = _stats(winner_id)
            loser_stats_by_id = {lid: _stats(lid) for lid in loser_ids}
            if (
                winner_stats["detections"] > 0
                and winner_stats["crops"] == 0
                and any(
                    s["detections"] > 0 and s["crops"] > 0
                    for s in loser_stats_by_id.values()
                )
            ):
                # Rule 5 (hazard H-2, see 09-03-PLAN.md <why_rule_5_skips>):
                # the winner holds detections but zero crops while a loser's
                # detections hold crops — a downstream INSERT OR REPLACE
                # migrated the shared crop_path onto a later duplicate's
                # detection_id, leaving the winner crop-less and a loser
                # holding every crop the file has. Neither automatic
                # resolution is safe: deleting the loser destroys the only
                # crops for that physical file (its gallery tiles vanish);
                # re-parenting the loser's detections onto the winner
                # duplicates the same animal in the gallery; and choosing
                # the loser as winner instead would mean re-litigating the
                # D-01 tie-break, which is outside this phase's authority.
                # Skipping leaves the group exactly as production has it
                # today — no better, no worse, no data destroyed — and
                # reports it so the operator sees the size of the
                # unconsolidated remainder at the D-05 go/no-go gate.
                skipped_reason = "winner-crops-migrated"

    pairing_repoints = []
    if not skipped_reason:
        pairing_repoints = collect_pairing_repoints(conn, winner_id, loser_ids, cache=cache)

    detections = species = crops = corrections = 0
    for loser_id in loser_ids:
        # Reuse the rule-5 check's stats when they were already computed
        # for this loser (avoids calling child_stats()/_cached_child_stats()
        # a second time for the same video); otherwise compute fresh —
        # covers both the reparent-triggered branch (never populated
        # loser_stats_by_id) and any group already skipped before that
        # branch ran (conflicting-corrections), where the totals are still
        # needed for the report even though nothing will be deleted.
        stats = loser_stats_by_id[loser_id] if loser_id in loser_stats_by_id else _stats(loser_id)
        detections += stats["detections"]
        species += stats["species"]
        crops += stats["crops"]
        corrections += stats["video_corrections"]

    # A skipped group is never touched, so it has nothing to clean up —
    # collecting candidate files for it would be meaningless work and could
    # confuse the report into implying files are slated for removal. A
    # re-parent source's crop paths stay live under the winner, so they are
    # excluded from the candidate set even though the group itself proceeds.
    if skipped_reason:
        candidate_files = []
    else:
        cleanup_loser_ids = [lid for lid in loser_ids if lid != reparent_from]
        candidate_files = collect_candidate_files(conn, cleanup_loser_ids, cache=cache)

    return GroupPlan(
        filename=filename,
        camera_name=camera_name,
        winner_id=winner_id,
        loser_ids=loser_ids,
        rule=rule,
        skipped_reason=skipped_reason,
        reparent_from=reparent_from,
        detections=detections,
        species=species,
        crops=crops,
        corrections=corrections,
        candidate_files=candidate_files,
        pairing_repoints=pairing_repoints,
    )


def apply_group(conn, plan, cache=None):
    """Delete a group's loser rows and their full child graph on the
    caller's open connection, so the caller controls the transaction
    boundary. Deletes in foreign-key dependency order — crops, species,
    detections, video_corrections, then the videos row itself — because
    get_conn() sets PRAGMA foreign_keys=ON and none of these child tables
    declares ON DELETE CASCADE; removing a parent before its children raises
    sqlite3.IntegrityError (Pitfall 2's safety backstop, never routed
    around). Returns the actual row counts removed per table, measured via
    cursor.rowcount, so the audit log records measured values rather than
    the plan's projections.

    A skipped group (plan.skipped_reason set) returns immediately with no
    statement issued at all — not even a no-op one. A skipped group must
    leave production exactly as found.

    When plan.reparent_from is set, that loser's crops/species/detections
    rows are excluded from the deletion set — its detections were moved
    onto the winner (by reparent_detections(), which the caller must run
    before this function, in the same transaction), not removed, so
    deleting them here would delete data that now belongs to the winner.
    Its video_corrections row (should be none — a re-parent source can't
    hold one, see reparent_detections()'s docstring) and its now-empty
    videos row are still removed, same as any other loser.

    When `cache` (a _bulk_prefetch() dict, built once before any write in
    this run) is supplied, crops/species are deleted by their own
    primary-key `id` (from cache's crop_ids_by_video/species_ids_by_video)
    instead of `detection_id IN (SELECT id FROM detections WHERE
    video_id=?)`. Required at production scale: crops.detection_id and
    species.detection_id carry no index (only detections.video_id does —
    see database.py's SCHEMA), so the subquery form is a full table scan
    of crops/species for every single loser processed. Measured during the
    09-04 rehearsal at roughly 0.75s per group (~1200 unindexed DELETE
    subqueries per --batch-size 500 batch), which would have projected to
    hours for the full population and directly determines how long
    production services must stay stopped in Task 3. Deleting by primary
    key sidesteps the missing secondary index entirely without adding one
    to the shared production schema. Safe to use a cache snapshotted
    before this run's writes started: each video belongs to exactly one
    duplicate group, so no other code path in this run touches the same
    loser's children between the snapshot and this delete."""
    counts = {"crops": 0, "species": 0, "detections": 0, "video_corrections": 0, "videos": 0}
    if plan.skipped_reason:
        return counts
    for loser_id in plan.loser_ids:
        if loser_id != plan.reparent_from:
            if cache is not None:
                crop_ids = cache["crop_ids_by_video"].get(loser_id, [])
                if crop_ids:
                    placeholders = ",".join("?" for _ in crop_ids)
                    cur = conn.execute(
                        f"DELETE FROM crops WHERE id IN ({placeholders})", tuple(crop_ids)
                    )
                    counts["crops"] += cur.rowcount
                species_ids = cache["species_ids_by_video"].get(loser_id, [])
                if species_ids:
                    placeholders = ",".join("?" for _ in species_ids)
                    cur = conn.execute(
                        f"DELETE FROM species WHERE id IN ({placeholders})", tuple(species_ids)
                    )
                    counts["species"] += cur.rowcount
            else:
                cur = conn.execute(
                    "DELETE FROM crops WHERE detection_id IN "
                    "(SELECT id FROM detections WHERE video_id=?)",
                    (loser_id,),
                )
                counts["crops"] += cur.rowcount
                cur = conn.execute(
                    "DELETE FROM species WHERE detection_id IN "
                    "(SELECT id FROM detections WHERE video_id=?)",
                    (loser_id,),
                )
                counts["species"] += cur.rowcount
            cur = conn.execute("DELETE FROM detections WHERE video_id=?", (loser_id,))
            counts["detections"] += cur.rowcount
        cur = conn.execute("DELETE FROM video_corrections WHERE video_id=?", (loser_id,))
        counts["video_corrections"] += cur.rowcount
        cur = conn.execute("DELETE FROM videos WHERE id=?", (loser_id,))
        counts["videos"] += cur.rowcount
    return counts


def _still_referenced_paths(conn, paths):
    """Return the subset of `paths` that at least one live row currently
    references (a crops.crop_path match or a videos.thumbnail_path match),
    via two batched IN-clause queries instead of one query per path.

    Found necessary during the 09-04 full-scale rehearsal: neither
    videos.thumbnail_path nor (implicitly, through the JOIN) the per-path
    lookup pattern is backed by an index that makes a single-path query
    cheap at production scale (crops.crop_path is UNIQUE and so has an
    implicit index, but videos.thumbnail_path has none — see database.py's
    SCHEMA), so a per-candidate-path query against the live videos table
    (93k+ rows) is a full table scan every time. Across a --batch-size 500
    run this meant ~500 full scans per batch; the first two real batches of
    the rehearsal took roughly 3 minutes each before this fix, projecting
    to over an hour for the full population — directly determines how long
    production services must stay stopped in Task 3, so it is fixed here
    rather than left as a rehearsal-only inconvenience.

    Chunks the IN clause at 500 placeholders per query, well under
    SQLite's default SQLITE_MAX_VARIABLE_NUMBER, so this stays correct
    regardless of how large a single cleanup_files() call's candidate list
    is (batch-size is caller-configurable, not fixed at 500)."""
    unique_paths = list(dict.fromkeys(paths))  # de-dupe, preserve order
    if not unique_paths:
        return set()

    still_referenced = set()
    chunk_size = 500
    for start in range(0, len(unique_paths), chunk_size):
        chunk = unique_paths[start:start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT crop_path FROM crops WHERE crop_path IN ({placeholders})",
            tuple(chunk),
        ):
            still_referenced.add(row["crop_path"])
        for row in conn.execute(
            f"SELECT thumbnail_path FROM videos WHERE thumbnail_path IN ({placeholders})",
            tuple(chunk),
        ):
            still_referenced.add(row["thumbnail_path"])
    return still_referenced


def cleanup_files(conn, candidate_paths, quarantine_dir=None):
    """Call only after the group's transaction has committed, never inside
    it — this guard re-queries live database state, so running it
    pre-commit would read stale (pre-delete) rows and reach the wrong
    answer. For each candidate path, first check whether any surviving row
    still references it (via _still_referenced_paths(), batched rather
    than one query per path — see that function's docstring); if so, skip
    removal entirely. This is the mitigation for hazard H-1:
    extract_thumbnail() derives its output path from camera name and
    filename alone with no uniquifier, so every duplicate sibling of one
    physical file stores the identical thumbnail_path string, and removing
    a loser's copy would delete the file the surviving winner still points
    at (measured at ~98.7% of production groups per 09-01-SUMMARY.md).

    For a path with no surviving reference: move it to quarantine_dir
    (reversible) when set, otherwise os.remove() it. Mirrors
    purge_video_file()'s error-handling style — Path.exists() before
    removal, try/except OSError around the removal itself, log and continue
    — so a missing or already-removed file never aborts the run. Returns
    counts of removed/retained/failed plus a per-path outcome list for the
    audit log."""
    removed = retained = failed = 0
    details = []
    seen = set()
    still_referenced_paths = _still_referenced_paths(conn, candidate_paths)
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)

        if path in still_referenced_paths:
            retained += 1
            details.append({"path": path, "outcome": "retained"})
            continue

        p = Path(path)
        if not p.exists():
            removed += 1
            details.append({"path": path, "outcome": "already-missing"})
            continue

        try:
            if quarantine_dir:
                os.makedirs(quarantine_dir, exist_ok=True)
                shutil.move(str(p), os.path.join(quarantine_dir, p.name))
                outcome = "quarantined"
            else:
                p.unlink()
                outcome = "removed"
            removed += 1
            details.append({"path": path, "outcome": outcome})
        except OSError as exc:
            failed += 1
            details.append({"path": path, "outcome": "failed", "error": str(exc)})

    return {"removed": removed, "retained": retained, "failed": failed, "details": details}


def snapshot_db(src_path, dest_path):
    """Take an online backup of the database at src_path into dest_path
    using sqlite3.Connection.backup(). A plain file copy is wrong here:
    get_conn() sets PRAGMA journal_mode=WAL, and copying the main database
    file alone can miss data still sitting in the -wal file. Callers must
    treat a raised exception here as a hard abort before any write pass —
    the snapshot is the only rollback path for the database half of the
    operation."""
    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    src_conn = sqlite_connect(src_path)
    try:
        dest_conn = sqlite_connect(dest_path)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


def verify_post_conditions(conn):
    """Return fk_violations (row count from PRAGMA foreign_key_check),
    broken_pairings (database.check_pairing_consistency(), not
    re-implemented here), and remaining_groups (find_duplicate_groups()
    count). Callers must treat a non-zero fk_violations as a hard abort, not
    a warning — and a non-zero broken_pairings just as prominently: it means
    a dual-lens pairing broke during the run, exactly the silent-data-loss
    mode Pitfall 5 exists to catch."""
    fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    broken_pairings = database.check_pairing_consistency()
    remaining_groups = len(find_duplicate_groups(conn))
    return {
        "fk_violations": fk_violations,
        "broken_pairings": broken_pairings,
        "remaining_groups": remaining_groups,
    }


def open_audit_log(path):
    """Open a JSON-lines file for append. Raises OSError (uncaught) if the
    path can't be opened — callers in apply mode must treat that as a hard
    abort before any write: an unlogged irreversible operation cannot be
    reconstructed from the snapshot."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return open(path, "a", encoding="utf-8")


def write_audit_line(handle, payload):
    """Write one compact JSON object per line and flush immediately, so a
    crash mid-run still leaves every completed group's line durable on
    disk."""
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
    handle.flush()


def render_plan_report(plans, as_json=False):
    """Print the per-group plan and a totals block. In dry-run this is the
    whole output."""
    if as_json:
        print(json.dumps([dataclasses.asdict(p) for p in plans], indent=2))
        return

    print("Dedup Backfill Consolidation Plan (dry-run unless --apply --confirm-irreversible)")
    print("=" * 44)
    if not plans:
        print("No duplicate groups found.")
        return
    for p in plans:
        print(
            f"{p.filename} (camera={p.camera_name}): "
            f"winner={p.winner_id} losers={p.loser_ids} rule={p.rule}"
            + (f" skipped={p.skipped_reason}" if p.skipped_reason else "")
        )
        print(
            f"    detections={p.detections} species={p.species} "
            f"crops={p.crops} corrections={p.corrections} "
            f"candidate_files={len(p.candidate_files)}"
        )
    print("-" * 44)
    correction_groups = sum(1 for p in plans if p.rule == "correction-precedence")
    skipped_groups = sum(1 for p in plans if p.skipped_reason)
    print(f"Total groups: {len(plans)}")
    print(f"Total loser rows: {sum(len(p.loser_ids) for p in plans)}")
    print(f"Groups resolved by correction-precedence: {correction_groups}")
    print(f"Groups skipped: {skipped_groups}")


def render_skipped_report(plans, as_json=False):
    """List every skipped group's identity and reason — the manual-review
    queue Pitfall 3 requires the operator see before authorizing the real
    run. Groups with no skipped_reason are omitted entirely."""
    skipped = [p for p in plans if p.skipped_reason]
    if as_json:
        print(json.dumps(
            [
                {
                    "filename": p.filename,
                    "camera_name": p.camera_name,
                    "member_ids": [p.winner_id] + p.loser_ids,
                    "reason": p.skipped_reason,
                }
                for p in skipped
            ],
            indent=2,
        ))
        return

    print("Skipped Groups (manual review required)")
    print("=" * 44)
    if not skipped:
        print("No skipped groups.")
        return
    for p in skipped:
        member_ids = [p.winner_id] + p.loser_ids
        print(
            f"{p.filename} (camera={p.camera_name}): "
            f"members={member_ids} reason={p.skipped_reason}"
        )


def iter_batches(items, size):
    """Yield successive slices of items, each up to size elements long, in
    order. The last slice may be shorter. This is the batch-granularity
    commit boundary Pitfall 7's Performance Traps table requires: bounding
    a run to --batch-size groups per transaction rather than one
    transaction spanning the whole population, so a mid-run failure rolls
    back only the in-flight batch and a re-run resumes naturally."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _preview_deletion_counts(conn, plan, cache=None):
    """Read-only projection, for the dry-run report only, of what
    apply_group() would delete for this plan — honoring the same
    reparent-source exclusion apply_group() itself applies. apply_group()
    measures the real counts once it actually runs; this never writes
    anything.

    When `cache` (a _bulk_prefetch() dict) is supplied, per-loser stats
    come from it instead of a fresh child_stats() query — required at
    production scale, called once per consolidated group."""
    counts = {"crops": 0, "species": 0, "detections": 0, "video_corrections": 0, "videos": 0}
    if plan.skipped_reason:
        return counts
    for loser_id in plan.loser_ids:
        stats = _cached_child_stats(cache, loser_id) if cache is not None else child_stats(conn, loser_id)
        if loser_id != plan.reparent_from:
            counts["crops"] += stats["crops"]
            counts["species"] += stats["species"]
            counts["detections"] += stats["detections"]
        counts["video_corrections"] += stats["video_corrections"]
        counts["videos"] += 1
    return counts


def _preview_file_disposition(conn, candidate_paths, deleted_video_ids, cache=None):
    """Read-only preview, for the dry-run report only, of cleanup_files()'s
    disposition: how many distinct candidate paths would be removed versus
    retained because a surviving row still references them. Issues no
    filesystem operation and no database write.

    Unlike cleanup_files() — which only ever runs after a batch's DB
    transaction has committed, so its "still referenced" query already
    reads post-deletion state — this preview runs entirely before any
    write. Naively re-querying the live, not-yet-mutated database for
    "does any row reference this path" would find the candidate's own
    about-to-be-deleted row every time, since it hasn't been deleted yet,
    which would misreport nearly every removable file as "would retain".
    `deleted_video_ids` is this run's full set of video ids that will be
    removed (every consolidated plan's loser_ids, including reparent
    sources); a referrer is only counted as "still referenced" if it
    belongs to a video id NOT in that set — simulating the post-deletion
    state cleanup_files() will actually see, without requiring the writes
    to have happened first.

    When `cache` (a _bulk_prefetch() dict) is supplied, referrers come
    from its crop_referrers/thumbnail_referrers reverse indexes instead of
    one query per distinct candidate path — required at production scale."""
    would_remove = would_retain = 0
    seen = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if cache is not None:
            referrers = cache["crop_referrers"].get(path, []) + cache["thumbnail_referrers"].get(path, [])
            still_referenced = any(vid not in deleted_video_ids for vid in referrers)
        elif deleted_video_ids:
            placeholders = ",".join("?" for _ in deleted_video_ids)
            row = conn.execute(
                f"SELECT 1 FROM crops c JOIN detections d ON c.detection_id = d.id "
                f"WHERE c.crop_path = ? AND d.video_id NOT IN ({placeholders}) "
                f"UNION ALL "
                f"SELECT 1 FROM videos WHERE thumbnail_path = ? AND id NOT IN ({placeholders}) "
                f"LIMIT 1",
                (path, *deleted_video_ids, path, *deleted_video_ids),
            ).fetchone()
            still_referenced = row is not None
        else:
            row = conn.execute(
                "SELECT 1 FROM crops WHERE crop_path = ? "
                "UNION ALL SELECT 1 FROM videos WHERE thumbnail_path = ? LIMIT 1",
                (path, path),
            ).fetchone()
            still_referenced = row is not None
        if still_referenced:
            would_retain += 1
        else:
            would_remove += 1
    return {"would_remove": would_remove, "would_retain": would_retain}


def build_summary_report(conn, plans, groups_discovered, cache=None):
    """Compute the full go/no-go summary (D-05) from a set of GroupPlans and
    the total discovered-group count (before --limit). A single dict shared
    by both the human-readable and --json renderings in main(), so the two
    can never drift apart.

    Builds its own `cache` (a _bulk_prefetch() dict) when the caller
    doesn't supply one, so this function is always fast on its own; main()
    passes through the same cache it already built for plan_group(), so
    the whole run shares one set of bulk scans rather than repeating them."""
    if cache is None:
        cache = _bulk_prefetch(conn)

    skipped_plans = [p for p in plans if p.skipped_reason]
    consolidated_plans = [p for p in plans if not p.skipped_reason]
    conflicting = [p for p in skipped_plans if p.skipped_reason == "conflicting-corrections"]
    crop_migrated = [p for p in skipped_plans if p.skipped_reason == "winner-crops-migrated"]
    reparented = [p for p in plans if p.reparent_from is not None]
    correction_plans = [p for p in plans if p.rule.startswith("correction-precedence")]

    rows_deleted = {"crops": 0, "species": 0, "detections": 0, "video_corrections": 0, "videos": 0}
    for p in consolidated_plans:
        projected = _preview_deletion_counts(conn, p, cache=cache)
        for key in rows_deleted:
            rows_deleted[key] += projected[key]

    detections_moved = sum(
        _cached_child_stats(cache, p.reparent_from)["detections"] for p in reparented
    )

    # This run's full video-id deletion set (every consolidated plan's
    # losers, including reparent sources — apply_group() deletes a
    # reparent source's own videos row even though its children moved
    # rather than being deleted). Used so the file-disposition preview
    # below simulates the post-deletion state, not the current one.
    deleted_video_ids = {lid for p in consolidated_plans for lid in p.loser_ids}

    all_candidate_files = sorted({
        path for p in consolidated_plans for path in p.candidate_files
    })
    file_disposition = _preview_file_disposition(
        conn, all_candidate_files, deleted_video_ids, cache=cache
    )

    return {
        "groups_discovered": groups_discovered,
        "groups_processed": len(plans),
        "groups_planned_for_consolidation": len(consolidated_plans),
        "groups_skipped": len(skipped_plans),
        "rows_deleted_projected": rows_deleted,
        "groups_correction_precedence": len(correction_plans),
        "corrections_preserved": len(correction_plans),
        "groups_skipped_conflicting_corrections": len(conflicting),
        "groups_skipped_winner_crops_migrated": len(crop_migrated),
        "groups_reparented": len(reparented),
        "detections_moved_by_reparenting": detections_moved,
        "pairings_repointed": sum(len(p.pairing_repoints) for p in plans),
        "files_would_remove": file_disposition["would_remove"],
        "files_would_retain": file_disposition["would_retain"],
    }


def _top_species_label(conn, video_id):
    """Return the label of the video's highest-confidence species row (ties
    broken by lowest species id), or None if the video has no species rows."""
    row = conn.execute(
        "SELECT s.label FROM species s JOIN detections d ON s.detection_id = d.id "
        "WHERE d.video_id = ? ORDER BY s.confidence DESC, s.id ASC LIMIT 1",
        (video_id,),
    ).fetchone()
    return row["label"] if row else None


def _bulk_prefetch(conn):
    """Precompute every per-video lookup run_audit() needs in a handful of
    full-table/GROUP BY queries, so the per-group loop below does O(1) dict
    lookups instead of issuing several fresh queries for every member of
    every group. At production scale (~64k duplicate member rows across
    ~19,291 groups) the naive per-row query pattern this replaces issues on
    the order of 500,000+ individual SQL statements — correct, but far too
    slow for an audit meant to inform a go/no-go decision. This function
    computes the identical values via ~8 bulk scans instead."""
    # detection_ids_by_video/crop_ids_by_video/species_ids_by_video (added
    # alongside the pre-existing *_count dicts, same scans, no extra
    # queries) exist so apply_group() can delete crops/species by their own
    # primary-key `id` instead of `detection_id IN (subquery)` — see
    # apply_group()'s docstring for why: crops.detection_id and
    # species.detection_id carry no index (only detections.video_id does),
    # so the subquery form is a full table scan of crops/species for every
    # single loser processed. At production scale this measured at roughly
    # 0.75s per group during the 09-04 rehearsal (~1200 unindexed DELETE
    # subqueries per --batch-size 500 batch), projecting to hours for the
    # full population. Deleting by primary-key id avoids the missing
    # secondary index entirely, without adding one to the shared production
    # schema.
    detections_count = {}
    detection_ids_by_video = {}
    for row in conn.execute("SELECT id, video_id FROM detections"):
        vid = row["video_id"]
        detections_count[vid] = detections_count.get(vid, 0) + 1
        detection_ids_by_video.setdefault(vid, []).append(row["id"])

    species_count = {}
    corrected_species_count = {}
    species_ids_by_video = {}
    for row in conn.execute(
        "SELECT s.id AS id, s.corrected_at AS corrected_at, d.video_id AS video_id "
        "FROM species s JOIN detections d ON s.detection_id = d.id"
    ):
        vid = row["video_id"]
        species_count[vid] = species_count.get(vid, 0) + 1
        species_ids_by_video.setdefault(vid, []).append(row["id"])
        if row["corrected_at"] is not None:
            corrected_species_count[vid] = corrected_species_count.get(vid, 0) + 1

    crops_count = {}
    crop_paths_by_video = {}
    crop_ids_by_video = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, c.id AS id, c.crop_path AS crop_path FROM crops c "
        "JOIN detections d ON c.detection_id = d.id"
    ):
        vid = row["video_id"]
        crops_count[vid] = crops_count.get(vid, 0) + 1
        crop_paths_by_video.setdefault(vid, []).append(row["crop_path"])
        crop_ids_by_video.setdefault(vid, []).append(row["id"])

    video_corrections_count = {}
    for row in conn.execute(
        "SELECT video_id, COUNT(*) AS n FROM video_corrections GROUP BY video_id"
    ):
        video_corrections_count[row["video_id"]] = row["n"]

    thumbnail_by_video = {}
    paired_by_video = {}
    paired_ref_counts = {}
    paired_referrers = {}
    for row in conn.execute("SELECT id, thumbnail_path, paired_video_id FROM videos"):
        vid = row["id"]
        thumbnail_by_video[vid] = row["thumbnail_path"]
        paired_by_video[vid] = row["paired_video_id"]
        if row["paired_video_id"] is not None:
            target = row["paired_video_id"]
            paired_ref_counts[target] = paired_ref_counts.get(target, 0) + 1
            paired_referrers.setdefault(target, []).append(vid)

    top_label_by_video = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, s.label AS label FROM species s "
        "JOIN detections d ON s.detection_id = d.id "
        "ORDER BY d.video_id, s.confidence DESC, s.id ASC"
    ):
        vid = row["video_id"]
        if vid not in top_label_by_video:
            top_label_by_video[vid] = row["label"]

    # Reverse indexes for crop_path/thumbnail_path -> referring video ids,
    # built by inverting crop_paths_by_video/thumbnail_by_video in Python
    # (no extra SQL query) rather than issuing a lookup query per candidate
    # path. Used by _preview_file_disposition() so a pre-apply dry-run can
    # tell whether a path would still be referenced after this run's planned
    # deletions, not just whether it's referenced by the current
    # (pre-deletion) row set — see that function's docstring for why the
    # distinction matters.
    crop_referrers = {}
    for vid, paths in crop_paths_by_video.items():
        for path in paths:
            crop_referrers.setdefault(path, []).append(vid)
    thumbnail_referrers = {}
    for vid, path in thumbnail_by_video.items():
        if path is not None:
            thumbnail_referrers.setdefault(path, []).append(vid)

    return {
        "detections_count": detections_count,
        "species_count": species_count,
        "crops_count": crops_count,
        "crop_paths_by_video": crop_paths_by_video,
        "video_corrections_count": video_corrections_count,
        "corrected_species_count": corrected_species_count,
        "thumbnail_by_video": thumbnail_by_video,
        "paired_by_video": paired_by_video,
        "paired_ref_counts": paired_ref_counts,
        "paired_referrers": paired_referrers,
        "top_label_by_video": top_label_by_video,
        "crop_referrers": crop_referrers,
        "thumbnail_referrers": thumbnail_referrers,
        "detection_ids_by_video": detection_ids_by_video,
        "crop_ids_by_video": crop_ids_by_video,
        "species_ids_by_video": species_ids_by_video,
    }


def _cached_child_stats(cache, video_id):
    """Cache-backed equivalent of child_stats() for a single video, using
    _bulk_prefetch()'s dicts instead of five fresh queries. Same return
    shape as child_stats() so callers can switch between the two
    interchangeably based on whether a cache is available."""
    return {
        "detections": cache["detections_count"].get(video_id, 0),
        "species": cache["species_count"].get(video_id, 0),
        "crops": cache["crops_count"].get(video_id, 0),
        "video_corrections": cache["video_corrections_count"].get(video_id, 0),
        "corrected_species": cache["corrected_species_count"].get(video_id, 0),
    }


def _cached_correction_signal(cache, video_id):
    """Cache-backed equivalent of correction_signal() for a single video."""
    return (
        cache["corrected_species_count"].get(video_id, 0) > 0
        or cache["video_corrections_count"].get(video_id, 0) > 0
    )


def run_audit(conn):
    """Walk every duplicate group and accumulate the read-only shape metrics
    that drive the operator's go/no-go decision (D-05) and the 09-03
    consolidation rules. Performs no writes.

    Uses _bulk_prefetch() rather than child_stats() in the per-group loop —
    child_stats() remains available as a correct single-video lookup for
    future callers (e.g. 09-02/09-03 processing one group's winner at a
    time), but run_audit() itself needs O(1) lookups across every duplicate
    member row to stay fast at production scale.
    """
    groups = find_duplicate_groups(conn)
    cache = _bulk_prefetch(conn)

    metrics = {
        "groups": len(groups),
        "duplicate_rows": 0,
        "group_size_histogram": {},
        "groups_with_corrections": 0,
        "groups_with_conflicting_corrections": 0,
        "groups_winner_zero_detections": 0,
        "groups_winner_zero_crops": 0,
        "groups_shared_thumbnail": 0,
        "groups_with_pairing": 0,
        "crop_paths_on_losers": 0,
        "crop_paths_also_on_survivors": 0,
        "disagreement_groups": 0,
        "total_detections": conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0],
        "total_species": conn.execute("SELECT COUNT(*) FROM species").fetchone()[0],
        "total_crops": conn.execute("SELECT COUNT(*) FROM crops").fetchone()[0],
        "total_video_corrections": conn.execute(
            "SELECT COUNT(*) FROM video_corrections"
        ).fetchone()[0],
    }

    all_loser_crop_paths = set()
    all_survivor_crop_paths = set()

    def _stats(vid):
        return {
            "detections": cache["detections_count"].get(vid, 0),
            "species": cache["species_count"].get(vid, 0),
            "crops": cache["crops_count"].get(vid, 0),
            "video_corrections": cache["video_corrections_count"].get(vid, 0),
            "corrected_species": cache["corrected_species_count"].get(vid, 0),
        }

    for group in groups:
        member_ids = group_member_ids(conn, group["filename"], group["camera_name"])
        n = len(member_ids)
        metrics["duplicate_rows"] += n
        metrics["group_size_histogram"][n] = metrics["group_size_histogram"].get(n, 0) + 1

        winner_id = member_ids[0]
        loser_ids = member_ids[1:]

        stats_by_id = {vid: _stats(vid) for vid in member_ids}

        correction_members = [
            vid for vid, s in stats_by_id.items()
            if s["video_corrections"] > 0 or s["corrected_species"] > 0
        ]
        if correction_members:
            metrics["groups_with_corrections"] += 1
        if len(correction_members) >= 2:
            metrics["groups_with_conflicting_corrections"] += 1

        winner_stats = stats_by_id[winner_id]
        loser_stats = [stats_by_id[vid] for vid in loser_ids]

        if winner_stats["detections"] == 0 and any(s["detections"] > 0 for s in loser_stats):
            metrics["groups_winner_zero_detections"] += 1

        if (
            winner_stats["detections"] > 0
            and winner_stats["crops"] == 0
            and any(s["detections"] > 0 and s["crops"] > 0 for s in loser_stats)
        ):
            metrics["groups_winner_zero_crops"] += 1

        thumbnail_counts = {}
        for vid in member_ids:
            path = cache["thumbnail_by_video"].get(vid)
            if path is not None:
                thumbnail_counts[path] = thumbnail_counts.get(path, 0) + 1
        if any(count >= 2 for count in thumbnail_counts.values()):
            metrics["groups_shared_thumbnail"] += 1

        has_pairing = False
        for vid in member_ids:
            if cache["paired_by_video"].get(vid) is not None:
                has_pairing = True
            if cache["paired_ref_counts"].get(vid, 0) > 0:
                has_pairing = True
        if has_pairing:
            metrics["groups_with_pairing"] += 1

        for vid in loser_ids:
            for path in cache["crop_paths_by_video"].get(vid, []):
                all_loser_crop_paths.add(path)

        for path in cache["crop_paths_by_video"].get(winner_id, []):
            all_survivor_crop_paths.add(path)

        top_labels = {
            cache["top_label_by_video"][vid]
            for vid in member_ids
            if vid in cache["top_label_by_video"]
        }
        if len(top_labels) >= 2:
            metrics["disagreement_groups"] += 1

    metrics["crop_paths_on_losers"] = len(all_loser_crop_paths)
    metrics["crop_paths_also_on_survivors"] = len(
        all_loser_crop_paths & all_survivor_crop_paths
    )

    return metrics


def render_audit_report(metrics, as_json=False):
    """Print the audit report. JSON when as_json is true, one metric per
    line otherwise. D-05 leaves the exact format to implementer discretion —
    the requirement is that every metric is legible for a go/no-go call."""
    if as_json:
        print(json.dumps(metrics, indent=2))
        return

    print("Dedup Backfill Audit Report (read-only)")
    print("=" * 44)
    for key, value in metrics.items():
        print(f"{key}: {value}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Historical dedup backfill for the videos table"
    )
    parser.add_argument("--db", default="data/wildlife.db", help="Path to the SQLite database")
    parser.add_argument("--audit", action="store_true", help="Run the read-only shape audit")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    parser.add_argument(
        "--consolidate", action="store_true",
        help="Plan (and, with --apply --confirm-irreversible, apply) duplicate-group "
             "consolidation. Prints a dry-run plan and writes nothing unless both "
             "write flags are given.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform real writes/deletes — requires --confirm-irreversible too "
             "(default: dry-run plan only, nothing written)",
    )
    parser.add_argument(
        "--confirm-irreversible", action="store_true",
        help="Required together with --apply to actually delete rows/files. Two "
             "independent flags so no single mistyped or shell-history-recalled "
             "argument can start an irreversible pass.",
    )
    parser.add_argument(
        "--audit-log", default=None,
        help="Path to the JSONL audit log written during --apply (required for --apply)",
    )
    parser.add_argument(
        "--snapshot-dir", default=None,
        help="Directory for the pre-apply online DB backup (required for --apply)",
    )
    parser.add_argument(
        "--quarantine-dir", default=None,
        help="Move removed crop/thumbnail files here instead of permanently deleting them",
    )
    parser.add_argument(
        "--skip-file-cleanup", action="store_true",
        help="Skip the file-cleanup pass entirely during --apply",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Groups per commit boundary during --apply (default: 500). Bounds each "
             "transaction so a mid-run failure rolls back only the in-flight batch.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N duplicate groups this run, in stable (filename, "
             "camera_name) order. For the 09-04 rehearsal: run a small real-data "
             "pass and inspect it before committing to the full population.",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.audit:
        database.set_db_path(args.db)
        with database.get_conn() as conn:
            metrics = run_audit(conn)
        render_audit_report(metrics, as_json=args.json)
        sys.exit(0)

    if not args.consolidate and not args.apply:
        # No recognized mode at all — unchanged help-plus-non-zero-exit
        # fallback from 09-01 (whose only mode flag was --audit).
        parser.print_help()
        sys.exit(1)

    if args.apply and not args.confirm_irreversible:
        print(
            "ERROR: --apply requires --confirm-irreversible. Consolidation deletes "
            "database rows and files; both flags must be passed deliberately so no "
            "single mistyped or shell-history-recalled argument can start an "
            "irreversible pass over production.",
            file=sys.stderr,
        )
        sys.exit(1)

    database.set_db_path(args.db)

    with database.get_conn() as conn:
        all_groups = find_duplicate_groups(conn)
        # --limit caps how many groups this run processes (planning and
        # apply both), in the stable order find_duplicate_groups() now
        # guarantees. groups_discovered in the summary still reflects the
        # unlimited total, so a limited rehearsal run doesn't understate
        # the population size it's rehearsing against.
        groups = all_groups[: args.limit] if args.limit else all_groups
        # Built once per invocation and threaded through every plan_group()
        # call, build_summary_report(), and (below, for --apply) every
        # apply_group() call: at production scale (~19,291 groups) planning
        # without this cache issues on the order of hundreds of thousands
        # of individual SQL statements (the same N+1 shape 09-01 found and
        # fixed in run_audit(), rediscovered here during the 09-04
        # full-scale rehearsal — see that plan's SUMMARY), and apply_group()
        # without it re-hits an unindexed crops.detection_id/
        # species.detection_id scan per loser (also found during that
        # rehearsal). Safe to reuse the same one-time snapshot for the
        # later --apply writes too, even though it's taken before any
        # delete: each video belongs to exactly one duplicate group, so no
        # other step in this run touches a given loser's children between
        # this snapshot and that loser's own (single, one-time) delete —
        # the cached id list for a not-yet-processed loser is exactly what
        # a fresh query would still find at the moment it's actually used.
        cache = _bulk_prefetch(conn)
        plans = [
            plan_group(conn, g["filename"], g["camera_name"], cache=cache) for g in groups
        ]
        summary = build_summary_report(conn, plans, len(all_groups), cache=cache)

    if args.json:
        full_report = {
            "plans": [dataclasses.asdict(p) for p in plans],
            "skipped": [
                {
                    "filename": p.filename,
                    "camera_name": p.camera_name,
                    "member_ids": [p.winner_id] + p.loser_ids,
                    "reason": p.skipped_reason,
                }
                for p in plans if p.skipped_reason
            ],
            "summary": summary,
            "snapshot_dir": args.snapshot_dir,
            "audit_log": args.audit_log,
        }
        print(json.dumps(full_report, indent=2))
    else:
        render_plan_report(plans, as_json=False)
        render_skipped_report(plans, as_json=False)
        print()
        print("Go/No-Go Summary (D-05)")
        print("=" * 44)
        print(f"Groups discovered: {summary['groups_discovered']}")
        print(f"Groups processed this run: {summary['groups_processed']}")
        print(
            f"Groups planned for consolidation: "
            f"{summary['groups_planned_for_consolidation']}"
        )
        print(f"Groups skipped: {summary['groups_skipped']}")
        print(f"  conflicting-corrections: {summary['groups_skipped_conflicting_corrections']}")
        print(f"  winner-crops-migrated: {summary['groups_skipped_winner_crops_migrated']}")
        # P-02: this summary prints BEFORE the `if not args.apply` guard
        # below -- nothing has been snapshotted, batched, or deleted yet at
        # this point, even in apply mode. So apply-mode wording must stay
        # definite future ("will be"), never past tense ("were"): past
        # tense would just be a second, worse version of the inaccuracy
        # FIX-03 exists to remove. Dry-run wording is unchanged (D-07).
        print(f"Rows that {'will be' if args.apply else 'would be'} deleted: {summary['rows_deleted_projected']}")
        print(
            f"Groups resolved by correction-precedence: "
            f"{summary['groups_correction_precedence']}"
        )
        print(f"Corrections preserved on surviving rows: {summary['corrections_preserved']}")
        print(f"Groups resolved by re-parenting: {summary['groups_reparented']}")
        print(f"Detections moved by re-parenting: {summary['detections_moved_by_reparenting']}")
        print(f"Pairings re-pointed: {summary['pairings_repointed']}")
        print(f"Files that {'will be' if args.apply else 'would be'} removed: {summary['files_would_remove']}")
        print(f"Files retained (still referenced): {summary['files_would_retain']}")
        print(f"Resolved snapshot destination: {args.snapshot_dir}")
        print(f"Resolved audit-log path: {args.audit_log}")

    if not args.apply:
        # Dry-run is what you get whenever --apply is absent: the plan was
        # already printed above, and nothing has been written or removed.
        sys.exit(0)

    # From here: args.apply and args.confirm_irreversible are both true —
    # the two independent flags required to reach an irreversible write.
    if not args.snapshot_dir:
        print("ERROR: --apply requires --snapshot-dir; aborting before any write.", file=sys.stderr)
        sys.exit(1)
    if not args.audit_log:
        print("ERROR: --apply requires --audit-log; aborting before any write.", file=sys.stderr)
        sys.exit(1)

    snapshot_path = os.path.join(
        args.snapshot_dir, f"backfill-snapshot-{datetime.now():%Y%m%dT%H%M%S%f}.db"
    )
    try:
        snapshot_db(args.db, snapshot_path)
    except Exception as exc:
        print(f"ERROR: pre-apply snapshot failed, aborting before any write: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        audit_handle = open_audit_log(args.audit_log)
    except OSError as exc:
        print(
            f"ERROR: could not open audit log for writing, aborting before any write: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        for batch in iter_batches(plans, args.batch_size):
            batch_deleted_counts = []
            with database.get_conn() as conn:
                # One get_conn() block per batch, not per group: the whole
                # batch commits or rolls back as a unit (Pitfall 7's
                # Performance Traps table — bounded transactions, not one
                # spanning the whole population). Within the batch, order
                # matters per group: pairing repoints and the re-parent move
                # both land before apply_group()'s deletes touch the same
                # video ids.
                for plan in batch:
                    if plan.pairing_repoints:
                        apply_pairing_repoints(conn, plan.pairing_repoints)
                    if plan.reparent_from is not None:
                        reparent_detections(conn, plan.reparent_from, plan.winner_id)
                    deleted_counts = apply_group(conn, plan, cache=cache)
                    batch_deleted_counts.append(deleted_counts)

            # The batch has committed. Record its candidate file paths to
            # the audit log *before* attempting cleanup — this is the
            # residual gap's mitigation: an interruption between this line
            # and cleanup finishing leaves inert orphaned files, but the log
            # still records exactly which paths were in flight (see
            # 09-03-PLAN.md's must_haves backstop truth and the SUMMARY's
            # "Residual Interruption Gap" section).
            batch_candidate_files = sorted({
                path for plan in batch for path in plan.candidate_files
            })
            write_audit_line(audit_handle, {
                "batch_in_flight": True,
                "candidate_files": batch_candidate_files,
            })

            if args.skip_file_cleanup:
                cleanup_result = {"removed": 0, "retained": 0, "failed": 0, "details": []}
            else:
                with database.get_conn() as conn:
                    # Fresh connection opened after the batch's transaction
                    # exited (committed), so the still-referenced guard
                    # inside cleanup_files() reads post-delete state, never
                    # a stale pre-delete snapshot.
                    cleanup_result = cleanup_files(
                        conn, batch_candidate_files, quarantine_dir=args.quarantine_dir
                    )
            detail_by_path = {d["path"]: d for d in cleanup_result["details"]}

            for plan, deleted_counts in zip(batch, batch_deleted_counts):
                plan_details = [
                    detail_by_path[p] for p in plan.candidate_files if p in detail_by_path
                ]
                plan_cleanup = {"removed": 0, "retained": 0, "failed": 0, "details": plan_details}
                for d in plan_details:
                    if d["outcome"] in ("removed", "quarantined", "already-missing"):
                        plan_cleanup["removed"] += 1
                    elif d["outcome"] == "retained":
                        plan_cleanup["retained"] += 1
                    elif d["outcome"] == "failed":
                        plan_cleanup["failed"] += 1

                write_audit_line(audit_handle, {
                    "filename": plan.filename,
                    "camera_name": plan.camera_name,
                    "winner_id": plan.winner_id,
                    "loser_ids": plan.loser_ids,
                    "rule": plan.rule,
                    "skipped_reason": plan.skipped_reason,
                    "reparent_from": plan.reparent_from,
                    "deleted_counts": deleted_counts,
                    "file_cleanup": plan_cleanup,
                })
    finally:
        audit_handle.close()

    with database.get_conn() as conn:
        verification = verify_post_conditions(conn)
    print("Post-run verification:")
    print(json.dumps(verification, indent=2))

    if verification["fk_violations"] != 0 or verification["broken_pairings"] != 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
