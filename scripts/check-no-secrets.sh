#!/usr/bin/env bash
# Fail if anything that belongs to a household has been committed. A key or a kit in the history is not fixable by a
# follow-up commit - it means deleting the repo - so this runs on every push and before every release.
# Local use: ./scripts/check-no-secrets.sh   (checks the tracked files of the current checkout)
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
say() { echo "  $*"; }

# --- files that must never be tracked, whatever they contain
paths=$(git ls-files | grep -E '(^|/)\.env$|(^|/)[^/]+\.env$|(^|/)vault\.enc|(^|/)kits/|(^|/)data/|\.env\.bak-' | grep -vE '\.env\.example$' || true)
if [[ -n "$paths" ]]; then
  echo "FAIL: files that belong to an install are tracked:"; echo "$paths" | sed 's/^/  /'; fail=1
fi

# --- values that look like real credentials. Patterns match the value, not the variable name, so code that writes
#     "VAULT_PASSPHRASE=..." or an .env.example with an empty key is fine.
declare -A patterns=(
  ["Anthropic key"]='sk-ant-[A-Za-z0-9_-]{30,}'
  ["OpenAI key"]='sk-(proj-)?[A-Za-z0-9]{40,}'
  ["Discord bot token"]='[MNO][A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}'
  ["ElevenLabs key"]='sk_[a-f0-9]{40,}'
  ["xAI key"]='xai-[A-Za-z0-9]{40,}'
  ["private key block"]='-----BEGIN [A-Z ]*PRIVATE KEY-----'
  ["filled passphrase"]='VAULT_(PASSPHRASE|KEY)=[^[:space:]$"'"'"'{]{8,}'
)
for name in "${!patterns[@]}"; do
  hits=$(git grep -nIE "${patterns[$name]}" -- . ':!scripts/check-no-secrets.sh' 2>/dev/null || true)
  if [[ -n "$hits" ]]; then
    echo "FAIL: something shaped like a $name is committed:"
    echo "$hits" | sed -E 's/(sk-ant-|sk_|xai-)[A-Za-z0-9_-]+/\1<redacted>/g; s/^/  /' | head -5
    fail=1
  fi
done

if [[ $fail -eq 0 ]]; then say "no secrets, kits or install data are tracked"; fi
exit $fail
