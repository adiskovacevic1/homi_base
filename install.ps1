# bot-lab installer (Windows PowerShell): builds the images, opens the setup page in your browser, starts everything.
# Safe to re-run: existing configuration is kept unless you choose to replace it.
#   Right-click -> Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#   Add -Terminal to answer the questions in this window instead of a browser.
param([switch]$Terminal)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Host "Docker is not installed. Install Docker Desktop, then re-run."; exit 1 }
docker compose version *> $null; if (-not $?) { Write-Host "'docker compose' is not available; update Docker Desktop."; exit 1 }
docker info *> $null; if (-not $?) { Write-Host "Docker Desktop is installed but not running. Start it, then re-run."; exit 1 }

Write-Host "Building the three images (several minutes the first time; the voice image downloads a speech model)..."
docker compose build; if (-not $?) { exit 1 }
Write-Host ""

if ($Terminal) {
  docker compose run --rm --no-deps dev python /opt/forge/setup.py; if (-not $?) { exit 1 }
} else {
  $key = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
  $url = "http://localhost:8792/setup?key=$key"
  Write-Host "Opening the setup page: $url"
  Write-Host "(if no browser opens, paste that address into one on this PC; Ctrl+C here aborts)"
  $p = Start-Process -PassThru -NoNewWindow docker -ArgumentList "compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY=$key dev python /opt/forge/setup_web.py"
  for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing "http://localhost:8792/health" -TimeoutSec 2 *> $null; break } catch { Start-Sleep -Milliseconds 500 }
  }
  Start-Process $url
  $p.WaitForExit()
}
if (-not (Test-Path "bots\example-bot\.env")) { Write-Host "No configuration was written; nothing started."; exit 1 }
Write-Host ""
Write-Host "Starting..."
docker compose up -d; if (-not $?) { exit 1 }
Write-Host ""
docker compose ps
Write-Host ""
Write-Host "Watch the bot:   docker compose logs -f example-bot"
Write-Host "Voice bot:       docker compose logs -f voice-bot"
Write-Host "Reconfigure:     .\install.ps1   (or edit bots\*\.env and 'docker compose up -d')"
