#!/usr/bin/env bash
# Uninstaller (Linux / macOS / Git Bash): stops and removes this install's containers, network, volumes and images.
# Your configuration, vault and tool kit stay unless you ask:
#   ./uninstall.sh            containers, images; keeps .env, data/, kits/
#   ./uninstall.sh --purge    also deletes .env files, data/ (the vault) and kits/
# The folder itself is left for you to delete. (Windows: uninstall.ps1, which also removes the LAN helper task.)
set -uo pipefail
cd "$(dirname "$0")"
here="$(pwd)"
project="$(basename "$here" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"     # compose names the stack after the folder
purge=0; yes=0
for a in "$@"; do case "$a" in --purge) purge=1;; --yes|-y) yes=1;; esac; done

echo "Uninstalling the bot in $here"; echo

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "Containers: stopping and removing..."
  docker compose down -v --rmi local --remove-orphans 2>&1 | grep -E 'Removed|Stopped|Error' | sed 's/^/  /' || true
  # exactly this stack's images (compose names them <folder>-<service>); a prefix match would also hit "<folder>-something"
  for img in $(docker compose config --images 2>/dev/null); do
    docker image inspect "$img" >/dev/null 2>&1 && docker rmi -f "$img" >/dev/null 2>&1 && echo "  image $img removed"
  done
  echo "Containers: gone."
else
  echo "Docker is not running; skipping containers and images (run 'docker compose down -v --rmi local' here later)."
fi
echo

mine=()
for p in .env bots/example-bot/.env bots/voice-bot/.env bots/example-bot/data bots/example-bot/kits .restart-needed; do [ -e "$p" ] && mine+=("$p"); done
if [ "$purge" = 0 ]; then
  if [ ${#mine[@]} -gt 0 ]; then
    echo "Kept (your configuration, vault and tool kit):"; printf '  %s\n' "${mine[@]}"
    echo "Delete them too with:  ./uninstall.sh --purge   (the vault and the kit cannot be recovered afterwards)"
  fi
elif [ ${#mine[@]} -gt 0 ]; then
  echo "About to delete for good:"; printf '  %s\n' "${mine[@]}"
  echo "That is the Discord token, the vault with every key, and the tool kit the bot built."
  if [ "$yes" = 0 ]; then read -r -p "Type DELETE to confirm: " ans; [ "$ans" = "DELETE" ] || { echo "Left in place."; mine=(); }; fi
  for p in "${mine[@]}"; do rm -rf "$p" && echo "  deleted $p"; done
fi
echo
echo "Done. The folder $here can be deleted now, or kept for a reinstall with ./install.sh."
