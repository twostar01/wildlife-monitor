"""
database.py — SQLite schema and query helpers for Wildlife Processor
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Optional

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
    paired_video_id INTEGER REFERENCES videos(id)  -- id of the other lens for dual-lens cameras
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
    corrected_at        TEXT            -- ISO datetime of last correction
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

CREATE INDEX IF NOT EXISTS idx_videos_recorded_at ON videos(recorded_at);
CREATE INDEX IF NOT EXISTS idx_videos_camera ON videos(camera_name);
CREATE INDEX IF NOT EXISTS idx_detections_video_id ON detections(video_id);
CREATE INDEX IF NOT EXISTS idx_species_label ON species(label);
CREATE INDEX IF NOT EXISTS idx_crops_quality ON crops(quality_score DESC);
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
ALTER TABLE videos ADD COLUMN paired_video_id INTEGER REFERENCES videos(id);
"""

# Labels to exclude from all dashboard queries.
# SpeciesNet returns ';;;;;;blank' when it determines a crop has no animal.
BLANK_LABEL_FILTER = "s.label NOT LIKE '%;;;;;;blank'"

# Suppression filter — exclude Unknown species and blank labels for any video
# that also has at least one real identified species. If a video has a known
# species, the Unknown/blank entries are just low-confidence frames of the
# same animal and clutter the display.
SUPPRESS_UNKNOWN_IF_IDENTIFIED = """(
    s.label != 'Unknown species'
    OR NOT EXISTS (
        SELECT 1 FROM species s2
        JOIN detections d2 ON s2.detection_id = d2.id
        WHERE d2.video_id = d.video_id
          AND s2.label != 'Unknown species'
          AND s2.label NOT LIKE '%;;;;;;blank'
    )
)"""

# Combined filter — always exclude blank, and suppress Unknown when a real
# species is present on the same video.
KNOWN_SPECIES_FILTER = f"{BLANK_LABEL_FILTER} AND {SUPPRESS_UNKNOWN_IF_IDENTIFIED}"

# SQL expression that returns the display name — user correction when set, else SpeciesNet common_name
DISPLAY_COMMON     = "COALESCE(NULLIF(s.user_common_name,''), s.common_name)"
DISPLAY_SCIENTIFIC = "COALESCE(NULLIF(s.user_scientific_name,''), s.scientific_name)"


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
                    paired_video_id INTEGER REFERENCES videos_new(id) ON DELETE SET NULL
                );
                INSERT INTO videos_new SELECT * FROM videos;
                DROP TABLE videos;
                ALTER TABLE videos_new RENAME TO videos;
                PRAGMA foreign_keys=ON;
            """)
            log.info("DB migration: filepath NOT NULL constraint removed")


# ── Write helpers ──────────────────────────────────────────────────────────────

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
    with get_conn() as conn:
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
    import re as _re
    stem = Path(filename).stem
    m = _re.match(r'^(.+)_(\d{2})_(\d{14})$', stem)
    if m:
        return m.group(1), int(m.group(2)), m.group(3)
    return None


def link_lens_pair(video_id: int, filename: str) -> Optional[int]:
    """
    After inserting a video, find its paired lens and link both rows.
    Returns the paired video's id if a pair was found and linked, else None.
    """
    parsed = parse_dual_lens_filename(filename)
    if parsed is None:
        return None
    camera_base, lens_index, timestamp = parsed

    with get_conn() as conn:
        # Find a video with the same camera_base + timestamp but different lens
        # Match on filename pattern: camera_base + any lens + same timestamp
        rows = conn.execute(
            "SELECT id, filename FROM videos WHERE id != ? AND filename LIKE ?",
            (video_id, f"{camera_base}\\_%\\_{timestamp}%"),
        ).fetchall()

        # Filter to only the other lens(es) for this camera base + timestamp
        import re as _re
        pair_id = None
        for row in rows:
            p = parse_dual_lens_filename(row["filename"])
            if p and p[0] == camera_base and p[2] == timestamp and p[1] != lens_index:
                pair_id = row["id"]
                break

        if pair_id is None:
            # Store lens_index anyway so we know which lens this is
            conn.execute(
                "UPDATE videos SET lens_index=? WHERE id=?",
                (lens_index, video_id),
            )
            return None

        # Link both rows to each other
        conn.execute(
            "UPDATE videos SET paired_video_id=?, lens_index=? WHERE id=?",
            (pair_id, lens_index, video_id),
        )
        conn.execute(
            "UPDATE videos SET paired_video_id=? WHERE id=? AND paired_video_id IS NULL",
            (video_id, pair_id),
        )
        # Also set lens_index on the pair if not already set
        p2 = parse_dual_lens_filename(
            conn.execute("SELECT filename FROM videos WHERE id=?", (pair_id,)).fetchone()["filename"]
        )
        if p2:
            conn.execute(
                "UPDATE videos SET lens_index=? WHERE id=? AND lens_index IS NULL",
                (p2[1], pair_id),
            )
        return pair_id


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
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO species
               (detection_id, label, common_name, scientific_name, confidence)
               VALUES (?,?,?,?,?)""",
            (detection_id, label, common_name, scientific_name, confidence),
        )
        return cur.lastrowid


def update_video_filepath(video_id: int, new_filepath: str):
    """Update the stored filepath for a video after it has been moved to the NAS archive."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE videos SET filepath = ? WHERE id = ?",
            (new_filepath, video_id),
        )


def correct_species(
    detection_id: int,
    user_common_name: str,
    user_scientific_name: str,
):
    """Save a human correction for a species detection. Pass empty strings to clear a correction."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE species
               SET user_common_name=?, user_scientific_name=?, corrected_at=?
               WHERE detection_id=?""",
            (
                user_common_name.strip() or None,
                user_scientific_name.strip() or None,
                datetime.now().isoformat() if (user_common_name or user_scientific_name) else None,
                detection_id,
            ),
        )
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
        if not max_days:
            return False
        try:
            dt = datetime.fromisoformat(row["recorded_at"])
            age_days = (datetime.now() - dt).days
            return age_days > max_days
        except (ValueError, TypeError):
            return False

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
                except OSError:
                    pass

        # Always null out filepath and record purge time
        conn.execute(
            "UPDATE videos SET filepath=NULL, file_purged_at=? WHERE id=?",
            (datetime.now().isoformat(), video_id),
        )
        return deleted


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

    return {
        "blank_videos":        blank["count"],
        "blank_gb":            round(blank["total_mb"] / 1024, 2),
        "kept_videos":         kept["count"],
        "kept_gb":             round(kept["total_mb"] / 1024, 2),
        "purged_videos":       purged["count"],
        "purged_gb_reclaimed": round(purged["total_mb"] / 1024, 2),
        "total_active_gb":     round((blank["total_mb"] + kept["total_mb"]) / 1024, 2),
    }


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

    return {
        "blank_videos":        blank["count"],
        "blank_gb":            round(blank["total_mb"] / 1024, 2),
        "kept_videos":         kept["count"],
        "kept_gb":             round(kept["total_mb"] / 1024, 2),
        "purged_videos":       purged["count"],
        "purged_gb_reclaimed": round(purged["total_mb"] / 1024, 2),
        "total_active_gb":     round((blank["total_mb"] + kept["total_mb"]) / 1024, 2),
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
                   COALESCE(s.common_name, 'Unknown') as species,
                   s.label,
                   COUNT(DISTINCT v.id) as count
            FROM dates
            LEFT JOIN videos v
                ON DATE(v.recorded_at) = dates.day AND v.kept = 1
            LEFT JOIN detections d ON v.id = d.video_id
            LEFT JOIN species s ON s.detection_id = d.id AND {KNOWN_SPECIES_FILTER}
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
                MAX(CASE WHEN s.corrected_at IS NOT NULL THEN 1 ELSE 0 END) AS has_correction,
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

        crops = conn.execute("""
            SELECT c.crop_path, c.quality_score, v.id as video_id,
                   v.filename, v.recorded_at
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
    where = "WHERE " + " AND ".join(conditions)

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM crops c JOIN detections d ON c.detection_id=d.id "
            f"JOIN species s ON s.detection_id=d.id {where}", params
        ).fetchone()[0]

        rows = conn.execute(f"""
            SELECT c.crop_path, c.quality_score, c.width, c.height,
                   s.label,
                   {DISPLAY_COMMON}     AS common_name,
                   {DISPLAY_SCIENTIFIC} AS scientific_name,
                   s.common_name        AS ai_common_name,
                   s.detection_id,
                   CASE WHEN s.corrected_at IS NOT NULL THEN 1 ELSE 0 END AS has_correction,
                   v.id as video_id, v.filename, v.recorded_at
            FROM crops c
            JOIN detections d ON c.detection_id = d.id
            JOIN species s ON s.detection_id = d.id
            JOIN videos v ON d.video_id = v.id
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

        return {
            "items":    [dict(r) for r in rows],
            "total":    total,
            "page":     page,
            "per_page": per_page,
            "pages":    max(1, (total + per_page - 1) // per_page),
        }


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
                       THEN {DISPLAY_COMMON} END) as species_list
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
    with get_conn() as conn:
        video = conn.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        if not video:
            return {}

        detections = conn.execute(f"""
            SELECT d.id, d.frame_number, d.timestamp_secs, d.category, d.confidence,
                   s.label, {DISPLAY_COMMON} as common_name, s.scientific_name,
                   c.crop_path, c.quality_score
            FROM detections d
            LEFT JOIN species s ON s.detection_id = d.id
            LEFT JOIN crops c ON c.detection_id = d.id
            WHERE d.video_id = ?
              AND ({KNOWN_SPECIES_FILTER} OR s.label IS NULL)
            ORDER BY d.timestamp_secs
        """, (video_id,)).fetchall()

        # Fetch paired lens video if this is a dual-lens camera
        paired = None
        pair_detections = []
        pair_id = dict(video).get("paired_video_id")
        if pair_id:
            paired_row = conn.execute("SELECT * FROM videos WHERE id=?", (pair_id,)).fetchone()
            if paired_row:
                paired = dict(paired_row)
                pair_detections = [dict(r) for r in conn.execute(f"""
                    SELECT d.id, d.frame_number, d.timestamp_secs, d.category, d.confidence,
                           s.label, {DISPLAY_COMMON} as common_name, s.scientific_name,
                           c.crop_path, c.quality_score
                    FROM detections d
                    LEFT JOIN species s ON s.detection_id = d.id
                    LEFT JOIN crops c ON c.detection_id = d.id
                    WHERE d.video_id = ?
                      AND ({KNOWN_SPECIES_FILTER} OR s.label IS NULL)
                    ORDER BY d.timestamp_secs
                """, (pair_id,)).fetchall()]

        return {
            "video":           dict(video),
            "detections":      [dict(r) for r in detections],
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
                from datetime import date
                d1 = date.fromisoformat(date_from)
                d2 = date.fromisoformat(date_to)
                window_days = (d2 - d1).days
            elif date_from:
                from datetime import date
                window_days = (date.today() - date.fromisoformat(date_from)).days
            else:
                window_days = days or 30
        else:
            n = days or 30
            where = f"AND v.recorded_at >= DATE('now', '-{n} days')"
            params = []
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
