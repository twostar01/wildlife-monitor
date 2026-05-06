# Technology Stack

**Analysis Date:** 2026-05-06

## Languages

**Primary:**
- Python 3.10–3.12 - All backend processing, API, database, and AI pipeline
  - `wildlife_processor.py`, `web_app.py`, `database.py`, `image_quality.py`
  - Python 3.11 is the recommended target (speciesnet `<=3.13` constraint causes issues with 3.13.x)

**Secondary:**
- Bash - NAS sync orchestration and setup automation
  - `nas_sync.sh`, `nas_connect.sh`, `setup.sh`
- HTML/CSS/JavaScript (vanilla) - Single-page frontend dashboard
  - `static/index.html` (single file, no build step)

## Runtime

**Environment:**
- Ubuntu 20.04+ (Linux required — systemd, NFS/CIFS mounts, `ss`, `mountpoint`)
- Python venv at `~/wildlife_env/` (isolated, never touches system Python)

**Package Manager:**
- pip (inside venv only; `--no-deps` and `--only-binary` flags used to resolve onnx conflicts)
- No lockfile (requirements installed via `setup.sh` script, not a requirements.txt)

## Frameworks

**Core:**
- FastAPI (latest) - REST API and static file serving; `web_app.py`
- Uvicorn (standard extras) - ASGI server; started via `uvicorn.run()` in `web_app.py`
- Pydantic (via FastAPI) - Request/response model validation; `web_app.py`

**AI/ML:**
- PyTorch (CPU or CUDA 11/12 depending on GPU) - Model inference backend
- MegaDetector V6 (`megadetector` pip package, model `v1000.0.0-spruce` default) - Animal/person/vehicle detection; `wildlife_processor.py`
- SpeciesNet (`speciesnet` pip package, model `kaggle:google/speciesnet/pyTorch/v4.0.2a/1`) - Species identification; `wildlife_processor.py`
- ONNX Runtime (`onnxruntime>=1.16.3`) - Required by MegaDetector for model inference
- ONNX (`onnx>=1.15.0`) - Model format; pinned to avoid Python 3.12 build failures

**Computer Vision:**
- OpenCV (`opencv-python-headless`) - Frame extraction, thumbnail generation, image quality scoring; `wildlife_processor.py`, `image_quality.py`
- Pillow (PIL) - Image loading for MegaDetector inference; `wildlife_processor.py`
- NumPy - Pixel-level quality metric computation; `image_quality.py`

**Testing:**
- pytest - Installed in venv via `setup.sh`

**Linting:**
- ruff - Installed in venv via `setup.sh`

**Other pip packages (MegaDetector dependencies):**
- scikit-learn (`>=1.3.1`)
- ultralytics-yolov5 (`0.1.1`, installed `--no-deps`)
- yolov9pip (`0.0.4`, installed `--no-deps`)
- clipboard, dill, fastquadtree (`>=1.1.2`), jsonpickle (`>=3.0.2`), send2trash, tqdm

## Key Dependencies

**Critical:**
- `megadetector` - Core animal detection; downloaded from PyPI, model (~160 MB `.pt`) cached at `~/.cache/megadetector/`
- `speciesnet` - Species identification; model (~640 MB) downloaded via Kaggle API on first run, cached at `~/.cache/kaggle/models/google/speciesnet/`
- `fastapi` + `uvicorn` - Entire web dashboard depends on these
- `opencv-python-headless` - All video frame extraction and image processing
- `torch` + `torchvision` - PyTorch wheels selected at install time based on GPU/CUDA presence

**Infrastructure:**
- `psutil` (optional) - System resource stats endpoint (`/api/system`); gracefully degraded if missing
- `configparser` (stdlib) - INI config file support for `wildlife.conf`
- `sqlite3` (stdlib) - Only database driver; no ORM

## Configuration

**Environment:**
- No `.env` file — NAS credentials stored at `~/.config/wildlife_monitor/nas.conf` and `~/.config/wildlife_monitor/nas_smb_creds` (chmod 600, SMB only)
- Kaggle API credentials required at `~/.kaggle/kaggle.json` for SpeciesNet model download
- Processing settings persisted as JSON at `<data_dir>/settings.json`
- Dashboard INI config at `wildlife.conf` (optional, alongside `web_app.py`)

**Key runtime config values (with defaults):**
- `hours` = 24 — lookback window for video scanning
- `sample_rate` = 30 — frame sampling interval
- `md_threshold` = 0.2 — MegaDetector confidence cutoff
- `species_threshold` = 0.7 — SpeciesNet confidence cutoff
- `country` = "US" — ISO country code for SpeciesNet geo-filtering
- `admin1_region` = "" — State/province code for finer geo-filtering
- `blank_retention_days` = 60, `blank_retention_gb` = 20.0
- `kept_retention_days` = 730, `kept_retention_gb` = 500.0

**Build:**
- No build step; Python files run directly in venv
- `setup.sh` handles all installation (system packages via apt, PyTorch via pip with CUDA autodetection, remaining packages in venv)
- Systemd units in `systemd/` directory installed by `setup.sh` to `/etc/systemd/system/`

## Platform Requirements

**Development:**
- Ubuntu/Debian (apt-get required by `setup.sh`)
- Python 3.10–3.12 (3.11 recommended; 3.13 breaks speciesnet)
- `ffmpeg`, `cmake`, `libgl1`, `libglib2.0-0`, `libsm6`, `libxext6`, `libxrender-dev`, `libgomp1` (system packages)
- Optional: NVIDIA GPU with CUDA 11 or 12 for ~10x faster inference

**Production:**
- Linux with systemd (Ubuntu 20.04+)
- NAS accessible via NFS or SMB/CIFS mount
- Local disk space for video staging (temporary; cleaned up after processing)
- Dashboard runs on port 8080 by default, bound to `0.0.0.0`

---

*Stack analysis: 2026-05-06*
