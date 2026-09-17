#!/usr/bin/env bash
# homi console (Linux / macOS / Git Bash): add, view, change or delete the bot's keys and settings from this PC.
# Opens a local page; vault changes apply live, a Discord token or settings change restarts the containers when you finish.
set -euo pipefail
cd "$(dirname "$0")"
[[ -f bots/example-bot/.env ]] || { echo "No configuration on this PC yet - run ./install.sh first."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start it, then re-run."; exit 1; }
rm -f .restart-needed

key=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
url="http://127.0.0.1:8792/setup?key=$key"
docker compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY="$key" -e SETUP_MODE=console dev python /opt/forge/setup_web.py &
pid=$!
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:8792/health" >/dev/null 2>&1 && break; sleep 0.5; done
{ command -v pbcopy >/dev/null && printf '%s' "$url" | pbcopy; } 2>/dev/null || { command -v xclip >/dev/null && printf '%s' "$url" | xclip -selection clipboard; } 2>/dev/null || { command -v clip.exe >/dev/null && printf '%s' "$url" | clip.exe; } 2>/dev/null || true
printf '\n  ============================================================\n   CONSOLE  (copied to your clipboard; Ctrl+click or paste into a browser)\n\n   %s\n\n  ============================================================\n' "$url"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true
elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$url" >/dev/null 2>&1 || true; fi
echo "  This waits until you press Finish there (Ctrl+C aborts)."
wait $pid
if [[ -f .restart-needed ]]; then echo; echo "Applying: $(cat .restart-needed)"; docker compose up -d; rm -f .restart-needed
else echo; echo "Done. Vault changes are already live."; fi
