# Changelog

All notable changes to Wildlife Monitor are documented here.

---

## [1.0.0] — 2026-04-21

Initial release.

### Core pipeline
- MegaDetector V6 animal/person/vehicle detection (CPU and GPU)
- SpeciesNet species identification with state/province-level geographic filtering
- Image quality scoring for each animal crop (sharpness, brightness, contrast, pixel area)
- Automatic deduplication — one best-quality crop per species per video
- Configurable detection threshold and species confidence threshold

### NAS integration
- Synology NAS support via SMB or NFS
- Interactive setup wizard (`nas_connect.sh`)
- Videos copied locally for processing, then archived back to NAS
- Archive structure: `camera/year/month/day/filename.mp4`
- Auto-mount on boot via fstab

### Dashboard (web UI)
- Dashboard tab — stat cards, 7-day stacked activity chart by species, top species donut, recent detections feed
- Species tab — card grid with best crop image, detection counts, first/last seen dates
- Gallery tab — all animal crops, sortable by quality or date, filterable by species
- Videos tab — kept videos with thumbnails, filterable by camera/species/date/search
- Trends tab — detection line chart and heatmap with 30-day / 90-day / 1-year / 2-year / all-time / custom date range views; auto-aggregates by day/week/month
- Settings tab — all processing settings, NAS connection status, retention policy, storage usage, software update checker, backfill tool, manual run with live log streaming
- Mobile-responsive layout

### Cross-linking
- Dashboard activity chart — click any column to view that day's videos
- Dashboard top species donut — click any segment to open species detail
- Recent detections — species name chips open species detail modal
- Stat cards — clickable navigation to relevant tabs
- Species modal — "View all in Gallery" and "View all Videos" buttons

### Species identification
- Geographic filtering by country and state/province (e.g. `US-UT`) using SpeciesNet occurrence data
- Eliminates impossible identifications (African Elephant in Utah, European Roe Deer in North America)
- User corrections — ✏ button on any gallery crop to override SpeciesNet identification
- Corrections shown with badge and propagated to all dashboard views

### Dual-lens camera support
- Automatic pairing of fixed wide and adjustable telephoto lenses
- Naming format: `{CameraName}_{00|01}_{YYYYMMDDHHMMSS}.mp4`
- Side-by-side synchronised video player — play/pause/seek synced between lenses
- Detection chips merged from both lenses, preferring telephoto crops
- Backfill script to pair existing footage in database

### Retention policy
- Separate limits for blank videos and kept videos
- Supports both age-based (days) and storage-based (GB) limits
- First limit hit triggers purge, oldest first
- Preview Purge shows exactly what would be deleted before committing
- 7-day grace period prevents purging videos from active backfill
- DB records (detections, species, crops, timestamps) always preserved

### Backfill and date filtering
- `--date-from` / `--date-to` flags for processing any historical date range
- Backfill card in Settings UI with date pickers
- Uses filename-embedded dates (not mtime) for accurate date filtering
- Configurable filename date format with auto-detection for common DVR patterns

### Automation
- systemd service for web dashboard (`wildlife-monitor.service`) — auto-starts on boot
- systemd timer for daily analysis (`wildlife-analysis.timer`) — runs at 6:00 AM
- Persistent timer — catches up missed runs after reboot
- All settings read from `data/settings.json` — no hardcoded values in service files

### Setup
- Single-script install (`setup.sh`) — 9-step guided setup
- Automatically uses the installing user — no hardcoded usernames
- Python version detection and compatibility shim (3.10–3.12 required)
- GPU auto-detection (CUDA 11, CUDA 12, or CPU fallback)
- Systemd installation as part of setup with Y/n prompt
- NAS wizard as part of setup with Y/n prompt
