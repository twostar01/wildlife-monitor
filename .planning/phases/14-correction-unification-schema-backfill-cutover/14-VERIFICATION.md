---
phase: 14-correction-unification-schema-backfill-cutover
verified: 2026-10-06T18:00:00Z
status: human_needed
score: 6/8 must-haves verified
covered_files:
  - database.py
  - scripts/backfill_species_corrections.py
  - scripts/verify_backfill_species_corrections.py
  - scripts/verify_phase10.py
  - scripts/verify_phase12.py
  - scripts/verify_phase14.py
  - web_app.py
  - wildlife_processor.py
covered_digest: "v2:sha256:35b94748576f7cebbe8ad1721f10ab49473c8df4e2391232f3c76221bd7b25e0"
behavior_unverified: 1
overrides_applied: 0
re_verification:
  previous_status: none
gaps: []
behavior_unverified_items:
  - truth: "A --reprocess-flagged pass clears the detection's species_corrections row so a stale correction never overrides a fresh classification (D-02 / CR-01 fix)"
    test: "On a scratch DB copy, correct one detection (Gallery) and suppress another (video player), run `python wildlife_processor.py --reprocess-flagged` against a flagged video, then query species_corrections for those detection_ids."
    expected: "No species_corrections row remains for the reprocessed detections; Gallery and video player show the freshly classified label."
    why_human: "Pinned only by verify_phase14 G1, which is a source-text assertion (the DELETE appears after the UPDATE in the reprocess block). No test executes the reprocess loop, so the cleanup invariant is present and ordered correctly but never exercised."
human_verification:
  - test: "Operator accepts the retroactive record of the 2026-08-22 production migration as satisfying SC1's process clause"
    expected: "Operator states whether the 14-04-SUMMARY.md retroactive record (audit log, two snapshots, journalctl, 2026-10-06 byte-copy rehearsal, joint-run statement) is acceptable in place of a contemporaneous dry-run capture, rehearsal-before-Go, and recorded Go/No-Go."
    why_human: "Plan 14-04 required: production dry-run captured verbatim before the write, a full --apply rehearsal on a byte-copy BEFORE the Go/No-Go, and a recorded explicit operator Go. None was written down on 2026-08-22; the Go wording is unrecoverable and the byte-copy rehearsal was only run on 2026-10-06, after the write (and was a no-op). This cannot be re-created, only accepted or not. The 14-04 prohibition ('never start the irreversible write on an agent's own judgement') is judgment-tier and its compliance rests solely on the operator's after-the-fact statement; it is flagged here as an unverified-prohibition, not a silent pass."
  - test: "Confirm the commit 94a5f94 fixes (CR-01, CR-02, WR-02) are running on ubuntulaptop"
    expected: "`git -C <repo> log -1` on ubuntulaptop is 94a5f94 or later and wildlife-monitor.service was restarted after the pull."
    why_human: "14-REVIEW-DISPOSITION.md still says 'fixed (not yet deployed)'. The task brief states they are deployed. I did not touch production and cannot confirm it from the dev machine."
---

# Phase 14: Correction Unification (Schema, Backfill & Cutover) Verification Report

**Phase Goal:** A single `species_corrections` table (keyed uniquely per detection) becomes the one authoritative source for a detection's corrected species, replacing `species.user_common_name` and `video_corrections` as the system of record, with both existing write paths cut over to it and every historical correction preserved, per the operator-approved full-schema-unification decision.
**Verified:** 2026-10-06
**Status:** human_needed
**Re-verification:** No, initial verification

## Verdict

The code goal is achieved and independently re-proven. The remaining items are process-evidence and deployment-confirmation decisions for the operator. There are no FAILED truths and no blocker anti-patterns.

- `species_corrections` exists in `SCHEMA` with `UNIQUE(detection_id)`.
- Both write paths UPSERT into it.
- Every per-detection read path resolves through it.
- The legacy tables are frozen.
- The original AI label is untouched.

I did not touch production. Production claims (15-row migration, 0 FK violations, unchanged digests) come from 14-04-SUMMARY.md's account of the on-box audit log and snapshots. They are consistent with the code and harness behaviour, but I did not re-observe them.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | SC1: every historical correction is backfilled with zero data loss, using the dry-run, fixture rehearsal, production rehearsal, Go/No-Go, `--apply` pattern | ? UNCERTAIN (human decision) | **Data outcome verified.** `backfill_species_corrections.py` is dry-run by default and needs `--apply` plus `--confirm-irreversible`, `--snapshot-dir` and `--audit-log` (main(), lines 544-561). It takes a WAL-safe online snapshot and writes audit lines only after commit (606-610). It reports the unmatched row instead of dropping it. Harness `verify_backfill_species_corrections` passes plan 7/7, apply 8/8, gates 5/5. Production result, per 14-04-SUMMARY's audit-log account: 15 rows, digests identical across both passes, 0 FK violations, second pass a no-op. **Process clause not evidenced:** no contemporaneous dry-run, rehearsal-before-Go or recorded Go/No-Go exists (see Human Verification 1). The "~17,298" figure is correctly treated as the crops total, context only (report line 313). |
| 2 | SC2: both write paths write through to `species_corrections` with most-recent-write-wins, proven by an explicit both-corrections regression | ✓ VERIFIED | `correct_species()` (database.py:1162-1208) and `save_video_correction()` (1560-1624) both UPSERT `ON CONFLICT(detection_id) DO UPDATE`. My independent spot-check against a temp DB: Gallery then video left the video values on the detection; a later Gallery write on one detection left that detection on Gallery while its siblings stayed on video. `verify_phase14` precedence 4/4 and fanout 6/6 pass. |
| 3 | SC3: `species_corrections` is the sole source for the per-detection resolution/display path, with no visible regression | ✓ VERIFIED | `EFFECTIVE_COMMON/SCIENTIFIC`, `HAS_CORRECTION`, `NOT_EFFECTIVELY_UNKNOWN` and `IS_SUPPRESSED_DETECTION` read only `species_corrections` (database.py:265-304, 381, 440-441). `get_gallery`, `get_species_detail`, `get_videos` and `get_video_by_id` use them. Only the Phase-15 readers (`get_stats` 1905, `get_species_list` 1941, `get_timeline` 2420) still use `DISPLAY_COMMON`, as scoped. The Python overlay is retired from `get_video_by_id()`. Operator browser checks a-i, recorded 2026-10-06 in 14-04-SUMMARY, all pass or accepted. Spot-check: `get_gallery`, `get_video_by_id` and `get_species_detail` all returned the unified name with `has_correction=1`. |
| 4 | SC4: the original AI label survives untouched and stays readable for every corrected row | ✓ VERIFIED | No code path writes `species.label` in a correction flow. `correct_species` and `save_video_correction` touch only `species_corrections`. In my spot-check, `species` rows (label, `user_common_name`, `corrected_at`) were byte-identical before and after Gallery, video, suppress and clear operations. The backfill gates on before/after digests of `(species.id, label)` and `(video_corrections.id, original_label)` and aborts on mismatch (615-629). `verify_phase14` audit 5/5. Production digests unchanged across both passes, per the audit log summary. Operator saw "AI: Domestic Cat" on a corrected tile. |
| 5 | SC5: the Gallery-vs-video collision case has a pinned regression test in both write orders | ✓ VERIFIED | `suite_precedence` PR1-PR4 in `scripts/verify_phase14.py` (Gallery-then-video and video-then-Gallery, separate videos so they cannot contaminate each other). 4/4 pass locally. I reproduced both directions by hand. |
| 6 | D-02 (as corrected by CR-01): reprocessing clears the detection's unified correction rather than leaving a stale override | ⚠️ PRESENT_BEHAVIOR_UNVERIFIED | `wildlife_processor.py:864` runs `DELETE FROM species_corrections WHERE detection_id=?` right after the label `UPDATE`. `verify_phase14` G1 passes, but only as a source-order text assertion. Nothing executes the reprocess loop. Routed to Human Verification. |
| 7 | D-06: no correction write path touches `species.user_*` / `corrected_at` / `video_corrections` after cutover | ✓ VERIFIED (deviation noted) | Grep of every `INSERT/UPDATE/DELETE` across non-harness `.py` files: the only writers of `species_corrections` are `database.py` and the backfill script. Nothing writes `video_corrections` except the already-spent Phase 9 `backfill_dedup_videos.py` (DELETE). Production confirms the legacy tables are untouched since freeze (`video_corrections` 11 rows, `user_common_name` 4 rows) after two new corrections. **Deviation:** `wildlife_processor.py:856-858` still sets `user_common_name/user_scientific_name/corrected_at=NULL` on reprocess. It is a clear, not a correction write, and arguably helpful (it stops a stale legacy name showing in Species/Stats). It is a literal breach of "no code path writes", so I list it as a warning. |
| 8 | D-07: legacy removal is explicitly tracked as a deferred follow-up | ✓ VERIFIED (artifact warning) | Tracked in `.planning/PROJECT.md` Key Decisions row (line 154) and 14-CONTEXT.md. **However**, the dedicated todo `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` that 14-01-PLAN and 14-01-SUMMARY ("FOUND") claim does not exist on disk. PROJECT.md points at a missing file. |

**Score:** 6/8 truths verified. One is UNCERTAIN (human decision) and one is present, behavior-unverified.

### Plan-level must-haves (spot sample against code)

| Plan | Must-have | Status | Evidence |
|------|-----------|--------|----------|
| 14-01 | `UNIQUE(detection_id)` upsert, so a second `correct_species` leaves one row with the second values | ✓ | Schema line 139; my spot-check; unified 7/7 |
| 14-01 | `correct_species(unknown_id)` returns 0 and writes nothing; clearing an already-clear detection returns 1 | ✓ | database.py:1192-1196; spot-check returned `0` and `1` |
| 14-01 | Empty `corrected_common` falls through to the raw name | ✓ | `NULLIF(...,'')` in EFFECTIVE_COMMON, line 440 |
| 14-01 | Every EFFECTIVE_/HAS_CORRECTION interpolation site executes (backstop) | ✓ | Read paths ran in my spot-check; phase 10/12 harnesses exercise them (all pass) |
| 14-02 | Fan-out is a snapshot, returns 0 on no match and `None` on a missing video, and suppress is the `suppressed` column | ✓ | database.py:1596-1624; spot-check `None` / `0` / suppressed=1 rows; suppress 6/6 |
| 14-02 | Gallery-only correction now shows the badge in the video player | ✓ | `HAS_CORRECTION AS corrected` in `get_video_by_id`; operator check d |
| 14-02 | Large fan-out in one transaction (backstop) | ✓ | `executemany` in one `get_conn()` block; fanout suite passes. No explicit bound-parameter-limit test, but the INSERT is per-row parameterised and never builds an `IN (...)` list. |
| 14-03 | Idempotent apply that never clobbers a newer row | ✓ | `WHERE excluded.corrected_at > species_corrections.corrected_at` (455); apply 8/8; production pass 2 wrote 0 / skipped 15 |
| 14-03 | Three independent apply gates, checked before the snapshot | ✓ | main() 547-561; gates 5/5 (stderr output seen in my run) |
| 14-03 | Prohibition: never silently drop a correction | ✓ | Unmatched rows are listed in the report (329-333); production reported 1 unmatched row (video 31680) |
| 14-03 | Prohibition: the report never claims more than it did | ✓ | Future tense for dry-run, past for applied (314-323). Audit lines are written only after commit (606-610). Minor known flaw: re-running on a migrated DB prints "written this run: N" when 0 were written (14-04-SUMMARY item 4; one-shot script). |
| 14-04 | Production dry-run, rehearsal-before-Go and recorded Go/No-Go | ? | Not contemporaneously evidenced (Human Verification 1) |
| 14-04 | Snapshot and JSONL audit log exist on ubuntulaptop | ? (reported) | Paths and timestamps are recorded in 14-04-SUMMARY. I did not inspect the box. |
| 14-04 | Dashboard restarted, then a second `--apply` that is a no-op | ✓ (reported) | Audit log pass 2 per 14-04-SUMMARY: 0 written / 15 skipped; journalctl stop 10:37:51, start 10:37:57 |
| 14-04 | Operator browser confirmation and acknowledged intentional changes | ✓ | Checks a-i recorded 2026-10-06; h and i marked ACCEPTED |
| 14-04 | New corrections via each entry point are visible immediately | ✓ | Checks f and g; rows #33-#35 |

### Required Artifacts

| Artifact | Status | Details |
|----------|--------|---------|
| `database.py`: `species_corrections` in SCHEMA, constants, `_upsert_species_correction`, `correct_species`, fan-out `save_video_correction`, `delete_correction`, `get_corrections` | ✓ VERIFIED | Exists, substantive, wired, data flows. Read paths return real unified values. |
| `web_app.py`: `/api/species/correct`, `/api/corrections` GET/POST/DELETE | ✓ VERIFIED | Wired to `db.*`. GET lists `species_corrections` (same id space as DELETE) and DELETE 404s on a miss (CR-02 fixed). |
| `wildlife_processor.py` reprocess DELETE | ✓ present, behavior unverified | Truth 6 |
| `scripts/backfill_species_corrections.py` | ✓ VERIFIED | Full gate, snapshot, audit, digests. Refuses a nonexistent `--db` (WR-02 fixed, G5). |
| `scripts/verify_phase14.py`, `verify_backfill_species_corrections.py` | ✓ VERIFIED | All suites pass (below) |
| `scripts/verify_phase12.py`, `verify_phase10.py` revised to unified behaviour | ✓ VERIFIED | 8/8, 8/8, 10/10 and 11/11, 7/7, 3/3 pass |
| `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` | ✗ MISSING | Claimed "FOUND" in 14-01-SUMMARY; absent. Warning only (D-07 still tracked in PROJECT.md). |
| `BUILDLOG.md` Phase 14 entry; PROJECT.md updates | ✓ VERIFIED | BUILDLOG.md line 1; PROJECT.md lines 89-91, 153-154 |

### Key Link Verification

| From | To | Status | Details |
|------|----|--------|---------|
| `correct_species()` to species_corrections UPSERT to `EFFECTIVE_COMMON` to `get_gallery()` | WIRED | Spot-check: Gallery name shown with `has_correction=1` |
| `save_video_correction()` fan-out predicate to backfill fan-out predicate | WIRED | Both use `FROM detections d JOIN species s ON s.detection_id = d.id WHERE d.video_id = ? AND s.label = ?` (`_fanout_detection_ids` and `expand_video_fanout`) |
| `IS_SUPPRESSED_DETECTION` to `get_video_by_id()` | WIRED | Both detection SELECTs. Suppress hides only in the player; the Gallery still lists the crop (operator check e) |
| `HAS_CORRECTION AS corrected` to the player chip badge (frontend unchanged) | WIRED | `static/index.html` is not in the phase diff, and the field name is preserved |
| `web_app` POST `/api/corrections` to `db.save_video_correction` (`None` becomes 404, `0` becomes ok) | WIRED | web_app.py:714-730 |

### Data-Flow Trace (Level 4)

| Artifact | Data variable | Source | Real data | Status |
|----------|---------------|--------|-----------|--------|
| `get_gallery` `common_name` / `has_correction` | `EFFECTIVE_COMMON`, `HAS_CORRECTION` | correlated query on `species_corrections` | Yes (spot-check) | FLOWING |
| `get_video_by_id` `common_name` / `corrected` | same | same | Yes | FLOWING |
| `get_species_detail` crops | same | same | Yes | FLOWING |
| `get_species_list` / `get_stats` / `get_timeline` names | `DISPLAY_COMMON` | frozen `species.user_common_name` | n/a | Out of scope, Phase 15 (accepted interim) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Unified write-path behaviour, D-03 both orders, CORR-04, 404 contracts, suppress, delete | `python scratchpad/spot.py` (temp DB, real `database` functions) | All expectations met | PASS |
| `verify_phase14 --suite all` | `python scripts/verify_phase14.py --suite all` | unified 7/7, audit 5/5, fanout 6/6, suppress 6/6, precedence 4/4, gaps 5/5 | PASS |
| `verify_backfill_species_corrections --suite all` | same pattern | plan 7/7, apply 8/8, gates 5/5 (gate-refusal stderr is expected output) | PASS |
| `verify_phase12 --suite all` | same pattern | badge 8/8, ui 8/8, propagation 10/10 | PASS |
| `verify_phase10 --suite all` | same pattern | fix01 11/11, fix03 7/7, audit 3/3 | PASS |

Python 3.14.4 on the Windows dev box. The project target is 3.11, and the harnesses also pass on ubuntulaptop per 14-04-SUMMARY.

### Probe Execution

No `probe-*.sh` scripts are declared or present for this phase. Step 7c: SKIPPED. The Python harnesses above are the phase's verification instruments.

### Requirements Coverage

Requirement IDs declared across the plans: 14-01 [CORR-01, CORR-04], 14-02 [CORR-01, CORR-02, CORR-04], 14-03 [CORR-03, CORR-04], 14-04 [CORR-01..04]. REQUIREMENTS.md maps CORR-01..04 to Phase 14 and nothing else, so every ID is accounted for and none is orphaned.

| Requirement | Source plans | Description | Status | Evidence |
|-------------|--------------|-------------|--------|----------|
| CORR-01 | 14-01, 14-02, 14-04 | Single `species_corrections` (`UNIQUE(detection_id)`) is the system of record for correction resolution | ✓ SATISFIED | Schema and constants; every per-detection reader rewired; legacy frozen. Species/Stats/Timeline names still read the frozen column. That is the scoped, accepted Phase 15 boundary and does not count as a gap. |
| CORR-02 | 14-02, 14-04 | Both write paths write through with most-recent-write-wins | ✓ SATISFIED | Truth 2 and truth 5. Real-use evidence: production rows #33-#35 from both entry points. |
| CORR-03 | 14-03, 14-04 | Every existing correction backfilled with zero loss via the dry-run, rehearsal, Go/No-Go, `--apply` pattern | ? SATISFIED (data) / NEEDS HUMAN (process record) | 15 rows written, digests identical, 0 FK violations. One legacy row (video 31680) matched 0 detections and is reported, not in the table. It corrects no current detection and remains in the frozen legacy table. Accepted. See Human Verification 1. |
| CORR-04 | 14-01, 14-02, 14-03, 14-04 | The original AI label is untouched; no correction write path overwrites it | ✓ SATISFIED | Truth 4 |

REQUIREMENTS.md still shows CORR-01..04 as `[ ]` / "Pending". ROADMAP.md shows Phase 14 as `[ ]` / "Planned". Both are bookkeeping for the orchestrator to flip once the human items are resolved.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `wildlife_processor.py` | 856-858 | Reprocess nulls the frozen legacy `user_*` / `corrected_at` columns (D-06 literal breach) | Warning | Benign and pre-existing. It stops a stale legacy name surviving a reprocess. Decide whether D-06 should say "no correction write path". |
| `.planning/todos/pending/2026-08-21-legacy-correction-column-removal.md` | n/a | Claimed artifact missing; PROJECT.md references it | Warning | D-07 is still tracked in PROJECT.md, but the pointer is dangling. Recreate the todo file. |
| `scripts/backfill_dedup_videos.py` | 121-146, 565 | Blind to `species_corrections`; would hit FK violations if rerun (WR-01) | Warning (accepted, deferred) | Matters only if that script is rerun. Fix before any rerun. |
| `database.py` / readers | 1905, 1941, 2420 | Species/Stats/Timeline show the frozen name, so clearing a pre-cutover Gallery correction leaves the old name there (WR-03) | Warning (accepted, Phase 15) | Documented interim state. |
| `scripts/backfill_species_corrections.py` | 120-124 | No `id ASC` tie-breaker in the legacy-row ordering (WR-04) | Warning (deferred) | Production had 0 duplicates, so no data impact. |
| `scripts/verify_phase14.py` | G1 | The CR-01 fix is pinned by a source-text assertion, not a run of the reprocess loop | Warning | Truth 6 |
| n/a | n/a | No TBD/FIXME/XXX debt markers found in the phase's changed code that lack a reference | Info | Debt-marker gate not tripped |

Accepted and excluded from gaps per the brief: Species/Stats/Timeline grouping by original label until Phase 15; lowercase video-player corrections; the unmatched legacy row for video 31680; WR-01/WR-03/WR-04 and IN-* deferrals.

### Deferred Items

| # | Item | Addressed in | Evidence |
|---|------|--------------|----------|
| 1 | Species/Stats/Timeline names and groups keyed on the effective label | Phase 15 | ROADMAP Phase 15 SC1; LABEL-01, LABEL-02, LABEL-04 |
| 2 | Filter dropdown and predicate lockstep; zero-remaining labels disappearing | Phase 15 | ROADMAP Phase 15 SC2 and SC4 |
| 3 | Casing normalisation and sorted species dropdowns | Phase 15 (input) | 14-04-SUMMARY observations |

### Human Verification Required

#### 1. Accept the retroactive record of the production migration (CORR-03 / SC1 process clause)

**Test:** Review 14-04-SUMMARY.md's "Status of this record" and "Production run" sections. They contain the audit-log timestamps, two snapshot paths, the journalctl restart, the 2026-10-06 byte-copy rehearsal and the statement that the run was done jointly. Then decide whether that substitutes for the planned contemporaneous artifacts.
**Expected:** The operator says accept or reject. If accepted, add an `overrides:` entry to this file for truth 1 and the 14-04 process truths. The status then becomes `passed` once item 3 is also resolved.
**Why human:** The data outcome is the strong evidence and is consistent. What is missing is the process: the Go/No-Go wording, the pre-write dry-run capture and the rehearsal-before-Go ordering. They cannot be recreated and only the operator can waive them. This is also the flagged judgment-tier prohibition ("never `--apply` on an agent's own judgement"), whose only evidence is the operator's after-the-fact statement. It is an unverified-prohibition with human review recommended, not a pass.

#### 2. Confirm CR-01, CR-02 and WR-02 are deployed

**Test:** On ubuntulaptop, check that the checked-out commit is 94a5f94 or later and that the service was restarted.
**Expected:** Both are true.
**Why human:** The disposition file says "not yet deployed", the brief says deployed, and I will not touch production. Until confirmed, production may still carry the stale-correction-on-reprocess and DELETE id-space defects.

#### 3. Exercise the reprocess cleanup (behavior-unverified, truth 6)

**Test:** See `behavior_unverified_items` in the frontmatter. Alternatively, add a harness case that runs the reprocess loop against a fixture.
**Expected:** No `species_corrections` row survives for a reprocessed detection.
**Why human:** Only a text-order assertion covers it.

### Gaps Summary

There are no code gaps. The phase goal is achieved in the codebase.

The write paths, read path, schema, backfill tooling and audit-trail invariants are real, wired and behaviourally confirmed. I confirmed that by spot-check and by re-running all four harnesses green.

The status is `human_needed` rather than `passed` for three reasons:

- Plan 14-04's process evidence for an irreversible production write was reconstructed after the fact. The operator must explicitly accept or reject that substitution.
- Whether the review fixes are live in production is unconfirmed.
- The CR-01 reprocess cleanup is covered only by a source-text check.

Housekeeping before closing the phase:

- Recreate the missing D-07 todo file.
- Flip the CORR-01..04 and ROADMAP bookkeeping.
- Decide whether D-06 should read "correction write paths", given the reprocess nulling.

---

_Verified: 2026-10-06_
_Verifier: Claude (gsd-verifier)_
