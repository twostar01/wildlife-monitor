"""
backfill_species_corrections.py — one-time migration that moves every
existing correction out of the two legacy correction mechanisms
(species.user_common_name/user_scientific_name/corrected_at and
video_corrections) into the unified species_corrections table (D-00,
CORR-01..04). Follows this project's own backfill/production-write rigor
(D-04), the same shape as scripts/migrate_stale_paths.py (Phase 11) and
scripts/backfill_dedup_videos.py (Phase 9): dry-run report by default,
four independent flags required to write for real
(--apply --confirm-irreversible --snapshot-dir --audit-log), a pre-write
online snapshot, a JSONL audit log, and pre/post audit-trail reconciliation
digests.

Do NOT trust the "~17,298" figure that appears in CONTEXT.md/ROADMAP.md/
REQUIREMENTS.md -- it is the total number of crops rows in production, not
the number of corrected detections (see migrate_stale_paths.py's own
docstring, which independently confirms this is the total crops count).
The real corrected-row count is computed at runtime from the actual
database and printed in every dry-run report, on its own line, clearly
separated from the crops total (which is printed only as context, never
as a write target).

Precedence (D-03/D-05): when a detection is reachable from BOTH legacy
sources, whichever source has the LATER corrected_at wins -- an exact tie
resolves to the video-player source, matching today's read-time
EFFECTIVE_COMMON ordering so a tied row's displayed value does not change
across the migration. See resolve_precedence().

No production database is touched by this plan (14-03) -- plan 14-04 owns
the full-scale production-copy rehearsal, the operator Go/No-Go, and the
real production --apply.

Usage:
    python scripts/backfill_species_corrections.py --db data/wildlife.db
    python scripts/backfill_species_corrections.py --db data/wildlife.db \
        --apply --confirm-irreversible --snapshot-dir /tmp/snap --audit-log /tmp/audit.jsonl
"""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from sqlite3 import connect as sqlite_connect, OperationalError
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database


@dataclasses.dataclass
class PlannedRow:
    detection_id: int
    corrected_label: Optional[str]
    corrected_common: Optional[str]
    corrected_scientific: Optional[str]
    suppressed: int
    source: str  # 'gallery' | 'video_player'
    corrected_at: str  # '' when the winning source's own corrected_at was NULL
    note: Optional[str]


@dataclasses.dataclass
class UnmatchedVideoCorrection:
    video_id: int
    original_label: str
    corrected_at: str


@dataclasses.dataclass
class BackfillPlan:
    rows: list
    gallery_source_rows: int
    video_source_rows_before_fanout: int
    fanout_expansion_count: int
    both_sources_count: int
    unmatched: list
    duplicate_count: int
    null_corrected_at_gallery_count: int
    skipped_not_newer: int
    crops_total: int


def collect_gallery_corrections(conn):
    """Every `species` row carrying a real Gallery correction, keyed by
    detection_id. Deliberately does NOT gate on `corrected_at IS NOT
    NULL` -- a row with names but no timestamp is a real historical
    possibility and must still be migrated, counted separately as a
    data-quality note (dQ), and treated as having the lowest possible
    precedence (see resolve_precedence())."""
    rows = conn.execute(
        """SELECT detection_id, user_common_name, user_scientific_name, corrected_at
           FROM species
           WHERE NULLIF(user_common_name, '') IS NOT NULL
              OR NULLIF(user_scientific_name, '') IS NOT NULL"""
    ).fetchall()
    return {
        row["detection_id"]: {
            "corrected_common": row["user_common_name"],
            "corrected_scientific": row["user_scientific_name"],
            "corrected_at": row["corrected_at"],  # may be NULL
        }
        for row in rows
    }


def collect_video_corrections(conn):
    """Every legacy video-level correction row, deduplicated to the
    latest corrected_at per (video_id, original_label) -- the legacy
    writer never enforced a UNIQUE constraint on that pair, so more than
    one row can exist for the same key. Returns (kept_rows,
    duplicate_count): kept_rows is a list of dicts (video_id,
    original_label, corrected_label, corrected_common,
    corrected_scientific, corrected_at, note); duplicate_count is how many
    rows were discarded as an older duplicate of a kept key."""
    all_rows = conn.execute(
        """SELECT video_id, original_label, corrected_label, corrected_common,
                  corrected_scientific, corrected_at, note
           FROM video_corrections
           ORDER BY video_id, original_label, corrected_at ASC"""
    ).fetchall()
    latest = {}
    duplicate_count = 0
    for row in all_rows:
        key = (row["video_id"], row["original_label"])
        if key in latest:
            duplicate_count += 1
        # ORDER BY corrected_at ASC means the last row seen per key is the
        # most recent -- plain string comparison is safe because both
        # legacy corrected_at columns are naive-local datetime.now().isoformat()
        # strings with no timezone suffix (verified, database.py).
        latest[key] = row
    return [dict(row) for row in latest.values()], duplicate_count


def expand_video_fanout(conn, rows):
    """For each kept legacy video-level row, expand to matching
    detections via the fan-out predicate -- character-for-character
    identical to HAS_VIDEO_CORRECTION's correlated subquery (database.py)
    and to _fanout_detection_ids() (plan 14-02). Returns (expanded,
    unmatched): expanded is a dict detection_id -> row dict; unmatched is
    a list of UnmatchedVideoCorrection for any row whose (video_id,
    original_label) matches zero current detections -- reported, never
    dropped."""
    expanded = {}
    unmatched = []
    for row in rows:
        detection_ids = [
            r[0]
            for r in conn.execute(
                """SELECT d.id FROM detections d
                   JOIN species s ON s.detection_id = d.id
                   WHERE d.video_id = ? AND s.label = ?""",
                (row["video_id"], row["original_label"]),
            ).fetchall()
        ]
        if not detection_ids:
            unmatched.append(
                UnmatchedVideoCorrection(
                    video_id=row["video_id"],
                    original_label=row["original_label"],
                    corrected_at=row["corrected_at"],
                )
            )
            continue
        for det_id in detection_ids:
            expanded[det_id] = row
    return expanded, unmatched


def resolve_precedence(gallery_corrected_at, video_corrected_at):
    """Returns 'gallery' or 'video_player' -- whichever corrected_at is
    later, treating None as the empty string (which sorts before every
    real ISO-8601 timestamp). Callers must only invoke this when BOTH
    sources have an entry for the detection -- for a single-source
    detection, use that source directly without calling this function (a
    NULL corrected_at from a lone Gallery row means "no video row exists
    to compare against", not "video wins"; see build_plan()).

    An exact tie resolves to 'video_player', reproducing today's
    read-time COALESCE ordering (EFFECTIVE_COMMON puts the video value
    first) so a tied row's displayed name does not change across the
    migration -- a decision (D-03), not an accident."""
    gallery_value = gallery_corrected_at or ""
    video_value = video_corrected_at or ""
    return "gallery" if gallery_value > video_value else "video_player"


def build_plan(conn):
    """Merge both legacy sources into one detection_id-keyed mapping
    using resolve_precedence() only where both sources are present (D-03/
    D-05: the same rule live writes use, applied retroactively so the
    backfilled state equals what a from-scratch unified system would have
    produced). Returns a BackfillPlan carrying every count the Go/No-Go
    report needs."""
    gallery = collect_gallery_corrections(conn)
    video_rows, duplicate_count = collect_video_corrections(conn)
    video_expanded, unmatched = expand_video_fanout(conn, video_rows)

    all_detection_ids = set(gallery.keys()) | set(video_expanded.keys())
    both_sources_count = len(set(gallery.keys()) & set(video_expanded.keys()))
    null_corrected_at_gallery_count = sum(1 for v in gallery.values() if not v["corrected_at"])

    planned_rows = []
    for detection_id in sorted(all_detection_ids):
        gallery_row = gallery.get(detection_id)
        video_row = video_expanded.get(detection_id)

        if gallery_row and video_row:
            winner = resolve_precedence(gallery_row["corrected_at"], video_row["corrected_at"])
        elif gallery_row:
            winner = "gallery"
        else:
            winner = "video_player"

        if winner == "gallery":
            planned_rows.append(PlannedRow(
                detection_id=detection_id,
                corrected_label=None,
                corrected_common=gallery_row["corrected_common"],
                corrected_scientific=gallery_row["corrected_scientific"],
                suppressed=0,
                source="gallery",
                corrected_at=gallery_row["corrected_at"] or "",
                note=None,
            ))
        else:
            planned_rows.append(PlannedRow(
                detection_id=detection_id,
                corrected_label=video_row["corrected_label"],
                corrected_common=video_row["corrected_common"],
                corrected_scientific=video_row["corrected_scientific"],
                suppressed=1 if video_row["corrected_label"] is None else 0,
                source="video_player",
                corrected_at=video_row["corrected_at"] or "",
                note=video_row.get("note"),
            ))

    crops_total = conn.execute("SELECT COUNT(*) FROM crops").fetchone()[0]

    return BackfillPlan(
        rows=planned_rows,
        gallery_source_rows=len(gallery),
        video_source_rows_before_fanout=len(video_rows),
        fanout_expansion_count=len(video_expanded),
        both_sources_count=both_sources_count,
        unmatched=unmatched,
        duplicate_count=duplicate_count,
        null_corrected_at_gallery_count=null_corrected_at_gallery_count,
        skipped_not_newer=0,  # populated by apply_backfill(), not at plan time
        crops_total=crops_total,
    )


def audit_trail_digest(conn):
    """Two SHA-256 hex digests proving CORR-04's invariant: one over
    (id, label) for every `species` row, one over (id, original_label)
    for every legacy video-level correction row, both ordered by id.
    Each value is serialised via json.dumps(sort_keys=True) and NUL-
    joined, exactly as migrate_stale_paths.py's non_path_digest() does,
    so NULL and the empty string hash distinctly."""

    def _digest(rows):
        digest = hashlib.sha256()
        for row in rows:
            serialized = "\x00".join(json.dumps(value, sort_keys=True) for value in row)
            digest.update(serialized.encode("utf-8"))
            digest.update(b"\x00\x00")
        return digest.hexdigest()

    species_rows = conn.execute("SELECT id, label FROM species ORDER BY id ASC").fetchall()
    video_rows = conn.execute(
        "SELECT id, original_label FROM video_corrections ORDER BY id ASC"
    ).fetchall()
    return {
        "species_label": _digest(species_rows),
        "video_original_label": _digest(video_rows),
    }


def render_plan_report(plan, applied, digests=None, as_json=False):
    """Print a Go/No-Go summary. applied=False describes what the run
    WOULD do (future tense); applied=True describes what the run DID do
    (past tense) -- never the reverse, per the FIX-03 / P-02 honesty
    lesson this project already paid for once. The crops total is always
    printed on its own line labelled as context, never as the write
    target."""
    if as_json:
        payload = {
            "applied": applied,
            "planned_row_count": len(plan.rows),
            "gallery_source_rows": plan.gallery_source_rows,
            "video_source_rows_before_fanout": plan.video_source_rows_before_fanout,
            "fanout_expansion_count": plan.fanout_expansion_count,
            "both_sources_count": plan.both_sources_count,
            "unmatched_count": len(plan.unmatched),
            "duplicate_count": plan.duplicate_count,
            "null_corrected_at_gallery_count": plan.null_corrected_at_gallery_count,
            "skipped_not_newer": plan.skipped_not_newer,
            "crops_total_context_only": plan.crops_total,
        }
        if digests:
            payload["audit_trail_digests"] = dict(digests)
        print(json.dumps(payload, sort_keys=True))
        return

    print("Species Correction Backfill -- Go/No-Go Summary")
    print("=" * 48)
    print(f"crops.rowcount (context only, NOT the write target -- see module docstring): {plan.crops_total}")
    if applied:
        print(f"species_corrections rows written this run: {len(plan.rows)}")
        print("Status: APPLIED -- rows were written as counted above.")
    else:
        print(f"species_corrections rows that would be written: {len(plan.rows)}")
        print(
            "Status: DRY-RUN -- rows would be written as counted above; "
            "nothing has been written."
        )
    verb = "were" if applied else "would be"
    print(f"Gallery-source rows ({verb} written from species.user_common_name): {plan.gallery_source_rows}")
    print(f"Video-source rows before fan-out ({verb} matched): {plan.video_source_rows_before_fanout}")
    print(f"Video-source rows after fan-out (matched detections): {plan.fanout_expansion_count}")
    print(f"Detections reachable from BOTH legacy sources (D-03 precedence applied): {plan.both_sources_count}")
    print(f"Unmatched legacy video-level rows (0 detections matched, never dropped silently): {len(plan.unmatched)}")
    for unmatched_row in plan.unmatched:
        print(
            f"  UNMATCHED: video_id={unmatched_row.video_id} "
            f"original_label={unmatched_row.original_label!r} corrected_at={unmatched_row.corrected_at}"
        )
    print(f"Duplicate legacy video-level rows discarded (later corrected_at kept): {plan.duplicate_count}")
    print(
        "Gallery rows with a NULL corrected_at (data-quality note, still migrated): "
        f"{plan.null_corrected_at_gallery_count}"
    )
    if applied:
        print(f"Rows skipped as not-newer than an existing species_corrections row: {plan.skipped_not_newer}")
    if digests:
        label = "verified unchanged" if applied else "before write"
        print(f"audit_trail_digest[species.label] ({label}): {digests.get('species_label', '')}")
        print(
            f"audit_trail_digest[video_corrections.original_label] ({label}): "
            f"{digests.get('video_original_label', '')}"
        )
    print(f"RUNVAR: report_generated_at={datetime.now().isoformat()}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="One-time migration of every existing species correction "
                     "(species.user_common_name/user_scientific_name and "
                     "video_corrections) into the unified species_corrections table."
    )
    parser.add_argument("--db", default="data/wildlife.db", help="Path to the SQLite database")
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the real write -- requires --confirm-irreversible, "
             "--snapshot-dir and --audit-log too (default: dry-run plan only, "
             "nothing written)",
    )
    parser.add_argument(
        "--confirm-irreversible", action="store_true",
        help="Required together with --apply to actually write rows. Two "
             "independent flags so no single mistyped or shell-history-recalled "
             "argument can start an irreversible pass.",
    )
    parser.add_argument(
        "--snapshot-dir", default=None,
        help="Directory for the pre-apply online DB backup (required for --apply)",
    )
    parser.add_argument(
        "--audit-log", default=None,
        help="Path to the JSONL audit log written during --apply (required for --apply)",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    database.set_db_path(args.db)
    database.init_db(args.db)

    with database.get_conn() as conn:
        plan = build_plan(conn)
        before_digests = audit_trail_digest(conn)

    render_plan_report(plan, applied=False, digests=before_digests, as_json=args.json)

    # Nothing in this task writes to species_corrections -- task 3 adds
    # the apply half (four-flag gate, snapshot, audit log, reconciliation)
    # below this exit.
    if not args.apply:
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
