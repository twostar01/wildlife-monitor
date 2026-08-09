"""
backfill_dedup_videos.py — historical dedup backfill for the `videos` table
(audit-only form; the --apply write path lands in 09-02/09-03).

Consolidates the ~19,291 duplicate (filename, camera_name) `videos` row
identities left behind by the pre-v1.1 archive-collision bug (04-DEFERRED.md)
down to one authoritative row per physical file. This script currently
implements only the read-only shape audit (PITFALLS.md Pitfall 1) — it
contains no row-mutating statement and no filesystem-removal call anywhere.

Usage:
    python scripts/backfill_dedup_videos.py --db data/wildlife.db --audit [--json]
"""

import argparse
import json
import sys
from pathlib import Path

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
        description="Historical dedup backfill for the videos table (audit-only in this form)"
    )
    parser.add_argument("--db", default="data/wildlife.db", help="Path to the SQLite database")
    parser.add_argument("--audit", action="store_true", help="Run the read-only shape audit")
    parser.add_argument("--json", action="store_true", help="Emit the audit report as JSON")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.audit:
        # No default action — the destructive --apply mode does not exist in
        # this plan's code at all (lands in 09-02/09-03), so an accidental
        # invocation with no flags must never write anything.
        parser.print_help()
        sys.exit(1)

    database.set_db_path(args.db)
    with database.get_conn() as conn:
        metrics = run_audit(conn)
    render_audit_report(metrics, as_json=args.json)
    sys.exit(0)


if __name__ == "__main__":
    main()
