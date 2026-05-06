<!-- refreshed: 2026-05-06 -->
# Architecture

**Analysis Date:** 2026-05-06

## System Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│                    NAS / Camera Storage (SMB or NFS)                  │
│         <camera>/<year>/<month>/<day>/<filename>.mp4                  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ nas_sync.sh: rsync to local staging
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  Local Staging  (local_videos/)                       │
│                  Mirrors NAS folder structure                         │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  wildlife_processor.py  (CLI batch job)               │
│   find_videos → extract_frames → MegaDetector → save_crops           │
│   → SpeciesNet → score_image → insert_* into SQLite                  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  writes to
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  SQLite  data/wildlife.db                             │
│  videos | detections | species | crops | blacklist | corrections      │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  read by
                             ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  web_app.py  (FastAPI + uvicorn)                      │
│   REST API /api/*   +   static SPA  static/index.html                │
└──────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| NAS Sync | Copy videos from NAS to local staging; archive kept videos back | `nas_sync.sh` |
| Video Processor | Frame extraction, MegaDetector inference, SpeciesNet classification, DB writes | `wildlife_processor.py` |
| Database Layer | Schema definition, all SQL queries, migrations, write/read helpers | `database.py` |
| Image Quality | Score animal crop images on sharpness, brightness, contrast, size | `image_quality.py` |
| Web Dashboard | FastAPI REST API + serves single-page frontend | `web_app.py` |
| Frontend SPA | Single-file vanilla JS dashboard (no build step) | `static/index.html` |
| Systemd Units | Schedule daily analysis + keep dashboard running as a service | `systemd/` |

## Pattern Overview

**Overall:** Pipeline / ETL with separated processing and serving processes.

**Key Characteristics:**
- No shared in-process state between processor and web app — SQLite is the only handoff point.
- Processing is a one-shot CLI batch job; the web app is a long-running server. They never run in the same process.
- All ML inference is performed at processing time, never at query time.
- The frontend is a zero-build SPA: one HTML file with inline CSS/JS using vanilla JS and Chart.js loaded from CDN.

## Layers

**NAS / Sync Layer:**
- Purpose: Bridge between NAS storage and local processing. Copies recent videos in, archives processed videos out.
- Location: `nas_sync.sh`
- Contains: Bash with embedded Python blocks for date filtering and DB path updates
- Depends on: `~/.config/wildlife_monitor/nas.conf`, `database.py` (via inline python3 calls)
- Used by: `systemd/wildlife-analysis.service`, `web_app.py` (`/api/run` endpoint)

**Processing Layer:**
- Purpose: AI-powered video analysis pipeline — detect animals, identify species, score crop quality
- Location: `wildlife_processor.py`
- Contains: Video file discovery, OpenCV frame extraction, MegaDetector detection, SpeciesNet classification, crop saving
- Depends on: `database.py`, `image_quality.py`, `megadetector` pip package, `speciesnet` pip package, `cv2`, `PIL`
- Used by: `nas_sync.sh --then-process`, direct CLI invocation

**Database Layer:**
- Purpose: All data access — schema creation, migrations, write helpers, read queries
- Location: `database.py`
- Contains: SCHEMA SQL, migration constants, `get_conn()` context manager, `insert_*`/`get_*` functions, filter constants
- Depends on: `sqlite3` (stdlib only, no ORM)
- Used by: `wildlife_processor.py`, `web_app.py`

**Image Quality Layer:**
- Purpose: Score animal crop images using computer vision metrics
- Location: `image_quality.py`
- Contains: `score_image()`, `score_images_batch()`, `rank_crops_by_quality()`
- Depends on: `cv2`, `numpy`
- Used by: `wildlife_processor.py` (called after `save_crops`)

**Web / API Layer:**
- Purpose: REST API for dashboard data access and management operations; serves static frontend
- Location: `web_app.py`
- Contains: FastAPI app, all `/api/*` route handlers, settings persistence, subprocess management for triggered runs
- Depends on: `database.py`, `fastapi`, `uvicorn`, `psutil` (optional)
- Used by: `static/index.html` (browser fetch calls), `systemd/wildlife-monitor.service`

**Frontend Layer:**
- Purpose: Browser-rendered dashboard — species gallery, video browser, timeline, settings, maintenance
- Location: `static/index.html`
- Contains: All HTML, CSS, and JavaScript in a single file; uses Chart.js from CDN
- Depends on: `/api/*` endpoints on the same host
- Used by: End users via browser

## Data Flow

### Standard Nightly Processing Path

1. Systemd timer fires at 06:00 → runs `wildlife-analysis.service` (`systemd/wildlife-analysis.timer`)
2. `nas_sync.sh --then-process` mounts NAS, scans for videos in the last N hours (`nas_sync.sh:165-237`)
3. Videos are copied to `local_videos/` preserving `<camera>/<year>/<month>/<day>/` structure (`nas_sync.sh:284-318`)
4. `wildlife_processor.py` is invoked with `--video-dir local_videos/ --data-dir data/` (`nas_sync.sh:343-347`)
5. For each video: extract thumbnail (`wildlife_processor.py:190`), sample frames (`wildlife_processor.py:211`), run MegaDetector (`wildlife_processor.py:245`)
6. Animals detected → save crops, run SpeciesNet, deduplicate per species, score quality (`wildlife_processor.py:438-514`)
7. Insert video, detections, species, crops into SQLite (`database.py:insert_video`, `insert_detection`, `insert_species`, `insert_crop`)
8. Kept videos moved to NAS archive `wildlife_archive/<camera>/<year>/<month>/<day>/` (`nas_sync.sh:412-500`)
9. Blank videos moved to NAS `wildlife_archive/blanks/...` (`nas_sync.sh:519-604`)
10. Local staging directory cleaned up; retention purge runs (`nas_sync.sh:612-684`)

### Manual Trigger Path (via Dashboard)

1. User clicks "Run Now" in dashboard → `POST /api/run` (`web_app.py:782`)
2. `web_app.py` launches `nas_sync.sh --then-process` as a subprocess, writing to `data/run_manual.log`
3. `GET /api/run/status` polls `run_manual.log` and subprocess `.poll()` for live status (`web_app.py:814`)

### Dashboard Read Path

1. Browser loads `GET /` → `static/index.html` served as `HTMLResponse` (`web_app.py:240`)
2. Frontend calls `/api/stats`, `/api/species`, `/api/videos`, `/api/gallery`, `/api/timeline` etc.
3. Each endpoint delegates directly to a `database.py` function, returns `dict` or `list` serialized as JSON

### SpeciesNet Reprocess Path

1. User blacklists a species in dashboard → `POST /api/blacklist` with `requeue=True`
2. `database.requeue_species()` sets `videos.needs_reprocess=1` for affected videos (`database.py:657`)
3. On next `nas_sync.sh --then-process` run, after main processing, reprocess queue is checked (`nas_sync.sh:363-382`)
4. `wildlife_processor.py --reprocess-flagged` reruns SpeciesNet on flagged videos' existing crops

**State Management:**
- All persistent state lives in `data/wildlife.db` (SQLite).
- Processing settings persisted to `data/settings.json` (JSON, written by `POST /api/settings`).
- NAS credentials stored in `~/.config/wildlife_monitor/nas.conf` (INI, never exposed via API).
- Web app has one piece of in-process mutable state: `_run_process` (subprocess handle, guarded by `_run_lock` threading.Lock).

## Key Abstractions

**`get_conn()` context manager:**
- Purpose: Opens a WAL-mode SQLite connection with `row_factory=sqlite3.Row`, auto-commits or rolls back
- Location: `database.py:24`
- Pattern: Used by every DB query function — never open connections outside this context manager

**`KNOWN_SPECIES_FILTER` / `BLANK_LABEL_FILTER` / `SUPPRESS_UNKNOWN_IF_IDENTIFIED`:**
- Purpose: SQL string constants that compose the "what counts as a real species detection" filter
- Location: `database.py:158-180`
- Pattern: Concatenated into query WHERE clauses — any change to display logic goes here, not scattered in route handlers

**`DISPLAY_COMMON` / `DISPLAY_SCIENTIFIC`:**
- Purpose: SQL expressions using `COALESCE` to prefer human-corrected names over SpeciesNet labels
- Location: `database.py:183-184`
- Pattern: Used in every species SELECT query so corrections surface automatically

**`score_image()`:**
- Purpose: Returns a dict `{quality_score, sharpness, brightness, contrast, pixel_area, width, height}` for a crop
- Location: `image_quality.py:25`
- Pattern: Called once per saved crop during processing; score stored in DB, used to rank gallery display

**`parse_label()`:**
- Purpose: Normalizes SpeciesNet label strings (two formats: UUID-prefixed 7-part and legacy) into `(scientific_name, common_name)`
- Location: `wildlife_processor.py:306`
- Pattern: Called whenever a SpeciesNet label is stored or displayed

## Entry Points

**Batch Processor:**
- Location: `wildlife_processor.py:559` (`if __name__ == "__main__":`)
- Triggers: CLI invocation, or called by `nas_sync.sh`
- Responsibilities: Parses args, optionally generates taxonomy cache or runs reprocess-flagged mode, then calls `process_videos(args)`

**Web Dashboard:**
- Location: `web_app.py:984` (`if __name__ == "__main__":` → `main()`)
- Triggers: Direct `python web_app.py` or `systemd/wildlife-monitor.service`
- Responsibilities: Parses config file + CLI args, initializes DB, starts uvicorn

**NAS Sync:**
- Location: `nas_sync.sh:1`
- Triggers: `systemd/wildlife-analysis.service` (scheduled via timer), `POST /api/run` (manual trigger from dashboard)
- Responsibilities: Mount NAS, find/copy videos, optionally run processor, archive results, cleanup staging

## Architectural Constraints

- **Threading:** Web app is single-process with uvicorn's default event loop. One subprocess slot for triggered runs (`_run_process`), protected by `threading.Lock`.
- **Global state:** `database.py` has a module-level `DB_PATH = "data/wildlife.db"` string mutated by `set_db_path()`. `web_app.py` has module-level `DATA_DIR` and `_run_process`. `wildlife_processor.py` has `_speciesnet = None` (lazy singleton for SpeciesNet model).
- **Circular imports:** None detected. Dependency is strictly one-directional: `web_app.py` → `database.py`; `wildlife_processor.py` → `database.py` + `image_quality.py`.
- **No migrations framework:** Migrations are manual `ALTER TABLE` strings in `database.py` (see `MIGRATION_*` constants), applied in `init_db()` by checking `PRAGMA table_info`.
- **SQLite WAL mode:** Enabled on every connection. Safe for concurrent reads from web app while writes happen from processor, but only one writer at a time.

## Anti-Patterns

### `get_storage_stats` function body orphaned from its `def`

**What happens:** `database.py:826` contains the body of a `get_storage_stats` function but its `def` line was removed, leaving the code as a dangling block after `promote_paired_blanks()`.
**Why it's wrong:** The function is unreachable — `web_app.py` calls `db.get_storage_stats()` which will raise `AttributeError` at runtime.
**Do this instead:** Add `def get_storage_stats() -> dict:` before line 826 in `database.py`.

### Inline Python in bash scripts

**What happens:** `nas_sync.sh` contains multiple `python3 - <<PYEOF` heredoc blocks that perform DB operations and file archiving (lines 174-237, 363-370, 388-395, 412-500, 519-604, 643-684).
**Why it's wrong:** This logic is untestable in isolation, bypasses `get_conn()`'s WAL/rollback safety in some blocks, and duplicates the filename date-parsing logic already in `wildlife_processor.py`.
**Do this instead:** Move archiving and DB operations into `database.py` functions callable from a thin Python CLI shim, keeping bash only for mount/copy/cleanup orchestration.

### Module-level mutable `DB_PATH` global

**What happens:** `database.py:9` sets `DB_PATH = "data/wildlife.db"` and `set_db_path()` mutates it.
**Why it's wrong:** Any code that imports `database` before `init_db()` is called gets the default path; order-of-import bugs are silent.
**Do this instead:** Pass `db_path` as a parameter to `get_conn()` or use a thread-local / app-level config object.

## Error Handling

**Strategy:** Mostly try/except-and-continue in the processing loop; API layer raises `HTTPException` for 4xx/5xx.

**Patterns:**
- Processor per-frame errors are caught silently: `except Exception: result = {"detections": [], ...}` (`wildlife_processor.py:255`)
- `score_image()` returns `None` on read failure; callers check for `None` before using the result
- `database.py` functions let exceptions propagate — `get_conn()` rolls back on any exception
- Web API uses `raise HTTPException(404, ...)` / `raise HTTPException(400, ...)` for validation failures
- File operations in `nas_sync.sh` use `set -euo pipefail`; individual copy failures are counted but don't abort the batch

## Cross-Cutting Concerns

**Logging:** `wildlife_processor.py` uses a named logger `"wildlife_processor"` (via `setup_logging()`), writing to both stdout and a timestamped `run_YYYYMMDD_HHMMSS.log` file in `data/`. Web app uses uvicorn's built-in logging at `log_level="warning"`.

**Validation:** Input validation is minimal — FastAPI/Pydantic validates request body types via `BaseModel`. Path traversal is guarded explicitly in media routes (`if ".." in filename`). No input sanitization library used.

**Authentication:** None. The dashboard is assumed to run on a trusted local network. The NAS password is stripped from `_load_nas_config()` before being returned to the frontend.

---

*Architecture analysis: 2026-05-06*
