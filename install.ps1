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

# The LAN helper first: it is what lets the bot see TVs, speakers and the rest of the house (containers cannot on Windows).
# It needs one administrator approval; the rest of the install does not.
$helperScript = Join-Path $PSScriptRoot "lan-helper\lan-helper.ps1"
function Test-LanHelper { try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8793/health" -TimeoutSec 2 *> $null; $true } catch { $false } }
$task = Get-ScheduledTask -TaskName "homi lan-helper" -ErrorAction SilentlyContinue
if ($task -and $task.Actions[0].Arguments -notlike "*$helperScript*") {
  # Registered by an install in another folder (moved, re-cloned): point it at this one, which is the same admin step as installing.
  Write-Host "LAN helper: installed for another folder; re-registering for this one."
  $task = $null
}
if ($task) {
  if (Test-LanHelper) { Write-Host "LAN helper: already installed and running." }
  else {
    # Registered but not running: it starts at logon, so a fresh install into an existing registration finds it stopped.
    Start-ScheduledTask -TaskName "homi lan-helper" -ErrorAction SilentlyContinue
    if (-not (Test-Path "bots\example-bot\.env")) { Write-Host "LAN helper: already installed; started it (it answers once setup has written the shared token)." }
    else {
      $up = $false; for ($i = 0; $i -lt 20; $i++) { if (Test-LanHelper) { $up = $true; break }; Start-Sleep -Milliseconds 500 }
      if ($up) { Write-Host "LAN helper: already installed; started it." }
      else { Write-Host "LAN helper: installed but not answering on 8793. Check: Get-ScheduledTask 'homi lan-helper' | Get-ScheduledTaskInfo" -ForegroundColor Yellow }
    }
  }
} else {
  Write-Host "LAN helper: installing (Windows will ask for administrator approval once)..."
  try {
    $p = Start-Process -PassThru -Wait -Verb RunAs powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\lan-helper\install-lan-helper.ps1`""
    if ($p.ExitCode -eq 0 -and (Get-ScheduledTask -TaskName "homi lan-helper" -ErrorAction SilentlyContinue)) { Write-Host "LAN helper: installed and running." }
    else { Write-Host "LAN helper: not installed (exit $($p.ExitCode)). The bot works without it, just blind to the network; run lan-helper\install-lan-helper.ps1 as administrator later." -ForegroundColor Yellow }
  } catch { Write-Host "LAN helper: skipped (approval declined). The bot works without it, just blind to the network; run lan-helper\install-lan-helper.ps1 as administrator later." -ForegroundColor Yellow }
}
Write-Host ""

Write-Host "Building the three images (several minutes the first time; the voice image downloads a speech model)..."
docker compose build; if (-not $?) { exit 1 }
Write-Host ""

if ($Terminal) {
  docker compose run --rm --no-deps dev python /opt/forge/setup.py; if (-not $?) { exit 1 }
} else {
  $key = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
  $url = "http://127.0.0.1:8792/setup?key=$key"
  $p = Start-Process -PassThru -NoNewWindow docker -ArgumentList "compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY=$key dev python /opt/forge/setup_web.py"
  $up = $false
  for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8792/health" -TimeoutSec 2 *> $null; $up = $true; break } catch { Start-Sleep -Milliseconds 500 }
  }
  try { Set-Clipboard -Value $url } catch {}
  Write-Host ""
  Write-Host "  ============================================================" -ForegroundColor Green
  Write-Host "   SETUP PAGE  (copied to your clipboard; Ctrl+click or paste into a browser)" -ForegroundColor Green
  Write-Host ""
  Write-Host "   $url" -ForegroundColor Cyan
  Write-Host ""
  Write-Host "  ============================================================" -ForegroundColor Green
  if (-not $up) { Write-Host "  (the setup server is taking a while to answer; the page may need a refresh)" -ForegroundColor Yellow }
  $opened = $false
  try { Start-Process $url; $opened = $true } catch {}
  if (-not $opened) { try { Start-Process "rundll32.exe" -ArgumentList "url.dll,FileProtocolHandler $url"; $opened = $true } catch {} }
  if ($opened) { Write-Host "  Opening it in your browser now. This window waits until you save or abort there (Ctrl+C aborts)." }
  else { Write-Host "  Could not open a browser from here; paste the address above into one. This window waits (Ctrl+C aborts)." -ForegroundColor Yellow }
  Write-Host ""
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
Write-Host "Keys & settings: .\console.ps1   (change any key or setting from this PC; the vault applies live)"
Write-Host "Reconfigure:     .\install.ps1   (the whole setup again; the vault passphrase is kept)"
