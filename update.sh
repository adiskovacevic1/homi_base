#!/usr/bin/env bash
# homi update (Linux / macOS / Git Bash): bring this install up to the latest released version.
#   ./update.sh           update now
#   ./update.sh --check   show what is waiting, change nothing
# Your keys, kits, memory and settings live outside the code and are not touched.
set -euo pipefail
cd "$(dirname "$0")"

branch=$(git rev-parse --abbrev-ref HEAD)
[[ "$branch" == "stable" ]] || echo "Note: this install follows '$branch', not 'stable'. Switch with: git checkout stable"
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start it, then re-run."; exit 1; }

echo "Checking for a new version..."
git fetch -q origin "$branch"
behind=$(git rev-list --count "HEAD..origin/$branch")
if [[ "$behind" == "0" ]]; then echo "Already up to date ($(git log --oneline -1))."; exit 0; fi

printf '\n  %s change(s) waiting:\n' "$behind"
git log --oneline --no-decorate "HEAD..origin/$branch" | sed 's/^/   /'
echo
if [[ "${1:-}" == "--check" ]]; then echo "Run without --check to apply."; exit 0; fi

if [[ -n "$(git status --porcelain)" ]]; then echo "This folder has local edits to tracked files; commit or discard them first:"; git status --short; exit 1; fi

git merge --ff-only "origin/$branch"
echo "Rebuilding and restarting (about half a minute of downtime)..."
docker compose up -d --build
echo
docker compose ps
echo
echo "Updated to $(git log --oneline -1)."
echo "Watch it come back:  docker compose logs -f example-bot"
