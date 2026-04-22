#!/usr/bin/env python3
"""
Wildlife Video Processor — with database, crop storage, and quality scoring.
"""

import os
import sys
import json
import argparse
import tempfile
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path

import cv2

from database import init_db, insert_video, insert_detection, insert_species, insert_crop, link_lens_pair, parse_dual_lens_filename
from image_quality import score_image

MD_ANIMAL, MD_PERSON = "1", "2"

# Model registry — URLs come from megadetector's own known_models dict.
# v1000.0.0-spruce is the fastest model (12.7x baseline), best for CPU-only machines.
# v1000.0.0-redwood is the baseline speed equivalent to MDv5, more thorough.
DEFAULT_MODEL    = "v1000.0.0-spruce"
MODEL_CACHE_DIR  = Path.home() / ".cache" / "megadetector"


def download_model_if_needed(model_name: str) -> str:
    """
    Return a local path to the model file, downloading it if not already cached.
    Uses the URL from megadetector's own known_models registry.
    """
    from megadetector.detection.pytorch_detector import known_models
    import urllib.request

    if model_name not in known_models:
        raise ValueError(
            f"Unknown model '{model_name}'. "
            f"Available: {list(known_models.keys())}"
        )

    url      = known_models[model_name]["url"]
    filename = url.split("/")[-1]
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODEL_CACHE_DIR / filename

    if dest.exists():
        return str(dest)

    print(f"  Downloading {model_name} (~160 MB, one-time)...")
    print(f"  From: {url}")
    print(f"  To:   {dest}")

    def progress(count, block_size, total_size):
        if total_size > 0:
            pct = min(100, count * block_size * 100 // total_size)
            print(f"\r  Progress: {pct}%", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=progress)
    print()  # newline after progress
    return str(dest)

def setup_logging(log_file):
    logger = logging.getLogger("wildlife_processor")
    logger.setLevel(logging.INFO)
    # Clear any handlers added by previous calls (Python caches named loggers)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for h in [logging.FileHandler(log_file), logging.StreamHandler(sys.stdout)]:
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger

VIDEO_EXTS = {".mp4",".avi",".mov",".mkv",".m4v",".mts",".ts",".wmv"}

# All supported filename date patterns: (regex, strptime_format)
FILENAME_DATE_PATTERNS = [
    (r'_(\d{14})$',                                '%Y%m%d%H%M%S'),
    (r'_(\d{8}_\d{6})$',                          '%Y%m%d_%H%M%S'),
    (r'_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$', '%Y-%m-%d_%H-%M-%S'),
    (r'_(\d{2}-\d{2}-\d{4}_\d{6})$',              '%d-%m-%Y_%H%M%S'),
    (r'_(\d{8})$',                                 '%Y%m%d'),
]

FILENAME_FORMAT_MAP = {
    'auto':                None,  # use FILENAME_DATE_PATTERNS (try all)
    'YYYYMMDDHHMMSS':      [(r'_(\d{14})$',                                '%Y%m%d%H%M%S')],
    'YYYYMMDD_HHMMSS':     [(r'_(\d{8}_\d{6})$',                          '%Y%m%d_%H%M%S')],
    'YYYY-MM-DD_HH-MM-SS': [(r'_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$', '%Y-%m-%d_%H-%M-%S')],
    'DD-MM-YYYY_HHMMSS':   [(r'_(\d{2}-\d{2}-\d{4}_\d{6})$',              '%d-%m-%Y_%H%M%S')],
    'MM-DD-YYYY_HHMMSS':   [(r'_(\d{2}-\d{2}-\d{4}_\d{6})$',              '%m-%d-%Y_%H%M%S')],
    'YYYYMMDD':            [(r'_(\d{8})$',                                 '%Y%m%d')],
}

def _date_from_filename(path: Path, fmt: str = 'auto') -> Optional[datetime]:
    """
    Extract recording datetime from DVR filename.
    Uses the configured format if specified, otherwise tries all known patterns.
    Returns None if no date found.
    """
    patterns = FILENAME_FORMAT_MAP.get(fmt) or FILENAME_DATE_PATTERNS
    stem = path.stem
    for regex, strpfmt in patterns:
        m = re.search(regex, stem)
        if m:
            try:
                return datetime.strptime(m.group(1), strpfmt)
            except ValueError:
                pass
    return None


def _video_date(path: Path, fmt: str = 'auto') -> datetime:
    """Return the best available date — filename date preferred over mtime."""
    return _date_from_filename(path, fmt) or datetime.fromtimestamp(path.stat().st_mtime)


def find_videos(directory, hours=24, date_from=None, date_to=None, filename_date_format='auto'):
    fmt = filename_date_format or 'auto'
    if date_from or date_to:
        from_dt = datetime.strptime(date_from, "%Y-%m-%d") if date_from else datetime.min
        to_dt   = datetime.strptime(date_to,   "%Y-%m-%d").replace(hour=23, minute=59, second=59) if date_to else datetime.max
        return sorted(p for p in Path(directory).rglob("*")
                      if p.suffix.lower() in VIDEO_EXTS and p.is_file()
                      and from_dt <= _video_date(p, fmt) <= to_dt)
    cutoff = datetime.now() - timedelta(hours=hours)
    return sorted(p for p in Path(directory).rglob("*")
                  if p.suffix.lower() in VIDEO_EXTS and p.is_file()
                  and _video_date(p, fmt) >= cutoff)

def get_video_duration(path):
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return frames / fps if fps > 0 else 0.0


def _camera_from_filename(stem: str) -> str:
    """
    Extract camera name from filename when there is no camera subfolder.
    Handles common DVR naming patterns:
      World Watch_00_20260327160902   →  World Watch_00   (14-digit timestamp)
      camera1_20260327_160902         →  camera1          (date + time separated)
      front_20260327                  →  front            (date only)
    """
    # 14-digit timestamp: _YYYYMMDDHHMMSS
    m = re.match(r'^(.+)_\d{14}$', stem)
    if m:
        return m.group(1)
    # 8-digit date + 6-digit time separated: _YYYYMMDD_HHMMSS
    m = re.match(r'^(.+)_\d{8}_\d{6}$', stem)
    if m:
        return m.group(1)
    # 8-digit date alone: _YYYYMMDD
    m = re.match(r'^(.+)_\d{8}$', stem)
    if m:
        return m.group(1)
    return ""


def extract_camera_name(video_path: Path, video_dir: str) -> str:
    """
    Derive the camera name from the folder structure or filename.

    Two supported layouts:
      A) <video_dir>/<CameraName>/<year>/<month>/<day>/<file>
         → camera name is the first subdirectory
      B) <video_dir>/<year>/<month>/<day>/<file>
         → no camera subfolder; parse name from filename instead

    Falls back to empty string if nothing can be determined.
    """
    try:
        rel = video_path.relative_to(video_dir)
        parts = rel.parts
        if len(parts) >= 2:
            first = parts[0]
            # If parts[0] is a 4-digit year (2000-2099), we're in layout B
            if re.match(r'^20\d{2}$', first):
                return _camera_from_filename(video_path.stem)
            return first
    except ValueError:
        pass
    return _camera_from_filename(video_path.stem)

def extract_thumbnail(video_path, thumb_dir, camera_name="", size=(320, 180)):
    Path(thumb_dir).mkdir(parents=True, exist_ok=True)
    # Prefix with camera name so files from different cameras never collide
    prefix = f"{camera_name[:20]}_" if camera_name else ""
    out = os.path.join(thumb_dir, f"{prefix}{video_path.stem[:40]}_thumb.jpg")
    if os.path.exists(out):
        return out
    cap = cv2.VideoCapture(str(video_path))
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.1))
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None
    resized = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if ok:
        with open(out, 'wb') as f:
            f.write(buf.tobytes())
    return out

def extract_frames(video_path, sample_rate, temp_dir):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    stem = video_path.stem[:30].replace(" ", "_")
    data, fn = [], 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if fn % sample_rate == 0:
            out = os.path.join(temp_dir, f"{stem}_f{fn:07d}.jpg")
            ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                with open(out, 'wb') as f:
                    f.write(buf.tobytes())
                data.append((out, fn, fn / fps))
        fn += 1
    cap.release()
    return data

def load_megadetector(model_name=DEFAULT_MODEL, force_cpu=False):
    from megadetector.detection.pytorch_detector import PTDetector
    model_path = download_model_if_needed(model_name)
    # PTDetector auto-selects CPU when no CUDA is available.
    # force_cpu is kept in the signature so --cpu CLI arg still parses cleanly.
    return PTDetector(model_path)

def _det_conf(det: dict) -> float:
    """Return detection confidence, handling both 'conf' and 'confidence' key names."""
    return float(det.get("conf", det.get("confidence", 0.0)))


def run_megadetector(detector, frame_data, threshold=0.2):
    import PIL.Image
    results = {}
    for fp, fn, ts in frame_data:
        try:
            result = detector.generate_detections_one_image(
                PIL.Image.open(fp), fp, detection_threshold=threshold
            )
            result["_fn"] = fn
            result["_ts"] = ts
        except Exception:
            result = {"detections": [], "_fn": fn, "_ts": ts}
        results[fp] = result
    return results

def save_crops(frame_data, md_results, crops_dir, stem, threshold=0.2):
    import PIL.Image
    Path(crops_dir).mkdir(parents=True, exist_ok=True)
    crops = []
    for fp, fn, ts in frame_data:
        dets = md_results.get(fp, {}).get("detections", [])
        if not dets:
            continue
        try:
            img = PIL.Image.open(fp)
            w, h = img.size
        except Exception:
            continue
        for i, det in enumerate(dets):
            if det.get("category") != MD_ANIMAL or _det_conf(det) < threshold:
                continue
            bx, by, bw, bh = det["bbox"]
            x1,y1 = max(0,int(bx*w)), max(0,int(by*h))
            x2,y2 = min(w,int((bx+bw)*w)), min(h,int((by+bh)*h))
            if x2<=x1 or y2<=y1:
                continue
            name = f"{stem[:30]}_f{fn:07d}_d{i:02d}.jpg"
            path = os.path.join(crops_dir, name)
            img.crop((x1,y1,x2,y2)).save(path, "JPEG", quality=90)
            crops.append({"crop_path": path, "frame_number": fn, "timestamp_secs": ts,
                          "bbox": det["bbox"], "confidence": _det_conf(det)})
    return crops

_speciesnet = None
def run_speciesnet(crop_paths, country=None, admin1_region=None):
    global _speciesnet
    if not crop_paths:
        return []
    if _speciesnet is None:
        from speciesnet import SpeciesNet, DEFAULT_MODEL
        # DEFAULT_MODEL is 'kaggle:google/speciesnet/pyTorch/v4.0.2a/1'
        # speciesnet handles download via Kaggle API internally on first run.
        # Requires ~/.kaggle/kaggle.json with your Kaggle API credentials.
        _speciesnet = SpeciesNet(DEFAULT_MODEL)
    kw = {"filepaths": crop_paths}
    if country:
        kw["country"] = country
    if admin1_region:
        kw["admin1_region"] = admin1_region
    return _speciesnet.predict(**kw).get("predictions", [])

def parse_label(label):
    """
    Parse a SpeciesNet label string into (scientific_name, common_name).

    SpeciesNet returns labels in two formats:
      1. New format: uuid;class;order;family;genus;species;common name
         e.g. "febff896-...;mammalia;artiodactyla;cervidae;odocoileus;hemionus;mule deer"
      2. Legacy format: "Genus species (Common Name)"
    """
    if not label or label in ("Unknown species", "No animal"):
        return "", label or "Unknown species"

    # New format: 7 semicolon-separated parts starting with a UUID
    parts = label.split(";")
    if len(parts) >= 7 and "-" in parts[0]:
        genus      = parts[4].strip().capitalize()
        species    = parts[5].strip()
        common     = parts[6].strip().title()
        if genus and species:
            sci = f"{genus} {species}"
        elif genus:
            sci = genus
        else:
            sci = ""
        return sci, common if common else (sci or "Unknown species")

    # Legacy format: "Genus species (Common Name)"
    if ";" in label:
        p = label.split(";", 1)
        return p[0].strip(), p[1].strip()
    m = re.match(r"^([A-Z][a-z]+ [a-z]+)\s+\((.+)\)$", label)
    if m:
        return m.group(1), m.group(2)
    return "", label

def process_videos(args):
    Path(args.data_dir).mkdir(parents=True, exist_ok=True)
    for sub in ("crops", "thumbnails"):
        Path(args.data_dir, sub).mkdir(parents=True, exist_ok=True)

    log_path = os.path.join(args.data_dir, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    log = setup_logging(log_path)

    db_path = os.path.join(args.data_dir, "wildlife.db")
    init_db(db_path)
    log.info(f"DB: {db_path}")

    videos = find_videos(
        args.video_dir,
        hours=args.hours,
        date_from=args.date_from,
        date_to=args.date_to,
        filename_date_format=args.filename_date_format,
    )
    if not videos:
        log.info("No videos found."); return

    log.info(f"Found {len(videos)} video(s)")
    log.info("Loading MegaDetector...")
    detector = load_megadetector(args.megadetector_model, force_cpu=args.cpu)
    log.info("MegaDetector ready")

    crops_dir = os.path.join(args.data_dir, "crops")
    thumb_dir = os.path.join(args.data_dir, "thumbnails")

    for video_path in videos:
        log.info(f"→ {video_path.name}")
        file_size   = video_path.stat().st_size / 1_048_576
        # Prefer filename-embedded date over mtime — mtime can be wrong after copy/move
        video_date  = _video_date(video_path, args.filename_date_format or 'auto')
        recorded_at = video_date.isoformat()
        duration    = get_video_duration(video_path)
        camera_name = extract_camera_name(video_path, args.video_dir)
        if camera_name:
            log.info(f"  Camera: {camera_name}")
        thumb       = extract_thumbnail(video_path, thumb_dir, camera_name)
        has_animal = has_person = False

        with tempfile.TemporaryDirectory() as tmp:
            frame_data = extract_frames(video_path, args.sample_rate, tmp)
            if not frame_data:
                log.warning("  No frames extracted — skipping")
                lens_parsed_nf = parse_dual_lens_filename(video_path.name)
                insert_video(video_path.name, str(video_path), camera_name, file_size, duration,
                             recorded_at, False, False, False, thumb, 0,
                             lens_index=lens_parsed_nf[1] if lens_parsed_nf else None)
                continue

            log.info(f"  {len(frame_data)} frames")
            md_results = run_megadetector(detector, frame_data, threshold=args.md_threshold)

            for fp, fn, ts in frame_data:
                for det in md_results.get(fp, {}).get("detections", []):
                    if _det_conf(det) < args.md_threshold:
                        continue
                    if det["category"] == MD_ANIMAL:
                        has_animal = True
                    elif det["category"] == MD_PERSON:
                        has_person = True

            kept = has_animal or has_person
            log.info(f"  animal={has_animal} person={has_person} → {'KEEP' if kept else 'DELETE'}")

            # Parse dual-lens info from filename
            lens_parsed = parse_dual_lens_filename(video_path.name)
            lens_index  = lens_parsed[1] if lens_parsed else None

            vid_id = insert_video(video_path.name, str(video_path), camera_name, file_size, duration,
                                  recorded_at, has_animal, has_person, kept, thumb, len(frame_data),
                                  lens_index=lens_index)
            # Link to paired lens if this is a dual-lens camera
            link_lens_pair(vid_id, video_path.name)

            # Person detections
            for fp, fn, ts in frame_data:
                for det in md_results.get(fp, {}).get("detections", []):
                    if det["category"] == MD_PERSON and _det_conf(det) >= args.md_threshold:
                        insert_detection(vid_id, fn, ts, "person", _det_conf(det), det["bbox"])

            if has_animal:
                saved = save_crops(frame_data, md_results, crops_dir, video_path.stem, args.md_threshold)
                log.info(f"  {len(saved)} crops saved")

                if not args.skip_speciesnet and saved:
                    log.info("  Running SpeciesNet...")
                    preds = run_speciesnet(
                        [c["crop_path"] for c in saved],
                        country=args.country,
                        admin1_region=args.admin1_region,
                    )
                    if len(preds) != len(saved):
                        log.warning(f"  SpeciesNet returned {len(preds)} predictions for {len(saved)} crops — pairing by index")

                    # ── Deduplicate species per video ─────────────────────────────
                    # Many crops may show the same animal. Rather than storing every
                    # crop's prediction independently (which inflates counts and causes
                    # multi-species noise), we:
                    #   1. Apply the confidence threshold — below 0.7 → Unknown species
                    #   2. Group crops by their top species label
                    #   3. Keep only the highest-quality crop per species per video
                    # This gives one clean representative detection per species per video.

                    SPECIES_CONFIDENCE_THRESHOLD = args.species_threshold
                    best_per_species = {}  # label → (crop, pred, score, quality)

                    for crop, pred in zip(saved, preds):
                        classes = pred.get("classifications", {}).get("classes", [])
                        scores  = pred.get("classifications", {}).get("scores", [])
                        if classes and scores and scores[0] >= SPECIES_CONFIDENCE_THRESHOLD:
                            label = classes[0]
                            score = scores[0]
                        else:
                            label = "Unknown species"
                            score = scores[0] if scores else 0.0

                        q = score_image(crop["crop_path"])
                        quality = q["quality_score"] if q else 0.0

                        # Keep the highest-quality crop for each distinct species label
                        if label not in best_per_species or quality > best_per_species[label][3]:
                            best_per_species[label] = (crop, pred, score, quality, q)

                    # Store one detection + species + crop per distinct species
                    for label, (crop, pred, score, quality, q) in best_per_species.items():
                        sci, common = parse_label(label)
                        det_id = insert_detection(vid_id, crop["frame_number"], crop["timestamp_secs"],
                                                  "animal", crop["confidence"], crop["bbox"])
                        insert_species(det_id, label, common, sci, score)
                        if q:
                            insert_crop(det_id, crop["crop_path"], **q)
                        if label == "Unknown species":
                            log.info(f"    Unknown species (best confidence: {score:.2f}, below threshold {SPECIES_CONFIDENCE_THRESHOLD})")
                        else:
                            log.info(f"    {common or label} ({sci}) confidence: {score:.2f} quality: {quality:.0f}/100")
                else:
                    for crop in saved:
                        det_id = insert_detection(vid_id, crop["frame_number"], crop["timestamp_secs"],
                                                  "animal", crop["confidence"], crop["bbox"])
                        q = score_image(crop["crop_path"])
                        if q:
                            insert_crop(det_id, crop["crop_path"], **q)

        if not kept:
            if args.dry_run:
                log.info(f"  [DRY-RUN] Would delete")
            else:
                try:
                    video_path.unlink()
                    log.info("  Deleted")
                except OSError as e:
                    log.error(f"  Delete failed: {e}")

    log.info(f"Done. Launch dashboard: python web_app.py --data-dir {args.data_dir}")

def parse_args():
    p = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--video-dir",          required=True)
    p.add_argument("--data-dir",           default="./data")
    p.add_argument("--hours",              type=int,   default=24,
                   help="Find videos modified within the last N hours. Ignored if --date-from/--date-to are set.")
    p.add_argument("--date-from",          default=None,
                   help="Process videos from this date inclusive (YYYY-MM-DD). Overrides --hours.")
    p.add_argument("--date-to",            default=None,
                   help="Process videos up to this date inclusive (YYYY-MM-DD). Overrides --hours.")
    p.add_argument("--sample-rate",        type=int,   default=30)
    p.add_argument("--md-threshold",       type=float, default=0.2)
    p.add_argument("--species-threshold",  type=float, default=0.7,
                   help="Minimum SpeciesNet confidence to accept a species ID (default: 0.7)")
    p.add_argument("--megadetector-model", default=DEFAULT_MODEL,
                   help=f"Model name from known_models or local path (default: {DEFAULT_MODEL})")
    p.add_argument("--filename-date-format", default="auto",
                   choices=["auto", "YYYYMMDDHHMMSS", "YYYYMMDD_HHMMSS",
                            "YYYY-MM-DD_HH-MM-SS", "DD-MM-YYYY_HHMMSS",
                            "MM-DD-YYYY_HHMMSS", "YYYYMMDD"],
                   help="Date format embedded in camera filenames. 'auto' tries all known patterns.")
    p.add_argument("--country",            default=None,
                   help="ISO country code for SpeciesNet geographic filtering (e.g. US, GB, AU)")
    p.add_argument("--admin1-region",      default=None,
                   help="State/province code for SpeciesNet geographic filtering "
                        "(e.g. US-UT, US-CA, GB-ENG). Narrows species candidates to "
                        "those with occurrence records in that region. More precise than "
                        "country-only filtering.")
    p.add_argument("--skip-speciesnet",    action="store_true")
    p.add_argument("--cpu",                action="store_true")
    p.add_argument("--dry-run",            action="store_true")
    return p.parse_args()

if __name__ == "__main__":
    process_videos(parse_args())
