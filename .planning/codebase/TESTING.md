# Testing Patterns

**Analysis Date:** 2026-05-06

## Test Framework

**Runner:** None detected

No test framework is configured in this project. No test files exist anywhere in the repository.

**Assertion Library:** Not applicable

**Run Commands:**
```bash
# No test commands defined — no pytest, unittest, or test runner configuration found
```

**Configuration:**
- No `pytest.ini`, `setup.cfg [tool:pytest]`, `pyproject.toml [tool.pytest]`, or `conftest.py`
- No `requirements-test.txt` or test-specific dependencies
- No `Makefile` or `tox.ini`

## Test File Organization

**Location:** No test files exist

**Naming:** No pattern established

**Structure:** Not applicable

## Test Structure

No tests exist in this codebase. The following are the testable units that currently have no test coverage:

**`database.py`** — All database query helpers, schema migrations, filter constants:
- `insert_video`, `insert_detection`, `insert_species`, `insert_crop`
- `get_stats`, `get_species_list`, `get_videos`, `get_video_by_id`, `get_gallery`, `get_timeline`
- `parse_dual_lens_filename`, `link_lens_pair`
- `search_taxonomy`
- `apply_corrections_to_species`
- `get_purgeable_videos`, `purge_video_file`
- `promote_paired_blanks`
- All migration logic in `init_db()`

**`wildlife_processor.py`** — Core processing pipeline functions:
- `_date_from_filename` (regex date parsing across 5 format patterns)
- `_camera_from_filename` (regex camera name extraction)
- `extract_camera_name` (layout A vs B detection)
- `parse_label` (SpeciesNet label format parsing — new UUID format vs legacy)
- `_det_conf` (key name normalization)

**`image_quality.py`** — Image scoring functions:
- `score_image` (quality metric calculation)
- `rank_crops_by_quality` (sort correctness)

**`web_app.py`** — API route behavior, Pydantic model validation:
- Settings load/save round-trip
- Port resolution logic (`is_port_free`, `find_free_port`, `resolve_port`)

## Mocking

**Framework:** Not applicable — no mocks defined

**What would need mocking when tests are written:**
- SQLite database: use `set_db_path()` in `database.py` with a `:memory:` or temp file path — the path setter is already available
- `cv2.VideoCapture` calls in `wildlife_processor.py` and `image_quality.py` — mock for frame extraction tests
- MegaDetector `PTDetector` in `load_megadetector()` — mock `generate_detections_one_image`
- SpeciesNet `_speciesnet.predict()` — mock the module-level `_speciesnet` singleton
- `urllib.request.urlopen` in `api_check_updates()` — mock PyPI HTTP calls
- `subprocess.run` / `subprocess.Popen` in `web_app.py` — mock for run trigger and port detection tests
- `Path.exists()`, `Path.unlink()` — mock for file purge tests

## Fixtures and Factories

**Test Data:** None defined

**When tests are written, the following fixtures would be appropriate:**

```python
# Example fixture pattern for database tests
import pytest
import database as db

@pytest.fixture
def test_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    db.init_db(db_file)
    yield db_file
    db.set_db_path("data/wildlife.db")  # restore default
```

**Location:** No `tests/` or `conftest.py` directory exists; would need to be created

## Coverage

**Requirements:** None enforced (no coverage configuration)

**View Coverage:**
```bash
# No coverage tooling configured — to add:
pip install pytest pytest-cov
pytest --cov=database --cov=wildlife_processor --cov=image_quality --cov=web_app --cov-report=html
```

## Test Types

**Unit Tests:** Not present
- Pure functions that are most suitable for unit testing: `parse_dual_lens_filename`, `parse_label`, `_date_from_filename`, `_camera_from_filename`, `apply_corrections_to_species`, `score_image`

**Integration Tests:** Not present
- Database round-trip tests (insert → query) would be high-value; `set_db_path()` enables this without mocking

**E2E Tests:** Not present; FastAPI supports `TestClient` from `httpx`/`starlette.testclient` for API-level testing without a live server

## Common Patterns

**Async Testing:** Not applicable (all routes are synchronous `def`, not `async def`)

**Error Testing:** Not applicable

## Notes on Testability

The codebase has several design choices that aid testability despite having no tests:

1. **`set_db_path(path)`** in `database.py` allows redirecting the global `DB_PATH` to a temp file, making database integration tests straightforward without any mocking
2. **Pure logic functions** (`parse_label`, `_date_from_filename`, `apply_corrections_to_species`) have no side effects and can be unit-tested directly
3. **`score_image`** in `image_quality.py` takes a file path string — tests need a real or fixture JPEG, or mock `cv2.imread`
4. **FastAPI `TestClient`** would work with `web_app.app` directly — the app object is module-level and importable
5. **Lazy model loading** (`_speciesnet = None` singleton) makes it easy to inject a mock before calling `run_speciesnet()` in tests

The main testability gap is that `process_videos()` is a large monolithic function (~200 lines) mixing I/O, model calls, and DB writes — it would need significant refactoring to test individual steps in isolation.

---

*Testing analysis: 2026-05-06*
