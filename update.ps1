# homi update (Windows PowerShell): bring this install up to the latest released version.
#   powershell -ExecutionPolicy Bypass -File .\update.ps1            update now
#   powershell -ExecutionPolicy Bypass -File .\update.ps1 -Check     show what is waiting, change nothing
# Your keys, kits, memory and settings live outside the code and are not touched.
param([switch]$Check)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$branch = git rev-parse --abbrev-ref HEAD
if ($branch -ne "stable") { Write-Host "This install follows '$branch', not 'stable'. Switch with: git checkout stable" -ForegroundColor Yellow }
docker info *> $null; if (-not $?) { Write-Host "Docker is not running. Start it, then re-run."; exit 1 }

Write-Host "Checking for a new version..."
git fetch -q origin $branch
$behind = (git rev-list --count "HEAD..origin/$branch").Trim()
if ($behind -eq "0") { Write-Host "Already up to date ($(git log --oneline -1))."; exit 0 }

Write-Host ""
Write-Host "  $behind change(s) waiting:" -ForegroundColor Green
git log --oneline --no-decorate "HEAD..origin/$branch" | ForEach-Object { Write-Host "   $_" }
Write-Host ""
if ($Check) { Write-Host "Run without -Check to apply."; exit 0 }

$dirty = git status --porcelain
if ($dirty) { Write-Host "This folder has local edits to tracked files; commit or discard them first:" -ForegroundColor Yellow; Write-Host $dirty; exit 1 }

git merge --ff-only "origin/$branch"; if (-not $?) { Write-Host "Could not fast-forward. Resolve by hand."; exit 1 }
Write-Host "Rebuilding and restarting (about half a minute of downtime)..."
docker compose up -d --build; if (-not $?) { exit 1 }
Write-Host ""
docker compose ps
Write-Host ""
Write-Host "Updated to $(git log --oneline -1)."
Write-Host "Watch it come back:  docker compose logs -f example-bot"
