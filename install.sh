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
  url="http://localhost:8792/setup?key=$key"
  echo "Opening the setup page: $url"
  echo "(if no browser opens, paste that address into one on this PC; Ctrl+C here aborts)"
  docker compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY="$key" dev python /opt/forge/setup_web.py &
  pid=$!
  for _ in $(seq 1 60); do curl -fs "http://localhost:8792/health" >/dev/null 2>&1 && break; sleep 0.5; done
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$url" || true
  elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe /c start "" "$url" >/dev/null 2>&1 || true; fi
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
echo "Reconfigure:     ./install.sh   (or edit bots/*/.env and 'docker compose up -d')"
