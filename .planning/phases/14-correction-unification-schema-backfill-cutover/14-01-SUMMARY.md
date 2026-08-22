---
phase: 14-correction-unification-schema-backfill-cutover
plan: 01
subsystem: database
tags: [sqlite, upsert, species-corrections, correction-unification]

requires: []
provides:
  - species_corrections table (UNIQUE(detection_id), suppressed/source/corrected_at/note columns)
  - UNIFIED_CORRECTION_COMMON/SCIENTIFIC, HAS_UNIFIED_CORRECTION, IS_SUPPRESSED_DETECTION SQL constants
  - correct_species() cut over to species_corrections via _upsert_species_correction()
  - EFFECTIVE_COMMON/EFFECTIVE_SCIENTIFIC/HAS_CORRECTION/NOT_EFFECTIVELY_UNKNOWN rewired to read species_corrections exclusively
  - scripts/verify_phase14.py with unified (7 cases) and audit (5 cases) suites
affects: [14-02, 14-03, 14-04]

actuals:
  tokens: 11045
  tasks: 3
  commits: 4

tech-stack:
  added: []
  patterns:
    - "UPSERT via INSERT ... ON CONFLICT(detection_id) DO UPDATE (add_to_blacklist()'s proven shape) for most-recent-write-wins correction precedence"
    - "Precedence resolved at WRITE time (UNIQUE(detection_id) UPSERT) rather than READ time (COALESCE chain over two tables)"

key-files:
  created:
    - scripts/verify_phase14.py
    - .planning/todos/pending/2026-08-21-legacy-correction-column-removal.md
  modified:
    - database.py
    - .planning/PROJECT.md

key-decisions:
  - "species_corrections gained a dedicated suppressed INTEGER column (planner's discretion), not RESEARCH.md's corrected_label-IS-NULL suppress encoding -- avoids the same NULL meaning two different things (Gallery no-label vs. video-player suppress sentinel)"
  - "correct_species() return value semantics changed from rowcount to existence-boolean (1 = detection exists, 0 = it doesn't) since UPSERT always affects exactly one row when the detection exists -- preserves the IN-02 404 contract"
  - "New SQL constants (UNIFIED_CORRECTION_COMMON/SCIENTIFIC, HAS_UNIFIED_CORRECTION, IS_SUPPRESSED_DETECTION) placed immediately above NOT_EFFECTIVELY_UNKNOWN, not immediately above DISPLAY_COMMON as PLAN.md's task 1 literally described -- NOT_EFFECTIVELY_UNKNOWN's rewired body interpolates HAS_UNIFIED_CORRECTION, and a module-level f-string constant can only reference a name already defined above it in the file, so DISPLAY_COMMON's placement would raise NameError at import time"

requirements-completed: [CORR-01, CORR-04]

coverage:
  - id: D1
    description: "A Gallery-popover correction travels correct_species() -> species_corrections -> EFFECTIVE_COMMON -> get_gallery()/get_species_detail() end to end, including the UNIQUE(detection_id) upsert, clear-correction path, blank-name fallback, and unknown-detection 404 contract"
    requirement: "CORR-01"
    verification:
      - kind: unit
        ref: "scripts/verify_phase14.py --suite unified (U1-U7)"
        status: pass
    human_judgment: false
  - id: D2
    description: "species.label stays byte-identical after a Gallery correction, the three frozen species columns are never written, and every species_corrections row's original AI label is still readable (CORR-04 audit trail)"
    requirement: "CORR-04"
    verification:
      - kind: unit
        ref: "scripts/verify_phase14.py --suite audit (A1-A5)"
        status: pass
    human_judgment: false

duration: ~40min
completed: 2026-08-22
status: complete
---

# Phase 14 Plan 01: Correction Unification Schema, Write-Path Cutover & Audit Trail Summary

**Unified `species_corrections` table with UPSERT-based write-time precedence; `correct_species()` cut over end-to-end, proven by a new TDD-driven `verify_phase14.py` harness (unified 7/7, audit 5/5).**

## Performance

- **Duration:** ~40 min
- **Tasks:** 3
- **Files modified:** 4 (2 tracked: `database.py`, `.planning/PROJECT.md`; 1 new tracked: `scripts/verify_phase14.py`; 1 new untracked/gitignored: the D-07 todo)

## Accomplishments

- `species_corrections` table (`UNIQUE(detection_id)`, `suppressed INTEGER NOT NULL DEFAULT 0`, `source TEXT NOT NULL`, `corrected_at TEXT NOT NULL`, plus `corrected_label`/`corrected_common`/`corrected_scientific`/`note`) added to `SCHEMA`, created idempotently by `init_db()`, no `MIGRATION_ADD_*` needed
- `UNIFIED_CORRECTION_COMMON`/`UNIFIED_CORRECTION_SCIENTIFIC`/`HAS_UNIFIED_CORRECTION`/`IS_SUPPRESSED_DETECTION` SQL constants added; `EFFECTIVE_COMMON`, `EFFECTIVE_SCIENTIFIC`, `HAS_CORRECTION`, `NOT_EFFECTIVELY_UNKNOWN` all rewired to read `species_corrections` exclusively (precedence now resolved at write time, D-03, not by a two-table COALESCE chain at read time)
- `correct_species()` rewired via a new `_upsert_species_correction()` helper (same `ON CONFLICT ... DO UPDATE` shape as `add_to_blacklist()`), preserving the IN-02 404 contract, the clear-correction path, and the blank-common/set-scientific fallback
- `scripts/verify_phase14.py` created with a real RED-then-GREEN TDD cycle: `unified` suite (U1-U7, 7/7) confirmed failing (`no such table: species_corrections`) before the schema/write-path landed, passing after; `audit` suite (A1-A5, 5/5) pins CORR-04 (audit trail) and D-06 (legacy freeze) as both fixture behaviour assertions and region-scoped source assertions
- `database.py`'s boundary comment block extended (not replaced) documenting the new sole-source-of-truth, the D-06 legacy freeze, the Phase-14-to-Phase-15 interim staleness window (RESEARCH.md Pitfall 1), the `suppressed`-column suppression signal, and D-02 (reprocess does not reapply corrections)
- `.planning/PROJECT.md` gained two new Key Decisions rows (unified-table cutover + D-03 recency rule; D-06 freeze + D-07 deferred removal); `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` created tracking D-07 with no deadline

## Task Commits

Each task was committed atomically (task 1's tracer/TDD requirement produced two commits — RED then GREEN):

1. **Task 1a: RED — failing `verify_phase14.py` unified suite** - `b278b9d` (test)
2. **Task 1b: GREEN — `species_corrections` schema + `correct_species()` cutover** - `034facb` (feat)
3. **Task 2: Pin CORR-04 audit trail and D-06 legacy freeze** - `d75352c` (test)
4. **Task 3: Document the boundary, staleness window, and D-07 deferral** - `5e0f99a` (docs)

**Plan metadata:** SUMMARY commit will follow this file (see below).

## Files Created/Modified

- `database.py` - `species_corrections` table + index in `SCHEMA`; 4 new SQL constants; `EFFECTIVE_COMMON`/`EFFECTIVE_SCIENTIFIC`/`HAS_CORRECTION`/`NOT_EFFECTIVELY_UNKNOWN` rewired; `_upsert_species_correction()` added; `correct_species()` cut over; boundary comment extended
- `scripts/verify_phase14.py` - new stdlib-only verification harness, `unified` (7 cases) + `audit` (5 cases) suites, `--suite`/`--list` CLI matching `verify_phase10.py`/`verify_phase12.py`'s established shape
- `.planning/PROJECT.md` - two new Key Decisions rows (D-03/unified-table cutover; D-06/D-07 freeze+deferral)
- `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` - new D-07 deferred-item todo (untracked; `.planning/todos/` is gitignored per project convention, same as the existing todos in that directory)

## Decisions Made

- The four new SQL constants (`UNIFIED_CORRECTION_COMMON`, `UNIFIED_CORRECTION_SCIENTIFIC`, `HAS_UNIFIED_CORRECTION`, `IS_SUPPRESSED_DETECTION`) were placed immediately above `NOT_EFFECTIVELY_UNKNOWN` rather than immediately above `DISPLAY_COMMON` as PLAN.md's task 1 literally instructed. `NOT_EFFECTIVELY_UNKNOWN`'s rewired body interpolates `HAS_UNIFIED_CORRECTION` via an f-string, and Python module-level f-string constants can only reference names already defined earlier in the file — placing them at `DISPLAY_COMMON` (which sits below `NOT_EFFECTIVELY_UNKNOWN`) would raise `NameError` at import time, failing the plan's own acceptance criterion (`python -c "import database, web_app"` exits 0). Documented in a code comment at the new constants' actual location.
- `correct_species()`'s return-value contract is preserved exactly (1 = detection exists, whether written or cleared; 0 = unknown `detection_id`) but is now computed as an existence check rather than `cur.rowcount`, since a single `UPSERT`/`DELETE` against a `UNIQUE(detection_id)` table always affects exactly one row (or zero, for a no-op `DELETE` on an already-clear correction) — the old rowcount-based contract doesn't map cleanly onto UPSERT semantics.
- `species_corrections.suppressed` (planner's discretion, carried over unchanged from PLAN.md's objective) is the suppression signal instead of RESEARCH.md's `corrected_label IS NULL` encoding, avoiding a NULL meaning two different things depending on `source`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] New SQL constants relocated to avoid a NameError**
- **Found during:** Task 1 (schema + constant rewrite)
- **Issue:** PLAN.md's task 1 instructs placing `UNIFIED_CORRECTION_COMMON`/`UNIFIED_CORRECTION_SCIENTIFIC`/`HAS_UNIFIED_CORRECTION`/`IS_SUPPRESSED_DETECTION` "immediately above the existing `DISPLAY_COMMON` constant." `DISPLAY_COMMON` is defined textually *after* `NOT_EFFECTIVELY_UNKNOWN`, but the plan's own rewrite of `NOT_EFFECTIVELY_UNKNOWN` interpolates `HAS_UNIFIED_CORRECTION` via an f-string. Following the literal instruction would define `NOT_EFFECTIVELY_UNKNOWN` before `HAS_UNIFIED_CORRECTION` exists, raising `NameError: name 'HAS_UNIFIED_CORRECTION' is not defined` on `import database`.
- **Fix:** Placed the four new constants immediately above `NOT_EFFECTIVELY_UNKNOWN` instead (i.e., right after `BLANK_LABEL_FILTER`), with a comment explaining the placement deviation and why it's required for the module to import at all.
- **Files modified:** `database.py`
- **Verification:** `python -c "import database, web_app"` exits 0; `unified`/`audit` suites both pass.
- **Committed in:** `034facb` (Task 1 GREEN commit)

---

**Total deviations:** 1 auto-fixed (1 bug — placement constraint required for correctness)
**Impact on plan:** Necessary fix; the plan's placement instruction and its own SQL-rewrite instruction were mutually inconsistent given Python's top-to-bottom name resolution. No scope creep — same four constants, same content, different line position.

## Issues Encountered

- `.planning/PROJECT.md` is a git-tracked file (force-tracked historically, per its own Context note) but still matches the blanket `.planning/` `.gitignore` rule — this git version refuses `git add` on it without `-f` even though it's already tracked. Used `git add -f .planning/PROJECT.md` for the tracked-file edit; did NOT force-add the new `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` todo, which stays untracked/gitignored per existing project convention (other pending todos in that directory are likewise untracked).
- PLAN.md's acceptance criterion for task 3 (`grep -c "request for one, not a bug" database.py` returns 1) cannot literally pass — the phrase spans two comment lines in the pre-existing (pre-Phase-14) text ("...it failing is a request for\n# one, not a bug."), so no single-line grep can match it, in the base commit or after this plan's changes. Verified the underlying intent instead: both `grep -c "it failing is a request for"` and `grep -c "one, not a bug\."` return 1, confirming the original closing sentence was preserved verbatim, not replaced. This is a pre-existing plan-acceptance-criterion quirk, not something introduced by this plan.
- `python scripts/verify_phase12_ops.py --suite all` fails on `suite_docs` with `FileNotFoundError: .planning/REQUIREMENTS.md` — pre-existing (REQUIREMENTS.md was removed from the repo in the base commit `b73b2c7 "chore: remove REQUIREMENTS.md for v1.3 milestone"`, before this plan branched), unrelated to this plan's changes. `--suite logging` (the anchor this plan cares about) passes 12/12. Out of scope per the deviation-rules scope boundary — not fixed.

## RED Window Confirmed (planned, closed by 14-02)

Per PLAN.md's `<verification>` steps 4-5, this plan intentionally leaves two harness suites partially red, closed by plan 14-02's video-player write-path cutover:

- `python scripts/verify_phase10.py --suite fix01` → **10/11**, F4 is the only failing case (the video-player write path, `save_video_correction()`, is still legacy).
- `python scripts/verify_phase12.py --suite propagation` → **5/10**, P1/P2/P5/P6/P8 fail (all video-player-correction-display cases); P3/P4/P7/P9/P10 pass (the no-regression anchors: pre-existing Gallery-popover path, uncorrected crop, `get_video_by_id()`'s Python-side overlay, suppression sentinel + blank-name guard, and the Phase-15 non-goal pin).

Both are the exact case sets PLAN.md predicted. Not "fixed" here per the plan's explicit instruction not to touch the video-player path in this plan.

## `species_corrections` Final Column List

```sql
CREATE TABLE IF NOT EXISTS species_corrections (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id         INTEGER NOT NULL UNIQUE REFERENCES detections(id),
    corrected_label      TEXT,
    corrected_common     TEXT,
    corrected_scientific TEXT,
    suppressed           INTEGER NOT NULL DEFAULT 0,
    source               TEXT NOT NULL,  -- 'gallery' | 'video_player' -- observability only, never precedence (D-03)
    corrected_at         TEXT NOT NULL,
    note                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_species_corrections_label ON species_corrections(corrected_label);
```

**`suppressed`-column discretion (one-line rationale):** a dedicated `suppressed INTEGER NOT NULL DEFAULT 0` column was added beyond RESEARCH.md's recommended DDL because RESEARCH.md's own proposed `corrected_label IS NULL`-means-suppress encoding would collide with the Gallery path's NULL `corrected_label` (Gallery never collects a formal taxonomy label), making a single NULL value mean two different things depending on `source` — a dedicated boolean removes that ambiguity structurally and matches this schema's existing boolean-flag convention (`videos.kept`, `videos.has_animal`, `needs_reprocess`).

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `species_corrections`, all four new SQL constants, and `_upsert_species_correction()` are in place and proven end-to-end for the Gallery write path — plan 14-02 (video-player fan-out cutover) and plan 14-03 (backfill script) both build directly on this.
- `IS_SUPPRESSED_DETECTION` is defined but intentionally unused by any query yet — plan 14-02 is expected to wire it into `get_video_by_id()`.
- The RED window (verify_phase10 F4, verify_phase12 P1/P2/P5/P6/P8) is the exact, expected, and only outstanding gap before plan 14-02 lands; no other regression introduced.
- No blockers for 14-02.

---
*Phase: 14-correction-unification-schema-backfill-cutover*
*Completed: 2026-08-22*

## Self-Check: PASSED

- FOUND: database.py
- FOUND: scripts/verify_phase14.py
- FOUND: .planning/PROJECT.md
- FOUND: .planning/todos/pending/2026-08-21-legacy-correction-column-removal.md
- FOUND: commit b278b9d (test: RED)
- FOUND: commit 034facb (feat: GREEN)
- FOUND: commit d75352c (test: audit suite)
- FOUND: commit 5e0f99a (docs: boundary/PROJECT.md/todo)
