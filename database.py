"""
database.py — SQLite schema and query helpers for Wildlife Processor
"""

import sqlite3
import json
import re
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
import logging

log = logging.getLogger("wildlife_processor")
_taxonomy_cache: list | None = None

DB_PATH = "data/wildlife.db"


def get_db_path() -> str:
    return DB_PATH


def set_db_path(path: str):
    global DB_PATH
    DB_PATH = path


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    filename        TEXT NOT NULL,
    filepath        TEXT UNIQUE,
    camera_name     TEXT,          -- top-level folder name on NAS (e.g. "FrontDoor")
    file_size_mb    REAL,
    duration_secs   REAL,
    recorded_at     TEXT,          -- ISO datetime (file mtime)
    processed_at    TEXT NOT NULL,
    has_animal      INTEGER DEFAULT 0,
    has_person      INTEGER DEFAULT 0,
    kept            INTEGER DEFAULT 0,
    thumbnail_path  TEXT,
    frame_count     INTEGER DEFAULT 0,
    file_purged_at  TEXT,           -- ISO datetime when video file was deleted (record kept)
    lens_index      INTEGER,        -- 0 = wide/fixed, 1 = telephoto/adjustable, NULL = unknown
    paired_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,  -- id of the other lens for dual-lens cameras
    needs_reprocess INTEGER DEFAULT 0,
    raw_purged_at   TEXT            -- ISO datetime when the NAS raw_recordings source file was deleted (cumulative)
);

CREATE TABLE IF NOT EXISTS detections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        INTEGER NOT NULL REFERENCES videos(id),
    frame_number    INTEGER,
    timestamp_secs  REAL,
    category        TEXT,           -- 'animal', 'person', 'vehicle'
    confidence      REAL,
    bbox_json       TEXT            -- [x, y, w, h] normalised
);

CREATE TABLE IF NOT EXISTS species (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id        INTEGER NOT NULL REFERENCES detections(id),
    label               TEXT NOT NULL,  -- full SpeciesNet label
    common_name         TEXT,
    scientific_name     TEXT,
    confidence          REAL,
    user_common_name    TEXT,           -- human correction (overrides SpeciesNet)
    user_scientific_name TEXT,
    corrected_at        TEXT,           -- ISO datetime of last correction
    top_candidates_json TEXT
);

CREATE TABLE IF NOT EXISTS crops (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id    INTEGER NOT NULL REFERENCES detections(id),
    crop_path       TEXT NOT NULL UNIQUE,
    quality_score   REAL,           -- 0-100
    sharpness       REAL,
    brightness      REAL,
    contrast        REAL,
    pixel_area      INTEGER,
    width           INTEGER,
    height          INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT UNIQUE NOT NULL,
    common_name     TEXT,
    scientific_name TEXT,
    created_at      TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS video_corrections (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id             INTEGER NOT NULL REFERENCES videos(id),
    original_label       TEXT NOT NULL,
    corrected_label      TEXT,           -- NULL means suppress
    corrected_common     TEXT,
    corrected_scientific TEXT,
    corrected_at         TEXT NOT NULL,
    note                 TEXT
);

-- Unified correction record (D-00, CORR-01): the single authoritative
-- per-detection correction row, replacing species.user_common_name/
-- user_scientific_name/corrected_at and video_corrections as the system of
-- record. UNIQUE(detection_id) + an UPSERT write path (add_to_blacklist()'s
-- proven ON CONFLICT ... DO UPDATE shape) gives deterministic most-recent-
-- write-wins semantics (D-03) with no read-time precedence chain needed.
-- `suppressed` (not a NULL corrected_label) is the suppression signal — see
-- this plan's objective discretion note for why a dedicated column was
-- chosen over RESEARCH.md's corrected_label-IS-NULL encoding. `source` is
-- carried for observability only, never for precedence (D-03).
CREATE TABLE IF NOT EXISTS species_corrections (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id         INTEGER NOT NULL UNIQUE REFERENCES detections(id),
    corrected_label      TEXT,
    corrected_common     TEXT,
    corrected_scientific TEXT,
    suppressed           INTEGER NOT NULL DEFAULT 0,
    source               TEXT NOT NULL,  -- 'gallery' | 'video_player' -- observability only, never precedence (D-03)
    corrected_at         TEXT NOT NULL,
    note                 TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time            TEXT NOT NULL,
    end_time              TEXT,                                -- NULL while running
    status                TEXT,                                -- 'success'|'partial'|'failure'; NULL while running
    "trigger"             TEXT NOT NULL DEFAULT 'scheduled',   -- 'manual'|'scheduled'
    videos_processed      INTEGER DEFAULT 0,
    detections_found      INTEGER DEFAULT 0,
    error_summary         TEXT,
    cameras_json          TEXT,                                -- {"CameraName": {"videos": n, "detections": n}, ...}
    offline_cameras_json  TEXT,                                -- ["CameraName", ...] flagged as possibly offline this run
    raw_cleanup_removed   INTEGER,                             -- raw_recordings files removed this run (NULL = no cleanup run yet)
    raw_cleanup_gb        REAL,                                -- GB reclaimed by raw_recordings cleanup this run
    raw_cleanup_skipped   INTEGER                              -- raw_recordings files skipped (failed verification) this run
);

CREATE INDEX IF NOT EXISTS idx_videos_recorded_at ON videos(recorded_at);
CREATE INDEX IF NOT EXISTS idx_videos_camera ON videos(camera_name);
-- Non-unique: production carries 2-6 duplicate (filename, camera_name) rows per
-- identity (04-DEFERRED.md), so a UNIQUE index is not viable. This index exists
-- purely to keep the identity lookup in insert_video()/find_existing_video()
-- fast against an 81k-row table (T-05-03).
CREATE INDEX IF NOT EXISTS idx_videos_identity ON videos(filename, camera_name);
CREATE INDEX IF NOT EXISTS idx_detections_video_id ON detections(video_id);
CREATE INDEX IF NOT EXISTS idx_species_label ON species(label);
CREATE INDEX IF NOT EXISTS idx_crops_quality ON crops(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_blacklist_label ON blacklist(label);
CREATE INDEX IF NOT EXISTS idx_corrections_video ON video_corrections(video_id);
CREATE INDEX IF NOT EXISTS idx_corrections_label ON video_corrections(original_label);
-- No index on species_corrections(detection_id): the UNIQUE constraint above
-- already creates one.
CREATE INDEX IF NOT EXISTS idx_species_corrections_label ON species_corrections(corrected_label);
CREATE INDEX IF NOT EXISTS idx_runs_start_time ON runs(start_time DESC);
-- The three indexes below were added during Phase 9's dedup backfill
-- (09-04 full-scale rehearsal, 2026-08-09): none of crops.detection_id,
-- species.detection_id, or videos.paired_video_id previously carried an
-- index, even though all three are FOREIGN KEY columns. SQLite enforces
-- foreign keys per-connection (get_conn() turns PRAGMA foreign_keys=ON),
-- and an unindexed FK column makes every DELETE of the referenced parent
-- row (videos or detections) do a full table scan to find/cascade any
-- child that references it — regardless of how the application phrases
-- its own DELETE statement. Measured directly during the rehearsal: a
-- 50-group consolidation sample went from 24.655s to 0.011s (~2,241x)
-- once these three indexes existed. CREATE INDEX IF NOT EXISTS is
-- idempotent and applies retroactively to an already-deployed database
-- on its next init_db() call, the same as the six indexes above.
CREATE INDEX IF NOT EXISTS idx_crops_detection_id ON crops(detection_id);
CREATE INDEX IF NOT EXISTS idx_species_detection_id ON species(detection_id);
CREATE INDEX IF NOT EXISTS idx_videos_paired_video_id ON videos(paired_video_id);
"""

# Migration: add camera_name to existing databases that predate this column
MIGRATION_ADD_CAMERA = """
ALTER TABLE videos ADD COLUMN camera_name TEXT;
"""

MIGRATION_ADD_CORRECTIONS = """
ALTER TABLE species ADD COLUMN user_common_name TEXT;
ALTER TABLE species ADD COLUMN user_scientific_name TEXT;
ALTER TABLE species ADD COLUMN corrected_at TEXT;
"""

MIGRATION_ADD_PURGED_AT = """
ALTER TABLE videos ADD COLUMN file_purged_at TEXT;
"""

MIGRATION_ADD_LENS = """
ALTER TABLE videos ADD COLUMN lens_index INTEGER;
ALTER TABLE videos ADD COLUMN paired_video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL;
"""

MIGRATION_ADD_REPROCESS = """
ALTER TABLE videos ADD COLUMN needs_reprocess INTEGER DEFAULT 0;
"""

MIGRATION_ADD_CANDIDATES = """
ALTER TABLE species ADD COLUMN top_candidates_json TEXT;
"""

MIGRATION_ADD_RAW_PURGED = """
ALTER TABLE videos ADD COLUMN raw_purged_at TEXT;
"""

MIGRATION_ADD_RAW_CLEANUP_STATS = """
ALTER TABLE runs ADD COLUMN raw_cleanup_removed INTEGER;
ALTER TABLE runs ADD COLUMN raw_cleanup_gb REAL;
ALTER TABLE runs ADD COLUMN raw_cleanup_skipped INTEGER;
"""

# Labels to exclude from all dashboard queries.
# SpeciesNet returns ';;;;;;blank' when it determines a crop has no animal.
BLANK_LABEL_FILTER = "s.label NOT LIKE '%;;;;;;blank'"

# ── Unified correction-resolution constants (D-00, CORR-01) ────────────────
#
# These four read from the single species_corrections table (SCHEMA above),
# which is now the sole source read by NOT_EFFECTIVELY_UNKNOWN, HAS_CORRECTION
# and EFFECTIVE_COMMON/EFFECTIVE_SCIENTIFIC below — precedence between the
# Gallery popover and video-player write paths is resolved at WRITE time by
# the UNIQUE(detection_id) UPSERT (D-03, plain recency), not at read time by
# a COALESCE chain over two tables.
#
# Placed here (immediately above NOT_EFFECTIVELY_UNKNOWN), not immediately
# above DISPLAY_COMMON where 14-01-PLAN.md's task 1 describes them, because
# NOT_EFFECTIVELY_UNKNOWN's rewired body below now interpolates
# HAS_UNIFIED_CORRECTION — a module-level f-string constant can only
# reference a name already defined above it in the file, so this placement
# is required for `import database` to succeed at all, not just a style
# preference (14-01-SUMMARY.md documents this as a plan deviation).
#
# LIMIT 1 on the two scalar subqueries is defensive, not load-bearing:
# species_corrections.detection_id carries a UNIQUE constraint, so at most
# one row can ever match. Every interpolation site below needs a `d`
# (detections) alias in scope (d.id) — the same alias-precondition
# convention NOT_EFFECTIVELY_UNKNOWN and HAS_VIDEO_CORRECTION already
# document for their own correlated subqueries.
UNIFIED_CORRECTION_COMMON = """(
    SELECT sc.corrected_common FROM species_corrections sc
    WHERE sc.detection_id = d.id AND sc.suppressed = 0
    LIMIT 1
)"""

UNIFIED_CORRECTION_SCIENTIFIC = """(
    SELECT sc.corrected_scientific FROM species_corrections sc
    WHERE sc.detection_id = d.id AND sc.suppressed = 0
    LIMIT 1
)"""

HAS_UNIFIED_CORRECTION = """EXISTS (
    SELECT 1 FROM species_corrections sc
    WHERE sc.detection_id = d.id AND sc.suppressed = 0
)"""

# True when a detection carries a suppress row (species_corrections.suppressed
# = 1) rather than a normal correction. Unused by any query in this plan —
# 14-02-PLAN.md wires this into get_video_by_id() to replace the video-player
# suppression signal. Defined here so both plans agree on one definition.
IS_SUPPRESSED_DETECTION = """EXISTS (
    SELECT 1 FROM species_corrections sc
    WHERE sc.detection_id = d.id AND sc.suppressed = 1
)"""

# True when a species row's *effective* (post-correction) label is something
# other than 'Unknown species' — i.e. the row was never Unknown, OR it was
# corrected away from Unknown via a species_corrections row (either write
# path — the unified table doesn't distinguish here). HAS_UNIFIED_CORRECTION
# already excludes suppressed rows, so a suppress sentinel never counts as a
# correction away from Unknown, matching the old third disjunct's intent.
# Disjunct order matters for cost, not just correctness: SQL OR short-
# circuits left to right, so for the overwhelming majority of rows (label is
# not 'Unknown species') the correlated EXISTS subquery below is never
# evaluated.
NOT_EFFECTIVELY_UNKNOWN = f"""(
    s.label != 'Unknown species'
    OR {HAS_UNIFIED_CORRECTION}
)"""

# Suppression filter — exclude Unknown species and blank labels for any video
# that also has at least one real identified species. If a video has a known
# species, the Unknown/blank entries are just low-confidence frames of the
# same animal and clutter the display.
#
# The row's own visibility is decided against its corrected/effective label
# (NOT_EFFECTIVELY_UNKNOWN) so a corrected Unknown-species row is no longer
# suppressed. The inner NOT EXISTS sibling-detection subquery deliberately
# keeps testing the raw s2.label rather than becoming correction-aware (P-01):
# making it correction-aware would mean correcting one crop on an all-Unknown
# video causes that video's remaining uncorrected Unknown crops to vanish —
# the operator would correct one crop and watch its siblings disappear with
# no way to reach them again. That's the opposite of the trust problem this
# filter exists to fix, so the subquery errs toward showing data.
#
# The combined filter below requires both an `s` (species) and a `d`
# (detections) alias in scope at every interpolation site, since
# NOT_EFFECTIVELY_UNKNOWN's correlated subquery references d.video_id.
SUPPRESS_UNKNOWN_IF_IDENTIFIED = f"""(
    {NOT_EFFECTIVELY_UNKNOWN}
    OR NOT EXISTS (
        SELECT 1 FROM species s2
        JOIN detections d2 ON s2.detection_id = d2.id
        WHERE d2.video_id = d.video_id
          AND s2.label != 'Unknown species'
          AND s2.label NOT LIKE '%;;;;;;blank'
    )
)"""

# Combined filter — always exclude blank, suppress Unknown when a real species
# is present, and exclude any species on the blacklist.
KNOWN_SPECIES_FILTER = (
    f"{BLANK_LABEL_FILTER} AND {SUPPRESS_UNKNOWN_IF_IDENTIFIED} "
    f"AND s.label NOT IN (SELECT label FROM blacklist)"
)

# SQL expression that returns the display name — user correction when set, else SpeciesNet common_name
DISPLAY_COMMON     = "COALESCE(NULLIF(s.user_common_name,''), s.common_name)"
DISPLAY_SCIENTIFIC = "COALESCE(NULLIF(s.user_scientific_name,''), s.scientific_name)"

# Retained but unreferenced pending D-07 (legacy-table removal follow-up
# phase): no query in this codebase evaluates HAS_VIDEO_CORRECTION any more
# after this plan's HAS_CORRECTION rewrite below — it stays defined, and
# video_corrections stays readable, only because D-06 freezes the table
# read-only rather than dropping it in this phase.
#
# True when a species row was corrected through the VIDEO PLAYER's per-crop
# editor rather than the Gallery popover. The gallery path writes
# species.user_common_name and stamps species.corrected_at directly via
# correct_species(); the video-player path instead writes a
# video_corrections row via save_video_correction(), keyed by
# (video_id, original_label). A NULL corrected_label is the schema's
# suppress sentinel (video_corrections.corrected_label, "NULL means
# suppress") and apply_corrections_to_species() treats it as "skip this
# species" rather than "this species was corrected" — the third conjunct
# below is load-bearing, not defensive, and mirrors the identical conjunct
# already present in NOT_EFFECTIVELY_UNKNOWN above for the same reason.
#
# Every interpolation site MUST have both an `s` (species) and a `d`
# (detections) alias in scope, since this correlated subquery references
# d.video_id — the same precondition NOT_EFFECTIVELY_UNKNOWN's own comment
# documents at (see above, "requires both an `s` ... and a `d` ...").
HAS_VIDEO_CORRECTION = """EXISTS (
    SELECT 1 FROM video_corrections vc
    WHERE vc.video_id = d.video_id
      AND vc.original_label = s.label
      AND vc.corrected_label IS NOT NULL
)"""

# True (1) when a detection carries a non-suppressed species_corrections row
# (CORR-01, D-00) — i.e. it was corrected through EITHER write path (Gallery
# popover OR video-player editor), since both now write into the same
# unified table. Its own s.corrected_at IS NOT NULL / HAS_VIDEO_CORRECTION
# disjuncts are gone: precedence between the two write paths is resolved at
# write time (D-03), not by testing both legacy sources at read time.
HAS_CORRECTION = f"CASE WHEN {HAS_UNIFIED_CORRECTION} THEN 1 ELSE 0 END"

# Scalar correlated subquery returning the VIDEO PLAYER's corrected common
# name for a species row, or NULL when no matching, non-suppressed
# video_corrections row exists. The predicate is character-for-character
# the one inside HAS_VIDEO_CORRECTION above, deliberately — the flag and
# the name must agree about what counts as a correction, or a tile could
# show a pencil next to an uncorrected name. LIMIT 1 is defensive rather
# than load-bearing: save_video_correction() deletes before inserting so at
# most one row can match a given (video_id, original_label), but no UNIQUE
# constraint enforces that at the schema level — a scalar subquery
# returning two rows would raise at runtime on production data rather than
# in the fixture.
#
# Suppression semantics: a NULL corrected_label means "suppress this
# species" (video_corrections.corrected_label schema comment), not "this
# species was corrected" — the third conjunct excludes those rows, so a
# suppress-sentinel row never supplies a name.
#
# Every interpolation site MUST have both an `s` (species) and a `d`
# (detections) alias in scope, since this correlated subquery references
# d.video_id — the same precondition NOT_EFFECTIVELY_UNKNOWN's and
# HAS_VIDEO_CORRECTION's own comments document above.
VIDEO_CORRECTION_COMMON = """(
    SELECT vc.corrected_common FROM video_corrections vc
    WHERE vc.video_id = d.video_id
      AND vc.original_label = s.label
      AND vc.corrected_label IS NOT NULL
    LIMIT 1
)"""

# Same shape as VIDEO_CORRECTION_COMMON, selecting the corrected scientific
# name instead.
VIDEO_CORRECTION_SCIENTIFIC = """(
    SELECT vc.corrected_scientific FROM video_corrections vc
    WHERE vc.video_id = d.video_id
      AND vc.original_label = s.label
      AND vc.corrected_label IS NOT NULL
    LIMIT 1
)"""

# The effective display name: the unified species_corrections value when
# present and non-blank, else the raw SpeciesNet value. NULLIF(...,'') is
# what makes a correction saved with a blank name fall through to the raw
# value instead of blanking the display — the same guard DISPLAY_COMMON used
# to apply to s.user_common_name.
#
# The fallback is now the RAW s.common_name/s.scientific_name, not
# DISPLAY_COMMON/DISPLAY_SCIENTIFIC — chaining through DISPLAY_COMMON would
# resurrect the frozen legacy species.user_common_name column as a live read
# source (violating D-06/CORR-01), since the Gallery correction that
# DISPLAY_COMMON used to supply now arrives through UNIFIED_CORRECTION_COMMON
# above instead. This collapses the previous two-source COALESCE chain
# (video-corrections-then-Gallery) into one source, because precedence
# between the two write paths is now resolved at WRITE time (D-03) via the
# UNIQUE(detection_id) UPSERT, not at read time by chain ordering.
#
# Same alias precondition as UNIFIED_CORRECTION_COMMON above: every
# interpolation site needs both `s` and `d` in scope.
EFFECTIVE_COMMON = f"COALESCE(NULLIF({UNIFIED_CORRECTION_COMMON},''), s.common_name)"
EFFECTIVE_SCIENTIFIC = f"COALESCE(NULLIF({UNIFIED_CORRECTION_SCIENTIFIC},''), s.scientific_name)"

# ── Deliberately NOT converted to EFFECTIVE_COMMON/EFFECTIVE_SCIENTIFIC ──
#
# The following readers stay on DISPLAY_COMMON/DISPLAY_SCIENTIFIC (or never
# used them), on purpose, and case P10 (scripts/verify_phase12.py) pins
# this boundary as a regression:
#
#   - get_species_list()      (GROUP BY s.label)
#   - get_stats() top_species  (GROUP BY s.label)
#   - get_timeline()           (GROUP BY period, s.label)
#     These three GROUP BY the raw label. Selecting an effective name over
#     a group keyed on the raw label makes SQLite return an arbitrary
#     member row's value, so a label with some corrected and some
#     uncorrected detections would display non-deterministically —
#     strictly worse than the current stable-if-stale behaviour.
#
#   - get_stats() activity_raw (selects the raw s.common_name directly)
#     Already selects the raw common_name and has never used DISPLAY_COMMON
#     at all — stale for BOTH correction paths. That predates this phase
#     and is unrelated to it.
#
#   - search()
#     Never used DISPLAY_COMMON, and its species query has no `d`
#     (detections) alias in scope, so EFFECTIVE_COMMON cannot be
#     interpolated there without a join change.
#
#   - get_gallery()'s species filter (s.label = ?) and get_videos()'s
#     species filter and its `s.common_name LIKE ?` search predicate
#     Match on raw values — a video-corrected crop still answers to its
#     original label in those filters.
#
# Correcting any of the above means grouping and filtering by the
# EFFECTIVE (post-correction) label instead of the raw one, which changes
# the drilldown key get_species_detail(label) accepts, the <option> values
# populateSpeciesFilters() emits (static/index.html), and chart series
# identity. No source artifact decides what that key should be, so this
# plan records the boundary rather than acting on it. If case P10 ever
# fails, that decision has not been made yet — it failing is a request for
# one, not a bug.
#
# ── Phase 14 (Correction Unification) additions ─────────────────────────
#
# species_corrections is now the SOLE source read by EFFECTIVE_COMMON,
# EFFECTIVE_SCIENTIFIC, HAS_CORRECTION and NOT_EFFECTIVELY_UNKNOWN above
# (CORR-01, D-00). Precedence between the two correction entry points
# (Gallery popover, video-player editor) is resolved at WRITE time by the
# species_corrections UNIQUE(detection_id) UPSERT (D-03, plain recency —
# whichever write is most recent wins), not at read time by the order of a
# COALESCE chain. This is a real behaviour change from the previously-
# shipped "video-player value always wins" ordering this comment block used
# to document (see the old EFFECTIVE_COMMON comment, now superseded).
#
# species.user_common_name / species.user_scientific_name /
# species.corrected_at, and the entire video_corrections table, are frozen
# read-only from this phase forward (D-06): still readable, never written,
# by any code path in this codebase. get_video_corrections() is the one
# function that still reads the frozen video_corrections table on purpose —
# RESEARCH.md's Open Question 2 is resolved here: GET /api/corrections has
# no frontend caller (grep-verified — static/index.html's only reference to
# '/api/corrections' is a POST, in applyCorrection()), so it is deliberately
# left reading the frozen table rather than rewired to species_corrections.
#
# Interim staleness window (accepted, not a bug): get_species_list(),
# get_stats()'s top_species and get_timeline() (see the bullets above) still
# resolve their displayed NAME through DISPLAY_COMMON, which reads the
# now-frozen species.user_common_name. Any correction made after this phase
# deploys and before Phase 15 ships will therefore NOT change the name shown
# in the Species tab, Stats top-species tile, or Timeline chart — even
# though it correctly changes the Gallery grid, species-detail crop grid,
# Videos tab and video player (all rewired to EFFECTIVE_COMMON/
# EFFECTIVE_SCIENTIFIC above). The ✏ corrected badge in the Species tab
# stays accurate throughout, because HAS_CORRECTION is rewired here and
# those three readers already consume it. This is RESEARCH.md's Pitfall 1,
# accepted deliberately: converting those three readers' displayed name
# without also converting their GROUP BY key would reintroduce the exact
# SQLite arbitrary-row-per-group hazard this comment block exists to
# prevent (see the bullets above). Phase 15 (LABEL-01..05) closes this
# window.
#
# The `suppressed` column on species_corrections — not a NULL
# corrected_label — is the suppression signal. A NULL corrected_label on a
# source='gallery' row is normal and means only "the Gallery popover never
# collects a formal taxonomy label" (SpeciesCorrectionRequest has no
# `label` field). Any future query that tests for suppression must test
# `suppressed`, not `corrected_label IS NULL`.
#
# Reprocessing a video does NOT re-apply prior corrections (D-02): a
# reprocessed video's new detections start uncorrected, exactly like
# newly-processed footage. wildlife_processor.py's reprocess flow is
# untouched by this phase.


def init_db(db_path: Optional[str] = None):
    if db_path:
        set_db_path(db_path)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(videos)").fetchall()]
        if "camera_name" not in cols:
            conn.executescript(MIGRATION_ADD_CAMERA)
        if "file_purged_at" not in cols:
            conn.executescript(MIGRATION_ADD_PURGED_AT)
        if "lens_index" not in cols:
            conn.executescript(MIGRATION_ADD_LENS)
        sp_cols = [r[1] for r in conn.execute("PRAGMA table_info(species)").fetchall()]
        if "user_common_name" not in sp_cols:
            conn.executescript(MIGRATION_ADD_CORRECTIONS)
        if "top_candidates_json" not in sp_cols:
            conn.executescript(MIGRATION_ADD_CANDIDATES)
        if "needs_reprocess" not in cols:
            conn.executescript(MIGRATION_ADD_REPROCESS)
        if "raw_purged_at" not in cols:
            conn.executescript(MIGRATION_ADD_RAW_PURGED)
        run_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        if "raw_cleanup_removed" not in run_cols:
            conn.executescript(MIGRATION_ADD_RAW_CLEANUP_STATS)
        # Migration: drop NOT NULL constraint on filepath so purged/blank records can have NULL
        filepath_notnull = next(
            (r[3] for r in conn.execute("PRAGMA table_info(videos)").fetchall() if r[1] == "filepath"), 0
        )
        if filepath_notnull:
            log.info("DB migration: removing NOT NULL from videos.filepath...")
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE videos_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename        TEXT NOT NULL,
                    filepath        TEXT UNIQUE,
                    camera_name     TEXT,
                    file_size_mb    REAL,
                    duration_secs   REAL,
                    recorded_at     TEXT,
                    processed_at    TEXT,
                    has_animal      INTEGER DEFAULT 0,
                    has_person      INTEGER DEFAULT 0,
                    kept            INTEGER DEFAULT 0,
                    thumbnail_path  TEXT,
                    frame_count     INTEGER,
                    file_purged_at  TEXT,
                    lens_index      INTEGER,
                    paired_video_id INTEGER REFERENCES videos_new(id) ON DELETE SET NULL,
                    needs_reprocess INTEGER DEFAULT 0,
                    raw_purged_at   TEXT
                );
                INSERT INTO videos_new (
                    id, filename, filepath, camera_name, file_size_mb, duration_secs,
                    recorded_at, processed_at, has_animal, has_person, kept, thumbnail_path,
                    frame_count, file_purged_at, lens_index, paired_video_id, needs_reprocess,
                    raw_purged_at
                )
                SELECT
                    id, filename, filepath, camera_name, file_size_mb, duration_secs,
                    recorded_at, processed_at, has_animal, has_person, kept, thumbnail_path,
                    frame_count, file_purged_at, lens_index, paired_video_id,
                    COALESCE(needs_reprocess, 0),
                    raw_purged_at
                FROM videos;
                DROP TABLE videos;
                ALTER TABLE videos_new RENAME TO videos;
            """)
            conn.execute("PRAGMA foreign_keys=ON")   # restore after executescript resets pragma state
            log.info("DB migration: filepath NOT NULL constraint removed")
            # The table rebuild above drops and recreates `videos` after
            # executescript(SCHEMA) already ran, so the identity index would be
            # missing for the remainder of this run. Re-execute it (idempotent).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_videos_identity ON videos(filename, camera_name)"
            )

        # Dual-lens pairing repair (SYNC-04, D-01/D-03/D-04): re-validates ALL
        # paired_video_id values on every init_db() call, inside this same
        # transaction, so a mid-pass failure rolls back as a unit.
        summary = _repair_lens_pairings(conn)
        log.info(
            "Dual-lens pairing repair: %d pair(s) linked/fixed, %d value(s) unlinked, "
            "%d group(s) left ambiguous (no auto-safe match)",
            summary["linked"], summary["unlinked"], summary["ambiguous_groups"],
        )

    # Pairing consistency check (D-06) runs AFTER the `with get_conn()` block
    # exits — get_conn() opens a fresh connection per call, so calling this
    # from inside the block above would read a pre-commit snapshot and log a
    # stale count.
    broken = check_pairing_consistency()
    if broken:
        log.warning(
            "Pairing consistency check: %d video(s) have a broken/asymmetric paired_video_id",
            broken,
        )


# ── Write helpers ──────────────────────────────────────────────────────────────

# Attribution invariant (02-RESEARCH.md Pitfall 3): per-camera attribution in
# record_run_end()'s cameras_json snapshot relies on videos.processed_at being a
# reliable "which run touched this video" signal. That in turn depends on
# nas_sync.sh deleting local staging copies after every normal run, so a video is
# never re-scanned by a later run. A manual `--no-cleanup` run breaks that
# invariant: the same local file can be rescanned and re-inserted (advancing
# processed_at) on a subsequent run before it's archived, re-attributing it to
# whichever run touched it last.
def record_run_start(trigger: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO runs (start_time, "trigger") VALUES (?, ?)""",
            (datetime.now().isoformat(), trigger),
        )
        return int(cur.lastrowid)


def record_run_end(
    run_id: int,
    status: str,
    videos_processed: int,
    detections_found: int,
    error_summary: Optional[str],
    cameras_json: Optional[str],
    offline_cameras_json: Optional[str],
) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE runs SET end_time=?, status=?, videos_processed=?,
               detections_found=?, error_summary=?, cameras_json=?, offline_cameras_json=?
               WHERE id=?""",
            (
                datetime.now().isoformat(), status, videos_processed, detections_found,
                error_summary, cameras_json, offline_cameras_json, run_id,
            ),
        )


def reconcile_interrupted_runs() -> int:
    """Close any runs row abandoned by a process that exited without calling
    record_run_end() (e.g. a service restart mid-run).

    Safety: nas_sync.sh's flock guard (line 90) ensures only one
    nas_sync.sh-spawned wildlife_processor.py can be alive at a time, so any
    row still status IS NULL when this runs (called before this run's own
    record_run_start()) belongs to an already-exited process — no age
    threshold needed. This assumption does NOT hold for a wildlife_processor.py
    invoked directly, bypassing nas_sync.sh (see wildlife_processor.py:330-337
    and 459-464 for the same accepted invariant-risk precedent).

    Set-based (no LIMIT) — reconciles ALL stale rows in one statement in case
    more than one interruption happened before a run completed successfully.

    end_time is set to the sweep timestamp (not left NULL or estimated) so
    duration_secs is derivable for the reconciled row. This means the shown
    duration reflects when the interruption was *detected*, not when the
    process actually died — a reconciled row will usually show an inflated
    multi-hour duration. That is expected behaviour, not a bug.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE runs SET status=?, end_time=?, error_summary=?
               WHERE status IS NULL""",
            (
                "interrupted",
                datetime.now().isoformat(),
                "Process did not complete — likely interrupted by a service restart or crash",
            ),
        )
        return cur.rowcount


def record_raw_cleanup_stats(run_id: Optional[int], removed: int, gb_reclaimed: float, skipped: int) -> None:
    """
    Record this run's raw_recordings cleanup outcome on its `runs` row.
    Touches only the three raw_cleanup_* columns — no other runs column.

    A no-op when run_id is None, so a caller running against a database with
    no recorded runs (e.g. an early/edge-case invocation) does not raise.
    """
    if run_id is None:
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE runs SET raw_cleanup_removed=?, raw_cleanup_gb=?, raw_cleanup_skipped=?
               WHERE id=?""",
            (removed, gb_reclaimed, skipped, run_id),
        )


def insert_video(
    filename: str,
    filepath: str,
    camera_name: Optional[str],
    file_size_mb: float,
    duration_secs: float,
    recorded_at: str,
    has_animal: bool,
    has_person: bool,
    kept: bool,
    thumbnail_path: Optional[str],
    frame_count: int,
    lens_index: Optional[int] = None,
) -> int:
    """
    Insert or update a video row. The dedup key is (filename, camera_name) per
    D-03, not filepath — a staging path is not stable file identity across
    archive moves (DEDUP-01). The returned id may be a pre-existing row's id,
    so callers must not assume a fresh row.
    """
    with get_conn() as conn:
        # Take the write lock before the identity read so a second concurrent
        # writer can't also miss the lookup and also insert (threat T-05-02).
        # get_conn() yields a fresh connection on which only PRAGMAs have run,
        # so no transaction is open yet and this explicit begin-immediate call
        # is valid. get_conn()'s trailing conn.commit() closes this transaction.
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")

        existing = _find_existing_video_row(conn, filename, camera_name)

        if existing is None:
            # No identity match — insert. The upsert clause below is retained
            # as the hard backstop for the filepath UNIQUE constraint: if a
            # concurrent writer inserted the same path between this
            # transaction's start and this insert, this degrades to an
            # in-place update instead of crashing with an IntegrityError.
            cur = conn.execute(
                """INSERT INTO videos
                   (filename, filepath, camera_name, file_size_mb, duration_secs, recorded_at,
                    processed_at, has_animal, has_person, kept, thumbnail_path, frame_count, lens_index)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(filepath) DO UPDATE SET
                     filename=excluded.filename,
                     camera_name=excluded.camera_name,
                     file_size_mb=excluded.file_size_mb,
                     duration_secs=excluded.duration_secs,
                     recorded_at=excluded.recorded_at,
                     processed_at=excluded.processed_at,
                     has_animal=excluded.has_animal,
                     has_person=excluded.has_person,
                     kept=excluded.kept,
                     thumbnail_path=excluded.thumbnail_path,
                     frame_count=excluded.frame_count,
                     lens_index=excluded.lens_index
                   RETURNING id""",
                (
                    filename, filepath, camera_name, file_size_mb, duration_secs, recorded_at,
                    datetime.now().isoformat(),
                    int(has_animal), int(has_person), int(kept),
                    thumbnail_path, frame_count, lens_index,
                ),
            )
            return cur.fetchone()[0]

        # Identity match — do not insert. Update metadata on the matched row.
        conn.execute(
            """UPDATE videos SET
                 camera_name=?, file_size_mb=?, duration_secs=?, recorded_at=?,
                 processed_at=?, has_animal=?, has_person=?, kept=?,
                 thumbnail_path=?, frame_count=?, lens_index=?
               WHERE id=?""",
            (
                camera_name, file_size_mb, duration_secs, recorded_at,
                datetime.now().isoformat(),
                int(has_animal), int(has_person), int(kept),
                thumbnail_path, frame_count, lens_index,
                existing["id"],
            ),
        )

        # filepath and file_purged_at need their own rule — the most
        # consequential decision in this function. Set filepath to the passed
        # filepath only when the matched row has both filepath IS NULL and
        # file_purged_at IS NULL — an un-located row that was never purged
        # legitimately gains a location. In every other case leave both
        # columns exactly as they are: a non-NULL path already on the row is
        # the authoritative archived location and must win over a transient
        # staging copy, and a purge marker must never be resurrected by
        # re-pointing filepath. Never clear file_purged_at in this function.
        # Leaving the staging path unreferenced here is safe because
        # nas_sync.sh deletes local staging copies by directory sweep over its
        # own file list (nas_sync.sh:618-636), not by DB reference.
        if existing["filepath"] is None and existing["file_purged_at"] is None:
            conn.execute(
                "UPDATE videos SET filepath=? WHERE id=?",
                (filepath, existing["id"]),
            )

        return existing["id"]


def _find_existing_video_row(conn, filename: str, camera_name: Optional[str]):
    """
    Module-private identity lookup. Takes an already-open connection so
    insert_video() can call this inside its own transaction without opening a
    second one.

    `camera_name IS ?` is SQLite's NULL-safe equality — a plain `=` never
    matches a NULL camera_name and would silently fail the
    identity/null-camera-matches-null case. `filename = ?` is exact equality,
    never LIKE — this is what makes a percent sign or underscore in a filename
    inert (identity/like-metacharacter-filename, threat T-05-01).

    The ORDER BY clause below is the deterministic tie-break: rows with a live
    filepath sort first (the expression is 0 for non-NULL), then lowest id;
    because id is unique the total order is total, so repeated calls always
    return the same row. The tie-break exists because production carries 2-6
    rows per identity and repair is out of scope for v1.1.
    """
    return conn.execute(
        "SELECT id, filename, filepath, camera_name, file_purged_at FROM videos "
        "WHERE filename = ? AND camera_name IS ? "
        "ORDER BY (filepath IS NULL), id LIMIT 1",
        (filename, camera_name),
    ).fetchone()


def find_existing_video(filename: str, camera_name: Optional[str]):
    """Public wrapper around _find_existing_video_row(). Returns the
    deterministic matching row for (filename, camera_name), or None."""
    with get_conn() as conn:
        return _find_existing_video_row(conn, filename, camera_name)


def find_archived_duplicate(filename: str, camera_name: Optional[str], candidate_filepath: str):
    """
    Guard predicate: a return value means "this physical file already has a
    row recorded at a different location (archived or purged), so re-doing
    work for it is wasted". None means "either unknown, or the same still-
    staged file".

    Does not compare against the NAS archive root or any configured path
    prefix — path-differs is the signal, because nas_sync.sh writes both a
    main-archive path and a blanks/ path and process_videos() has no
    archive-root argument. A NULL stored filepath compares unequal to any
    candidate string and therefore returns the row — that is the
    purged/archived case and is intended.
    """
    existing = find_existing_video(filename, camera_name)
    if existing is None:
        return None
    if existing["filepath"] == candidate_filepath:
        return None
    return existing


def parse_dual_lens_filename(filename: str) -> Optional[tuple]:
    """
    Parse a dual-lens camera filename into (camera_base, lens_index, timestamp).

    Expects format: {CameraBase}_{LensNum}_{YYYYMMDDHHMMSS}.ext
    where LensNum is a zero-padded integer (00, 01, etc.)

    Returns (camera_base, lens_index, timestamp_str) or None if not a dual-lens name.
    Examples:
      "World Watch_00_20260327160902.mp4" → ("World Watch", 0, "20260327160902")
      "World Watch_01_20260327160902.mp4" → ("World Watch", 1, "20260327160902")
    """
    stem = Path(filename).stem
    m = re.match(r'^(.+)_(\d{2})_(\d{14})$', stem)
    if m:
        return m.group(1), int(m.group(2)), m.group(3)
    return None


def link_lens_pair(video_id: int, filename: str) -> Optional[int]:
    """
    After inserting a video, find its paired lens and link both rows.
    Returns the paired video's id if a pair was found and linked, else None.
    Only links when exactly one unambiguous candidate exists (D-02/D-07) —
    never guesses among multiple matches.
    """
    parsed = parse_dual_lens_filename(filename)
    if parsed is None:
        return None
    camera_base, lens_index, timestamp = parsed

    with get_conn() as conn:
        # timestamp is guaranteed digit-only by the \d{14} regex in
        # parse_dual_lens_filename — no LIKE metacharacters possible, so no
        # escaping is needed (Pitfall 4). camera_base is intentionally NOT
        # part of the SQL pattern; the Python-side check below does that
        # exact match, same safety net as before.
        rows = conn.execute(
            "SELECT id, filename, paired_video_id FROM videos WHERE id != ? AND filename LIKE ?",
            (video_id, f"%_{timestamp}%"),
        ).fetchall()

        candidates = [
            row for row in rows
            if (p := parse_dual_lens_filename(row["filename"]))
            and p[0] == camera_base and p[2] == timestamp and p[1] != lens_index
        ]

        if len(candidates) != 1:
            # 0 candidates: no partner recorded yet (normal, Pitfall 2).
            # >1 candidates: ambiguous — almost certainly duplicate rows
            # (Pitfall 1). Don't guess. Still record lens_index so the video
            # is identifiable, but leave pairing alone.
            conn.execute(
                "UPDATE videos SET lens_index=? WHERE id=?", (lens_index, video_id)
            )
            if len(candidates) > 1:
                log.info(
                    "link_lens_pair: %d ambiguous candidates for %s (camera=%s, ts=%s) — left unpaired",
                    len(candidates), filename, camera_base, timestamp,
                )
            return None

        candidate = candidates[0]
        pair_id = candidate["id"]

        # Guard: only claim this candidate if it isn't already linked to a
        # DIFFERENT video. Without this check, a duplicate-row rescan could
        # silently overwrite one side of an already-correct pair (D-06 concern).
        if candidate["paired_video_id"] not in (None, video_id):
            log.info(
                "link_lens_pair: candidate %s for %s already paired elsewhere — left unpaired",
                pair_id, filename,
            )
            conn.execute(
                "UPDATE videos SET lens_index=? WHERE id=?", (lens_index, video_id)
            )
            return None

        conn.execute(
            "UPDATE videos SET paired_video_id=?, lens_index=? WHERE id=?",
            (pair_id, lens_index, video_id),
        )
        conn.execute(
            "UPDATE videos SET paired_video_id=? WHERE id=? AND paired_video_id IS NULL",
            (video_id, pair_id),
        )
        # Also set lens_index on the pair if not already set, sourced from the
        # already-fetched candidate row (no extra round-trip).
        p2 = parse_dual_lens_filename(candidate["filename"])
        if p2:
            conn.execute(
                "UPDATE videos SET lens_index=? WHERE id=? AND lens_index IS NULL",
                (p2[1], pair_id),
            )
        return pair_id


def _repair_lens_pairings(conn) -> dict:
    """
    Re-validate ALL videos.paired_video_id values against camera_base+timestamp+
    lens_index derived from filenames (D-04). Only links a pair when the group is
    unambiguous (exactly 2 members, differing lens). Never overwrites an existing
    correct link; never guesses among 3+ candidates. Returns summary counts for
    the startup log line (D-03).

    Takes an already-open connection so it participates in init_db()'s
    transaction — it never opens its own.
    """
    rows = conn.execute(
        "SELECT id, filename, lens_index, paired_video_id FROM videos"
    ).fetchall()

    groups = defaultdict(list)
    for r in rows:
        p = parse_dual_lens_filename(r["filename"])
        if p:
            camera_base, lens_index, timestamp = p
            groups[(camera_base, timestamp)].append(
                (r["id"], lens_index, r["paired_video_id"])
            )

    linked = unlinked = ambiguous_groups = 0

    for members in groups.values():
        if len(members) == 2 and members[0][1] != members[1][1]:
            (id1, lens1, pv1), (id2, lens2, pv2) = members
            if pv1 == id2 and pv2 == id1:
                continue  # already correct — no-op, keeps the pass idempotent
            conn.execute(
                "UPDATE videos SET paired_video_id=? WHERE id=?", (id2, id1)
            )
            conn.execute(
                "UPDATE videos SET paired_video_id=? WHERE id=?", (id1, id2)
            )
            linked += 1
        else:
            # Not a clean 2-member cross-lens group. Any member currently
            # pointing somewhere is wrong (or ambiguous) — clear it rather
            # than guess.
            for (vid, lens, pv) in members:
                if pv is not None:
                    conn.execute(
                        "UPDATE videos SET paired_video_id=NULL WHERE id=?", (vid,)
                    )
                    unlinked += 1
            if len(members) > 1:
                ambiguous_groups += 1

    # Rows whose filename never parsed as dual-lens (so they never joined a
    # group above) but which still carry a stray paired_video_id — e.g. set by
    # some future write path other than link_lens_pair()/this function — must
    # also be cleared to actually live up to the "ALL" in this function's
    # docstring, rather than being silently left broken forever (WR-02).
    grouped_ids = {vid for members in groups.values() for (vid, _, _) in members}
    for r in rows:
        if r["id"] not in grouped_ids and r["paired_video_id"] is not None:
            conn.execute(
                "UPDATE videos SET paired_video_id=NULL WHERE id=?", (r["id"],)
            )
            unlinked += 1

    return {"linked": linked, "unlinked": unlinked, "ambiguous_groups": ambiguous_groups}


def check_pairing_consistency() -> int:
    """
    Count videos whose paired_video_id doesn't point back symmetrically.
    Returns the broken-pointer count; callers log a warning if > 0.
    No schema change — read-only SELECT (D-06).
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) FROM videos v1
            LEFT JOIN videos v2 ON v1.paired_video_id = v2.id
            WHERE v1.paired_video_id IS NOT NULL
              AND (v2.id IS NULL OR v2.paired_video_id IS NULL OR v2.paired_video_id != v1.id)
        """).fetchone()
        return row[0]


def insert_detection(
    video_id: int,
    frame_number: int,
    timestamp_secs: float,
    category: str,
    confidence: float,
    bbox: list,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO detections
               (video_id, frame_number, timestamp_secs, category, confidence, bbox_json)
               VALUES (?,?,?,?,?,?)""",
            (video_id, frame_number, timestamp_secs, category, confidence, json.dumps(bbox)),
        )
        return cur.lastrowid


def insert_species(
    detection_id: int,
    label: str,
    common_name: Optional[str],
    scientific_name: Optional[str],
    confidence: float,
    top_candidates_json: Optional[str] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO species
               (detection_id, label, common_name, scientific_name, confidence, top_candidates_json)
               VALUES (?,?,?,?,?,?)""",
            (detection_id, label, common_name, scientific_name, confidence, top_candidates_json),
        )
        return cur.lastrowid


def update_video_filepath(video_id: int, new_filepath: str):
    """Update the stored filepath for a video after it has been moved to the NAS archive."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET filepath = ? WHERE id = ?",
            (new_filepath, video_id),
        )


def _upsert_species_correction(
    conn,
    detection_id: int,
    corrected_label: Optional[str] = None,
    corrected_common: Optional[str] = None,
    corrected_scientific: Optional[str] = None,
    suppressed: int = 0,
    source: str = "gallery",
    note: Optional[str] = None,
) -> None:
    """
    Insert or update the single species_corrections row for detection_id
    (D-00: UNIQUE(detection_id) + UPSERT gives deterministic most-recent-
    write-wins semantics per D-03 — same ON CONFLICT ... DO UPDATE shape as
    add_to_blacklist(), the only other proven UPSERT in this codebase).

    Caller must already be inside a `with get_conn() as conn:` block — this
    helper does not open its own connection, so a fan-out caller (video-
    player, plan 14-02) can write several detections' rows inside one shared
    transaction.

    Every bound value uses a `?` placeholder (T-14-02) — no f-string or
    `%`-interpolation of caller-supplied data into SQL.
    """
    conn.execute(
        """INSERT INTO species_corrections
           (detection_id, corrected_label, corrected_common, corrected_scientific,
            suppressed, source, corrected_at, note)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(detection_id) DO UPDATE SET
             corrected_label=excluded.corrected_label,
             corrected_common=excluded.corrected_common,
             corrected_scientific=excluded.corrected_scientific,
             suppressed=excluded.suppressed,
             source=excluded.source,
             corrected_at=excluded.corrected_at,
             note=excluded.note""",
        (
            detection_id,
            corrected_label,
            corrected_common,
            corrected_scientific,
            suppressed,
            source,
            datetime.now().isoformat(),
            note,
        ),
    )


def correct_species(
    detection_id: int,
    user_common_name: str,
    user_scientific_name: str,
) -> int:
    """
    Save a human correction for a species detection (Gallery popover write
    path) via the unified species_corrections table (D-00, CORR-01). Pass
    empty strings to clear a correction.

    Returns 1 if detection_id exists in `detections` (whether a correction
    was written or an existing one cleared), 0 for an unknown detection_id —
    so the API layer can distinguish "nothing to update" from success instead
    of always reporting {"ok": True} (IN-02). The existence check is now
    load-bearing rather than incidental: an UPSERT against a nonexistent
    detection_id would raise an IntegrityError (foreign keys are ON per
    get_conn()) and surface as a 500 instead of a 404 without it. Clearing an
    already-clear correction on an existing detection still returns 1 — that
    is not a 404.

    Does not read or write any column of the `species` table beyond the
    existence-adjacent check above (D-06 freeze; CORR-04) — species.label and
    the legacy user_common_name/user_scientific_name/corrected_at columns are
    untouched by this function after cutover. corrected_label is always NULL
    for source='gallery': the Gallery popover never collects a formal
    taxonomy label (SpeciesCorrectionRequest has no `label` field).
    """
    common = user_common_name.strip() or None
    scientific = user_scientific_name.strip() or None
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM detections WHERE id=?", (detection_id,)).fetchone()
        if not exists:
            return 0
        if common is None and scientific is None:
            conn.execute("DELETE FROM species_corrections WHERE detection_id=?", (detection_id,))
        else:
            _upsert_species_correction(
                conn,
                detection_id,
                corrected_label=None,
                corrected_common=common,
                corrected_scientific=scientific,
                suppressed=0,
                source="gallery",
                note=None,
            )
    return 1
def get_kept_video_paths() -> list:
    """
    Return id and filepath for all kept videos that are currently stored locally
    (i.e. not already on the NAS archive). Used by nas_sync.sh to move files.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, filepath, camera_name, recorded_at, filename
            FROM videos
            WHERE kept = 1
        """).fetchall()
        return [dict(r) for r in rows]


def _row_older_than(row, max_days) -> bool:
    if not max_days or max_days < 0:
        return False
    # A row with recorded_at NULL/malformed previously fell through to
    # the `except` below and was silently treated as "never old enough
    # to purge" — such rows could only ever be purged via the storage-
    # size limit, and would accumulate forever if an operator configures
    # only a day-based limit (IN-05). Fall back to processed_at (always
    # NOT NULL) so the row is still subject to age-based purging.
    try:
        dt = datetime.fromisoformat(row["recorded_at"])
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(row["processed_at"])
        except (ValueError, TypeError):
            return False
    age_days = (datetime.now() - dt).days
    return age_days > max_days


def get_purgeable_videos(
    blank_days: Optional[int],
    blank_gb: Optional[float],
    kept_days: Optional[int],
    kept_gb: Optional[float],
    grace_days: int = 7,
) -> dict:
    """
    Return videos eligible for file deletion under the configured retention policy.
    Returns two lists: blank_videos and kept_videos, each sorted oldest first.

    grace_days: videos processed within this many days are never purged, regardless
                of recorded_at. Prevents accidental purge during active backfill.
    """
    with get_conn() as conn:
        grace_cutoff = f"-{grace_days} days"

        blank_rows = conn.execute("""
            SELECT id, filepath, filename, recorded_at, file_size_mb,
                   has_animal, has_person, processed_at
            FROM videos
            WHERE has_animal = 0 AND has_person = 0
              AND kept = 0
              AND filepath IS NOT NULL
              AND file_purged_at IS NULL
              AND processed_at < DATETIME('now', ?)
            ORDER BY recorded_at ASC
        """, (grace_cutoff,)).fetchall()

        kept_rows = conn.execute("""
            SELECT id, filepath, filename, recorded_at, file_size_mb,
                   has_animal, has_person, processed_at
            FROM videos
            WHERE (has_animal = 1 OR has_person = 1)
              AND kept = 1
              AND filepath IS NOT NULL
              AND file_purged_at IS NULL
              AND processed_at < DATETIME('now', ?)
            ORDER BY recorded_at ASC
        """, (grace_cutoff,)).fetchall()

    def should_purge_by_age(row, max_days):
        # Delegates to the module-level _row_older_than() so there is exactly
        # one implementation of the recorded_at -> processed_at fallback
        # (also reused by get_raw_cleanup_candidates()).
        return _row_older_than(row, max_days)

    def apply_limits(rows, max_days, max_gb):
        """Return rows that should be purged based on age and/or storage limits."""
        to_purge = []
        already_flagged = set()

        # Age-based: flag all older than max_days
        if max_days:
            for r in rows:
                if should_purge_by_age(r, max_days):
                    to_purge.append(dict(r))
                    already_flagged.add(r["id"])

        # Storage-based: if total size exceeds max_gb, add oldest until under limit.
        # Only considers records with known (non-zero) file sizes — skipping records
        # with NULL/0 size prevents an infinite loop where freed never increases.
        if max_gb:
            sized_rows = [r for r in rows if (r["file_size_mb"] or 0) > 0]
            total_mb = sum(r["file_size_mb"] for r in sized_rows)
            total_gb = total_mb / 1024
            if total_gb > max_gb:
                overage_mb = (total_gb - max_gb) * 1024
                freed = 0.0
                for r in sized_rows:
                    if r["id"] not in already_flagged and freed < overage_mb:
                        to_purge.append(dict(r))
                        already_flagged.add(r["id"])
                        freed += r["file_size_mb"]

        return to_purge

    return {
        "blank": apply_limits(blank_rows, blank_days, blank_gb),
        "kept":  apply_limits(kept_rows,  kept_days,  kept_gb),
    }


def get_raw_cleanup_candidates(retention_days: int) -> list[dict]:
    """
    Return videos rows whose archived copy is old enough that the NAS
    raw_recordings source file should be verified-and-deleted.

    These are candidates only — the caller (nas_sync.sh) performs archive-
    existence and byte-size verification (CLEANUP-02, D-03/D-04) before
    deleting anything. This function never touches the filesystem.

    A single unified query (no kept/blank split) is used deliberately:
    CLEANUP-01 is one retention setting, not two — every row with a live
    archive copy is a candidate under the same raw_recordings_retention_days
    setting, unlike get_purgeable_videos()'s blank/kept split which has two
    different retention knobs.

    Mirrors _row_older_than()'s recorded_at -> processed_at fallback so the
    IN-05 bug class (rows silently never purged when recorded_at is NULL/
    malformed) is not reintroduced here.
    """
    if not retention_days:
        return []
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, filepath, filename, camera_name, recorded_at,
                   processed_at, file_size_mb
            FROM videos
            WHERE filepath IS NOT NULL
              AND file_purged_at IS NULL
              AND raw_purged_at IS NULL
            ORDER BY recorded_at ASC
        """).fetchall()
    return [dict(r) for r in rows if _row_older_than(r, retention_days)]


def purge_video_file(video_id: int) -> bool:
    """
    Delete the physical video file and null out filepath in the database.
    All DB records (detections, species, crops) are preserved.
    For blank videos the file is already deleted by the processor — we just
    update the DB record to mark it as purged.
    Returns True if a file was physically deleted, False if already missing.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filepath FROM videos WHERE id=?", (video_id,)
        ).fetchone()
        if not row:
            return False

        filepath = row["filepath"]
        deleted = False
        if filepath:
            p = Path(filepath)
            if p.exists():
                try:
                    p.unlink()
                    deleted = True
                except OSError as exc:
                    log.error("Failed to delete video file %s: %s", filepath, exc)
                    return False

        # Only mark as purged in DB if file was actually deleted (or didn't exist)
        conn.execute(
            "UPDATE videos SET filepath=NULL, file_purged_at=? WHERE id=?",
            (datetime.now().isoformat(), video_id),
        )
        return deleted


def mark_raw_purged(video_id: int) -> None:
    """
    Stamp videos.raw_purged_at for a row whose NAS raw_recordings source file
    was just deleted by the caller.

    Unlike purge_video_file(), this does NOT touch filepath or file_purged_at:
    the file being deleted lives at a path reconstructed by the caller (never
    stored in the DB), and filepath still legitimately points at the
    surviving archive copy, which must remain addressable after this call.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET raw_purged_at=? WHERE id=?",
            (datetime.now().isoformat(), video_id),
        )


def get_blank_videos(
    page: int = 1,
    per_page: int = 20,
    camera: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Return paginated blank videos (no detections), newest first."""
    conditions = [
        "v.has_animal = 0 AND v.has_person = 0",
        "v.kept = 0",
    ]
    params: list = []

    if camera:
        conditions.append("v.camera_name = ?")
        params.append(camera)
    if search:
        conditions.append("v.filename LIKE ?")
        params.append(f"%{search}%")
    if date_from:
        conditions.append("DATE(v.recorded_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(v.recorded_at) <= ?")
        params.append(date_to)

    where = "WHERE " + " AND ".join(conditions)
    offset = (page - 1) * per_page

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM videos v {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT v.id, v.filename, v.filepath, v.camera_name,
                       v.recorded_at, v.file_size_mb, v.duration_secs,
                       v.thumbnail_path, v.processed_at, v.file_purged_at
                FROM videos v
                {where}
                ORDER BY v.recorded_at DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "pages":    max(1, -(-total // per_page)),
        "per_page": per_page,
        "videos":   [dict(r) for r in rows],
    }



def get_blacklist() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM blacklist ORDER BY common_name"
        ).fetchall()
    return [dict(r) for r in rows]


def add_to_blacklist(label: str, common_name: str, scientific_name: str, note: str = "") -> dict:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO blacklist (label, common_name, scientific_name, created_at, note)
               VALUES (?,?,?,?,?)
               ON CONFLICT(label) DO UPDATE SET
                 common_name=excluded.common_name,
                 scientific_name=excluded.scientific_name,
                 note=excluded.note""",
            (label, common_name, scientific_name, datetime.now().isoformat(), note),
        )
    return {"ok": True}


def remove_from_blacklist(label: str) -> dict:
    with get_conn() as conn:
        conn.execute("DELETE FROM blacklist WHERE label=?", (label,))
    return {"ok": True}


def get_blacklist_affected_count(label: str) -> int:
    """Count videos with detections of a given species label."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(DISTINCT d.video_id) FROM species s
            JOIN detections d ON s.detection_id = d.id
            WHERE s.label = ?
        """, (label,)).fetchone()
    return row[0] if row else 0


def requeue_species(label: str) -> int:
    """Mark all kept videos with detections of label for SpeciesNet reprocessing."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE videos SET needs_reprocess=1
            WHERE kept=1 AND id IN (
                SELECT DISTINCT d.video_id FROM detections d
                JOIN species s ON s.detection_id = d.id
                WHERE s.label = ?
            )
        """, (label,))
        count = conn.total_changes
    return count


def get_reprocess_queue() -> list:
    """Return kept videos flagged for SpeciesNet reprocessing."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, filename, filepath, camera_name, recorded_at
            FROM videos
            WHERE needs_reprocess=1 AND kept=1 AND filepath IS NOT NULL
            ORDER BY recorded_at
        """).fetchall()
    return [dict(r) for r in rows]


def clear_reprocess_flag(video_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE videos SET needs_reprocess=0 WHERE id=?", (video_id,))


def _fanout_detection_ids(conn, video_id: int, original_label: str) -> list:
    """
    Return the list of detection ids matching (video_id, original_label) at
    this moment — the video-player write path's fan-out target set (D-01).

    This predicate MUST stay character-for-character identical to
    HAS_VIDEO_CORRECTION's correlated subquery match (d.video_id equality
    plus raw s.label equality, database.py's HAS_VIDEO_CORRECTION comment
    block) — plan 14-03's backfill script uses the identical predicate, and
    the two drifting apart is the single highest-value invariant in this
    phase.
    """
    rows = conn.execute(
        "SELECT d.id FROM detections d JOIN species s ON s.detection_id = d.id "
        "WHERE d.video_id = ? AND s.label = ?",
        (video_id, original_label),
    ).fetchall()
    return [r[0] for r in rows]


def save_video_correction(
    video_id: int,
    original_label: str,
    corrected_label: Optional[str],
    corrected_common: Optional[str],
    corrected_scientific: Optional[str],
    note: str = "",
) -> Optional[int]:
    """
    Fan out a video-level species correction (video-player per-crop editor
    write path) into one species_corrections row per detection currently
    matching (video_id, original_label) — a write-time SNAPSHOT (D-01): a
    detection added to this video+label group later does NOT inherit this
    correction; the operator must reapply it.

    Returns the number of detections this correction was applied to (an
    int; 0 when original_label matches no current detection on an existing
    video), or None if video_id doesn't reference an existing video — the
    caller's `is None` 404 guard (IN-02) is preserved unchanged; 0 is not
    None, so a no-match save still returns cleanly rather than raising.

    corrected_label=None fans out as suppressed=1 rows (the video player's
    "Suppress this species" action, D-06's replacement for the legacy
    correction table's NULL-corrected_label sentinel); any other
    corrected_label fans out as suppressed=0. A single
    datetime.now().isoformat() timestamp is stamped once, before the
    fan-out loop, and shared by every row this save writes via
    conn.executemany — an intra-fan-out timestamp skew would make D-03
    precedence non-deterministic within a single save. This duplicates
    _upsert_species_correction()'s UPSERT SQL literal rather than calling
    that helper per detection id, precisely so one shared stamp (not one
    freshly computed per call) is used across the whole fan-out.

    Does not write to the legacy correction table at all (D-06 freeze) —
    that table is frozen read-only from this phase forward.
    """
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM videos WHERE id=?", (video_id,)).fetchone()
        if not exists:
            return None
        detection_ids = _fanout_detection_ids(conn, video_id, original_label)
        suppressed = 1 if corrected_label is None else 0
        stamp = datetime.now().isoformat()
        conn.executemany(
            """INSERT INTO species_corrections
               (detection_id, corrected_label, corrected_common, corrected_scientific,
                suppressed, source, corrected_at, note)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(detection_id) DO UPDATE SET
                 corrected_label=excluded.corrected_label,
                 corrected_common=excluded.corrected_common,
                 corrected_scientific=excluded.corrected_scientific,
                 suppressed=excluded.suppressed,
                 source=excluded.source,
                 corrected_at=excluded.corrected_at,
                 note=excluded.note""",
            [
                (
                    det_id, corrected_label, corrected_common, corrected_scientific,
                    suppressed, "video_player", stamp, note,
                )
                for det_id in detection_ids
            ],
        )
    return len(detection_ids)


def get_video_corrections(video_id: int) -> list:
    """
    Read the frozen legacy video_corrections table for video_id (D-06: still
    readable, never written, by any code path after Phase 14's cutover).
    GET /api/corrections?video_id= has no frontend caller (grep-verified —
    static/index.html's only reference to '/api/corrections' is the POST in
    applyCorrection()), so this is deliberately left reading the frozen
    table rather than rewired to species_corrections (RESEARCH.md Open
    Question 2).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM video_corrections WHERE video_id=? ORDER BY corrected_at",
            (video_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_correction(correction_id: int):
    """Delete a single species_corrections row by id (replaces
    delete_video_correction() — D-00/CORR-01, the unified table is the only
    one any write path targets after Phase 14's cutover)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM species_corrections WHERE id=?", (correction_id,))


def apply_corrections_to_species(species_list: list, corrections: list) -> list:
    """
    Post-processing: overlay video_corrections onto a species list.
    corrections is the result of get_video_corrections(video_id).
    Returns species_list with corrected entries modified in place.

    Retained, unreferenced pending D-07 (legacy-table removal follow-up
    phase): get_video_by_id() stopped calling this function in Phase 14
    plan 14-02's read-path cutover (species_corrections is now read
    directly via SQL) — kept defined only because the legacy table it
    overlays stays readable (D-06) until a future removal phase.
    """
    corr_map = {c["original_label"]: c for c in corrections}
    result = []
    for sp in species_list:
        label = sp.get("label")
        if label and label in corr_map:
            c = corr_map[label]
            if c["corrected_label"] is None:
                continue  # suppressed
            sp = dict(sp)
            sp["label"]           = c["corrected_label"]
            sp["common_name"]     = c["corrected_common"]
            sp["scientific_name"] = c["corrected_scientific"]
            sp["corrected"]       = True
            sp["original_label"]  = label
        result.append(sp)
    return result


def search_taxonomy(
    query: str,
    classes_path: str,
    country: Optional[str] = None,
    admin1: Optional[str] = None,
    limit: int = 20,
    all_regions: bool = False,
) -> list:
    """
    Search SpeciesNet taxonomy from cached classes JSON.
    Parses speciesnet_classes.json once on first call and caches the result
    for the lifetime of the process. Geographic filtering by country/admin1
    is applied when those parameters are non-empty and all_regions is False.
    """
    global _taxonomy_cache
    if _taxonomy_cache is None:
        try:
            with open(classes_path) as f:
                _taxonomy_cache = json.load(f)
        except (FileNotFoundError, ValueError):
            return []

    q = query.lower().strip()
    if len(q) < 2:
        return []

    results = []
    for entry in _taxonomy_cache:
        label      = entry.get("label", "")
        common     = (entry.get("common_name") or "").lower()
        scientific = (entry.get("scientific_name") or "").lower()

        # Skip blank/unknown pseudo-labels
        if not label or label.endswith(";;;;;;blank") or label == "Unknown species":
            continue

        # Text match
        if q not in common and q not in scientific:
            continue

        # Geographic filtering — skip when all_regions=True or no filter params given
        if not all_regions and (country or admin1):
            entry_country = (entry.get("country") or "").upper()
            entry_admin1  = (entry.get("admin1") or "").upper()
            if country and entry_country and entry_country != country.upper():
                continue
            if admin1 and entry_admin1 and entry_admin1 != admin1.upper():
                continue

        results.append({
            "label":           entry.get("label"),
            "common_name":     entry.get("common_name"),
            "scientific_name": entry.get("scientific_name"),
        })

        if len(results) >= limit:
            break

    return results


def promote_paired_blanks() -> int:
    """
    If one lens of a dual-lens pair detected an animal or person, mark the
    other lens as kept=1 too — even if it had no detections itself.
    Both lenses are recorded simultaneously and should be kept or discarded together.
    Returns the number of videos promoted.
    """
    with get_conn() as conn:
        conn.execute("""
            UPDATE videos
            SET kept = 1
            WHERE kept = 0
              AND paired_video_id IS NOT NULL
              AND paired_video_id IN (
                  SELECT id FROM videos WHERE kept = 1
              )
        """)
        count = conn.total_changes
    return count


def get_storage_stats() -> dict:
    """Return storage usage broken out by blank vs kept vs purged videos."""
    with get_conn() as conn:
        blank = conn.execute("""
            SELECT COUNT(*) as count,
                   COALESCE(SUM(file_size_mb), 0) as total_mb
            FROM videos
            WHERE has_animal=0 AND has_person=0
              AND kept=0
              AND file_purged_at IS NULL
        """).fetchone()

        kept = conn.execute("""
            SELECT COUNT(*) as count,
                   COALESCE(SUM(file_size_mb), 0) as total_mb
            FROM videos
            WHERE (has_animal=1 OR has_person=1)
              AND kept=1
              AND file_purged_at IS NULL
        """).fetchone()

        purged = conn.execute("""
            SELECT COUNT(*) as count,
                   COALESCE(SUM(file_size_mb), 0) as total_mb
            FROM videos WHERE file_purged_at IS NOT NULL
        """).fetchone()

        raw = conn.execute("""
            SELECT COUNT(*) as count,
                   COALESCE(SUM(file_size_mb), 0) as total_mb
            FROM videos WHERE raw_purged_at IS NOT NULL
        """).fetchone()

    return {
        "blank_videos":        blank["count"],
        "blank_gb":            round(blank["total_mb"] / 1024, 2),
        "kept_videos":         kept["count"],
        "kept_gb":             round(kept["total_mb"] / 1024, 2),
        "purged_videos":       purged["count"],
        "purged_gb_reclaimed": round(purged["total_mb"] / 1024, 2),
        "total_active_gb":     round((blank["total_mb"] + kept["total_mb"]) / 1024, 2),
        "raw_purged_videos":   raw["count"],
        "raw_gb_reclaimed":    round(raw["total_mb"] / 1024, 2),
    }


def insert_crop(
    detection_id: int,
    crop_path: str,
    quality_score: float,
    sharpness: float,
    brightness: float,
    contrast: float,
    pixel_area: int,
    width: int,
    height: int,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO crops
               (detection_id, crop_path, quality_score, sharpness, brightness,
                contrast, pixel_area, width, height, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                detection_id, crop_path, quality_score, sharpness, brightness,
                contrast, pixel_area, width, height, datetime.now().isoformat(),
            ),
        )
        return cur.lastrowid


# ── Read helpers ───────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with get_conn() as conn:
        total_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        kept_videos  = conn.execute("SELECT COUNT(*) FROM videos WHERE kept=1").fetchone()[0]
        total_animal_events = conn.execute(
            "SELECT COUNT(DISTINCT video_id) FROM detections WHERE category='animal'"
        ).fetchone()[0]
        total_person_events = conn.execute(
            "SELECT COUNT(DISTINCT video_id) FROM detections WHERE category='person'"
        ).fetchone()[0]
        total_species = conn.execute(
            f"""SELECT COUNT(DISTINCT s.label)
                FROM species s
                JOIN detections d ON s.detection_id = d.id
                WHERE {KNOWN_SPECIES_FILTER}"""
        ).fetchone()[0]
        total_detections = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
        total_crops = conn.execute("SELECT COUNT(*) FROM crops").fetchone()[0]

        # Last 7 days activity broken out by species
        activity_raw = conn.execute(f"""
            WITH RECURSIVE dates(day) AS (
                SELECT DATE('now', '-6 days')
                UNION ALL
                SELECT DATE(day, '+1 day')
                FROM dates WHERE day < DATE('now')
            )
            SELECT dates.day,
                   s.common_name as species,
                   s.label,
                   COUNT(DISTINCT v.id) as count
            FROM dates
            LEFT JOIN videos v
                ON DATE(v.recorded_at) = dates.day AND v.kept = 1
            LEFT JOIN detections d ON v.id = d.video_id
            LEFT JOIN species s ON s.detection_id = d.id
                AND {KNOWN_SPECIES_FILTER}
                AND s.label != 'Unknown species'
            GROUP BY dates.day, s.label
            ORDER BY dates.day
        """).fetchall()

        # Top 5 species — exclude Unknown species from this list entirely
        # since it's not a real species and dominates the chart unhelpfully
        top_species = conn.execute(f"""
            SELECT {DISPLAY_COMMON} AS common_name, s.label, COUNT(*) as cnt
            FROM species s
            JOIN detections d ON s.detection_id = d.id
            WHERE {KNOWN_SPECIES_FILTER}
              AND s.label != 'Unknown species'
            GROUP BY s.label
            ORDER BY cnt DESC LIMIT 5
        """).fetchall()

        # Most recent detection
        latest = conn.execute("""
            SELECT v.filename, v.recorded_at
            FROM videos v
            WHERE v.kept = 1
            ORDER BY v.recorded_at DESC LIMIT 1
        """).fetchone()

        return {
            "total_videos":           total_videos,
            "kept_videos":            kept_videos,
            "animal_events":          total_animal_events,
            "person_events":          total_person_events,
            "unique_species":         total_species,
            "total_detections":       total_detections,
            "total_crops":            total_crops,
            "activity_7d_by_species": [dict(r) for r in activity_raw],
            "top_species":            [dict(r) for r in top_species],
            "latest":                 dict(latest) if latest else None,
        }


def get_species_list() -> list:
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT
                s.label,
                {DISPLAY_COMMON}     AS common_name,
                {DISPLAY_SCIENTIFIC} AS scientific_name,
                s.common_name        AS ai_common_name,
                COUNT(DISTINCT d.video_id) AS video_count,
                COUNT(*) AS detection_count,
                MAX(v.recorded_at) AS last_seen,
                MIN(v.recorded_at) AS first_seen,
                MAX({HAS_CORRECTION}) AS has_correction,
                (SELECT c.crop_path FROM crops c
                 JOIN detections d2 ON c.detection_id = d2.id
                 JOIN species s2 ON s2.detection_id = d2.id
                 WHERE s2.label = s.label
                 ORDER BY c.quality_score DESC LIMIT 1) AS best_crop
            FROM species s
            JOIN detections d ON s.detection_id = d.id
            JOIN videos v ON d.video_id = v.id
            WHERE {KNOWN_SPECIES_FILTER}
            GROUP BY s.label
            ORDER BY detection_count DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_species_detail(label: str) -> dict:
    with get_conn() as conn:
        info = conn.execute("""
            SELECT common_name, scientific_name, COUNT(*) as total_detections
            FROM species WHERE label = ?
        """, (label,)).fetchone()

        trend = conn.execute("""
            SELECT DATE(v.recorded_at) as day, COUNT(*) as count
            FROM species s
            JOIN detections d ON s.detection_id = d.id
            JOIN videos v ON d.video_id = v.id
            WHERE s.label = ?
            GROUP BY day ORDER BY day
        """, (label,)).fetchall()

        # Selects the same detection-identifying columns as get_gallery() (label,
        # detection_id, top_candidates_json, corrected common/scientific name,
        # confidence) so the frontend can route this grid through the same
        # openDetectionCorrection entry point the main Gallery tab uses, instead
        # of a plain video-open click — see WR-07.
        crops = conn.execute(f"""
            SELECT c.crop_path, c.quality_score, v.id as video_id,
                   v.filename, v.recorded_at,
                   s.label, s.detection_id, s.top_candidates_json,
                   s.confidence          AS species_confidence,
                   {EFFECTIVE_COMMON}     AS common_name,
                   {EFFECTIVE_SCIENTIFIC} AS scientific_name,
                   {HAS_CORRECTION}     AS has_correction
            FROM crops c
            JOIN detections d ON c.detection_id = d.id
            JOIN species s ON s.detection_id = d.id
            JOIN videos v ON d.video_id = v.id
            WHERE s.label = ?
            ORDER BY c.quality_score DESC LIMIT 50
        """, (label,)).fetchall()

        videos = conn.execute("""
            SELECT DISTINCT v.id, v.filename, v.recorded_at, v.thumbnail_path, v.duration_secs
            FROM videos v
            JOIN detections d ON v.id = d.video_id
            JOIN species s ON s.detection_id = d.id
            WHERE s.label = ?
            ORDER BY v.recorded_at DESC LIMIT 20
        """, (label,)).fetchall()

        return {
            "info":   dict(info) if info else {},
            "label":  label,
            "trend":  [dict(r) for r in trend],
            "crops":  [dict(r) for r in crops],
            "videos": [dict(r) for r in videos],
        }


def get_gallery(
    species_label: Optional[str] = None,
    camera_name: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_confidence: Optional[float] = None,
    sort_by: str = "quality",
    page: int = 1,
    per_page: int = 40,
) -> dict:
    offset = (page - 1) * per_page
    order = "c.quality_score DESC" if sort_by == "quality" else "v.recorded_at DESC"

    conditions = [KNOWN_SPECIES_FILTER]
    params = []
    if species_label:
        conditions.append("s.label = ?")
        params.append(species_label)
    if camera_name:
        conditions.append("v.camera_name = ?")
        params.append(camera_name)
    if date_from:
        conditions.append("DATE(v.recorded_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(v.recorded_at) <= ?")
        params.append(date_to)
    # min_confidence uses an explicit `is not None` guard (not truthiness) because
    # 0.0 is a meaningful threshold. `s.confidence >= ?` is false for a NULL
    # confidence, so min_confidence=0.0 excludes never-scored rows while
    # min_confidence=None (the "no filter" case) includes them.
    if min_confidence is not None:
        conditions.append("s.confidence >= ?")
        params.append(min_confidence)
    where = "WHERE " + " AND ".join(conditions)

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM crops c JOIN detections d ON c.detection_id=d.id "
            f"JOIN species s ON s.detection_id=d.id "
            f"JOIN videos v ON d.video_id = v.id {where}", params
        ).fetchone()[0]

        rows = conn.execute(f"""
            SELECT c.crop_path, c.quality_score, c.width, c.height,
                   s.label,
                   {EFFECTIVE_COMMON}     AS common_name,
                   {EFFECTIVE_SCIENTIFIC} AS scientific_name,
                   s.common_name        AS ai_common_name,
                   s.detection_id,
                   s.top_candidates_json,
                   s.confidence          AS species_confidence,
                   {HAS_CORRECTION} AS has_correction,
                   v.id as video_id, v.filename, v.recorded_at
            FROM crops c
            JOIN detections d ON c.detection_id = d.id
            JOIN species s ON s.detection_id = d.id
            JOIN videos v ON d.video_id = v.id
            {where}
            ORDER BY {order}, c.id DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

        return {
            "items":    [dict(r) for r in rows],
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    max(1, (total + per_page - 1) // per_page),
        }


def _run_row_to_dict(row) -> dict:
    """
    Convert one sqlite3.Row from the runs table into the dict shape every reader
    consumes: the raw columns plus three derived keys (duration_secs, cameras,
    offline_cameras). A malformed historical cameras_json/offline_cameras_json
    value never raises here — it just falls back to an empty container so one
    bad row can't break a whole page of run history.
    """
    d = dict(row)

    try:
        if d.get("end_time"):
            start = datetime.fromisoformat(d["start_time"])
            end = datetime.fromisoformat(d["end_time"])
            d["duration_secs"] = (end - start).total_seconds()
        else:
            d["duration_secs"] = None
    except (TypeError, ValueError):
        d["duration_secs"] = None

    try:
        d["cameras"] = json.loads(d["cameras_json"]) if d.get("cameras_json") else {}
    except (TypeError, ValueError):
        d["cameras"] = {}

    try:
        d["offline_cameras"] = json.loads(d["offline_cameras_json"]) if d.get("offline_cameras_json") else []
    except (TypeError, ValueError):
        d["offline_cameras"] = []

    return d


def get_recent_runs(limit: int = 30) -> list:
    """Return the most recent runs, newest start_time first. limit is clamped to 1..100."""
    limit = max(1, min(int(limit), 100))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY start_time DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_run_row_to_dict(r) for r in rows]


def get_last_run() -> Optional[dict]:
    """Return the most recently started run, or None if no run has ever been recorded."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM runs ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        return _run_row_to_dict(row) if row else None


def get_run_by_id(run_id: int) -> Optional[dict]:
    """Return the full run dict (including untruncated error_summary), or None if unknown."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run_row_to_dict(row) if row else None


def get_camera_offline_flags(current_cameras: dict, lookback: int = 5) -> list:
    """
    Compare this run's camera set against the last `lookback` completed runs.
    Returns camera names present in that recent history but absent (zero videos)
    this run — a possible-offline-camera signal (D-04).

    `lookback` (default 5 completed runs) is the tunable knob for the tradeoff
    between false positives (too small a window flags a camera after a single
    quiet night) and detection delay (too large a window takes longer to notice
    a genuinely offline camera) — see 02-RESEARCH.md assumption A5.

    current_cameras may be either the {"name": {"videos": n, ...}} snapshot shape
    or a bare {"name": n} shape; both are accepted.

    A run that synced zero videos across every camera is a quiet night, not a
    fleet of offline cameras (D-08 quiet-night semantics) — this returns []
    immediately without querying in that case.
    """
    def _video_count(v):
        if isinstance(v, dict):
            return v.get("videos", 0) or 0
        return v or 0

    current = {name for name, v in current_cameras.items() if _video_count(v) > 0}
    if not current:
        return []

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cameras_json FROM runs
               WHERE status IS NOT NULL AND cameras_json IS NOT NULL
               ORDER BY start_time DESC LIMIT ?""",
            (lookback,),
        ).fetchall()

    recent_cameras: set = set()
    for row in rows:
        try:
            recent_cameras |= set(json.loads(row["cameras_json"]).keys())
        except (TypeError, ValueError):
            continue

    return sorted(recent_cameras - current)


def get_cameras() -> list:
    """Return all distinct camera names that have kept videos, sorted alphabetically."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT camera_name, COUNT(*) as video_count,
                   MAX(recorded_at) as last_seen
            FROM videos
            WHERE kept = 1 AND camera_name IS NOT NULL AND camera_name != ''
            GROUP BY camera_name
            ORDER BY camera_name
        """).fetchall()
        return [dict(r) for r in rows]


def get_videos(
    species_label: Optional[str] = None,
    has_person: Optional[bool] = None,
    camera_name: Optional[str] = None,
    has_species: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    search: Optional[str] = None,
) -> dict:
    conditions = ["v.kept = 1"]
    params = []

    if species_label:
        conditions.append("""
            v.id IN (SELECT d.video_id FROM detections d
                     JOIN species s ON s.detection_id=d.id WHERE s.label=?)
        """)
        params.append(species_label)
    if has_person is not None:
        conditions.append("v.has_person = ?")
        params.append(int(has_person))
    if camera_name:
        conditions.append("v.camera_name = ?")
        params.append(camera_name)
    if date_from:
        conditions.append("DATE(v.recorded_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(v.recorded_at) <= ?")
        params.append(date_to)
    if has_species is True:
        conditions.append(f"""
            v.id IN (SELECT d.video_id FROM detections d
                     JOIN species s ON s.detection_id=d.id
                     WHERE {BLANK_LABEL_FILTER} AND s.label != 'Unknown species')
        """)
    elif has_species is False:
        conditions.append(f"""
            v.id NOT IN (SELECT d.video_id FROM detections d
                         JOIN species s ON s.detection_id=d.id
                         WHERE {BLANK_LABEL_FILTER} AND s.label != 'Unknown species')
        """)
    if search:
        conditions.append("(v.filename LIKE ? OR v.camera_name LIKE ? OR v.id IN "
                          "(SELECT d.video_id FROM detections d JOIN species s ON s.detection_id=d.id "
                          "WHERE s.common_name LIKE ? OR s.label LIKE ?))")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    offset = (page - 1) * per_page

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM videos v {where}", params
        ).fetchone()[0]

        rows = conn.execute(f"""
            SELECT v.id, v.filename, v.camera_name, v.recorded_at, v.duration_secs,
                   v.has_animal, v.has_person, v.thumbnail_path,
                   v.lens_index, v.paired_video_id,
                   GROUP_CONCAT(DISTINCT CASE WHEN {KNOWN_SPECIES_FILTER}
                       THEN {EFFECTIVE_COMMON} END) as species_list
            FROM videos v
            LEFT JOIN detections d ON v.id = d.video_id
            LEFT JOIN species s ON s.detection_id = d.id
            {where}
            GROUP BY v.id
            ORDER BY v.recorded_at DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

        return {
            "items":    [dict(r) for r in rows],
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    max(1, (total + per_page - 1) // per_page),
        }


def get_video_by_id(video_id: int) -> dict:
    """
    Every detection dict this returns (both the primary video's and, when
    paired, the other lens's) reads species_corrections directly — no
    Python-side correction overlay remains in this read path (retired,
    pending D-07 removal). `label` is always the RAW SpeciesNet label;
    `original_label` duplicates it under the name the re-correct action
    posts back as `original_label`, so the write-time fan-out
    (save_video_correction()) matches on the correct raw value.
    `corrected` is HAS_CORRECTION — true for either write path (Gallery
    popover OR video-player editor), a deliberate widening from the old
    overlay's video-player-only signal (RESEARCH.md Pitfall 5).
    Suppression (species_corrections.suppressed=1) is the only thing that
    removes a row from the two SELECTs below; it does not affect any other
    reader (get_gallery(), get_species_detail(), get_videos(),
    get_species_list()).
    """
    with get_conn() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not video:
            return {}

        detections = conn.execute(f"""
            SELECT d.id, d.frame_number, d.timestamp_secs, d.category, d.confidence,
                   s.label, {EFFECTIVE_COMMON} as common_name, {EFFECTIVE_SCIENTIFIC} as scientific_name,
                   {HAS_CORRECTION} as corrected, s.label as original_label,
                   s.top_candidates_json,
                   c.crop_path, c.quality_score
            FROM detections d
            LEFT JOIN species s ON s.detection_id = d.id
            LEFT JOIN crops c ON c.detection_id = d.id
            WHERE d.video_id = ?
              AND ({KNOWN_SPECIES_FILTER} OR s.label IS NULL)
              AND NOT {IS_SUPPRESSED_DETECTION}
            ORDER BY d.timestamp_secs
        """, (video_id,)).fetchall()

        det_list = [dict(r) for r in detections]

        # get_video_corrections() reads the frozen legacy correction table
        # on purpose (RESEARCH.md Open Question 2, D-06) — kept only for
        # this response's `corrections` key, whose shape is unchanged.
        corrections = get_video_corrections(video_id)

        # Fetch paired lens video if this is a dual-lens camera
        paired = None
        pair_detections = []
        pair_id = dict(video).get("paired_video_id")
        if pair_id:
            paired_row = conn.execute("SELECT * FROM videos WHERE id=?", (pair_id,)).fetchone()
            if paired_row:
                paired = dict(paired_row)
                pair_rows = conn.execute(f"""
                    SELECT d.id, d.frame_number, d.timestamp_secs, d.category, d.confidence,
                           s.label, {EFFECTIVE_COMMON} as common_name, {EFFECTIVE_SCIENTIFIC} as scientific_name,
                           {HAS_CORRECTION} as corrected, s.label as original_label,
                           s.top_candidates_json,
                           c.crop_path, c.quality_score
                    FROM detections d
                    LEFT JOIN species s ON s.detection_id = d.id
                    LEFT JOIN crops c ON c.detection_id = d.id
                    WHERE d.video_id = ?
                      AND ({KNOWN_SPECIES_FILTER} OR s.label IS NULL)
                      AND NOT {IS_SUPPRESSED_DETECTION}
                    ORDER BY d.timestamp_secs
                """, (pair_id,)).fetchall()
                pair_detections = [dict(r) for r in pair_rows]

        return {
            "video":           dict(video),
            "detections":      det_list,
            "corrections":     corrections,
            "paired":          paired,
            "pair_detections": pair_detections,
        }


def get_timeline(
    days: Optional[int] = 30,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """
    Return detection counts by species grouped by day, week, or month depending
    on the window size. Supports either a days lookback or an explicit date range.
    """
    with get_conn() as conn:
        # Build WHERE clause
        if date_from or date_to:
            conditions = []
            params = []
            if date_from:
                conditions.append("DATE(v.recorded_at) >= ?")
                params.append(date_from)
            if date_to:
                conditions.append("DATE(v.recorded_at) <= ?")
                params.append(date_to)
            where = "AND " + " AND ".join(conditions)
            # Calculate window size for granularity decision
            if date_from and date_to:
                d1 = date.fromisoformat(date_from)
                d2 = date.fromisoformat(date_to)
                window_days = (d2 - d1).days
            elif date_from:
                window_days = (date.today() - date.fromisoformat(date_from)).days
            else:
                # Only date_to known — use a large sentinel so granularity defaults to month
                window_days = (date.fromisoformat(date_to) - date(2020, 1, 1)).days
        else:
            n = int(days or 30)
            where = "AND v.recorded_at >= DATE('now', '-' || ? || ' days')"
            params = [str(n)]
            window_days = n

        # Choose granularity based on window size
        if window_days > 365:
            # Monthly — show YYYY-MM
            period_expr = "STRFTIME('%Y-%m', v.recorded_at)"
        elif window_days > 90:
            # Weekly — show start of week (Monday)
            period_expr = "DATE(v.recorded_at, 'weekday 1', '-6 days')"
        else:
            # Daily
            period_expr = "DATE(v.recorded_at)"

        rows = conn.execute(f"""
            SELECT
                {period_expr} as period,
                s.label,
                {DISPLAY_COMMON} AS common_name,
                COUNT(DISTINCT v.id) as count
            FROM videos v
            JOIN detections d ON v.id = d.video_id
            JOIN species s ON s.detection_id = d.id
            WHERE v.kept = 1 {where}
              AND {KNOWN_SPECIES_FILTER}
            GROUP BY period, s.label
            ORDER BY period
        """, params).fetchall()

        return {
            "rows":        [dict(r) for r in rows],
            "granularity": "month" if window_days > 365 else ("week" if window_days > 90 else "day"),
            "window_days": window_days,
        }


def search(query: str) -> dict:
    q = f"%{query}%"
    with get_conn() as conn:
        species = conn.execute("""
            SELECT DISTINCT label, common_name, scientific_name, COUNT(*) as cnt
            FROM species
            WHERE label LIKE ? OR common_name LIKE ? OR scientific_name LIKE ?
            GROUP BY label LIMIT 10
        """, (q, q, q)).fetchall()

        videos = conn.execute("""
            SELECT DISTINCT v.id, v.filename, v.recorded_at, v.thumbnail_path
            FROM videos v
            LEFT JOIN detections d ON v.id = d.video_id
            LEFT JOIN species s ON s.detection_id = d.id
            WHERE v.filename LIKE ? OR s.common_name LIKE ? OR s.label LIKE ?
            ORDER BY v.recorded_at DESC LIMIT 10
        """, (q, q, q)).fetchall()

        return {
            "species": [dict(r) for r in species],
            "videos":  [dict(r) for r in videos],
        }
