# homi console (Windows PowerShell): the bot's keys and settings, from this PC.
# The console runs all the time inside the dev container (http://127.0.0.1:8792/console, this PC only) and opens with the
# vault passphrase. This script just makes sure the container is up and opens the page.
#   powershell -ExecutionPolicy Bypass -File .\console.ps1            open the console
#   powershell -ExecutionPolicy Bypass -File .\console.ps1 -Apply     restart the containers after a change that needs it
param([switch]$Apply)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "bots\example-bot\.env")) { Write-Host "No configuration on this PC yet - run install.ps1 first."; exit 1 }
docker info *> $null; if (-not $?) { Write-Host "Docker Desktop is not running. Start it, then re-run."; exit 1 }

if ($Apply) {
  Write-Host "Applying: $(if (Test-Path '.restart-needed') { (Get-Content '.restart-needed' -Raw).Trim() } else { 'no pending change recorded; restarting anyway' })"
  docker compose up -d
  Remove-Item ".restart-needed" -ErrorAction SilentlyContinue
  exit 0
}

$state = docker compose ps --services --status running 2>$null
if ($state -notcontains "dev") { Write-Host "Starting the dev box (it serves the console)..."; docker compose up -d dev | Out-Null; Start-Sleep -Seconds 4 }
$port = 8792
if (Test-Path ".env") { $m = Select-String -Path ".env" -Pattern '^CONSOLE_PORT=(\d+)' | Select-Object -First 1; if ($m) { $port = [int]$m.Matches[0].Groups[1].Value } }
$url = "http://127.0.0.1:$port/console"
try { Set-Clipboard -Value $url } catch {}
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   CONSOLE  (copied to your clipboard; log in with the vault passphrase)" -ForegroundColor Green
Write-Host ""
Write-Host "   $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
try { Start-Process $url } catch { try { Start-Process "rundll32.exe" -ArgumentList "url.dll,FileProtocolHandler $url" } catch { Write-Host "  Could not open a browser; paste the address above into one." -ForegroundColor Yellow } }
Write-Host "  Vault changes apply live. If the page says a restart is needed:  .\console.ps1 -Apply"
