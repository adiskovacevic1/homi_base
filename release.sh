#!/usr/bin/env bash
# homi release: move `stable` forward to what is on `main`, so households get it on their next update.
# Run this from a checkout of main after you have tried the change on your own install.
#   ./release.sh         list, confirm, push
#   ./release.sh --yes   no confirmation
set -euo pipefail
cd "$(dirname "$0")"

git fetch -q origin main stable 2>/dev/null || git fetch -q origin main
if [[ -n "$(git status --porcelain)" ]]; then echo "Commit or discard your local changes first:"; git status --short; exit 1; fi
ahead=$(git log --oneline --no-decorate "origin/stable..origin/main" || true)
if [[ -z "$ahead" ]]; then echo "stable is already level with main ($(git log --oneline -1 origin/main))."; exit 0; fi

printf '\n  These would go out to every install on their next update:\n'
echo "$ahead" | sed 's/^/   /'
printf '\n  Have you run this version on your own install?\n'
if [[ "${1:-}" != "--yes" ]]; then
  read -r -p "  Type RELEASE to promote, anything else to stop: " answer
  [[ "$answer" == "RELEASE" ]] || { echo "Stopped; stable is unchanged."; exit 0; }
fi
git push origin origin/main:refs/heads/stable
printf '\nstable is now %s.\n' "$(git log --oneline -1 origin/main)"
echo "To undo: git push origin <older-commit>:refs/heads/stable --force-with-lease"
