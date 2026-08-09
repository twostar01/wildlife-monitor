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
    """Return one entry per duplicate (filename, camera_name) identity.

    SQLite's GROUP BY treats all NULL values in a grouped column as one
    group, which matches the NULL-safe `IS` semantics
    database._find_existing_video_row() uses elsewhere — a NULL camera_name
    groups only with other NULL camera_name rows, never with a non-NULL one.
    """
    rows = conn.execute(
        "SELECT filename, camera_name, COUNT(*) AS n FROM videos "
        "GROUP BY filename, camera_name HAVING COUNT(*) > 1"
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


def select_winner(conn, member_ids):
    """Return (winner_id, rule) for a duplicate group's already tie-break-
    ordered member ids. member_ids arrives ordered by group_member_ids()'s
    `ORDER BY (filepath IS NULL), id` expression, so the winner is simply
    its first element — this function does not re-sort and does not
    introduce a second ordering expression. PROJECT.md records this
    tie-break as confirmed correct against production data in Phase 5; D-01
    locks it as the rule for groups whose siblings disagree with no
    correction present. `conn` is accepted (and currently unused) so 09-03
    can extend this signature for correction-precedence lookups without an
    incompatible change."""
    return member_ids[0], "default-tiebreak"


def collect_candidate_files(conn, loser_ids):
    """Return the distinct, sorted set of file paths the given losers
    reference: every crop_path from crops joined through those videos'
    detections, plus every non-NULL thumbnail_path on the loser videos rows
    themselves. Collected before deletion, because once the rows are gone
    the paths are unrecoverable."""
    if not loser_ids:
        return []
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


def plan_group(conn, filename, camera_name):
    """Build and return a GroupPlan with no side effects at all — safe to
    call in dry-run mode against production. Gathers member ids, selects the
    winner, sums each loser's child_stats() into the count fields, and
    collects candidate_files via collect_candidate_files()."""
    member_ids = group_member_ids(conn, filename, camera_name)
    winner_id, rule = select_winner(conn, member_ids)
    loser_ids = list(member_ids[1:])

    detections = species = crops = corrections = 0
    for loser_id in loser_ids:
        stats = child_stats(conn, loser_id)
        detections += stats["detections"]
        species += stats["species"]
        crops += stats["crops"]
        corrections += stats["video_corrections"]

    return GroupPlan(
        filename=filename,
        camera_name=camera_name,
        winner_id=winner_id,
        loser_ids=loser_ids,
        rule=rule,
        skipped_reason="",
        reparent_from=None,
        detections=detections,
        species=species,
        crops=crops,
        corrections=corrections,
        candidate_files=collect_candidate_files(conn, loser_ids),
        pairing_repoints=[],
    )


def apply_group(conn, plan):
    """Delete a group's loser rows and their full child graph on the
    caller's open connection, so the caller controls the transaction
    boundary. Deletes in foreign-key dependency order — crops, species,
    detections, video_corrections, then the videos row itself — because
    get_conn() sets PRAGMA foreign_keys=ON and none of these child tables
    declares ON DELETE CASCADE; removing a parent before its children raises
    sqlite3.IntegrityError (Pitfall 2's safety backstop, never routed
    around). Returns the actual row counts removed per table, measured via
    cursor.rowcount, so the audit log records measured values rather than
    the plan's projections."""
    counts = {"crops": 0, "species": 0, "detections": 0, "video_corrections": 0, "videos": 0}
    for loser_id in plan.loser_ids:
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


def cleanup_files(conn, candidate_paths, quarantine_dir=None):
    """Call only after the group's transaction has committed, never inside
    it — this guard re-queries live database state, so running it
    pre-commit would read stale (pre-delete) rows and reach the wrong
    answer. For each candidate path, first check whether any surviving row
    still references it (a crops.crop_path match or a videos.thumbnail_path
    match); if so, skip removal entirely. This is the mitigation for hazard
    H-1: extract_thumbnail() derives its output path from camera name and
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
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)

        still_referenced = conn.execute(
            "SELECT 1 FROM crops WHERE crop_path = ? "
            "UNION ALL "
            "SELECT 1 FROM videos WHERE thumbnail_path = ? "
            "LIMIT 1",
            (path, path),
        ).fetchone()
        if still_referenced is not None:
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
    count). Callers must treat a non-zero fk_violations as a hard abort,
    not a warning."""
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
        )
        print(
            f"    detections={p.detections} species={p.species} "
            f"crops={p.crops} corrections={p.corrections} "
            f"candidate_files={len(p.candidate_files)}"
        )
    print("-" * 44)
    print(f"Total groups: {len(plans)}")
    print(f"Total loser rows: {sum(len(p.loser_ids) for p in plans)}")


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
    detections_count = {}
    for row in conn.execute("SELECT video_id, COUNT(*) AS n FROM detections GROUP BY video_id"):
        detections_count[row["video_id"]] = row["n"]

    species_count = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, COUNT(*) AS n FROM species s "
        "JOIN detections d ON s.detection_id = d.id GROUP BY d.video_id"
    ):
        species_count[row["video_id"]] = row["n"]

    crops_count = {}
    crop_paths_by_video = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, c.crop_path AS crop_path FROM crops c "
        "JOIN detections d ON c.detection_id = d.id"
    ):
        vid = row["video_id"]
        crops_count[vid] = crops_count.get(vid, 0) + 1
        crop_paths_by_video.setdefault(vid, []).append(row["crop_path"])

    video_corrections_count = {}
    for row in conn.execute(
        "SELECT video_id, COUNT(*) AS n FROM video_corrections GROUP BY video_id"
    ):
        video_corrections_count[row["video_id"]] = row["n"]

    corrected_species_count = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, COUNT(*) AS n FROM species s "
        "JOIN detections d ON s.detection_id = d.id "
        "WHERE s.corrected_at IS NOT NULL GROUP BY d.video_id"
    ):
        corrected_species_count[row["video_id"]] = row["n"]

    thumbnail_by_video = {}
    paired_by_video = {}
    paired_ref_counts = {}
    for row in conn.execute("SELECT id, thumbnail_path, paired_video_id FROM videos"):
        vid = row["id"]
        thumbnail_by_video[vid] = row["thumbnail_path"]
        paired_by_video[vid] = row["paired_video_id"]
        if row["paired_video_id"] is not None:
            paired_ref_counts[row["paired_video_id"]] = (
                paired_ref_counts.get(row["paired_video_id"], 0) + 1
            )

    top_label_by_video = {}
    for row in conn.execute(
        "SELECT d.video_id AS video_id, s.label AS label FROM species s "
        "JOIN detections d ON s.detection_id = d.id "
        "ORDER BY d.video_id, s.confidence DESC, s.id ASC"
    ):
        vid = row["video_id"]
        if vid not in top_label_by_video:
            top_label_by_video[vid] = row["label"]

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
        "top_label_by_video": top_label_by_video,
    }


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
        groups = find_duplicate_groups(conn)
        plans = [plan_group(conn, g["filename"], g["camera_name"]) for g in groups]

    render_plan_report(plans, as_json=args.json)

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
        for plan in plans:
            with database.get_conn() as conn:
                deleted_counts = apply_group(conn, plan)

            if args.skip_file_cleanup:
                cleanup_result = {"removed": 0, "retained": 0, "failed": 0, "details": []}
            else:
                with database.get_conn() as conn:
                    cleanup_result = cleanup_files(
                        conn, plan.candidate_files, quarantine_dir=args.quarantine_dir
                    )

            write_audit_line(audit_handle, {
                "filename": plan.filename,
                "camera_name": plan.camera_name,
                "winner_id": plan.winner_id,
                "loser_ids": plan.loser_ids,
                "rule": plan.rule,
                "skipped_reason": plan.skipped_reason,
                "deleted_counts": deleted_counts,
                "file_cleanup": cleanup_result,
            })
    finally:
        audit_handle.close()

    with database.get_conn() as conn:
        verification = verify_post_conditions(conn)
    print("Post-run verification:")
    print(json.dumps(verification, indent=2))

    if verification["fk_violations"] != 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
