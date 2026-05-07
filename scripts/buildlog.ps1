# buildlog — start a Claude session to discuss and draft a BUILDLOG.md entry
# Run from project root after a working session.

if (-not (Test-Path ".git")) {
    Write-Error "Not a git repository. Run this from a project root."
    exit 1
}

$project = Split-Path -Leaf (git rev-parse --show-toplevel)
$date = Get-Date -Format "yyyy-MM-dd"

$lastBuildlogLine = git log --oneline --follow -- BUILDLOG.md 2>$null | Select-Object -First 1
$lastBuildlogCommit = if ($lastBuildlogLine) { ($lastBuildlogLine -split ' ')[0] } else { $null }

if ($lastBuildlogCommit) {
    $commits = git log --oneline "${lastBuildlogCommit}..HEAD" -- . ':(exclude)BUILDLOG.md' 2>$null
    $stat    = git diff --stat "${lastBuildlogCommit}..HEAD" -- . ':(exclude)BUILDLOG.md' 2>$null
} else {
    $commits = git log --oneline -15 2>$null
    $stat    = git diff --stat HEAD~5..HEAD -- . ':(exclude)BUILDLOG.md' 2>$null
}

if (-not $commits) {
    Write-Host "No new commits since last buildlog entry. Nothing to document yet."
    exit 0
}

$prompt = @"
I'm working on the '$project' project and want to add an entry to BUILDLOG.md.

Date: $date

Commits since last entry:
$commits

Files changed:
$stat

Ask me a few focused questions to understand the key decisions, what I tried, and what someone building a similar project should know. Then draft a BUILDLOG.md entry in this format:

## $date --- [brief title]

[One or more narrative paragraphs -- conversational, practical, written for a builder trying to replicate or learn from this project. Capture the why behind decisions, not just what changed.]

Keep it readable, not a changelog. Aim for the style of a builder's project log -- what worked, what didn't, and why you made the calls you did.
"@

$tmp = [System.IO.Path]::GetTempFileName() + ".txt"
$prompt | Out-File -FilePath $tmp -Encoding utf8
try {
    Get-Content $tmp | claude
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
