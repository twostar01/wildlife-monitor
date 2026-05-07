# Wildlife Monitor — Project Guide

## What This Is

Automated wildlife detection pipeline for home security cameras. Videos are pulled nightly from a NAS, run through MegaDetector (animal detection) and SpeciesNet (species ID), and cataloged in a local FastAPI dashboard.

**Core Value:** Every animal that passes a camera gets detected, identified, and browsable — without the operator having to intervene to keep the system running.

## Stack

- **Backend:** Python 3.11, FastAPI, uvicorn, SQLite (WAL mode, no ORM)
- **Frontend:** Vanilla JS SPA — single `static/index.html`, no build step
- **ML:** MegaDetector V6 + SpeciesNet V4
- **Infrastructure:** Ubuntu 20.04+, systemd (timer + services)
- **No new dependencies** — all new features use Python stdlib or browser-native APIs

## Key Files

| File | Purpose |
|------|---------|
| `wildlife_processor.py` | Batch CLI job — frame extraction, MegaDetector, SpeciesNet, DB writes |
| `web_app.py` | FastAPI app — all REST endpoints + static file serving |
| `database.py` | All SQL — schema, migrations, `get_conn()`, query functions |
| `image_quality.py` | Crop quality scoring (sharpness, brightness, contrast) |
| `static/index.html` | Entire frontend — HTML + CSS + JS in one file |
| `nas_sync.sh` | Bash orchestration — mount NAS, sync videos, invoke processor |
| `systemd/` | Service and timer unit files |

## GSD Workflow

This project uses [Get Shit Done](https://github.com/anthropics/get-shit-done) for planning.

**Planning artifacts** (local only, not tracked in git):
- `.planning/PROJECT.md` — project context and requirements
- `.planning/ROADMAP.md` — 4-phase execution roadmap
- `.planning/REQUIREMENTS.md` — 26 v1 requirements with REQ-IDs
- `.planning/research/` — domain research (stack, features, architecture, pitfalls)
- `.planning/STATE.md` — current phase status

**Current milestone:** Stability & Feature Enhancement (4 phases)

| Phase | Name | Status |
|-------|------|--------|
| 1 | Foundation Stability | Not started |
| 2 | Run Monitoring & Failure Alerts | Not started |
| 3 | Scheduling & Gallery UX | Not started |
| 4 | Dual-Lens Sync Overhaul | Not started |

**Next step:** `/gsd-discuss-phase 1` — gather context and clarify approach for Phase 1.

## Website Publishing (BUILDLOG.md)

The repo has a GitHub Action (`wp-sync`) that watches `BUILDLOG.md`. When committed, it syncs the content to the project's WordPress page (slug: `wildlife-monitor`).

**Write to BUILDLOG.md** at meaningful milestones — phase completions, significant decisions, interesting bugs fixed. Content should be engineering narrative: the *why* behind decisions, what was tried and rejected, what was surprising. Not a changelog — more like a project devlog for a technical audience.

**Format:** Running log, newest entry at the top. Each commit replaces the entire WordPress page with the full BUILDLOG.md content, so the file accumulates all entries over time. Never truncate old entries.

Commit `BUILDLOG.md` alongside the code changes it describes.

## Development Rules

- **No build step** — frontend stays as a single HTML file; no npm, no bundler
- **No new pip dependencies** — all new features use Python stdlib or browser-native APIs
- **All DB access via `get_conn()`** — never open SQLite connections directly
- **New tables go into `SCHEMA`** — not just as `ALTER TABLE` migrations
- **Phase 1 is a mandatory gate** — the 6 STAB-* bug fixes must land before any feature work

## Known Constraints

- **Linux + systemd required** — scheduling features depend on systemd timer units
- **Python 3.11 target** — SpeciesNet has `<=3.13` constraints; 3.11 is recommended
- **Single-file frontend** — any JS changes go into `static/index.html`
- **No authentication** — local LAN only; risk accepted
