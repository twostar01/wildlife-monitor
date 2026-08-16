# Requirements: v1.3 Bug Fixes & Data Integrity

## v1 Requirements

### Bug Fixes

- [x] **FIX-01**: Species correction saves successfully when correcting from "unknown species" to a specific species (currently fails silently for that specific transition, though other corrections like cat→raccoon work)
- [x] **FIX-02**: Crop and thumbnail file paths referencing the stale `/home/nash/...` prefix are migrated to the current `/home/twostar/...` prefix, so all ~14,354 affected rows resolve to existing files
- [x] **FIX-03**: `scripts/backfill_dedup_videos.py`'s printed report says "deleted"/"removed" (not "would be deleted"/"would be removed") when actually run in `--apply` mode

### Observability & UX Decisions

- [ ] **OBS-02**: `web_app.py` gains logging calls at additional operational events beyond the 18 `print()` sites already converted, per an explicit decision on which events matter
- [ ] **UI-05**: Gallery confidence badge is suppressed or annotated on tiles whose species was human-corrected, so displayed confidence never misrepresents a corrected label

### Raw Cleanup Hardening

- [ ] **CLEANUP-04**: Raw-cleanup preview mode is reachable via `nas_sync.sh`'s own `--dry-run` flag, not only the internal `WM_RAW_CLEANUP_DRY_RUN` env var
- [ ] **CLEANUP-05**: Raw-retention misconfiguration warning also fires when raw retention is shorter than kept-video retention, not only blank-video retention

### Monitoring Closure

- [x] **NOTIFY-03**: NOTIFY-01 (partial-run failure alert) is documented as a permanent, accepted limitation — closed out of the live-verification backlog rather than left as an indefinitely open checkpoint, since no available failure-injection method reaches `wildlife_processor.py`'s error path

## Future Requirements (deferred)

- NOTIFY-02: zero-detection alert live-verification — remains a standing, no-deadline observation item; armed in production, will confirm itself whenever a qualifying quiet night occurs

## Out of Scope

- New user-facing features — this milestone is bug fixes and data-integrity cleanup only, no new capabilities
- Re-litigating the tie-break rule to also resolve the 2 groups Phase 9 deliberately left duplicated (`winner-crops-migrated`) — no operator-facing symptom, explicitly out of BACKFILL-01's scope per Phase 9

## Traceability

| REQ-ID | Phase | Status |
|--------|-------|--------|
| FIX-01 | Phase 10 | Complete |
| FIX-02 | Phase 11 | Complete |
| FIX-03 | Phase 10 | Complete |
| OBS-02 | Phase 12 | Pending |
| UI-05 | Phase 12 | Pending |
| CLEANUP-04 | Phase 13 | Pending |
| CLEANUP-05 | Phase 13 | Pending |
| NOTIFY-03 | Phase 12 | Complete |
