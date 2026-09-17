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

# LAN discovery: on Linux the containers share the host network, so no helper is needed. On Windows use install.ps1,
# which installs the LAN helper first.
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) echo "On Windows run install.ps1 instead (it also installs the LAN helper): powershell -ExecutionPolicy Bypass -File .\\install.ps1"; exit 1;; esac

echo "Building the three images (several minutes the first time; the voice image downloads a speech model)..."
docker compose build
echo

# The console's port on this machine: what this install already uses (root .env), else 8792, else the next free one, so
# a second bot in another folder does not fight the first over the port. Setup records the choice in the root .env.
console_port=$( [[ -f .env ]] && sed -n 's/^CONSOLE_PORT=\([0-9]*\).*/\1/p' .env | head -1 || true)
if [[ -z "$console_port" ]]; then
  console_port=8792
  while (echo >"/dev/tcp/127.0.0.1/$console_port") 2>/dev/null; do console_port=$((console_port + 10)); done   # 8802, 8812, ... (8793 is the LAN helper)
  [[ "$console_port" != 8792 ]] && echo "Console port: 8792 is taken (another bot on this machine?); this install uses $console_port."
fi

if [[ "${1:-}" == "--terminal" ]]; then
  docker compose run --rm --no-deps -e CONSOLE_PORT="$console_port" dev python /opt/forge/setup.py
else
  key=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 24)
  url="http://127.0.0.1:$console_port/setup?key=$key"
  docker compose run --rm --no-deps -p "127.0.0.1:$console_port:8792" -e SETUP_KEY="$key" -e CONSOLE_PORT="$console_port" dev python /opt/forge/setup_web.py &
  pid=$!
  for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$console_port/health" >/dev/null 2>&1 && break; sleep 0.5; done
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
echo "Update:          ./update.sh    (fetches the latest released version and rebuilds)"
