# Wildlife Monitor

Automated wildlife detection for security camera and camera trap footage. Videos are pulled from a Synology NAS, processed locally using MegaDetector and SpeciesNet, and results are browsed through a web dashboard. Footage with detections is archived back to the NAS in an organised folder structure. Footage with no detections is retained briefly then purged on a configurable schedule.

---

## What it does

1. **Syncs** recent video files from a Synology NAS to local staging storage
2. **Detects** animals, people, and vehicles using [MegaDetector V6](https://github.com/agentmorris/MegaDetector)
3. **Identifies** species using [SpeciesNet](https://github.com/google/wildlife-datasets) (2,000+ species, trained on 65M images), with state/province-level geographic filtering to eliminate impossible identifications
4. **Scores** each animal crop for image quality (sharpness, brightness, contrast, size)
5. **Archives** kept footage to the NAS under a clean camera/year/month/day/ structure
6. **Purges** old footage according to configurable retention limits (by age or total storage)
7. **Serves** a web dashboard for browsing species, gallery crops, videos, and activity trends
8. **Pairs** dual-lens cameras (fixed wide + adjustable telephoto) so both lenses play side by side in sync

---

## File layout

```
wildlife_monitor/
├── setup.sh                          # One-time installer
├── nas_connect.sh                    # NAS connection wizard (run once)
├── nas_sync.sh                       # Daily sync + process + archive script
├── wildlife_processor.py             # Core detection pipeline
├── database.py                       # SQLite schema and query helpers
├── image_quality.py                  # Crop quality scoring
├── web_app.py                        # FastAPI dashboard server
├── static/
│   └── index.html                    # Single-page dashboard UI
└── README.md

/etc/systemd/system/                  # Systemd service files (see Automation section)
├── wildlife-monitor.service          # Web dashboard — starts on boot
├── wildlife-analysis.service         # Daily analysis run (triggered by timer)
└── wildlife-analysis.timer           # Fires wildlife-analysis.service at 6 AM daily
```

After first run, these are created automatically:

```
wildlife_monitor/
└── data/
    ├── wildlife.db          # SQLite database (all detections, species, crops)
    ├── settings.json        # Dashboard settings (country, region, retention, etc.)
    ├── crops/               # Animal crop images (kept permanently)
    ├── thumbnails/          # Video thumbnails (kept permanently)
    ├── cron.log             # Output from scheduled analysis runs
    └── run_YYYYMMDD_*.log   # Per-run log files

~/.config/wildlife_monitor/
├── nas.conf                 # NAS connection settings (chmod 600)
└── nas_smb_creds            # SMB credentials if using SMB (chmod 600)
```

---

## System requirements

- Ubuntu 20.04 or later (tested on 22.04, 24.04, 25.04)
- Python 3.10–3.12 (Python 3.13 is not yet supported by SpeciesNet)
- 8 GB RAM minimum; 16 GB recommended
- A Synology NAS accessible on the local network via NFS or SMB
- GPU optional (NVIDIA with CUDA 11 or 12); CPU works but is ~10x slower

---

## Installation

### 1. Run the setup script

```bash
chmod +x setup.sh nas_connect.sh nas_sync.sh
./setup.sh
```

The setup script installs system dependencies, creates a virtual environment at ~/wildlife_env, and installs all Python packages inside it. MegaDetector and SpeciesNet models (~800 MB total) download automatically on first use.

**Options:**

| Flag | Effect |
|---|---|
| `--venv /path` | Custom venv location (default: `~/wildlife_env`) |
| `--reinstall` | Wipe and recreate the venv from scratch |
| `--no-gpu` | Force CPU-only PyTorch even if a GPU is present |
| `--skip-apt` | Skip all apt operations |

### 2. Configure the NAS connection

```bash
./nas_connect.sh
```

Interactive 7-step wizard:

| Step | What it asks |
|---|---|
| 1 | NAS hostname or IP address |
| 2 | Protocol: NFS (recommended) or SMB |
| 3 | Share path |
| 4 | SMB credentials (NFS skips this) |
| 5 | Local mount point (default: `/mnt/wildlife_nas`) |
| 6 | Source subdirectory within the share |
| 7 | Archive folder name (default: `wildlife_archive`) |

**Management commands:**

```bash
./nas_connect.sh --test      # Test the saved connection
./nas_connect.sh --show      # Display saved config (passwords hidden)
./nas_connect.sh --unmount   # Unmount the share
./nas_connect.sh             # Re-run wizard to change any setting
```

### 3. First run

```bash
source ~/wildlife_env/bin/activate
./nas_sync.sh --then-process
```

Country, region, and all other settings are read from data/settings.json automatically. On first run before settings have been saved via the dashboard, pass them explicitly:

```bash
./nas_sync.sh --then-process --country US --admin1-region US-UT
```

### 4. Start the dashboard

```bash
source ~/wildlife_env/bin/activate
python web_app.py --port 0 --host 0.0.0.0 --data-dir /home/nash/wildlife_monitor/data
```

Open **http://localhost:8080** in a browser (or the auto-selected port shown in output). From another device on the same network: `http://<machine-ip>:<port>`.

---

## Automation (systemd)

The system ships with three systemd unit files that replace the cron job and ensure the dashboard and analysis start automatically on boot.

### Installing the systemd units

```bash
sudo cp wildlife-monitor.service  /etc/systemd/system/
sudo cp wildlife-analysis.service /etc/systemd/system/
sudo cp wildlife-analysis.timer   /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now wildlife-monitor.service
sudo systemctl enable --now wildlife-analysis.timer

# Remove the old cron job if you had one
crontab -e    # delete any wildlife_monitor line
```

### What each unit does

**`wildlife-monitor.service`** — runs the web dashboard on port 8080. Starts on boot, restarts automatically on crash, waits 5 seconds for the NAS mount to be ready.

**`wildlife-analysis.service`** — one-shot service that runs `nas_sync.sh --then-process`. All settings (country, region, hours, sample rate) are read from `data/settings.json`. Change settings via the dashboard — no service file edits needed.

**`wildlife-analysis.timer`** — fires the analysis service at 6:00 AM daily. `Persistent=true` means if the machine was off at 6 AM, the analysis runs as soon as it next boots.

### Useful commands

```bash
# Web server
sudo systemctl status wildlife-monitor
journalctl -u wildlife-monitor -f          # live logs

# Analysis
sudo systemctl status wildlife-analysis.timer    # shows next scheduled run
sudo systemctl start wildlife-analysis.service   # trigger manually
tail -f ~/wildlife_monitor/data/cron.log         # analysis output
```

### Changing the run time

```bash
sudo systemctl edit wildlife-analysis.timer
```

Add (replacing 06:00:00 with your preferred time):

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 06:00:00
```

Then `sudo systemctl daemon-reload`.

---

## Settings page

The dashboard Settings tab controls all processing parameters. Settings are saved to `data/settings.json` and take effect on the next run — no restart or command-line changes needed.

| Setting | Default | Description |
|---|---|---|
| Hours lookback | 24 | Scan videos modified within the last N hours. 1 Week = 168 hrs, 1 Month = 730 hrs, 1 Year = 8760 hrs |
| Frame sample rate | 30 | Extract 1 frame per N frames (~1 fps at 30 fps video) |
| Detection threshold | 0.2 | MegaDetector confidence cutoff |
| Country code | US | ISO country code for SpeciesNet geo-filtering |
| State/Region code | | Tighter filtering by state/province (e.g. `US-UT`, `US-CA`, `GB-ENG`). Strongly recommended |
| Skip SpeciesNet | off | Detection only — no species identification |

The Settings page also provides:
- **NAS Connection** — read-only display of current NAS config and mount status
- **Retention Policy** — configure how long blank and kept videos are stored
- **Storage Usage** — live breakdown of disk usage
- **Software & Models** — check for package and model updates
- **Backfill** — process a specific date range
- **Manual Run** — trigger an immediate sync + analysis with live log streaming

---

## Backfill historical footage

Use the Backfill card in the Settings page. Pick a From and To date, click Run Backfill. Uses your saved country and region settings automatically.

From the command line:

```bash
./nas_sync.sh --then-process --date-from 2025-01-01 --date-to 2025-01-31
```

### Re-processing a period with corrected settings

If a period was processed without the correct region filter, clear it and re-run:

```bash
source ~/wildlife_env/bin/activate
python3 -c "
import sqlite3
conn = sqlite3.connect('data/wildlife.db')
start, end = '2025-01-01', '2025-02-01'
for q in [
    'DELETE FROM crops WHERE detection_id IN (SELECT d.id FROM detections d JOIN videos v ON d.video_id=v.id WHERE v.recorded_at >= ? AND v.recorded_at < ?)',
    'DELETE FROM species WHERE detection_id IN (SELECT d.id FROM detections d JOIN videos v ON d.video_id=v.id WHERE v.recorded_at >= ? AND v.recorded_at < ?)',
    'DELETE FROM detections WHERE video_id IN (SELECT id FROM videos WHERE recorded_at >= ? AND recorded_at < ?)',
    'UPDATE videos SET has_animal=0, has_person=0, kept=0 WHERE recorded_at >= ? AND recorded_at < ?',
]:
    conn.execute(q, (start, end))
conn.commit()
print('Cleared')
"
deactivate
./nas_sync.sh --then-process --date-from 2025-01-01 --date-to 2025-01-31
```

---

## Dual-lens cameras

Cameras with a fixed wide lens and an adjustable telephoto lens (e.g. Reolink dual-lens models) are automatically detected and paired. The naming format must be `{CameraName}_{Lens}_{YYYYMMDDHHMMSS}.mp4` where Lens is `00` for wide and `01` for telephoto.

When you open a paired video in the dashboard, both lenses play side by side in sync. Detection chips are merged from both lenses, preferring telephoto crops.

To backfill pairing for footage already in the database:

```bash
source ~/wildlife_env/bin/activate
python3 -c "
import sqlite3, re
from pathlib import Path
conn = sqlite3.connect('data/wildlife.db')
conn.row_factory = sqlite3.Row
def parse(fn):
    m = re.match(r'^(.+)_(\d{2})_(\d{14})$', Path(fn).stem)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else None
rows = conn.execute('SELECT id, filename FROM videos').fetchall()
paired = 0
for r in rows:
    p = parse(r['filename'])
    if not p: continue
    base, lens, ts = p
    conn.execute('UPDATE videos SET lens_index=? WHERE id=?', (lens, r['id']))
    match = conn.execute('SELECT id FROM videos WHERE id != ? AND filename LIKE ?',
        (r['id'], f'{base}_%_{ts}%')).fetchall()
    for m in match:
        row = conn.execute('SELECT filename FROM videos WHERE id=?', (m['id'],)).fetchone()
        mp = parse(row['filename'])
        if mp and mp[0]==base and mp[2]==ts and mp[1]!=lens:
            conn.execute('UPDATE videos SET paired_video_id=? WHERE id=?', (m['id'], r['id']))
            conn.execute('UPDATE videos SET paired_video_id=? WHERE id=?', (r['id'], m['id']))
            paired += 1
conn.commit()
print(f'Linked {paired} pairs')
"
```

---

## Species identification and geographic filtering

Set State/Region code in Settings. Format is `{country}-{region}`:

| Location | Code |
|---|---|
| Utah, USA | `US-UT` |
| California, USA | `US-CA` |
| Texas, USA | `US-TX` |
| England, UK | `GB-ENG` |
| Ontario, Canada | `CA-ON` |
| Victoria, Australia | `AU-VIC` |

The filter uses live occurrence data — new species genuinely present in your region are still identified correctly.

### Correcting misidentifications

In the Gallery tab, hover over any crop to reveal an ✏ button. Corrections are stored in the database and shown with a "✏ corrected" badge. They override SpeciesNet in all views.

---

## Retention policy

Two separate limits:

**Blank videos** (no animal/person): default 60 days / 20 GB
**Kept videos** (animal/person detected): default 730 days / 500 GB

The first limit hit triggers a purge, oldest first. All database records are preserved — only the video files are deleted. Configure in Settings → Retention Policy.

---

## Database schema

SQLite at `data/wildlife.db`, WAL mode. All migrations run automatically on startup.

```
videos       id, filename, filepath, camera_name, file_size_mb, duration_secs,
             recorded_at, processed_at, has_animal, has_person, kept,
             thumbnail_path, frame_count, file_purged_at,
             lens_index (0=wide, 1=telephoto), paired_video_id → videos

detections   id, video_id → videos, frame_number, timestamp_secs,
             category (animal/person/vehicle), confidence, bbox_json

species      id, detection_id → detections, label, common_name, scientific_name,
             confidence, user_common_name, user_scientific_name, corrected_at

crops        id, detection_id → detections, crop_path, quality_score,
             sharpness, brightness, contrast, pixel_area, width, height, created_at
```

---

## Troubleshooting

**Impossible species identifications (e.g. African Elephant in Utah)**
Set State/Region code in Settings (e.g. `US-UT`). Clear and reprocess any affected periods using the Re-processing section above.

**NAS won't mount**
Run `./nas_connect.sh --test`. For NFS, verify your machine's IP is in DSM → Shared Folder → NFS Permissions. For SMB, re-run `./nas_connect.sh`.

**No videos found during sync**
Check the hours window covers your camera's recording schedule. Run `./nas_connect.sh --show` to verify the source subdirectory.

**Videos won't play in dashboard**
The NAS must be mounted. The server streams video directly from the archive path. Run `./nas_connect.sh --test`.

**Web server not starting via systemd**
Check logs: `journalctl -u wildlife-monitor -n 50`. Verify paths in `wildlife-monitor.service` match your install.

**Analysis not running on schedule**
Check: `sudo systemctl status wildlife-analysis.timer`. Trigger manually: `sudo systemctl start wildlife-analysis.service`.

**Processor fails mid-run**
Local staging copies are kept. Fix the issue and re-run `./nas_sync.sh --then-process`. Already-copied and already-archived files are skipped safely.

---

## Security notes

- The web app has **no authentication**. When bound to `0.0.0.0` it is accessible to anyone on the network. For external access, use a reverse proxy (nginx, Caddy) with authentication.
- NAS credentials are stored in `~/.config/wildlife_monitor/` with `chmod 600`. Never commit this directory to version control.
- The processor only deletes files it has positively identified as having no detections. It will never delete anything in the archive folder.
