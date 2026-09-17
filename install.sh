#!/usr/bin/env bash
# bot-lab installer (Linux / macOS / Git Bash): builds the images, opens the setup page in your browser, starts everything.
# Safe to re-run: existing configuration is kept unless you choose to replace it.
#   ./install.sh              setup in the browser (http://localhost:8792)
#   ./install.sh --terminal   setup by answering questions in this terminal instead
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || { echo "Docker is not installed. Install Docker Desktop (Windows/macOS) or Docker Engine (Linux), then re-run."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "'docker compose' is not available; update Docker."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start it, then re-run."; exit 1; }

echo "Building the three images (several minutes the first time; the voice image downloads a speech model)..."
docker compose build
echo

if [[ "${1:-}" == "--terminal" ]]; then
  docker compose run --rm --no-deps dev python /opt/forge/setup.py
else
  key=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  url="http://127.0.0.1:8792/setup?key=$key"
  docker compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY="$key" dev python /opt/forge/setup_web.py &
  pid=$!
  for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:8792/health" >/dev/null 2>&1 && break; sleep 0.5; done
  { command -v pbcopy >/dev/null && printf '%s' "$url" | pbcopy; } 2>/dev/null || { command -v xclip >/dev/null && printf '%s' "$url" | xclip -selection clipboard; } 2>/dev/null || { command -v clip.exe >/dev/null && printf '%s' "$url" | clip.exe; } 2>/dev/null || true
  printf '\n  ============================================================\n'
  printf '   SETUP PAGE  (copied to your clipboard; Ctrl+click or paste into a browser)\n\n'
  printf '   %s\n\n' "$url"
  printf '  ============================================================\n'
  opened=0
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 && opened=1
  elif command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 && opened=1
  elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$url" >/dev/null 2>&1 && opened=1; fi
  if [[ $opened == 1 ]]; then echo "  Opening it in your browser now. This waits until you save or abort there (Ctrl+C aborts)."
  else echo "  Could not open a browser from here; paste the address above into one. This waits (Ctrl+C aborts)."; fi
  echo
  wait $pid
fi
[[ -f bots/example-bot/.env ]] || { echo "No configuration was written; nothing started."; exit 1; }
echo
echo "Starting..."
docker compose up -d
echo
docker compose ps
echo
echo "Watch the bot:   docker compose logs -f example-bot"
echo "Voice bot:       docker compose logs -f voice-bot"
echo "Keys & settings: ./console.sh   (change any key or setting from this PC; the vault applies live)"
echo "Reconfigure:     ./install.sh   (the whole setup again; the vault passphrase is kept)"
