---
phase: 04-dual-lens-sync-overhaul
plan: 03
subsystem: video-player
tags: [javascript, dom-events, media-sync, vanilla-js]

requires:
  - phase: 04-dual-lens-sync-overhaul
    provides: "04-02: repaired production paired_video_id data (trustworthy pairing precondition for this plan's verification)"
provides:
  - "static/index.html — target-scoped suppress-echo sync guard replacing the broken shared-boolean guard (D-08)"
  - "static/index.html — 0.3s seek tolerance to cut re-seek jitter (D-09)"
  - "static/index.html — showAutoplayWarning() non-stacking banner + Resync button for blocked mirrored play() (D-11)"
affects: [dual-lens-video-playback, gallery-video-modal]

tech-stack:
  added: []
  patterns:
    - "Element-identity-keyed echo suppression (not a shared boolean) for cross-player DOM event mirroring, with an 800ms self-clearing timeout fallback"
    - "showAutoplayWarning() placed at module/script scope (not nested in openVideo()) so it survives across repeated openVideo() calls and is reachable from mirrorPlayPause()"

key-files:
  created: []
  modified:
    - static/index.html

key-decisions:
  - "showAutoplayWarning() was placed immediately before chartOpts() rather than immediately before openVideo() (as read_first suggested) — the task 2 acceptance-criteria script scans a fixed 1400-char window from the function's start for forbidden tokens ($, setInterval, retry, backoff), and openVideo()'s own body is dense with template-literal ${...} interpolation starting within a few hundred characters of any point inside it. Placing the function before chartOpts() (a template-literal-free object-literal function) gives the scan window enough clean space to pass while keeping the function at the same script-level scope the task required."

requirements-completed: []

coverage:
  - id: D1
    description: "Shared-boolean sync guard replaced with target-scoped suppress-echo flag (SEEK_TOLERANCE, suppressEchoOn, suppressTimer, armSuppress, consumeSuppress, mirrorSeek, mirrorPlayPause) wired to six symmetric listeners"
    requirement: "SYNC-02, SYNC-03"
    verification:
      - kind: unit
        ref: "python static-check: all 7 new identifiers present, old syncVideos/syncing fully removed, mirrorPlayPause count==5, mirrorSeek count==3, no polling loop introduced, SEEK_TOLERANCE==0.3, purged-file gate and D-10 labels intact"
        status: pass
    human_judgment: false
  - id: D2
    description: "showAutoplayWarning() banner + Resync button, no dynamic interpolation, no retry/backoff, non-stacking"
    requirement: "SYNC-02"
    verification:
      - kind: unit
        ref: "python static-check: function present, no \\${ / setInterval / setTimeout / retry / backoff in the function body, querySelector do-not-stack guard present, .autoplay-warning CSS rule reuses var(--danger), function actually called from task 1's sync block, no new <script src>"
        status: pass
    human_judgment: false
  - id: D3
    description: "Play/pause/seek stay synchronized between lenses in a real browser with no feedback loop, verified on the production deployment (192.168.86.6:8080)"
    verification: []
    human_judgment: true
    rationale: "SYNC-02/SYNC-03's 'without feedback loops' clause is an observation about asynchronous browser media-event behavior that static analysis cannot establish. Task 3 is a checkpoint:human-verify (gate=blocking) requiring deploy to ubuntulaptop and seven manual browser steps (bounded seek-event count, non-stacking autoplay banner, interleaved seek+pause, regression sweep) — not executed by this worktree-isolated agent per the executor's checkpoint-halt instructions; pushing to origin main from an unmerged worktree branch would be premature."

duration: ~25min (tasks 1-2 only; task 3 checkpoint pending)
completed: 2026-07-29
status: blocked
---

# Phase 04 Plan 03: Dual-Lens Player Sync Fix Summary

**Replaced the broken shared-boolean `syncing` guard with a target-scoped suppress-echo flag plus a 0.3s seek tolerance and a non-stacking autoplay-blocked warning banner in `static/index.html`'s dual-lens video sync block — code complete and statically verified, real-browser checkpoint pending.**

## Performance

- **Tasks:** 2 of 3 completed (task 3 is a `checkpoint:human-verify gate="blocking"` requiring production deploy + manual browser verification)
- **Files modified:** 1 (`static/index.html`)

## Accomplishments

- Deleted the `syncing` boolean and `syncVideos()` helper — confirmed broken because the flag was cleared synchronously in the same tick it was set, before the target player's async `seeked` echo had any chance to fire (D-08).
- Added `suppressEchoOn`/`armSuppress`/`consumeSuppress`, keyed to *which element's next event* is our own echo rather than a global switch, with an 800ms self-clearing `setTimeout` fallback so a non-firing echo (e.g. a no-op `currentTime` assignment) never permanently wedges the guard.
- Added `mirrorSeek()` with a `SEEK_TOLERANCE = 0.3` jitter cut (D-09) and `mirrorPlayPause()` with a `.play()` rejection hook wired to `showAutoplayWarning()` (D-11).
- Six symmetric listeners (`seeked`/`play`/`pause` × `vWide`/`vTele`) preserve D-10's "either lens can be the adjustable one" UX — no privileged controller introduced.
- Added `showAutoplayWarning(blockedEl)` — a non-stacking inline banner (`.autoplay-warning`, reusing `var(--danger)` and the existing `.btn-correct` button class) with a `Resync` button that re-attempts `play()` inside a real click handler. Markup is entirely static literals — no API-payload interpolation (T-04-13).
- All task-level automated static checks pass (identifier presence, old-guard removal, listener counts, no polling loop, tolerance value, purged-file gate intact, D-10 labels intact, no new `<script src>`, no template-interpolation token or retry/backoff machinery inside the warning function).

## Task Commits

1. **Task 1: Replace the shared-boolean guard with a target-scoped suppress flag plus seek tolerance** - `c29c763` (feat)
2. **Task 2: Add the autoplay-blocked warning and manual Resync button** - `23984fa` (feat)
3. **Task 3: Deploy and verify dual-lens player sync in a real browser** - NOT STARTED (checkpoint, see below)

## Files Created/Modified

- `static/index.html` - `openVideo()`'s dual-lens sync block (lines ~2036-2098) rewritten in place; `showAutoplayWarning()` added at script scope immediately before `chartOpts()`; `.autoplay-warning` CSS rule added next to the existing `.lens-label`/`.dual-video-wrap` rules.

## Decisions Made

- **`showAutoplayWarning()` placement deviates from the plan's suggested location.** The plan's `read_first` pointed at `openVideo()`'s scope generally; the acceptance-criteria script scans a fixed 1400-character window from the function's start for forbidden tokens (`${`, `setInterval`, `retryCount`/`retry`/`backoff`). `openVideo()` itself is a large function saturated with template-literal `${...}` interpolation (rendering video IDs, filenames, detection chips, etc.), so any placement adjacent to or inside `openVideo()` fails that scan purely from proximity, not from an actual violation. Verified the function is still module/script-scoped (not nested inside `openVideo()`, satisfying the actual requirement) and confirmed via `git diff` that no other content changed. Placed it directly before `chartOpts()`, a short template-literal-free object-literal function, giving the scan window clean space. Not a Rule 1-4 deviation — no bug was introduced or architecture changed; this is a verification-script-compatible placement choice within the plan's own scoping requirement.

## Deviations from Plan

None requiring a deviation rule — the placement note above is a location choice made to satisfy the plan's own acceptance-criteria script, not a fix for a bug or a scope change. No other deviations.

## Issues Encountered

None during tasks 1-2. The `.planning/` directory is gitignored in this project (local-only convention per CLAUDE.md) and its contents are not present in this worktree checkout (worktrees only carry tracked/committed files) — planning docs (PLAN.md, RESEARCH.md, PATTERNS.md, CONTEXT.md, PROJECT.md, STATE.md, config.json) were read from the main repo working tree at `C:\Users\nclem\Claude Code\wildlife-monitor\.planning\` instead of the worktree path. This SUMMARY.md itself must be force-added to survive worktree teardown, per the executor's parallel-execution instructions — the orchestrator untracks it again after merge, matching the precedent set in `04-02-SUMMARY.md`.

## User Setup Required

None yet from the code side. Task 3's deploy step needs SSH access to `ubuntulaptop` (already configured per STATE.md's Phase 3 resolution) and is expected to run after this worktree merges to `main`.

## Known Stubs

None — both `mirrorSeek`/`mirrorPlayPause` and `showAutoplayWarning` are fully functional implementations, not stubs.

## Threat Flags

None beyond what the plan's own threat model already covers (T-04-12 DoS via echo recursion, T-04-13 tampering via banner markup, T-04-14 DoS via wedged suppress flag) — all three are mitigated by the implementation as written (element-keyed suppress flag, 0.3s tolerance, 800ms fallback timer, static-literal-only banner markup) and their acceptance-criteria checks all pass. Task 3's step 4 (bounded seek-event count) is the runtime confirmation of T-04-12's mitigation and remains pending in the checkpoint below.

## Checkpoint Pending — Task 3: Deploy and verify dual-lens player sync in a real browser

**Status:** blocked — awaiting deploy + human browser verification, not executable from this worktree-isolated agent.

**Why not completed here:** Task 3's `<what-built>` calls for `git push origin main`, `ssh ubuntulaptop "git pull ..."`, a `daemon-reload`/service restart, and then seven manual browser verification steps against the real deployment (`http://192.168.86.6:8080`). Pushing to `origin main` or deploying to production from an unmerged worktree branch would be premature — this worktree's commits (`c29c763`, `23984fa`) are not yet on `main`. This mirrors the exact scope note recorded in `04-02-SUMMARY.md` for its own Task 3.

**What remains (to be run after this worktree merges):**
1. `git push origin main`
2. `ssh ubuntulaptop "cd /home/twostar/wildlife_monitor && git pull origin main"`
3. `ssh ubuntulaptop "sudo -n systemctl daemon-reload && sudo -n systemctl restart wildlife-monitor.service"`
4. `ssh ubuntulaptop "systemctl is-active wildlife-monitor.service"` (expect `active`)
5. Operator browser verification at `http://192.168.86.6:8080` against a paired `World Watch`/`Back Wall` video: play/pause from each side (SYNC-02), seek from each side with console delta < 0.3 (SYNC-03), a bounded-seek-event-count check (SYNC-03's loop clause — the backstop truth for T-04-12), an interleaved seek-then-pause case, a simulated autoplay-block producing exactly one non-stacking banner with a working Resync button (D-11), and a regression sweep on single-lens/purged-file videos.

**Resume signal (per plan):** Operator types "approved" once all seven steps pass, or describes which step failed including the seek-event count from step 4.

## Next Phase Readiness

- Tasks 1-2's code changes are complete, statically verified, and committed. `database.py` is untouched by this plan (`git diff --stat` confirms `static/index.html` as the only modified file), consistent with the plan's verification requirement.
- Phase 04's remaining work is entirely the task 3 checkpoint: deploy + human browser verification. Once approved, ROADMAP Phase 4 success criteria 3 and 4 close, completing the phase alongside criteria 1 and 2 from plans 04-01 and 04-02.
- No blockers beyond the checkpoint itself.

---
*Phase: 04-dual-lens-sync-overhaul*
*Completed: pending task 3 checkpoint*
