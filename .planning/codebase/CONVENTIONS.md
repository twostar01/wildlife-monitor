# Coding Conventions

**Analysis Date:** 2026-05-06

## Naming Patterns

**Files:**
- `snake_case.py` for all Python modules: `database.py`, `web_app.py`, `wildlife_processor.py`, `image_quality.py`
- Single-word or short underscore-joined names; no class-per-file requirement

**Functions:**
- `snake_case` for all public functions: `get_video_by_id`, `insert_detection`, `find_free_port`
- Leading underscore `_snake_case` for module-private/internal helpers: `_load_settings`, `_save_settings`, `_load_nas_config`, `_camera_from_filename`, `_date_from_filename`, `_det_conf`, `_video_date`
- Verb-noun pattern for public functions: `get_*`, `insert_*`, `update_*`, `delete_*`, `search_*`, `parse_*`, `extract_*`, `save_*`, `find_*`
- API route handlers prefixed `api_*`: `api_stats`, `api_get_blacklist`, `api_add_blacklist`

**Variables:**
- `snake_case` throughout; no camelCase in Python code
- Descriptive names; abbreviations used only for well-understood concepts: `fps`, `det`, `ts`, `fn`, `cfg`
- Module-level singletons: `_speciesnet`, `_run_process`, `_run_lock` (leading underscore signals private)

**Constants:**
- `UPPER_SNAKE_CASE` for all module-level constants: `DB_PATH`, `DEFAULT_MODEL`, `MODEL_CACHE_DIR`, `VIDEO_EXTS`, `FILENAME_DATE_PATTERNS`, `BLANK_LABEL_FILTER`, `SCHEMA`, `WEIGHTS`, `MIN_AREA`
- SQL filter strings stored as named constants: `BLANK_LABEL_FILTER`, `SUPPRESS_UNKNOWN_IF_IDENTIFIED`, `KNOWN_SPECIES_FILTER`, `DISPLAY_COMMON`, `DISPLAY_SCIENTIFIC` (in `database.py`)
- Migration SQL strings: `MIGRATION_ADD_*` pattern

**Classes:**
- `PascalCase` for Pydantic models in `web_app.py`: `CorrectionRequest`, `BlacklistEntry`, `ProcessingSettings`, `RunRequest`, `UpdateRequest`
- No custom domain classes beyond Pydantic request/response models; domain entities are plain dicts

**Type Hints:**
- Used on all public function signatures in `database.py`, `image_quality.py`, `web_app.py`
- `Optional[T]` from `typing` for nullable parameters (not `T | None` style, except one `subprocess.Popen | None` in `web_app.py`)
- Return types annotated: `-> dict`, `-> list`, `-> int`, `-> str`, `-> bool`, `-> Optional[dict]`
- Some processor functions (`extract_frames`, `run_megadetector`, `process_videos`) omit return type annotations — convention is inconsistent in `wildlife_processor.py`

## Code Style

**Formatting:**
- No auto-formatter config file detected (no `.prettierrc`, no `pyproject.toml` with black/ruff settings, no `.flake8`)
- Consistent 4-space indentation throughout
- Lines generally kept short; long SQL strings use multiline triple-quoted strings

**Alignment:**
- Column-aligned assignment blocks used deliberately for readability in constant defs and dict literals:
  ```python
  DATA_DIR      = "./data"
  SETTINGS_FILE = "./data/settings.json"
  ```
  ```python
  "blank_videos":        blank["count"],
  "blank_gb":            round(blank["total_mb"] / 1024, 2),
  ```
- Pydantic model fields column-aligned:
  ```python
  hours:                  int   = 24
  sample_rate:            int   = 30
  md_threshold:           float = 0.2
  ```

**Linting:**
- No linter config detected; no `noqa` or `type: ignore` comments present
- Code is clean — no TODO, FIXME, HACK, or XXX comments in any source file

**Section Separators:**
- Consistent use of `# ── Section Name ────...` separators to divide logical sections within files:
  ```python
  # ── Schema ─────────────────────────────────────────────────────────────────────
  # ── Write helpers ──────────────────────────────────────────────────────────────
  # ── Read helpers ───────────────────────────────────────────────────────────────
  # ── API routes ─────────────────────────────────────────────────────────────────
  ```

## Import Organization

**Order (top-to-bottom in each file):**
1. Standard library (`os`, `sys`, `re`, `json`, `argparse`, `logging`, `threading`, `socket`, `sqlite3`, etc.)
2. Third-party packages (`cv2`, `numpy`, `fastapi`, `uvicorn`, `pydantic`)
3. Internal modules (`import database as db`, `from database import ...`, `from image_quality import score_image`)

**Deferred / Lazy Imports:**
- Heavy optional dependencies imported inside functions to defer load cost:
  - `from megadetector...` imported inside `load_megadetector()` and `download_model_if_needed()`
  - `from speciesnet import ...` imported inside `run_speciesnet()`
  - `import re as _re` imported inside `parse_dual_lens_filename()` and `link_lens_pair()`
  - `import json as _json` imported inside `search_taxonomy()` and at module top in `wildlife_processor.py` (`import json as _json`)
  - `import PIL.Image` imported inside `run_megadetector()` and `save_crops()`
- Pattern: `_aliased_name` (trailing underscore alias) used for locally-scoped standard lib imports to avoid shadowing: `import re as _re`, `import json as _json`, `import sqlite3 as _sqlite3`

**Path Aliases:** None — all internal imports use module names directly

## Error Handling

**Database layer (`database.py`):**
- Context manager `get_conn()` wraps all DB access: commits on success, rolls back on any exception, always closes connection
  ```python
  @contextmanager
  def get_conn():
      ...
      try:
          yield conn
          conn.commit()
      except Exception:
          conn.rollback()
          raise
      finally:
          conn.close()
  ```
- `except Exception:` (bare, reraises) used in the context manager — exceptions propagate to callers
- Specific exception types used where appropriate: `except (ValueError, TypeError)`, `except (FileNotFoundError, ValueError)`, `except OSError`

**API layer (`web_app.py`):**
- `raise HTTPException(status_code, detail)` for all API error responses — never return error dicts from route handlers
- Pattern: check precondition, raise immediately; no nested try/except in routes
  ```python
  if not result or not result.get("video"):
      raise HTTPException(404, "Video not found")
  ```
- Optional dependencies guarded with `try/except ImportError` at module level (psutil)
- Best-effort functions (port detection, NAS config) catch `except Exception: pass` and return fallback values — never bubble up to caller

**Processor layer (`wildlife_processor.py`):**
- Frame-level errors caught and suppressed with empty fallback: `except Exception: result = {"detections": [], ...}`
- Crop extraction errors caught individually per crop: `except Exception: continue`
- Blacklist loading failure falls back silently: `except Exception: blacklisted_labels = set()`
- No custom exception classes defined — all exceptions are built-in types

## Logging

**Framework:** Python `logging` module via named logger `"wildlife_processor"`

**Setup:** `setup_logging(log_file)` in `wildlife_processor.py` — attaches `FileHandler` + `StreamHandler(sys.stdout)`, format `"%(asctime)s  %(levelname)-8s  %(message)s"` with `datefmt="%Y-%m-%d %H:%M:%S"`

**Scope:**
- All logging is in `wildlife_processor.py` and `image_quality.py` — both use `log = logging.getLogger("wildlife_processor")`
- `web_app.py` uses no Python logging — informational messages go to `print()` only (startup messages, port suggestions)
- `database.py` references `log.info(...)` for DB migrations but `log` is not imported/defined there — this is a bug (see `CONCERNS.md`)

**Patterns:**
- Progress logging with indentation: `log.info(f"  {len(saved)} crops saved")` (2-space indent for per-video sub-steps)
- `log.warning()` for recoverable anomalies: mismatched prediction counts, skipped frames
- `log.info()` for all normal operational events
- `print()` for user-facing CLI progress: model download progress bar, port suggestions

## Comments

**When to Comment:**
- Complex business logic explained inline: species deduplication strategy, confidence threshold decisions, dual-lens pairing logic
- SQL filter constants have block-comment explanations above them in `database.py` (lines 157–180)
- Each file has a top-level module docstring: `"""database.py — ..."""`, `"""web_app.py — ..."""`

**Docstring Style:**
- Short imperative-style docstrings: `"""Return the best available date — filename date preferred over mtime."""`
- Multi-line docstrings for complex parsing functions show format examples:
  ```python
  """
  Parse a dual-lens camera filename into (camera_base, lens_index, timestamp).
  ...
  Examples:
    "World Watch_00_20260327160902.mp4" → ("World Watch", 0, "20260327160902")
  """
  ```
- No sphinx/Google/NumPy docstring format used — plain prose only
- Not all functions have docstrings; shorter or obvious helpers (e.g. `get_db_path`, `get_cameras`) have none or one-liners

**Section Comments:**
- `# ── Section ────` dividers used consistently as visual section markers
- Inline `--` SQL comments used within schema strings to annotate column meaning

## Function Design

**Size:**
- `process_videos()` in `wildlife_processor.py` is the largest function (~200 lines) — contains the main processing loop
- All other functions are concise (10–50 lines)
- Helper functions extracted for reuse: `_camera_from_filename`, `_date_from_filename`, `_det_conf`

**Parameters:**
- Optional parameters use `Optional[T] = None` default; callers pass `None` to mean "not set"
- Boolean parameters converted at call boundary: `int(has_animal)` for SQLite storage
- Keyword-only style enforced implicitly by many parameters with defaults

**Return Values:**
- Database read helpers always return `dict`, `list`, or simple scalars — never SQLite Row objects; rows converted with `dict(r)` or `[dict(r) for r in rows]`
- Write helpers return `int` (new row ID) or `dict` with `{"ok": True}`
- Empty-result pattern: return `{}` (empty dict) or `[]` (empty list) for missing data, never `None` from query functions
- `purge_video_file()` returns `bool` — True if file was physically deleted

## Module Design

**Exports:**
- No `__all__` defined in any module — all public symbols implicitly exported
- Internal helpers signalled by leading underscore: `_load_settings`, `_run_process`, `_speciesnet`

**Barrel Files:** Not used — imports reference module directly (`import database as db`, `from database import init_db, insert_video, ...`)

**Global State:**
- `DB_PATH` in `database.py` — module-level mutable, modified by `set_db_path()`
- `_speciesnet` in `wildlife_processor.py` — lazy singleton for the SpeciesNet model
- `_run_process`, `_run_lock`, `DATA_DIR`, `SETTINGS_FILE` in `web_app.py` — module-level mutable globals

---

*Convention analysis: 2026-05-06*
