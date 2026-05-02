#!/usr/bin/env python3
"""
web_app.py — Wildlife Monitor dashboard

Usage:
    python web_app.py                            # defaults: port 8080, data ./data
    python web_app.py --port 9090                # custom port
    python web_app.py --port 0                   # auto-select a free port
    python web_app.py --host 127.0.0.1           # localhost-only (more secure)
    python web_app.py --config wildlife.conf     # load settings from config file
    python web_app.py --check-port               # just check if the port is free, then exit

Config file format (INI-style, optional):
    [wildlife]
    port     = 8090
    host     = 0.0.0.0
    data_dir = /mnt/nas/wildlife_data
"""

import argparse
import configparser
import json
import os
import subprocess
import sys
import socket
import mimetypes
import threading
from pathlib import Path
from typing import Optional

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import database as db

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Wildlife Monitor", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR      = "./data"
SETTINGS_FILE = "./data/settings.json"
_run_process: subprocess.Popen | None = None
_run_lock = threading.Lock()

DEFAULT_PROCESSING_SETTINGS = {
    "hours":                  24,
    "sample_rate":            30,
    "md_threshold":           0.2,
    "country":                "US",
    "admin1_region":          "",
    "skip_speciesnet":        False,
    "filename_date_format":   "auto",
    # Retention — blank videos (no animal/person detected)
    "blank_retention_days":   60,
    "blank_retention_gb":     20.0,
    # Retention — kept videos (animal/person detected)
    "kept_retention_days":    730,
    "kept_retention_gb":      500.0,
}


def get_data_dir() -> str:
    return DATA_DIR


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        return {**DEFAULT_PROCESSING_SETTINGS, **saved}
    except (FileNotFoundError, json.JSONDecodeError):
        return DEFAULT_PROCESSING_SETTINGS.copy()


def _save_settings(data: dict):
    Path(SETTINGS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_nas_config() -> dict:
    config_file = Path.home() / ".config" / "wildlife_monitor" / "nas.conf"
    result: dict = {"configured": False, "mounted": False}
    if not config_file.exists():
        return result
    try:
        with open(config_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    result[key.strip()] = val.strip().strip('"').strip("'")
        result["configured"] = True
        mount = result.get("NAS_MOUNT", "")
        if mount:
            r = subprocess.run(["mountpoint", "-q", mount], capture_output=True)
            result["mounted"] = (r.returncode == 0)
    except Exception:
        pass
    # Never expose SMB password
    result.pop("NAS_SMB_PASS", None)
    return result


# ── Port utilities ─────────────────────────────────────────────────────────────

def is_port_free(host: str, port: int) -> bool:
    """Return True if the port is not currently bound by any process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
            return True
        except OSError:
            return False


def find_free_port(host: str, start: int = 8080, attempts: int = 20) -> int:
    """Find the first free port starting from `start`."""
    for port in range(start, start + attempts):
        if is_port_free(host, port):
            return port
    raise RuntimeError(f"No free port found in range {start}–{start + attempts}")


def get_process_on_port(port: int) -> str:
    """Try to identify what's using a port (best-effort, Linux only)."""
    try:
        import subprocess
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True, text=True, timeout=3
        )
        lines = [l for l in result.stdout.splitlines() if str(port) in l]
        if lines:
            # Extract process name from ss output if available
            import re
            m = re.search(r'users:\(\("([^"]+)"', lines[0])
            if m:
                return m.group(1)
        return "unknown process"
    except Exception:
        return "unknown process"


def resolve_port(requested_port: int, host: str) -> int:
    """
    Validate the requested port and return the port to use.
    - port 0  → auto-select a free port
    - port >0 → check it's free, warn loudly if not
    """
    if requested_port == 0:
        port = find_free_port(host)
        print(f"  Auto-selected port: {port}")
        return port

    if is_port_free(host, requested_port):
        return requested_port

    proc = get_process_on_port(requested_port)
    print(f"\n  ✗  Port {requested_port} is already in use by: {proc}")

    # Suggest alternatives
    alternatives = []
    for candidate in range(requested_port + 1, requested_port + 20):
        if is_port_free(host, candidate):
            alternatives.append(candidate)
        if len(alternatives) == 3:
            break

    if alternatives:
        print(f"     Free alternatives nearby: {', '.join(str(p) for p in alternatives)}")
        print(f"     Example: python web_app.py --port {alternatives[0]}")
    print(f"     Or use --port 0 to auto-select a free port.\n")
    sys.exit(1)


# ── Config file ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = "wildlife.conf"

def load_config(config_path: str) -> dict:
    """
    Load settings from an INI config file. Returns a dict of overrides.
    Missing file is silently ignored (config is optional).
    """
    cfg = {}
    if not os.path.exists(config_path):
        return cfg

    parser = configparser.ConfigParser()
    parser.read(config_path)
    section = "wildlife"

    if parser.has_section(section):
        if parser.has_option(section, "port"):
            cfg["port"] = parser.getint(section, "port")
        if parser.has_option(section, "host"):
            cfg["host"] = parser.get(section, "host")
        if parser.has_option(section, "data_dir"):
            cfg["data_dir"] = parser.get(section, "data_dir")

    return cfg


def write_example_config(path: str):
    """Write a documented example config file."""
    content = """\
# Wildlife Monitor configuration file
# Rename to wildlife.conf and place alongside web_app.py
# Command-line arguments override these values if both are specified.

[wildlife]
# Port to listen on. Use 0 for automatic selection.
port     = 8080

# Interface to bind. Use 0.0.0.0 for all interfaces, 127.0.0.1 for localhost only.
host     = 0.0.0.0

# Path to the data directory created by wildlife_processor.py
data_dir = ./data
"""
    with open(path, "w") as f:
        f.write(content)
    print(f"  Example config written to: {path}")


# ── Static / media routes ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    index = Path(__file__).parent / "static" / "index.html"
    if not index.exists():
        raise HTTPException(404, "static/index.html not found")
    return HTMLResponse(content=index.read_text())


@app.get("/media/crops/{filename}")
def serve_crop(filename: str):
    # Sanitise: no path traversal
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    path = Path(get_data_dir()) / "crops" / filename
    if not path.exists():
        raise HTTPException(404, "Crop not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/media/thumbnails/{filename}")
def serve_thumbnail(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(400, "Invalid filename")
    path = Path(get_data_dir()) / "thumbnails" / filename
    if not path.exists():
        raise HTTPException(404, "Thumbnail not found")
    return FileResponse(str(path), media_type="image/jpeg")


@app.get("/media/video/{video_id}")
def serve_video(video_id: int):
    result = db.get_video_by_id(video_id)
    if not result or not result.get("video"):
        raise HTTPException(404, "Video not found")
    filepath = result["video"].get("filepath")
    if not filepath:
        raise HTTPException(404, "Video file has been purged")
    if not os.path.exists(filepath):
        raise HTTPException(404, f"Video file missing on disk: {filepath}")
    mime = mimetypes.guess_type(filepath)[0] or "video/mp4"
    return FileResponse(filepath, media_type=mime)


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def api_stats():
    return db.get_stats()


@app.get("/api/species")
def api_species():
    rows = db.get_species_list()
    for r in rows:
        if r.get("best_crop"):
            r["best_crop"] = Path(r["best_crop"]).name
    return rows


@app.get("/api/species/search")
def api_species_search(
    q:           str  = Query(""),
    country:     str  = Query(None),
    admin1:      str  = Query(None),
    all_regions: bool = Query(False),
    limit:       int  = Query(20, ge=1, le=100),
):
    """Search SpeciesNet taxonomy. Filtered to region by default."""
    settings     = _load_settings()
    country      = country or settings.get("country")
    admin1       = admin1  or settings.get("admin1_region")
    classes_path = os.path.join(DATA_DIR, "speciesnet_classes.json")
    return db.search_taxonomy(q, classes_path, country=country, admin1=admin1,
                              limit=limit, all_regions=all_regions)


@app.get("/api/species/{label:path}")
def api_species_detail(label: str):
    detail = db.get_species_detail(label)
    if not detail.get("info"):
        raise HTTPException(404, "Species not found")
    for c in detail.get("crops", []):
        if c.get("crop_path"):
            c["crop_path"] = Path(c["crop_path"]).name
    for v in detail.get("videos", []):
        if v.get("thumbnail_path"):
            v["thumbnail_path"] = Path(v["thumbnail_path"]).name
    return detail


@app.get("/api/gallery")
def api_gallery(
    species: str = Query(None),
    sort: str = Query("quality"),
    page: int = Query(1, ge=1),
    per_page: int = Query(40, ge=1, le=100),
):
    result = db.get_gallery(species_label=species, sort_by=sort, page=page, per_page=per_page)
    for item in result["items"]:
        if item.get("crop_path"):
            item["crop_path"] = Path(item["crop_path"]).name
    return result


@app.get("/api/cameras")
def api_cameras():
    """Return all distinct camera names with video counts."""
    return db.get_cameras()


class CorrectionRequest(BaseModel):
    detection_id:         int
    user_common_name:     str
    user_scientific_name: str


@app.post("/api/species/correct")
def api_correct_species(body: CorrectionRequest):
    """Save a human correction for a species detection."""
    db.correct_species(
        detection_id=body.detection_id,
        user_common_name=body.user_common_name,
        user_scientific_name=body.user_scientific_name,
    )
    return {"ok": True}


@app.get("/api/videos")
def api_videos(
    species: str = Query(None),
    has_person: bool = Query(None),
    camera: str = Query(None),
    has_species: bool = Query(None),
    date_from: str = Query(None),
    date_to: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: str = Query(None),
):
    result = db.get_videos(
        species_label=species, has_person=has_person, camera_name=camera,
        has_species=has_species, date_from=date_from, date_to=date_to,
        page=page, per_page=per_page, search=search,
    )
    for v in result["items"]:
        if v.get("thumbnail_path"):
            v["thumbnail_path"] = Path(v["thumbnail_path"]).name
    return result


@app.get("/api/videos/{video_id}")
def api_video_detail(video_id: int):
    result = db.get_video_by_id(video_id)
    if not result:
        raise HTTPException(404, "Video not found")
    v = result.get("video", {})
    if v.get("thumbnail_path"):
        v["thumbnail_path"] = Path(v["thumbnail_path"]).name
    for d in result.get("detections", []):
        if d.get("crop_path"):
            d["crop_path"] = Path(d["crop_path"]).name
    # Normalise paired video paths
    paired = result.get("paired")
    if paired:
        if paired.get("thumbnail_path"):
            paired["thumbnail_path"] = Path(paired["thumbnail_path"]).name
    for d in result.get("pair_detections", []):
        if d.get("crop_path"):
            d["crop_path"] = Path(d["crop_path"]).name
    return result


@app.get("/api/timeline")
def api_timeline(
    days: int = Query(None, ge=1),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    return db.get_timeline(days=days, date_from=date_from, date_to=date_to)


@app.get("/api/blanks")
def api_blanks(
    page:      int = Query(1,  ge=1),
    per_page:  int = Query(20, ge=1, le=100),
    camera:    str = Query(None),
    search:    str = Query(None),
    date_from: str = Query(None),
    date_to:   str = Query(None),
):
    return db.get_blank_videos(
        page=page, per_page=per_page,
        camera=camera, search=search,
        date_from=date_from, date_to=date_to,
    )


@app.get("/api/system")
def api_system():
    """Return system resource usage — CPU, RAM, local disk, service uptime."""
    if not _PSUTIL:
        return {"available": False, "error": "psutil not installed"}

    import datetime

    # CPU — 0.5s interval is fast but non-blocking enough for a dashboard call
    cpu_pct = psutil.cpu_percent(interval=0.5)

    # RAM
    ram   = psutil.virtual_memory()
    ram_used_gb  = round(ram.used  / 1024**3, 1)
    ram_total_gb = round(ram.total / 1024**3, 1)
    ram_pct      = ram.percent

    # Local disk — partition containing the data directory
    try:
        disk = psutil.disk_usage(DATA_DIR)
        disk_used_gb  = round(disk.used  / 1024**3, 1)
        disk_total_gb = round(disk.total / 1024**3, 1)
        disk_pct      = round(disk.percent, 1)
    except Exception:
        disk_used_gb = disk_total_gb = disk_pct = None

    # Service uptime — time since the current process started
    try:
        proc         = psutil.Process(os.getpid())
        started_at   = datetime.datetime.fromtimestamp(proc.create_time())
        uptime_secs  = int((datetime.datetime.now() - started_at).total_seconds())
        h, rem       = divmod(uptime_secs, 3600)
        m, s         = divmod(rem, 60)
        uptime_str   = f"{h}h {m}m" if h else f"{m}m {s}s"
    except Exception:
        uptime_str = None

    return {
        "available":    True,
        "cpu_pct":      cpu_pct,
        "ram_used_gb":  ram_used_gb,
        "ram_total_gb": ram_total_gb,
        "ram_pct":      ram_pct,
        "disk_used_gb": disk_used_gb,
        "disk_total_gb":disk_total_gb,
        "disk_pct":     disk_pct,
        "uptime":       uptime_str,
        "hostname":     socket.gethostname(),
    }


class BlacklistEntry(BaseModel):
    label:           str
    common_name:     str
    scientific_name: str
    note:            Optional[str] = ""
    requeue:         bool = False   # if True, also requeue affected videos


class CorrectionRequest(BaseModel):
    video_id:            int
    original_label:      str
    corrected_label:     Optional[str] = None   # None = suppress
    corrected_common:    Optional[str] = None
    corrected_scientific: Optional[str] = None
    note:                Optional[str] = ""


@app.get("/api/blacklist")
def api_get_blacklist():
    entries = db.get_blacklist()
    for e in entries:
        e["affected_count"] = db.get_blacklist_affected_count(e["label"])
    return entries


@app.post("/api/blacklist")
def api_add_blacklist(entry: BlacklistEntry):
    db.add_to_blacklist(entry.label, entry.common_name, entry.scientific_name, entry.note or "")
    requeued = 0
    if entry.requeue:
        requeued = db.requeue_species(entry.label)
    return {"ok": True, "requeued": requeued}


@app.delete("/api/blacklist/{label:path}")
def api_remove_blacklist(label: str):
    db.remove_from_blacklist(label)
    return {"ok": True}


@app.post("/api/blacklist/{label:path}/requeue")
def api_requeue_species(label: str):
    count = db.requeue_species(label)
    return {"ok": True, "requeued": count}


@app.get("/api/corrections")
def api_get_corrections(video_id: int = Query(None)):
    if video_id:
        return db.get_video_corrections(video_id)
    # All corrections — for admin view
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT vc.*, v.filename, v.camera_name
            FROM video_corrections vc
            JOIN videos v ON vc.video_id = v.id
            ORDER BY vc.corrected_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/corrections")
def api_save_correction(req: CorrectionRequest):
    correction_id = db.save_video_correction(
        req.video_id, req.original_label, req.corrected_label,
        req.corrected_common, req.corrected_scientific, req.note or ""
    )
    return {"ok": True, "id": correction_id}


@app.delete("/api/corrections/{correction_id}")
def api_delete_correction(correction_id: int):
    db.delete_video_correction(correction_id)
    return {"ok": True}


@app.get("/api/maintenance/reprocess_queue")
def api_reprocess_queue():
    return {"videos": db.get_reprocess_queue(), "count": len(db.get_reprocess_queue())}



@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1)):
    result = db.search(q)
    for v in result.get("videos", []):
        if v.get("thumbnail_path"):
            v["thumbnail_path"] = Path(v["thumbnail_path"]).name
    return result


# ── Settings & run endpoints ───────────────────────────────────────────────────

@app.get("/api/updates")
def api_check_updates():
    """
    Check installed vs latest versions for pip packages and model files.
    Hits PyPI for package versions. Model checks are local-only.
    """
    import importlib.metadata
    import urllib.request
    import urllib.error

    results = []

    def pypi_latest(package: str) -> str:
        try:
            url = f"https://pypi.org/pypi/{package}/json"
            with urllib.request.urlopen(url, timeout=6) as r:
                return json.loads(r.read())["info"]["version"]
        except Exception:
            return None

    def installed_version(package: str) -> str:
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return None

    # ── pip packages ──────────────────────────────────────────────────────────
    for pkg in ["megadetector", "speciesnet"]:
        installed = installed_version(pkg)
        latest    = pypi_latest(pkg)
        up_to_date = (installed == latest) if (installed and latest) else None
        results.append({
            "id":          f"pkg_{pkg}",
            "type":        "package",
            "name":        pkg,
            "label":       pkg,
            "installed":   installed or "not found",
            "latest":      latest    or "unknown",
            "up_to_date":  up_to_date,
            "action":      "upgrade_package",
            "action_arg":  pkg,
        })

    # ── MegaDetector model ────────────────────────────────────────────────────
    try:
        # Try to find the cached model file — MegaDetector downloads to one of
        # several locations depending on the version and how it was invoked.
        md_cache_dirs = [
            Path.home() / ".cache" / "megadetector",
            Path.home() / ".cache" / "huggingface" / "hub",
            Path.home() / ".local" / "share" / "megadetector",
        ]
        md_cached_files = []
        for d in md_cache_dirs:
            if d.exists():
                md_cached_files += list(d.rglob("*.pt")) + list(d.rglob("*.onnx"))

        md_cached_name = md_cached_files[0].name if md_cached_files else None
        md_current     = md_cached_name or "not downloaded"

        # Try to get the expected model name from the package
        md_expected = None
        try:
            from megadetector.detection.pytorch_detector import DEFAULT_MODEL as MD_DEFAULT
            md_expected = MD_DEFAULT
        except ImportError:
            pass
        try:
            from megadetector.utils.ct_utils import DEFAULT_DETECTOR_LABEL as MD_DEFAULT2
            md_expected = md_expected or MD_DEFAULT2
        except ImportError:
            pass

        md_up_to_date = True if md_cached_name else False

        results.append({
            "id":         "model_megadetector",
            "type":       "model",
            "name":       "MegaDetector model",
            "label":      "MegaDetector model",
            "installed":  md_current,
            "latest":     md_expected or "see megadetector docs",
            "up_to_date": md_up_to_date,
            "action":     "clear_model_cache",
            "action_arg": str(md_cache_dirs[0]),
        })
    except Exception as e:
        results.append({"id": "model_megadetector", "type": "model", "name": "MegaDetector model",
                        "installed": f"error: {e}", "latest": "unknown", "up_to_date": None,
                        "action": None, "error": str(e)})

    # ── SpeciesNet model ──────────────────────────────────────────────────────
    try:
        from speciesnet import DEFAULT_MODEL as SN_DEFAULT, SUPPORTED_MODELS
        # Kaggle cache path: ~/.cache/kaggle/models/google/speciesnet/...
        kaggle_cache = Path.home() / ".cache" / "kaggle" / "models" / "google" / "speciesnet"
        sn_downloaded = kaggle_cache.exists() and any(kaggle_cache.rglob("*.pkl"))
        sn_cached     = "downloaded" if sn_downloaded else "not downloaded"
        sn_latest     = SUPPORTED_MODELS[-1] if SUPPORTED_MODELS else "unknown"
        # Only flag as update available if it IS downloaded but on an older version
        sn_up_to_date = True if not sn_downloaded else (SN_DEFAULT == sn_latest)
        results.append({
            "id":           "model_speciesnet",
            "type":         "model",
            "name":         "SpeciesNet model",
            "label":        "SpeciesNet model",
            "installed":    f"{SN_DEFAULT} ({sn_cached})",
            "latest":       sn_latest,
            "up_to_date":   sn_up_to_date,
            "downloaded":   sn_downloaded,
            "action":       "clear_model_cache" if sn_downloaded else None,
            "action_arg":   str(Path.home() / ".cache" / "kaggle" / "models" / "google" / "speciesnet"),
        })
    except Exception as e:
        results.append({"id": "model_speciesnet", "type": "model", "name": "SpeciesNet model",
                        "installed": "error", "latest": "unknown", "up_to_date": None,
                        "action": None, "error": str(e)})

    return {"components": results}


class UpdateRequest(BaseModel):
    action:     str   # "upgrade_package" | "clear_model_cache"
    action_arg: str   # package name or cache directory path


@app.post("/api/updates/apply")
def api_apply_update(body: UpdateRequest):
    """
    Apply an update action:
      upgrade_package  — pip install --upgrade <package> in the venv
      clear_model_cache — delete the model cache dir so it re-downloads on next run
    """
    if body.action == "upgrade_package":
        pkg = body.action_arg.strip()
        # Basic validation — only allow known packages
        if pkg not in ("megadetector", "speciesnet"):
            raise HTTPException(400, f"Unknown package: {pkg}")
        venv_pip = Path.home() / "wildlife_env" / "bin" / "pip"
        if not venv_pip.exists():
            raise HTTPException(500, f"pip not found at {venv_pip}")
        result = subprocess.run(
            [str(venv_pip), "install", "--upgrade", pkg],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise HTTPException(500, result.stderr[-500:])
        return {"ok": True, "output": result.stdout[-500:]}

    elif body.action == "clear_model_cache":
        import shutil
        cache_path = Path(body.action_arg)
        # Safety check — only allow deletion inside home dir cache paths
        home_cache = Path.home() / ".cache"
        try:
            cache_path.relative_to(home_cache)
        except ValueError:
            raise HTTPException(400, "Cache path must be inside ~/.cache")
        if cache_path.exists():
            shutil.rmtree(cache_path)
            return {"ok": True, "output": f"Cleared {cache_path}. Model will re-download on next run."}
        return {"ok": True, "output": "Cache directory did not exist — nothing to clear."}

    raise HTTPException(400, f"Unknown action: {body.action}")


class ProcessingSettings(BaseModel):
    hours:                  int   = 24
    sample_rate:            int   = 30
    md_threshold:           float = 0.2
    country:                str   = "US"
    admin1_region:          str   = ""
    skip_speciesnet:        bool  = False
    filename_date_format:   str   = "auto"
    blank_retention_days:   int   = 60
    blank_retention_gb:     float = 20.0
    kept_retention_days:    int   = 730
    kept_retention_gb:      float = 500.0


@app.get("/api/settings")
def api_get_settings():
    return {
        "processing": _load_settings(),
        "nas":        _load_nas_config(),
    }


@app.post("/api/settings")
def api_save_settings(body: ProcessingSettings):
    data = body.dict()
    data["country"] = data["country"].upper().strip()
    _save_settings(data)
    return {"ok": True}


class RunRequest(BaseModel):
    date_from: Optional[str] = None   # YYYY-MM-DD — if set, overrides --hours
    date_to:   Optional[str] = None   # YYYY-MM-DD


@app.post("/api/run")
def api_trigger_run(body: RunRequest = RunRequest()):
    global _run_process
    with _run_lock:
        if _run_process is not None and _run_process.poll() is None:
            return {"ok": False, "error": "A run is already in progress"}
        settings   = _load_settings()
        script_dir = Path(__file__).parent
        sync_script = script_dir / "nas_sync.sh"
        if not sync_script.exists():
            raise HTTPException(404, "nas_sync.sh not found alongside web_app.py")
        venv_dir = Path.home() / "wildlife_env"
        # Pass --data-dir so nas_sync.sh finds settings.json for country/region/sample-rate.
        # Don't duplicate those here — nas_sync.sh reads settings.json automatically.
        args = ["--then-process", "--data-dir", DATA_DIR]
        # Date range overrides hours; otherwise nas_sync.sh reads hours from settings.json
        if body.date_from or body.date_to:
            if body.date_from:
                args += ["--date-from", body.date_from]
            if body.date_to:
                args += ["--date-to", body.date_to]
        cmd = f'source "{venv_dir}/bin/activate" && bash "{sync_script}" {" ".join(args)}'
        log_path = Path(DATA_DIR) / "run_manual.log"
        log_f = open(log_path, "w")  # "w" truncates immediately, clearing old content
        _run_process = subprocess.Popen(
            cmd, shell=True, executable="/bin/bash",
            stdout=log_f, stderr=subprocess.STDOUT,
            cwd=str(script_dir),
        )
    return {"ok": True}


@app.get("/api/run/status")
def api_run_status():
    global _run_process
    running   = _run_process is not None and _run_process.poll() is None
    exit_code = _run_process.poll() if _run_process is not None else None
    log_lines: list[str] = []
    log_path = Path(DATA_DIR) / "run_manual.log"
    try:
        with open(log_path) as f:
            log_lines = f.readlines()[-100:]
    except FileNotFoundError:
        pass
    return {
        "running":   running,
        "exit_code": exit_code,
        "log":       "".join(log_lines),
    }


@app.get("/api/maintenance/storage")
def api_storage_stats():
    """Return storage usage broken out by blank vs kept videos."""
    return db.get_storage_stats()


@app.post("/api/maintenance/promote_paired")
def api_promote_paired():
    count = db.promote_paired_blanks()
    return {"ok": True, "promoted": count}


@app.post("/api/maintenance/purge")
def api_purge(dry_run: bool = Query(False)):
    """
    Run retention policy purge. Deletes video files older than the configured
    limits (by age or storage size), keeps all DB records.
    Pass dry_run=true to see what would be deleted without deleting anything.
    """
    settings = _load_settings()

    def _limit(val):
        """Convert setting value to None if 0/falsy (disabled), else return as-is."""
        return val if val else None

    purgeable = db.get_purgeable_videos(
        blank_days = _limit(settings.get("blank_retention_days")),
        blank_gb   = _limit(settings.get("blank_retention_gb")),
        kept_days  = _limit(settings.get("kept_retention_days")),
        kept_gb    = _limit(settings.get("kept_retention_gb")),
    )

    results = {"blank": [], "kept": [], "dry_run": dry_run}
    total_freed_mb = 0.0

    for category in ("blank", "kept"):
        for video in purgeable[category]:
            size_mb = video["file_size_mb"] or 0
            entry = {
                "id":          video["id"],
                "filename":    video["filename"],
                "recorded_at": video["recorded_at"],
                "size_mb":     round(size_mb, 1),
                "deleted":     False,
            }
            if not dry_run:
                deleted = db.purge_video_file(video["id"])
                entry["deleted"] = deleted
                if deleted:
                    total_freed_mb += size_mb
            else:
                # In dry run, sum up what would be freed
                total_freed_mb += size_mb
            results[category].append(entry)

    results["total_freed_gb"] = round(total_freed_mb / 1024, 2)
    results["blank_count"]    = len(results["blank"])
    results["kept_count"]     = len(results["kept"])
    return results


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global DATA_DIR

    parser = argparse.ArgumentParser(
        description="Wildlife Monitor Web Dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Config file (wildlife.conf) values are overridden by command-line args.",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port to listen on. Use 0 to auto-select a free port. (default: 8080)",
    )
    parser.add_argument(
        "--host", default=None,
        help="Interface to bind. Use 127.0.0.1 for local-only, 0.0.0.0 for all. (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Data directory used by the processor (default: ./data)",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH,
        help="Path to INI config file",
    )
    parser.add_argument(
        "--check-port", action="store_true",
        help="Just check whether the port is free and exit",
    )
    parser.add_argument(
        "--write-config", action="store_true",
        help="Write an example wildlife.conf and exit",
    )
    args = parser.parse_args()

    if args.write_config:
        write_example_config("wildlife.conf")
        sys.exit(0)

    # Load config file (silently ignored if missing)
    file_cfg = load_config(args.config)
    if file_cfg and args.config == DEFAULT_CONFIG_PATH:
        print(f"  Loaded config: {args.config}")

    # Merge: CLI args > config file > defaults
    host     = args.host     or file_cfg.get("host",     "0.0.0.0")
    port_req = args.port     if args.port is not None else file_cfg.get("port", 8080)
    DATA_DIR = args.data_dir or file_cfg.get("data_dir", "./data")
    SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

    # Port conflict check
    if args.check_port:
        if is_port_free(host, port_req):
            print(f"  Port {port_req} is free.")
            sys.exit(0)
        else:
            proc = get_process_on_port(port_req)
            print(f"  Port {port_req} is in use by: {proc}")
            sys.exit(1)

    port = resolve_port(port_req, host)

    # Validate data directory
    if not os.path.isdir(DATA_DIR):
        print(f"\n  Warning: data directory '{DATA_DIR}' does not exist yet.")
        print(f"  It will be created when you first run wildlife_processor.py.")
        print(f"  The dashboard will start but will show empty data until then.\n")

    # Initialise database
    db_path = os.path.join(DATA_DIR, "wildlife.db")
    db.init_db(db_path)

    bind_display = "localhost" if host == "127.0.0.1" else host
    print(f"\n  🦌  Wildlife Monitor")
    print(f"      Data dir  : {DATA_DIR}")
    print(f"      Database  : {db_path}")
    print(f"      Listening : http://{bind_display}:{port}")
    if host == "0.0.0.0":
        # Also show LAN IP if binding to all interfaces
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
            print(f"      LAN URL   : http://{lan_ip}:{port}")
        except Exception:
            pass
    print()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
