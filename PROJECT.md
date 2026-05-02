---
title: Wildlife Monitor
type: hardware-software
status: working
wp_page_id: 215
wp_slug: wildlife-monitor
github_url: https://github.com/twostar01/wildlife-monitor
tags: [wildlife, ai, computer-vision, nas, raspberry-pi]
---

## What It Does

Automated wildlife detection pipeline for security camera and camera trap footage. Videos are pulled from a network-attached storage (NAS), run through AI detection (MegaDetector) and species identification (SpeciesNet), and browsed through a local web dashboard. Footage with detections gets archived in an organised structure; blank footage is purged on a configurable schedule.

Supports dual-lens cameras (fixed wide + telephoto) with synchronised playback and automatic lens pairing.

## Hardware

| Component | Notes |
|-----------|-------|
| Linux machine (Ubuntu 20.04+) | Runs the detection pipeline and dashboard |
| Network-attached storage (NAS) | Accessible via NFS or SMB — stores raw and archived footage |
| Security cameras / camera traps | Any that write video to the NAS |
| GPU (optional) | NVIDIA CUDA 11/12 — ~10x faster than CPU |

## How It Works

1. `nas_sync.sh` pulls recent video from the NAS to local staging
2. MegaDetector V6 scans each frame for animals, people, and vehicles
3. SpeciesNet identifies species (2,000+ species, 65M training images) with state/province-level geo-filtering to cut impossible IDs
4. Each animal crop is scored for image quality (sharpness, brightness, contrast)
5. Kept footage is archived back to the NAS under `camera/year/month/day/`
6. Blank footage is purged on a configurable retention schedule
7. Web dashboard (FastAPI) lets you browse species, gallery crops, videos, and activity trends

Runs automatically at 6 AM daily via systemd. Dashboard starts on boot.

## Installation

See [GitHub repo](https://github.com/twostar01/wildlife-monitor) for full instructions.

Quick start:
```bash
chmod +x setup.sh nas_connect.sh nas_sync.sh
./setup.sh
./nas_connect.sh   # interactive NAS setup wizard
./nas_sync.sh --then-process --country US --admin1-region US-UT
```

## Build Log
