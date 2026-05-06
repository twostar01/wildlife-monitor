# Codebase Structure

**Analysis Date:** 2026-05-06

## Directory Layout

```
wildlife-monitor/                      # Project root
├── wildlife_processor.py              # Batch video analysis pipeline (CLI entry point)
├── web_app.py                         # FastAPI web dashboard (server entry point)
├── database.py                        # SQLite schema, migrations, all data access
├── image_quality.py                   # Crop image quality scoring
├── nas_sync.sh                        # NAS video sync + post-processing archive script
├── nas_connect.sh                     # Interactive NAS connection configuration wizard
├── setup.sh                           # Ubuntu install script (venv, apt packages)
├── static/
│   └── index.html                     # Single-file SPA (all HTML + CSS + JS inline)
├── systemd/
│   ├── wildlife-monitor.service       # Systemd unit: keep web_app.py running
│   ├── wildlife-analysis.service      # Systemd unit: one-shot daily analysis job
│   └── wildlife-analysis.timer       # Systemd timer: triggers analysis at 06:00 daily
├── .github/
│   └── workflows/
│       └── publish.yml                # GitHub Actions: publish project docs to WordPress
├── .planning/
│   └── codebase/                      # GSD codebase analysis documents (this directory)
├── VERSION                            # Plain-text version string
├── CHANGELOG.md                       # Release history
├── PROJECT.md                         # Project description for WordPress publish workflow
├── README.md                          # User-facing setup and usage documentation
└── LICENSE                            # License file
```

### Runtime-generated directories (not committed)

```
wildlife-monitor/
├── data/                              # Created by processor on first run
│   ├── wildlife.db                    # SQLite database
│   ├── settings.json                  # Dashboard processing settings (persisted via API)
│   ├── crops/                         # Saved animal crop images (JPEG)
│   ├── thumbnails/                    # Video thumbnail images (JPEG)
│   ├── speciesnet_classes.json        # Taxonomy cache (generated via --generate-taxonomy)
│   ├── cron.log                       # Output from systemd scheduled runs
│   └── run_*.log / run_manual.log     # Per-run and manual-trigger logs
├── local_videos/                      # Temporary NAS staging (created/deleted by nas_sync.sh)
└── wildlife.conf                      # Optional INI config for web_app.py (user-created)
```

### NAS archive structure (external, on NAS mount)

```
<NAS_MOUNT>/<NAS_ARCHIVE_SUBDIR>/       # Default: wildlife_archive/
├── <CameraName>/<year>/<month>/<day>/  # Kept videos (animal/person detected)
│   └── *.mp4
└── blanks/
    └── <CameraName>/<year>/<month>/<day>/  # Blank videos (no detections)
        └── *.mp4
```

## Directory Purposes

**`static/`:**
- Purpose: Web-served static assets
- Contains: One file — `index.html`, a self-contained SPA with all CSS, JS, and HTML inline
- Generated: No
- Committed: Yes

**`systemd/`:**
- Purpose: Production deployment configuration for Linux systems using systemd
- Contains: Two service units + one timer unit
- Generated: No
- Committed: Yes (deployed manually via `setup.sh` or by the user)

**`.github/workflows/`:**
- Purpose: CI/CD automation
- Contains: `publish.yml` — workflow that publishes `PROJECT.md` to a WordPress site via `twostar01/wp-sync`
- Generated: No
- Committed: Yes

**`data/`:**
- Purpose: All runtime data — database, model output, logs, settings
- Contains: SQLite DB, crop/thumbnail images, logs, settings JSON
- Generated: Yes (by `wildlife_processor.py` and `web_app.py`)
- Committed: No (in `.gitignore` or excluded by convention)

**`local_videos/`:**
- Purpose: Temporary local staging area for NAS videos during processing
- Contains: Mirror of NAS video folder structure, deleted after archiving
- Generated: Yes (by `nas_sync.sh`)
- Committed: No

## Key File Locations

**Entry Points:**
- `wildlife_processor.py`: `if __name__ == "__main__"` at line 559 — CLI batch processor
- `web_app.py`: `main()` at line 896, `if __name__ == "__main__"` at line 984 — web server
- `nas_sync.sh`: line 1 — NAS sync and full pipeline orchestration

**Configuration:**
- `wildlife.conf`: Optional INI config for web_app.py (port, host, data_dir); read by `load_config()` in `web_app.py:192`
- `data/settings.json`: Processing settings persisted from dashboard (hours, thresholds, retention, country)
- `~/.config/wildlife_monitor/nas.conf`: NAS credentials and mount config (created by `nas_connect.sh`, never committed)

**Core Logic:**
- `database.py`: All schema, migrations, and SQL at top of file; write helpers from line 241; read/query helpers from line 427
- `wildlife_processor.py`: `process_videos()` at line 341 — the main pipeline loop
- `image_quality.py`: `score_image()` at line 25 — the only function used externally

**Testing:**
- No test files detected. No test framework configured.

**API Routes (all in `web_app.py`):**
- `GET /` → serves `static/index.html`
- `GET /media/crops/{filename}` → serves crop images from `data/crops/`
- `GET /media/thumbnails/{filename}` → serves thumbnails from `data/thumbnails/`
- `GET /media/video/{video_id}` → streams video files from their stored filepath
- `GET /api/stats` → dashboard summary statistics
- `GET /api/species` → species list with best crops
- `GET /api/species/search` → taxonomy search against `speciesnet_classes.json`
- `GET /api/species/{label}` → species detail with crops and videos
- `GET /api/gallery` → paginated crop gallery, sortable by quality/date
- `GET /api/cameras` → distinct camera names
- `POST /api/species/correct` → save human species correction (detection-level)
- `GET /api/videos` → paginated video list with filters
- `GET /api/videos/{video_id}` → video detail with detections and paired lens info
- `GET /api/timeline` → daily activity chart data
- `GET /api/blanks` → paginated blank (no detection) videos
- `GET /api/system` → CPU/RAM/disk/uptime (requires psutil)
- `GET /api/blacklist` → species blacklist entries
- `POST /api/blacklist` → add species to blacklist (optionally requeue affected videos)
- `DELETE /api/blacklist/{label}` → remove from blacklist
- `POST /api/blacklist/{label}/requeue` → flag videos with this species for reprocessing
- `GET /api/corrections` → video-level species corrections
- `POST /api/corrections` → save video-level correction
- `DELETE /api/corrections/{id}` → delete correction
- `GET /api/maintenance/reprocess_queue` → videos flagged for SpeciesNet reprocess
- `GET /api/search` → cross-entity search
- `GET /api/settings` → current processing settings + NAS config
- `POST /api/settings` → save processing settings
- `POST /api/run` → trigger NAS sync + processing job
- `GET /api/run/status` → running job status + last 100 log lines
- `GET /api/maintenance/storage` → storage breakdown by blank/kept/purged
- `POST /api/maintenance/promote_paired` → promote paired-lens blank videos
- `POST /api/maintenance/purge` → run retention policy purge (supports dry_run)
- `GET /api/updates` → check installed vs latest versions of packages and models
- `POST /api/updates/apply` → upgrade a pip package or clear a model cache

## Naming Conventions

**Files:**
- Python modules: `snake_case.py` (`wildlife_processor.py`, `image_quality.py`, `web_app.py`, `database.py`)
- Shell scripts: `snake_case.sh` (`nas_sync.sh`, `nas_connect.sh`, `setup.sh`)
- Static assets: `lowercase.html` (`index.html`)
- Systemd units: `kebab-case.service` / `kebab-case.timer`

**Python functions:**
- All `snake_case`: `insert_video`, `get_species_list`, `score_image`, `run_megadetector`
- Private/internal helpers prefixed with underscore: `_load_settings`, `_save_settings`, `_video_date`, `_det_conf`, `_camera_from_filename`

**Python constants:**
- Module-level constants: `UPPER_SNAKE_CASE` (`VIDEO_EXTS`, `BLANK_LABEL_FILTER`, `DB_PATH`, `DEFAULT_MODEL`)
- SQL filter strings: `UPPER_SNAKE_CASE` with descriptive names (`KNOWN_SPECIES_FILTER`, `SUPPRESS_UNKNOWN_IF_IDENTIFIED`, `DISPLAY_COMMON`)

**Database columns:**
- `snake_case` throughout (`recorded_at`, `has_animal`, `file_purged_at`, `bbox_json`)

**API endpoints:**
- Path segments: `snake_case` (`/api/species`, `/api/run/status`, `/api/maintenance/reprocess_queue`)

**NAS / filesystem paths:**
- Camera folders: user-defined (whatever the DVR names them)
- Archive structure: `<camera>/<YYYY>/<MM>/<DD>/<filename>`
- Crop filenames: `<video_stem_30chars>_f<7digit_frame>_d<2digit_det>.jpg`
- Thumbnail filenames: `<camera_20chars>_<video_stem_40chars>_thumb.jpg`

## Where to Add New Code

**New API endpoint:**
- Add route handler function in `web_app.py` alongside existing route handlers (grouped by feature area in comments)
- Add corresponding query function in `database.py` following the `get_conn()` context manager pattern
- No separate router files — all routes are in the single `web_app.py` module

**New database column:**
- Add column to `SCHEMA` in `database.py` for new installations
- Add a `MIGRATION_ADD_<COLUMN>` constant string (see `database.py:128-154`)
- Apply migration in `init_db()` by checking `PRAGMA table_info` (see `database.py:187-238`)

**New processing step:**
- Add function to `wildlife_processor.py` following existing patterns (returns data, does not write to DB)
- Call from `process_videos()` inside the per-video loop
- Add corresponding `insert_*` or `update_*` function in `database.py` if DB writes are needed

**New frontend view/section:**
- Add HTML section and JavaScript in `static/index.html`
- Call existing `/api/*` endpoints via `fetch()`
- Follow existing inline CSS custom property usage (`:root` variables at top of `<style>`)

**New shell utility:**
- Add as a new `name_verb.sh` file in the project root, following `nas_sync.sh` pattern (colour helpers, `set -euo pipefail`, `--help` via sed)

**Tests:**
- No test infrastructure exists. New tests would need a framework (e.g., pytest) added to the venv and a `tests/` directory created at project root.

## Special Directories

**`data/crops/`:**
- Purpose: JPEG crops of detected animals, named with video stem + frame + detection index
- Generated: Yes — by `save_crops()` in `wildlife_processor.py:260`
- Committed: No

**`data/thumbnails/`:**
- Purpose: 320x180 JPEG thumbnails extracted from the first 10% of each video
- Generated: Yes — by `extract_thumbnail()` in `wildlife_processor.py:190`
- Committed: No

**`~/.cache/megadetector/`:**
- Purpose: MegaDetector model weights cache (auto-downloaded on first run, ~160 MB)
- Generated: Yes — by `download_model_if_needed()` in `wildlife_processor.py:31`
- Committed: No (user home directory)

**`~/.cache/kaggle/models/google/speciesnet/`:**
- Purpose: SpeciesNet model cache (downloaded via Kaggle API on first SpeciesNet run)
- Generated: Yes — by speciesnet package internals
- Committed: No (user home directory)

---

*Structure analysis: 2026-05-06*
