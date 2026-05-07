#!/usr/bin/env bash
# buildlog — start a Claude session to discuss and draft a BUILDLOG.md entry
# Run from project root after a working session.

set -e

if [ ! -d ".git" ]; then
    echo "Error: not a git repository. Run this from a project root."
    exit 1
fi

PROJECT=$(basename "$(git rev-parse --show-toplevel)")
DATE=$(date +%Y-%m-%d)

LAST_BUILDLOG_COMMIT=$(git log --oneline --follow -- BUILDLOG.md 2>/dev/null | head -1 | cut -d' ' -f1)

if [ -n "$LAST_BUILDLOG_COMMIT" ]; then
    COMMITS=$(git log --oneline "${LAST_BUILDLOG_COMMIT}..HEAD" -- . ':(exclude)BUILDLOG.md' 2>/dev/null)
    STAT=$(git diff --stat "${LAST_BUILDLOG_COMMIT}..HEAD" -- . ':(exclude)BUILDLOG.md' 2>/dev/null)
else
    COMMITS=$(git log --oneline -15 2>/dev/null)
    STAT=$(git diff --stat HEAD~5..HEAD -- . ':(exclude)BUILDLOG.md' 2>/dev/null)
fi

if [ -z "$COMMITS" ]; then
    echo "No new commits since last buildlog entry. Nothing to document yet."
    exit 0
fi

PROMPT="I'm working on the '$PROJECT' project and want to add an entry to BUILDLOG.md.

Date: $DATE

Commits since last entry:
${COMMITS}

Files changed:
${STAT}

Ask me a few focused questions to understand the key decisions, what I tried, and what someone building a similar project should know. Then draft a BUILDLOG.md entry in this format:

## ${DATE} — [brief title]

[One or more narrative paragraphs — conversational, practical, written for a builder trying to replicate or learn from this project. Capture the why behind decisions, not just what changed.]

Keep it readable, not a changelog. Aim for the style of a builder's project log — what worked, what didn't, and why you made the calls you did."

claude "$PROMPT"
