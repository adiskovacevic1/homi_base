#!/usr/bin/env bash
# homi console (Linux / macOS / Git Bash): the bot's keys and settings, from this PC.
# The console runs all the time inside the dev container (http://127.0.0.1:8792/console, this PC only) and opens with the
# vault passphrase. This script just makes sure the container is up and opens the page.
#   ./console.sh            open the console
#   ./console.sh --apply    restart the containers after a change that needs it
set -euo pipefail
cd "$(dirname "$0")"
[[ -f bots/example-bot/.env ]] || { echo "No configuration on this PC yet - run ./install.sh first."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start it, then re-run."; exit 1; }

if [[ "${1:-}" == "--apply" ]]; then
  echo "Applying: $(cat .restart-needed 2>/dev/null || echo 'no pending change recorded; restarting anyway')"
  docker compose up -d
  rm -f .restart-needed
  exit 0
fi

if ! docker compose ps --services --status running 2>/dev/null | grep -qx dev; then echo "Starting the dev box (it serves the console)..."; docker compose up -d dev >/dev/null; sleep 4; fi
port=$( [[ -f .env ]] && sed -n 's/^CONSOLE_PORT=\([0-9]*\).*/\1/p' .env | head -1 || true); port=${port:-8792}
url="http://127.0.0.1:$port/console"
{ command -v pbcopy >/dev/null && printf '%s' "$url" | pbcopy; } 2>/dev/null || { command -v xclip >/dev/null && printf '%s' "$url" | xclip -selection clipboard; } 2>/dev/null || { command -v clip.exe >/dev/null && printf '%s' "$url" | clip.exe; } 2>/dev/null || true
printf '\n  ============================================================\n'
printf '   CONSOLE  (copied to your clipboard; log in with the vault passphrase)\n\n'
printf '   %s\n\n' "$url"
printf '  ============================================================\n'
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true
elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$url" >/dev/null 2>&1 || true; fi
echo "  Vault changes apply live. If the page says a restart is needed:  ./console.sh --apply"
