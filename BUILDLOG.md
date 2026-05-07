## 2026-05-07 — Project initialized and milestone planned

Started using a proper planning workflow for the first time on this project, which has been running and evolving organically for a while now. The codebase reached a point where it works well enough to trust the output but has enough rough edges that fixing one thing breaks another — the classic "it works on my machine, in this exact order" problem.

The planning pass surfaced six silent failure modes that have been lurking in the code. The most insidious: a `NameError: log` in `database.py` that crashes any startup against a database that needs a filepath migration. It never showed up during normal operation — only when something changed in the environment. Similar story with `SETTINGS_FILE` using a global path instead of honoring `--data-dir`, meaning settings saved in the dashboard silently went to the wrong place when the service was running with a custom data directory.

The dual-lens sync situation turned out to be three separate bugs stacked on each other: a SQL `LIKE` wildcard matching `_` as any character (so `camera_01` could pair with `camera_X01`), a JavaScript `isSyncing` guard that resets synchronously and immediately lets the next `timeupdate` event fire (feedback loop), and no database migration to repair the pairs that got wrongly matched by the SQL bug. All three need to be fixed in the right order.

Planning broke into four phases: fix the six blocking bugs first (Phase 1), then run monitoring + email alerts (Phase 2), scheduling UI + gallery UX improvements (Phase 3), and the dual-lens overhaul (Phase 4). Phases 2–4 can run in parallel once Phase 1 clears. The constraint on dependencies is worth noting: the dual-lens database fix must land before the JavaScript player rewrite, and Phase 1 must land before anything else, because until those six bugs are fixed the codebase isn't safe to extend.

Key architectural constraint I locked in early: no new pip dependencies. The project already has a heavy ML stack (MegaDetector + SpeciesNet), and adding smtplib-style packages that are already in stdlib, or `<dialog>` elements that are now baseline browser-native, keeps the installation story clean. Everything new goes into Python stdlib or browser APIs.

Total scope: 26 requirements across the four phases.
