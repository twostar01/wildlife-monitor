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


def run_audit(conn):
    """Walk every duplicate group and accumulate the read-only shape metrics
    that drive the operator's go/no-go decision (D-05) and the 09-03
    consolidation rules. Performs no writes."""
    groups = find_duplicate_groups(conn)

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

    for group in groups:
        member_ids = group_member_ids(conn, group["filename"], group["camera_name"])
        n = len(member_ids)
        metrics["duplicate_rows"] += n
        metrics["group_size_histogram"][n] = metrics["group_size_histogram"].get(n, 0) + 1

        winner_id = member_ids[0]
        loser_ids = member_ids[1:]

        stats_by_id = {vid: child_stats(conn, vid) for vid in member_ids}

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
            row = conn.execute(
                "SELECT thumbnail_path FROM videos WHERE id = ?", (vid,)
            ).fetchone()
            path = row["thumbnail_path"]
            if path is not None:
                thumbnail_counts[path] = thumbnail_counts.get(path, 0) + 1
        if any(count >= 2 for count in thumbnail_counts.values()):
            metrics["groups_shared_thumbnail"] += 1

        has_pairing = False
        for vid in member_ids:
            row = conn.execute(
                "SELECT paired_video_id FROM videos WHERE id = ?", (vid,)
            ).fetchone()
            if row["paired_video_id"] is not None:
                has_pairing = True
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM videos WHERE paired_video_id = ?", (vid,)
            ).fetchone()[0]
            if ref_count > 0:
                has_pairing = True
        if has_pairing:
            metrics["groups_with_pairing"] += 1

        for vid in loser_ids:
            rows = conn.execute(
                "SELECT crop_path FROM crops c JOIN detections d ON c.detection_id = d.id "
                "WHERE d.video_id = ?", (vid,),
            ).fetchall()
            for row in rows:
                all_loser_crop_paths.add(row["crop_path"])

        winner_rows = conn.execute(
            "SELECT crop_path FROM crops c JOIN detections d ON c.detection_id = d.id "
            "WHERE d.video_id = ?", (winner_id,),
        ).fetchall()
        for row in winner_rows:
            all_survivor_crop_paths.add(row["crop_path"])

        top_labels = {
            label for label in (_top_species_label(conn, vid) for vid in member_ids)
            if label is not None
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
