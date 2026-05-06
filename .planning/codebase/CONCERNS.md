# Codebase Concerns

**Analysis Date:** 2026-05-06

## Tech Debt

**Schema/migration drift — `needs_reprocess` and `top_candidates_json` columns:**
- Issue: Both columns are added only via `MIGRATION_ADD_REPROCESS` and `MIGRATION_ADD_CANDIDATES` but are absent from the canonical `SCHEMA` CREATE TABLE statements in `database.py`. Fresh databases created from SCHEMA will have the columns added immediately via migration, but the authoritative schema definition is wrong and will confuse future developers.
- Files: `database.py` lines 42–126 (SCHEMA), 148–154 (migrations), 199–206 (init_db)
- Impact: A developer reading SCHEMA believes these columns don't exist; adding them again in a future schema version would cause conflicts. Any table-rebuild migration (like the filepath NOT NULL removal at line 213) silently drops them unless the rebuild SQL is kept in sync.
- Fix approach: Add `needs_reprocess INTEGER DEFAULT 0` to the `videos` CREATE TABLE in SCHEMA and `top_candidates_json TEXT` to the `species` CREATE TABLE. Remove the separate migration entries for these two columns once the SCHEMA is the ground truth.

**Orphaned dead code block in `database.py`:**
- Issue: Lines 826–860 contain a fully valid implementation of `get_storage_stats()` sitting as an unreachable code block between `promote_paired_blanks()` (which ends at line 822) and the actual `get_storage_stats()` function definition at line 863. The block has no `def` statement — it is a dangling docstring and function body.
- Files: `database.py` lines 824–860
- Impact: Silent dead code; no runtime error but creates confusion about which implementation is active. Both implementations are identical so there is no functional difference, but the orphan block should be deleted.
- Fix approach: Delete lines 824–861 from `database.py` (the orphaned block that starts with the raw docstring `"""Return storage usage...`).

**`SETTINGS_FILE` global not re-declared in `main()`:**
- Issue: `web_app.py` line 943 reassigns `SETTINGS_FILE` as a local variable inside `main()` using `SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")`, but the function only declares `global DATA_DIR` (line 897), not `global SETTINGS_FILE`. The reassignment silently creates a local that shadows the module-level variable. `_load_settings()` and `_save_settings()` still use the module-level `SETTINGS_FILE = "./data/settings.json"` when `--data-dir` is changed via CLI.
- Files: `web_app.py` lines 50–51, 897, 942–943
- Impact: If the operator passes `--data-dir /mnt/nas/data`, settings are read from and written to `./data/settings.json` (the default) rather than the specified directory. This is a silent misconfiguration.
- Fix approach: Add `global SETTINGS_FILE` at line 897 alongside `global DATA_DIR`.

**`re` module imported inside functions instead of at module level:**
- Issue: `database.py` functions `parse_dual_lens_filename()` (line 299) and `link_lens_pair()` (line 326) each do `import re as _re` inside the function body rather than at the top of the module.
- Files: `database.py` lines 299, 326
- Impact: Minor — Python caches module imports, so there is no meaningful performance cost, but it is non-idiomatic and inconsistent with the rest of the codebase.
- Fix approach: Add `import re` to the top-level imports in `database.py` and remove the two inline imports.

**`search_taxonomy()` accepts but ignores `country`, `admin1`, and `all_regions` parameters:**
- Issue: `database.py` `search_taxonomy()` (line 752) accepts `country`, `admin1`, and `all_regions` parameters but the docstring explicitly states "Region filtering is not applied here". The `all_regions` flag is documented as "accepted for API compatibility but has no effect currently."
- Files: `database.py` lines 752–800, `web_app.py` line 313
- Impact: Users who expect geographic filtering from the taxonomy search (e.g., searching for species in US-UT) get unfiltered global results. The UI exposes an "All Regions" toggle that has no effect.
- Fix approach: Either implement geographic filtering against the taxonomy classes JSON or remove the unused parameters and update the API/UI to remove the misleading toggle.

**Duplicate `CorrectionRequest` Pydantic model:**
- Issue: `web_app.py` defines `CorrectionRequest(BaseModel)` twice — at line 350 (for `/api/species/correct`, with fields `detection_id`, `user_common_name`, `user_scientific_name`) and again at line 496 (for `/api/corrections`, with fields `video_id`, `original_label`, etc.). The second definition silently overwrites the first in the module namespace.
- Files: `web_app.py` lines 350–353, 496–502
- Impact: The first `CorrectionRequest` (used by `api_correct_species` at line 357) is shadowed by the second definition. FastAPI resolves the route handler's type annotation at import time, so in practice the endpoint still works because FastAPI captures the type reference before the second definition. However this is fragile — any refactor or move of the second definition could silently break the first endpoint.
- Fix approach: Rename the first model to `SpeciesCorrectionRequest` and update its usage in `api_correct_species`.

**Duplicate blacklist log message in `wildlife_processor.py`:**
- Issue: Lines 369–372 of `wildlife_processor.py` contain an identical `if blacklisted_labels: log.info(...)` block twice back-to-back.
- Files: `wildlife_processor.py` lines 369–372
- Impact: Every processing run logs the blacklist count twice. Minor noise.
- Fix approach: Delete one of the two identical `if` blocks (lines 371–372).

**`log` reference in `database.py` with no logger defined:**
- Issue: `database.py` uses `log.info(...)` at lines 212 and 238 inside `init_db()`, but `log` is never defined in `database.py`. The module has no `import logging` or `log = logging.getLogger(...)`.
- Files: `database.py` lines 212, 238
- Impact: Any database migration that removes the NOT NULL constraint on `filepath` (triggered on databases created before this constraint was lifted) will raise `NameError: name 'log' is not defined`, crashing the migration and preventing startup.
- Fix approach: Add `import logging` and `log = logging.getLogger("wildlife_processor")` to the top of `database.py`, or replace the two `log.info()` calls with `print()`.

---

## Known Bugs

**`api_reprocess_queue` calls `get_reprocess_queue()` twice:**
- Symptoms: The `/api/maintenance/reprocess_queue` endpoint calls `db.get_reprocess_queue()` twice: once to build the `"videos"` list and once to compute `"count"`. This is a double DB round-trip returning identical results.
- Files: `web_app.py` line 566
- Trigger: Any request to `GET /api/maintenance/reprocess_queue`
- Fix: `queue = db.get_reprocess_queue(); return {"videos": queue, "count": len(queue)}`

**`link_lens_pair` uses SQL LIKE with backslash as literal character on Windows/NFS paths:**
- Symptoms: `link_lens_pair()` at `database.py` line 322 constructs a LIKE pattern `f"{camera_base}\\_%\\_{timestamp}%"`. In SQLite's LIKE, `_` is a wildcard matching any single character. The backslash is not SQLite's default LIKE escape character. This means the pattern matches more broadly than intended (any character in place of `_`), potentially linking unrelated cameras.
- Files: `database.py` lines 318–323
- Trigger: Any dual-lens camera processing run where camera names or timestamps share a common prefix.
- Fix: Use `LIKE ? ESCAPE '\'` with proper escaping of `_` and `%` in the camera_base, or switch to a Python-side filter (which is already done in the subsequent `parse_dual_lens_filename` call at line 330 — the LIKE is redundant filtering).

---

## Security Considerations

**CORS wildcard allows all origins:**
- Risk: The FastAPI app sets `allow_origins=["*"]` for CORS, allowing any website to make API requests to the dashboard. Combined with no authentication, a malicious page visited by the operator on the same machine could read all wildlife data, trigger a processing run, or delete video corrections.
- Files: `web_app.py` line 48
- Current mitigation: The dashboard is intended for local network use, so the risk is limited to the LAN.
- Recommendations: Restrict `allow_origins` to `["http://localhost:8080", "http://127.0.0.1:8080"]` for local-only use, or to the operator's specific LAN subnet. If LAN access is needed, add a bearer token or HTTP Basic Auth header requirement.

**No authentication on any API endpoint:**
- Risk: All API endpoints — including destructive ones (`POST /api/maintenance/purge`, `DELETE /api/blacklist/{label}`, `POST /api/updates/apply`) — are unauthenticated. Anyone who can reach the host's port 8080 can delete data or trigger package upgrades.
- Files: `web_app.py` — all route handlers
- Current mitigation: Default binding to `0.0.0.0:8080` on a home LAN reduces exposure. The systemd service (`systemd/wildlife-monitor.service` line 11) uses `--host 0.0.0.0`, making it reachable from the LAN.
- Recommendations: Add HTTP Basic Auth (FastAPI `HTTPBasic`) or restrict the host to `127.0.0.1` and access via SSH tunnel for remote use.

**`shell=True` subprocess in `api_trigger_run` with user-supplied date strings:**
- Risk: `web_app.py` line 803 constructs a shell command string using f-string interpolation of `DATA_DIR` (a module global, safe) and `args` (which include `body.date_from` and `body.date_to` from the POST request body). The command is then run with `shell=True`. If `date_from` or `date_to` contains shell metacharacters (`;`, `&&`, `$(`, etc.), they would be executed.
- Files: `web_app.py` lines 797–807
- Current mitigation: FastAPI/Pydantic accepts `date_from` as a plain `str` with no regex validation — only `Optional[str]`. No sanitization is applied before interpolation.
- Recommendations: Validate `date_from` and `date_to` against a strict `YYYY-MM-DD` regex before use, or pass them as list elements to `subprocess.Popen` (not `shell=True`) so the OS handles argument quoting.

**`clear_model_cache` path traversal partially mitigated but not fully:**
- Risk: `POST /api/updates/apply` with `action=clear_model_cache` checks that `cache_path` is under `Path.home() / ".cache"` using `relative_to()`. This prevents deletion outside `~/.cache`, but an attacker could still delete any subdirectory of `~/.cache` (e.g., `~/.cache/chromium/`, `~/.cache/pip/`).
- Files: `web_app.py` lines 730–742
- Current mitigation: The `relative_to(home_cache)` check prevents absolute path escapes.
- Recommendations: Further restrict to only the known model cache subdirectories: `~/.cache/megadetector` and `~/.cache/kaggle/models/google/speciesnet`.

**`serve_video` path traversal check missing backslash for Windows-style paths:**
- Risk: `serve_crop` and `serve_thumbnail` at `web_app.py` lines 251–252 reject filenames containing `".."` or `"/"` but do not check for `"\\"`. On Windows or if a Windows-formatted path leaks into the DB, `"\\"` could allow directory traversal.
- Files: `web_app.py` lines 251–252, 261–262
- Current mitigation: The system is designed for Linux (Raspberry Pi/Ubuntu), so backslash paths are not normally generated.
- Recommendations: Add `"\\" in filename` to the path traversal guard for defense in depth.

---

## Performance Bottlenecks

**`get_stats()` fires 7 separate database queries:**
- Problem: `database.py` `get_stats()` (line 928) executes 7 sequential `conn.execute().fetchone()` calls plus two larger aggregation queries. All run inside a single `get_conn()` context.
- Files: `database.py` lines 928–1001
- Cause: Each stat is fetched independently. The dashboard calls this on every load of the home tab.
- Improvement path: Combine into fewer queries using CTEs or SQLite's `WITH` clause. The 7-day activity and top species queries are already well-structured but run after the simpler counts complete.

**`search_taxonomy()` loads entire SpeciesNet classes JSON on every search request:**
- Problem: `database.py` `search_taxonomy()` (line 769) opens and parses `speciesnet_classes.json` (potentially thousands of entries) on every call. The `/api/species/search` endpoint is called on keypress as users type.
- Files: `database.py` lines 769–800, `web_app.py` line 312
- Cause: No caching of the parsed JSON. The file can be multi-MB when all SpeciesNet classes are loaded.
- Improvement path: Cache the parsed list in a module-level variable after first load, invalidating only if the file's mtime changes.

**`promote_paired_blanks()` uses `conn.total_changes` after UPDATE:**
- Problem: `database.py` line 821 uses `conn.total_changes` to count promoted rows. SQLite's `total_changes` returns changes since the connection was opened, not since the last statement. If the same connection executes other writes before this call (unlikely given the context manager pattern, but possible in future), the count will be wrong.
- Files: `database.py` lines 811–822
- Cause: Should use the cursor's `rowcount` attribute instead.
- Improvement path: Capture `cur = conn.execute(...)` and use `cur.rowcount`.

---

## Fragile Areas

**`init_db()` table-rebuild migration runs without verifying column count:**
- Files: `database.py` lines 207–238
- Why fragile: The migration that removes the NOT NULL constraint on `videos.filepath` inserts rows from `videos` into `videos_new` with `INSERT INTO videos_new SELECT * FROM videos`. If future columns are added to `videos` (or column order changes), this will silently drop them or error with a column count mismatch. The migration also does `PRAGMA foreign_keys=OFF` without a robust guarantee that it's turned back ON if the migration fails midway.
- Safe modification: Any new column added to the `videos` table must also be added to the `videos_new` CREATE TABLE statement inside this migration block. Consider replacing the SELECT * with an explicit column list.
- Test coverage: None — no automated tests exist for any database migration.

**`_speciesnet` global singleton is set inside an `if __name__ == "__main__"` block but also referenced in `run_speciesnet()`:**
- Files: `wildlife_processor.py` lines 288, 622–623
- Why fragile: The `_speciesnet` global is defined as `None` at module level (line 288). The `--reprocess-flagged` branch at line 622–623 sets it via `_speciesnet = SpeciesNet(...)` while also calling `run_speciesnet()` which independently initializes `_speciesnet` if None. The `--reprocess-flagged` branch opens its own `sqlite3.connect()` raw connection (bypassing the `get_conn()` context manager) and does not use `run_speciesnet()` at all, so the two initialization paths can diverge.
- Safe modification: Consolidate SpeciesNet initialization into `run_speciesnet()` only. Remove the manual initialization in the `--reprocess-flagged` branch.
- Test coverage: None.

**`log_f` file handle is never closed after `api_trigger_run`:**
- Files: `web_app.py` lines 805–810
- Why fragile: `log_f = open(log_path, "w")` opens the log file and passes it to `subprocess.Popen`. The file handle is never closed by the web app. It is closed when the subprocess exits (because the OS closes inherited handles), but if the request is called multiple times before the process exits, the old handle leaks. The file is also opened in `"w"` mode (truncating on every run) while `_run_process.poll() is None` prevents a second run, so double-open is currently avoided — but only via the lock.
- Safe modification: Store `log_f` alongside `_run_process` and close it when polling detects the process has finished (in `api_run_status`).
- Test coverage: None.

**`video_corrections` DELETE + INSERT is not atomic with respect to `get_video_corrections`:**
- Files: `database.py` lines 700–710 (`save_video_correction`)
- Why fragile: The function deletes an existing correction then inserts a new one in two separate `conn.execute()` calls within the same `get_conn()` context (one transaction). However, `get_video_corrections()` is called separately in `get_video_by_id()`. If a concurrent request reads corrections between the delete and insert (unlikely in single-threaded uvicorn but possible with async workers), it could see a temporary "correction deleted" state.
- Safe modification: Low priority in practice, but could be replaced with an `INSERT OR REPLACE` pattern using a unique constraint on `(video_id, original_label)`.

---

## Scaling Limits

**Single SQLite file for all operations:**
- Current capacity: Suitable for a home deployment with 1–5 cameras generating tens of thousands of video records.
- Limit: SQLite WAL mode handles concurrent readers well, but concurrent writes (processor running while web app serves requests) are serialized. At high camera counts (10+) or if the processor runs very frequently, write contention will cause slowdowns.
- Scaling path: SQLite is sufficient for the target use case. If multi-camera throughput becomes an issue, consider PostgreSQL — the `get_conn()` abstraction makes this change relatively contained.

**`find_videos()` uses `rglob("*")` on the entire NAS video path:**
- Current capacity: Works for NAS libraries up to ~100k files.
- Limit: For very large NAS libraries (hundreds of thousands of files), `rglob("*")` will be slow and memory-intensive as it builds the full file list in memory before filtering.
- Files: `wildlife_processor.py` lines 126–132, `nas_sync.sh` lines 229–230
- Scaling path: Add a depth limit or use `os.scandir()` with early pruning by directory date.

---

## Dependencies at Risk

**`psutil` is optional with no warning if missing:**
- Risk: `web_app.py` imports `psutil` in a try/except and sets `_PSUTIL = False` if unavailable. The `/api/system` endpoint returns `{"available": False}` silently. The setup script may not install it.
- Files: `web_app.py` lines 33–36
- Impact: System monitoring tab shows nothing if psutil is not installed.
- Migration plan: Add `psutil` to the setup requirements explicitly. It is a well-maintained package with low risk.

**SpeciesNet requires Kaggle API credentials (`~/.kaggle/kaggle.json`):**
- Risk: The comment in `wildlife_processor.py` line 296 notes that SpeciesNet requires Kaggle credentials for the model download. If credentials are missing or expired, the processor fails silently (the `run_speciesnet` call would raise an unhandled exception propagated from `SpeciesNet.__init__`).
- Files: `wildlife_processor.py` lines 288–304
- Impact: Processing continues but no species are identified; all animals show as "Unknown species". The error is logged but the run continues.
- Migration plan: Add an explicit pre-flight check for `~/.kaggle/kaggle.json` before calling `SpeciesNet()`, with a clear user-facing error message.

---

## Missing Critical Features

**No authentication or access control:**
- Problem: The web dashboard has no login, API key, or network restriction mechanism built in. The `--host 0.0.0.0` default (and the systemd service) expose it to the LAN.
- Blocks: Safe deployment in shared network environments.

**No input validation on date parameters passed to shell:**
- Problem: `body.date_from` and `body.date_to` from `POST /api/run` are passed directly into a shell command string. There is no regex or format validation.
- Files: `web_app.py` lines 778–803
- Blocks: Safe operation if the dashboard is exposed to untrusted users on the LAN.

**`--dry-run` flag in `wildlife_processor.py` is parsed but never used:**
- Problem: `parse_args()` at `wildlife_processor.py` line 556 accepts a `--dry-run` argument and stores it in `args.dry_run`, but `process_videos(args)` never checks `args.dry_run`. The flag is forwarded through `nas_sync.sh` as a cosmetic label only (`EXTRA_PROCESSOR_ARGS+=("--dry-run")`).
- Files: `wildlife_processor.py` lines 556, `nas_sync.sh` line 58
- Blocks: Safe preview of what would be processed without actually writing to the DB.

---

## Test Coverage Gaps

**No automated tests exist:**
- What's not tested: All of `database.py`, `web_app.py`, `wildlife_processor.py`, and `image_quality.py` have zero test coverage. There is no `tests/` directory, no `pytest.ini`, no `test_*.py` files.
- Files: Entire codebase
- Risk: Regressions in migration logic, SQL query correctness, species label parsing, dual-lens pairing, retention policy purge math, and image quality scoring cannot be detected automatically.
- Priority: High — the migration code in `init_db()` is especially risky to modify without tests. The `parse_label()` and `parse_dual_lens_filename()` functions are pure/deterministic and would be easy to unit test first.

**`image_quality.py` quality score weights are untested and uncalibrated:**
- What's not tested: The `WEIGHTS` dict in `image_quality.py` (line 14) assigns fixed weights to sharpness, brightness, contrast, and size. These weights are hardcoded with no validation that they produce useful rankings for wildlife camera footage.
- Files: `image_quality.py` lines 14–19
- Risk: Poor image ranking degrades the gallery's "best crop" selection, causing low-quality or blurry frames to be shown as the representative image for a species.
- Priority: Medium — add a visual validation test comparing scored crops to human judgement.

---

*Concerns audit: 2026-05-06*
