#!/usr/bin/env bash
# ==============================================================================
#  Wildlife Monitor — NAS Video Sync
#
#  Copies recent videos from the NAS to local storage for processing,
#  then optionally triggers the processor and cleans up afterwards.
#
#  Why copy locally instead of processing directly from the NAS?
#    - MegaDetector does many random seeks per video (frame extraction)
#    - Network latency on each seek adds up to 10-50x slower processing
#    - Local processing is resilient to NAS going offline mid-job
#    - Local copy is deleted after processing — no permanent disk use
#
#  Usage:
#    ./nas_sync.sh                          # Sync last 24h of videos
#    ./nas_sync.sh --hours 48               # Sync last 48h
#    ./nas_sync.sh --then-process           # Sync then run the processor
#    ./nas_sync.sh --then-process --country US --dry-run
#    ./nas_sync.sh --local-dir /data/tmp    # Override local staging directory
#    ./nas_sync.sh --no-cleanup             # Keep local copies after processing
# ==============================================================================
set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_FILE="$HOME/.config/wildlife_monitor/nas.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_LOCAL_DIR="$SCRIPT_DIR/local_videos"
DEFAULT_DATA_DIR="$SCRIPT_DIR/data"
DEFAULT_HOURS=24
VENV_DIR="$HOME/wildlife_env"

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "  ${CYAN}→${NC}  $*"; }
ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
err()   { echo -e "  ${RED}✗${NC}  $*"; }

# ── Argument parsing ───────────────────────────────────────────────────────────
HOURS=$DEFAULT_HOURS
LOCAL_DIR=$DEFAULT_LOCAL_DIR
DATA_DIR=$DEFAULT_DATA_DIR
THEN_PROCESS=false
NO_CLEANUP=false
DRY_RUN=false
COUNTRY=""
DATE_FROM=""
DATE_TO=""
EXTRA_PROCESSOR_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hours)          HOURS="$2"; shift 2 ;;
        --local-dir)      LOCAL_DIR="$2"; shift 2 ;;
        --data-dir)       DATA_DIR="$2"; shift 2 ;;
        --then-process)   THEN_PROCESS=true; shift ;;
        --no-cleanup)     NO_CLEANUP=true; shift ;;
        --dry-run)        DRY_RUN=true; EXTRA_PROCESSOR_ARGS+=("--dry-run"); shift ;;
        --country)        COUNTRY="$2"; EXTRA_PROCESSOR_ARGS+=("--country" "$2"); shift 2 ;;
        --skip-speciesnet) EXTRA_PROCESSOR_ARGS+=("--skip-speciesnet"); shift ;;
        --sample-rate)    EXTRA_PROCESSOR_ARGS+=("--sample-rate" "$2"); shift 2 ;;
        --admin1-region)  EXTRA_PROCESSOR_ARGS+=("--admin1-region" "$2"); shift 2 ;;
        --date-from)      DATE_FROM="$2"; EXTRA_PROCESSOR_ARGS+=("--date-from" "$2"); shift 2 ;;
        --date-to)        DATE_TO="$2";   EXTRA_PROCESSOR_ARGS+=("--date-to" "$2"); shift 2 ;;
        --filename-date-format) EXTRA_PROCESSOR_ARGS+=("--filename-date-format" "$2"); shift 2 ;;
        --help|-h)
            sed -n '/^#/p' "$0" | sed 's/^# \{0,2\}//' | sed 's/^#//'
            exit 0 ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Load settings.json (fallback for values not passed on command line) ────────
SETTINGS_FILE="$DATA_DIR/settings.json"
if [[ -f "$SETTINGS_FILE" ]]; then
    _country=$(python3 -c "import json; d=json.load(open('$SETTINGS_FILE')); print(d.get('country',''))" 2>/dev/null)
    _admin1=$(python3  -c "import json; d=json.load(open('$SETTINGS_FILE')); print(d.get('admin1_region',''))" 2>/dev/null)
    _hours=$(python3   -c "import json; d=json.load(open('$SETTINGS_FILE')); print(d.get('hours',24))" 2>/dev/null)
    _sample=$(python3  -c "import json; d=json.load(open('$SETTINGS_FILE')); print(d.get('sample_rate',30))" 2>/dev/null)
    _skip=$(python3    -c "import json; d=json.load(open('$SETTINGS_FILE')); print('1' if d.get('skip_speciesnet') else '')" 2>/dev/null)
    _datefmt=$(python3 -c "import json; d=json.load(open('$SETTINGS_FILE')); print(d.get('filename_date_format','auto'))" 2>/dev/null)

    # Only apply if not already set by CLI args
    if [[ -z "$COUNTRY" && -n "$_country" ]]; then
        COUNTRY="$_country"
        EXTRA_PROCESSOR_ARGS+=("--country" "$_country")
    fi
    if [[ ! " ${EXTRA_PROCESSOR_ARGS[*]} " =~ "--admin1-region" && -n "$_admin1" ]]; then
        EXTRA_PROCESSOR_ARGS+=("--admin1-region" "$_admin1")
    fi
    # hours — only if no date range specified and not overridden by CLI
    if [[ -z "$DATE_FROM" && -z "$DATE_TO" && "$HOURS" == "$DEFAULT_HOURS" && -n "$_hours" ]]; then
        HOURS="$_hours"
    fi
    if [[ ! " ${EXTRA_PROCESSOR_ARGS[*]} " =~ "--sample-rate" && -n "$_sample" ]]; then
        EXTRA_PROCESSOR_ARGS+=("--sample-rate" "$_sample")
    fi
    if [[ ! " ${EXTRA_PROCESSOR_ARGS[*]} " =~ "--skip-speciesnet" && -n "$_skip" ]]; then
        EXTRA_PROCESSOR_ARGS+=("--skip-speciesnet")
    fi
    if [[ ! " ${EXTRA_PROCESSOR_ARGS[*]} " =~ "--filename-date-format" && -n "$_datefmt" && "$_datefmt" != "auto" ]]; then
        EXTRA_PROCESSOR_ARGS+=("--filename-date-format" "$_datefmt")
    fi
fi

# Export for use in the NAS scan Python block
FILENAME_DATE_FORMAT="${_datefmt:-auto}"

# ── Load NAS config ────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
    err "NAS config not found at $CONFIG_FILE"
    err "Run ./nas_connect.sh first to configure your NAS connection."
    exit 1
fi
source "$CONFIG_FILE"

NAS_VIDEO_PATH="${NAS_MOUNT}"
[[ -n "${NAS_VIDEO_SUBDIR:-}" ]] && NAS_VIDEO_PATH="${NAS_MOUNT}/${NAS_VIDEO_SUBDIR}"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Wildlife Monitor — NAS Video Sync"
echo "  Source  : $NAS_VIDEO_PATH"
echo "  Dest    : $LOCAL_DIR"
if [[ -n "$DATE_FROM" || -n "$DATE_TO" ]]; then
    echo "  Window  : ${DATE_FROM:-beginning} → ${DATE_TO:-today}"
else
    echo "  Window  : last $HOURS hours"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Check NAS is mounted ───────────────────────────────────────────────────────
ensure_mounted() {
    if mountpoint -q "${NAS_MOUNT}" 2>/dev/null; then
        return 0
    fi

    warn "${NAS_MOUNT} is not mounted. Attempting to mount..."
    if grep -q "[ \t]${NAS_MOUNT}[ \t]" /etc/fstab 2>/dev/null; then
        sudo mount "${NAS_MOUNT}"
        ok "Mounted via fstab"
    else
        warn "No fstab entry found. Mounting manually..."
        CREDS_FILE="$HOME/.config/wildlife_monitor/nas_smb_creds"
        if [[ "${NAS_PROTOCOL}" == "nfs" ]]; then
            sudo mount -t nfs -o "rw,soft,timeo=30,retrans=3,rsize=131072,wsize=131072,noatime" \
                "${NAS_HOST}:${NAS_SHARE}" "${NAS_MOUNT}"
        else
            sudo mount -t cifs \
                -o "credentials=${CREDS_FILE},uid=$(id -u),gid=$(id -g),iocharset=utf8,file_mode=0644,dir_mode=0755,noatime" \
                "//${NAS_HOST}/${NAS_SHARE}" "${NAS_MOUNT}"
        fi
        ok "Mounted ${NAS_MOUNT}"
    fi
}

ensure_mounted

if [[ ! -d "$NAS_VIDEO_PATH" ]]; then
    err "Video path not found on NAS: $NAS_VIDEO_PATH"
    err "Check NAS_VIDEO_SUBDIR in $CONFIG_FILE or re-run ./nas_connect.sh"
    exit 1
fi

# ── Find videos on NAS ────────────────────────────────────────────────────────
echo ""
if [[ -n "$DATE_FROM" || -n "$DATE_TO" ]]; then
    info "Scanning NAS for videos from ${DATE_FROM:-beginning} to ${DATE_TO:-today}..."
else
    info "Scanning NAS for videos modified in the last $HOURS hours..."
fi

# Use Python for the time filter (consistent with the processor's logic)
FILELIST=$(python3 - <<PYEOF
import os, sys, re
from pathlib import Path
from datetime import datetime, timedelta

nas_path   = "$NAS_VIDEO_PATH"
hours      = $HOURS
date_from  = "$DATE_FROM"
date_to    = "$DATE_TO"
fmt_config = "$FILENAME_DATE_FORMAT"
exts       = {'.mp4','.avi','.mov','.mkv','.m4v','.mts','.ts','.wmv'}

# All supported auto-detect patterns: (regex_to_extract_string, strptime_format)
AUTO_PATTERNS = [
    (r'_(\d{14})$',              '%Y%m%d%H%M%S'),   # _YYYYMMDDHHMMSS
    (r'_(\d{8}_\d{6})$',        '%Y%m%d_%H%M%S'),  # _YYYYMMDD_HHMMSS
    (r'_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$', '%Y-%m-%d_%H-%M-%S'),
    (r'_(\d{2}-\d{2}-\d{4}_\d{6})$', '%d-%m-%Y_%H%M%S'),  # _DD-MM-YYYY_HHMMSS
    (r'_(\d{2}-\d{2}-\d{4}_\d{6})$', '%m-%d-%Y_%H%M%S'),  # _MM-DD-YYYY_HHMMSS
    (r'_(\d{8})$',               '%Y%m%d'),         # _YYYYMMDD
]

# Named format map (value stored in settings → strptime patterns to try)
FORMAT_MAP = {
    'auto':                    None,  # use AUTO_PATTERNS
    'YYYYMMDDHHMMSS':          [(r'_(\d{14})$',              '%Y%m%d%H%M%S')],
    'YYYYMMDD_HHMMSS':         [(r'_(\d{8}_\d{6})$',        '%Y%m%d_%H%M%S')],
    'YYYY-MM-DD_HH-MM-SS':     [(r'_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$', '%Y-%m-%d_%H-%M-%S')],
    'DD-MM-YYYY_HHMMSS':       [(r'_(\d{2}-\d{2}-\d{4}_\d{6})$', '%d-%m-%Y_%H%M%S')],
    'MM-DD-YYYY_HHMMSS':       [(r'_(\d{2}-\d{2}-\d{4}_\d{6})$', '%m-%d-%Y_%H%M%S')],
    'YYYYMMDD':                [(r'_(\d{8})$',               '%Y%m%d')],
}

patterns = FORMAT_MAP.get(fmt_config) or AUTO_PATTERNS

def filename_date(path):
    stem = path.stem
    for regex, fmt in patterns:
        m = re.search(regex, stem)
        if m:
            try: return datetime.strptime(m.group(1), fmt)
            except ValueError: pass
    return datetime.fromtimestamp(path.stat().st_mtime)

if date_from or date_to:
    from_dt = datetime.strptime(date_from, "%Y-%m-%d") if date_from else datetime.min
    to_dt   = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if date_to else datetime.max
    def in_range(p): return from_dt <= filename_date(p) <= to_dt
else:
    cutoff = datetime.now() - timedelta(hours=hours)
    def in_range(p): return filename_date(p) >= cutoff

found = []
try:
    for p in Path(nas_path).rglob("*"):
        if p.suffix.lower() in exts and p.is_file() and in_range(p):
            found.append(str(p))
except PermissionError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

for f in sorted(found):
    print(f)
PYEOF
) || { err "Failed to scan NAS. Check permissions."; exit 1; }

VIDEO_COUNT=$(echo "$FILELIST" | grep -c '.' 2>/dev/null || echo 0)
if [[ -z "$FILELIST" || "$VIDEO_COUNT" -eq 0 ]]; then
    if [[ -n "$DATE_FROM" || -n "$DATE_TO" ]]; then
        ok "No videos found on NAS for ${DATE_FROM:-beginning} → ${DATE_TO:-today}. Nothing to do."
    else
        ok "No new videos found on NAS in the last $HOURS hours. Nothing to do."
    fi
    exit 0
fi

# Calculate total size
TOTAL_SIZE_BYTES=$(echo "$FILELIST" | xargs -I{} stat --format="%s" "{}" 2>/dev/null | awk '{s+=$1} END {print s+0}')
TOTAL_SIZE_MB=$((TOTAL_SIZE_BYTES / 1048576))
TOTAL_SIZE_GB=$(echo "scale=1; $TOTAL_SIZE_BYTES / 1073741824" | bc)

ok "Found $VIDEO_COUNT video(s) — ${TOTAL_SIZE_GB} GB to copy"

# ── Check local disk space ─────────────────────────────────────────────────────
AVAIL_BYTES=$(df --output=avail -B1 "${LOCAL_DIR%/*}" 2>/dev/null | tail -1 || echo 0)
# Add 20% headroom
NEEDED_BYTES=$(echo "$TOTAL_SIZE_BYTES * 1.2 / 1" | bc 2>/dev/null || echo "$TOTAL_SIZE_BYTES")

if [[ "$AVAIL_BYTES" -lt "$NEEDED_BYTES" ]]; then
    AVAIL_GB=$(echo "scale=1; $AVAIL_BYTES / 1073741824" | bc)
    err "Insufficient local disk space."
    err "  Need  : ~${TOTAL_SIZE_GB} GB (+20% headroom)"
    err "  Free  : ${AVAIL_GB} GB"
    err "  Use --local-dir to point to a disk with more space."
    exit 1
fi
ok "Disk space OK (need ~${TOTAL_SIZE_GB} GB, available: $(echo "scale=1; $AVAIL_BYTES / 1073741824" | bc) GB)"

# ── Copy videos preserving camera/date folder structure ───────────────────────
mkdir -p "$LOCAL_DIR"

echo ""
info "Copying $VIDEO_COUNT video(s) to $LOCAL_DIR (preserving folder structure)..."
info "Structure: <camera>/<year>/<month>/<day>/<file>"
echo ""

COPIED=0
SKIPPED=0
FAILED=0

while IFS= read -r src; do
    [[ -z "$src" ]] && continue

    # Derive destination path relative to the NAS video root.
    # This preserves the full camera/year/month/day/file.mp4 structure locally,
    # which prevents filename collisions between cameras and makes it easy to
    # add new cameras — they just appear as new top-level folders.
    rel_path="${src#${NAS_VIDEO_PATH}/}"
    dest="$LOCAL_DIR/$rel_path"
    dest_dir="$(dirname "$dest")"

    # Skip if already copied and same size (safe resume after interruption)
    if [[ -f "$dest" ]]; then
        src_size=$(stat --format="%s" "$src" 2>/dev/null || echo 0)
        dst_size=$(stat --format="%s" "$dest" 2>/dev/null || echo 0)
        if [[ "$src_size" -eq "$dst_size" && "$src_size" -gt 0 ]]; then
            echo -e "  ${YELLOW}→${NC}  SKIP  $rel_path"
            SKIPPED=$((SKIPPED + 1))
            continue
        fi
    fi

    # Create destination subdirectory if needed
    mkdir -p "$dest_dir"

    # Copy and preserve timestamps (so the processor's time-window filter works)
    if cp --preserve=timestamps "$src" "$dest" 2>/dev/null; then
        FILE_MB=$(stat --format="%s" "$dest" | awk '{printf "%.0f", $1/1048576}')
        echo -e "  ${GREEN}✓${NC}  ${FILE_MB} MB  $rel_path"
        COPIED=$((COPIED + 1))
    else
        echo -e "  ${RED}✗${NC}  FAILED  $rel_path"
        FAILED=$((FAILED + 1))
    fi
done <<< "$FILELIST"

echo ""
ok "Sync complete — copied: $COPIED  skipped: $SKIPPED  failed: $FAILED"

if [[ "$FAILED" -gt 0 ]]; then
    warn "$FAILED file(s) failed to copy. Check NAS permissions and disk space."
fi

# ── Optionally run the processor ───────────────────────────────────────────────
if [[ "$THEN_PROCESS" == true ]]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Running wildlife processor..."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Activate venv
    if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
        err "Virtual environment not found at $VENV_DIR"
        err "Run ./setup.sh first."
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"

    python3 "$SCRIPT_DIR/wildlife_processor.py" \
        --video-dir "$LOCAL_DIR" \
        --data-dir  "$DATA_DIR" \
        --hours     "$HOURS" \
        "${EXTRA_PROCESSOR_ARGS[@]}"

    PROCESSOR_EXIT=$?

    if [[ "$PROCESSOR_EXIT" -ne 0 ]]; then
        warn "Processor exited with errors (code $PROCESSOR_EXIT)."
        warn "Local copies kept at: $LOCAL_DIR — fix the issue and re-run."
        exit $PROCESSOR_EXIT
    fi

    if [[ "$DRY_RUN" == true ]]; then
        info "--dry-run mode. No files moved or deleted."
        exit 0
    fi

    if [[ "$NO_CLEANUP" == false ]]; then
        # ── Archive kept videos to NAS, then clean up all local copies ─────────
        NAS_ARCHIVE_SUBDIR="${NAS_ARCHIVE_SUBDIR:-wildlife_archive}"
        NAS_ARCHIVE_ROOT="$NAS_MOUNT/$NAS_ARCHIVE_SUBDIR"
        DB_PATH="$DATA_DIR/wildlife.db"

        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Archiving kept videos → NAS: $NAS_ARCHIVE_ROOT"
        echo "  Structure: <camera>/<year>/<month>/<day>/<file>"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        python3 - <<PYEOF
import os, sys, sqlite3, shutil
from pathlib import Path
from datetime import datetime

db_path       = "$DB_PATH"
local_dir     = "$LOCAL_DIR"
archive_root  = "$NAS_ARCHIVE_ROOT"

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, filepath, camera_name, recorded_at, filename FROM videos WHERE kept=1"
).fetchall()

archived = skipped = failed = already_archived = 0

for row in rows:
    local_path = Path(row["filepath"])

    # Skip if this video is already in the archive (re-run safety)
    if str(local_path).startswith(archive_root):
        already_archived += 1
        continue

    # Skip if the local file no longer exists (already cleaned up previously)
    if not local_path.exists():
        skipped += 1
        continue

    # Build archive destination:
    #   <archive_root>/<camera>/<year>/<month>/<day>/<filename>
    camera    = row["camera_name"] or "unknown_camera"
    rec_dt    = row["recorded_at"] or ""
    try:
        dt = datetime.fromisoformat(rec_dt)
        year, month, day = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"
    except (ValueError, TypeError):
        # Fall back to file mtime if recorded_at is missing or malformed
        mtime = local_path.stat().st_mtime
        dt    = datetime.fromtimestamp(mtime)
        year, month, day = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"

    dest_dir  = Path(archive_root) / camera / year / month / day
    dest_path = dest_dir / row["filename"]

    dest_dir.mkdir(parents=True, exist_ok=True)

    # If destination already exists with the same size, just update the DB path
    if dest_path.exists():
        src_size  = local_path.stat().st_size
        dest_size = dest_path.stat().st_size
        if src_size == dest_size:
            conn.execute("UPDATE videos SET filepath=? WHERE id=?",
                         (str(dest_path), row["id"]))
            conn.commit()
            print(f"  → SKIP (already archived)  {camera}/{year}/{month}/{day}/{row['filename']}")
            already_archived += 1
            continue

    try:
        # Capture timestamps before the move (source is gone afterwards)
        src_stat = local_path.stat()
        src_atime = src_stat.st_atime
        shutil.move(str(local_path), str(dest_path))
        # Restore original mtime on the archive copy so date-based queries stay accurate
        os.utime(str(dest_path), (src_atime, dt.timestamp()))
        conn.execute("UPDATE videos SET filepath=? WHERE id=?",
                     (str(dest_path), row["id"]))
        conn.commit()
        size_mb = dest_path.stat().st_size / 1_048_576
        print(f"  ✓  {size_mb:.0f} MB  {camera}/{year}/{month}/{day}/{row['filename']}")
        archived += 1
    except Exception as e:
        print(f"  ✗  FAILED  {row['filename']}: {e}", file=sys.stderr)
        failed += 1

conn.close()

print(f"\n  Archived: {archived}   Already done: {already_archived}   Skipped: {skipped}   Failed: {failed}")
if failed:
    sys.exit(1)
PYEOF

        ARCHIVE_EXIT=$?

        if [[ "$ARCHIVE_EXIT" -ne 0 ]]; then
            warn "Some videos failed to archive. Local copies kept for safety."
            warn "Re-run ./nas_sync.sh --then-process to retry."
            exit 1
        fi

        # ── Archive blank videos to NAS blanks folder ─────────────────────────
        NAS_BLANK_ROOT="$NAS_ARCHIVE_ROOT/blanks"
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Archiving blank videos → NAS: $NAS_BLANK_ROOT"
        echo "  Structure: blanks/<camera>/<year>/<month>/<day>/<file>"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""

        python3 - <<PYEOF
import os, sys, sqlite3, shutil
from pathlib import Path
from datetime import datetime

db_path      = "$DB_PATH"
local_dir    = "$LOCAL_DIR"
blank_root   = "$NAS_BLANK_ROOT"
archive_root = "$NAS_ARCHIVE_ROOT"

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, filepath, camera_name, recorded_at, filename
    FROM videos
    WHERE kept=0
      AND has_animal=0 AND has_person=0
      AND filepath IS NOT NULL
      AND file_purged_at IS NULL
""").fetchall()

archived = skipped = failed = already_archived = 0

for row in rows:
    local_path = Path(row["filepath"])

    # Skip if already in archive
    if str(local_path).startswith(archive_root):
        already_archived += 1
        continue

    if not local_path.exists():
        skipped += 1
        continue

    camera = row["camera_name"] or "unknown_camera"
    rec_dt = row["recorded_at"] or ""
    try:
        dt = datetime.fromisoformat(rec_dt)
        year, month, day = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"
    except (ValueError, TypeError):
        mtime = local_path.stat().st_mtime
        dt    = datetime.fromtimestamp(mtime)
        year, month, day = f"{dt.year:04d}", f"{dt.month:02d}", f"{dt.day:02d}"

    dest_dir  = Path(blank_root) / camera / year / month / day
    dest_path = dest_dir / row["filename"]
    dest_dir.mkdir(parents=True, exist_ok=True)

    if dest_path.exists():
        src_size  = local_path.stat().st_size
        dest_size = dest_path.stat().st_size
        if src_size == dest_size:
            conn.execute("UPDATE videos SET filepath=? WHERE id=?",
                         (str(dest_path), row["id"]))
            conn.commit()
            print(f"  → SKIP (already archived)  blanks/{camera}/{year}/{month}/{day}/{row['filename']}")
            already_archived += 1
            continue

    try:
        src_stat  = local_path.stat()
        src_atime = src_stat.st_atime
        shutil.move(str(local_path), str(dest_path))
        os.utime(str(dest_path), (src_atime, dt.timestamp()))
        conn.execute("UPDATE videos SET filepath=? WHERE id=?",
                     (str(dest_path), row["id"]))
        conn.commit()
        size_mb = dest_path.stat().st_size / 1_048_576
        print(f"  ✓  {size_mb:.0f} MB  blanks/{camera}/{year}/{month}/{day}/{row['filename']}")
        archived += 1
    except Exception as e:
        print(f"  ✗  FAILED  {row['filename']}: {e}", file=sys.stderr)
        failed += 1

conn.close()
print(f"\n  Blank archived: {archived}   Already done: {already_archived}   Skipped: {skipped}   Failed: {failed}")
if failed:
    sys.exit(1)
PYEOF

        BLANK_ARCHIVE_EXIT=$?
        if [[ "$BLANK_ARCHIVE_EXIT" -ne 0 ]]; then
            warn "Some blank videos failed to archive. Local copies kept for safety."
            exit 1
        fi

        # ── Delete ALL local staging copies (kept and deleted alike) ─────────
        echo ""
        info "Cleaning up local staging directory..."
        REMOVED=0
        while IFS= read -r src; do
            [[ -z "$src" ]] && continue
            rel_path="${src#${NAS_VIDEO_PATH}/}"
            dest="$LOCAL_DIR/$rel_path"
            if [[ -f "$dest" ]]; then
                rm "$dest"
                REMOVED=$((REMOVED + 1))
            fi
        done <<< "$FILELIST"
        ok "Removed $REMOVED local staging copy/copies"
        find "$LOCAL_DIR" -mindepth 1 -type d -empty -delete 2>/dev/null || true
        if [[ -d "$LOCAL_DIR" ]] && [[ -z "$(ls -A "$LOCAL_DIR")" ]]; then
            rmdir "$LOCAL_DIR"
            ok "Removed empty staging directory: $LOCAL_DIR"
        fi

        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ok "All done. Footage archived to NAS at:"
        echo "     Kept:  $NAS_ARCHIVE_ROOT/<camera>/<year>/<month>/<day>/"
        echo "     Blank: $NAS_ARCHIVE_ROOT/blanks/<camera>/<year>/<month>/<day>/"
        echo "  The dashboard reads videos directly from there via the mount."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

        # ── Automatic retention purge ─────────────────────────────────────────
        echo ""
        info "Running retention policy purge..."
        python3 - <<PYEOF
import sys, json
sys.path.insert(0, "$SCRIPT_DIR")
from database import init_db, get_purgeable_videos, purge_video_file, get_storage_stats
import os

db_path = "$DATA_DIR/wildlife.db"
init_db(db_path)

# Load retention settings from settings.json
settings_path = "$DATA_DIR/settings.json"
settings = {}
try:
    with open(settings_path) as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    pass

blank_days = settings.get("blank_retention_days") or 60
blank_gb   = settings.get("blank_retention_gb")   or None
kept_days  = settings.get("kept_retention_days")  or None
kept_gb    = settings.get("kept_retention_gb")    or None

purgeable = get_purgeable_videos(blank_days, blank_gb, kept_days, kept_gb)
total = len(purgeable["blank"]) + len(purgeable["kept"])

if total == 0:
    print("  No videos eligible for purge under current retention policy.")
else:
    freed_mb = 0.0
    for category, label in [("blank", "blank"), ("kept", "kept with detections")]:
        for v in purgeable[category]:
            deleted = purge_video_file(v["id"])
            if deleted:
                freed_mb += v.get("file_size_mb") or 0
                print(f"  Purged: {v['filename']} ({v.get('file_size_mb',0):.0f} MB)")
    print(f"  Purge complete — {total} file(s) removed, {freed_mb/1024:.2f} GB freed")

stats = get_storage_stats()
print(f"  Storage remaining: {stats['total_active_gb']:.1f} GB active "
      f"({stats['blank_gb']:.1f} GB blank + {stats['kept_gb']:.1f} GB kept)")
PYEOF
    else
        info "--no-cleanup specified. Local copies kept at: $LOCAL_DIR"
        info "Kept videos have NOT been moved to NAS archive yet."
        info "Re-run without --no-cleanup to complete the archive step."
    fi

    exit 0
fi

# ── Standalone mode summary ────────────────────────────────────────────────────
NAS_ARCHIVE_SUBDIR="${NAS_ARCHIVE_SUBDIR:-wildlife_archive}"
echo ""
echo "  Videos ready for processing at: $LOCAL_DIR"
echo ""
echo "  Run the processor then archive to NAS in one step:"
echo "    ./nas_sync.sh --then-process --country US"
echo ""
echo "  Or activate and run the processor manually:"
echo "    source $VENV_DIR/bin/activate"
echo "    python wildlife_processor.py --video-dir $LOCAL_DIR --country US"
echo "  Then run nas_sync.sh again with --then-process to archive kept videos."
echo ""
echo ""
