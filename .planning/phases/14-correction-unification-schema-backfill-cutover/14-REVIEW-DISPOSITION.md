---
phase: 14
review: 14-REVIEW.md
updated: 2026-10-06
---

# Phase 14 review disposition

| ID | Severity | Disposition | Notes |
|----|----------|-------------|-------|
| CR-01 | Critical | **fixed and deployed** (94a5f94 on ubuntulaptop 2026-10-06, dashboard restarted, all harnesses green there) | `wildlife_processor.py` `--reprocess-flagged` loop now deletes the detection's `species_corrections` row after rewriting its label; `database.py` D-02 comment corrected. Pinned by `verify_phase14.py --suite gaps` G1. Restores the pre-Phase-14 behaviour (reprocess cleared the Gallery correction). |
| CR-02 | Critical | **fixed and deployed** (94a5f94 on ubuntulaptop 2026-10-06, dashboard restarted, all harnesses green there) | `GET /api/corrections` now lists `species_corrections` (new `database.get_corrections`), so its ids match `DELETE /api/corrections/{id}`; `delete_correction` returns the rowcount and the endpoint 404s on a miss. G2-G4. |
| WR-01 | Warning | open, deferred | `scripts/backfill_dedup_videos.py` cannot see `species_corrections`. Matters only if that script is rerun; fix before any rerun. |
| WR-02 | Warning | **fixed and deployed** (94a5f94 on ubuntulaptop 2026-10-06, dashboard restarted, all harnesses green there) | Backfill refuses a `--db` path that does not exist instead of creating an empty database. G5. Dry-run still runs `init_db` on an existing DB (same as the dashboard does at startup). |
| WR-03 | Warning | open, Phase 15 | Clearing a pre-cutover Gallery correction leaves the old name in Species/Stats/Timeline (they read the frozen `user_common_name`). Resolved by Phase 15's reader rewrite. |
| WR-04 | Warning | open, deferred | Add `, id ASC` tie-breaker to the legacy-row ordering in the backfill. No production impact (0 duplicates found). |
| IN-01..IN-05 | Info | open, deferred | See 14-REVIEW.md. |
