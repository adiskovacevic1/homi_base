# homi console (Windows PowerShell): add, view, change or delete the bot's keys and settings from this PC.
# Opens a local page; changes to the vault apply live, a Discord token or settings change restarts the containers when you finish.
#   powershell -ExecutionPolicy Bypass -File .\console.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "bots\example-bot\.env")) { Write-Host "No configuration on this PC yet - run install.ps1 first."; exit 1 }
docker info *> $null; if (-not $?) { Write-Host "Docker Desktop is not running. Start it, then re-run."; exit 1 }
Remove-Item ".restart-needed" -ErrorAction SilentlyContinue

$key = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ })
$url = "http://127.0.0.1:8792/setup?key=$key"
$p = Start-Process -PassThru -NoNewWindow docker -ArgumentList "compose run --rm --no-deps -p 127.0.0.1:8792:8792 -e SETUP_KEY=$key -e SETUP_MODE=console dev python /opt/forge/setup_web.py"
for ($i = 0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8792/health" -TimeoutSec 2 *> $null; break } catch { Start-Sleep -Milliseconds 500 } }
try { Set-Clipboard -Value $url } catch {}
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   CONSOLE  (copied to your clipboard; Ctrl+click or paste into a browser)" -ForegroundColor Green
Write-Host ""
Write-Host "   $url" -ForegroundColor Cyan
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
$opened = $false
try { Start-Process $url; $opened = $true } catch {}
if (-not $opened) { try { Start-Process "rundll32.exe" -ArgumentList "url.dll,FileProtocolHandler $url"; $opened = $true } catch {} }
Write-Host $(if ($opened) { "  Opening it in your browser. This window waits until you press Finish there (Ctrl+C aborts)." } else { "  Could not open a browser; paste the address above into one. This window waits (Ctrl+C aborts)." })
$p.WaitForExit()
if (Test-Path ".restart-needed") {
  Write-Host ""; Write-Host "Applying: $(Get-Content '.restart-needed' -Raw)".Trim()
  docker compose up -d
  Remove-Item ".restart-needed" -ErrorAction SilentlyContinue
} else { Write-Host ""; Write-Host "Done. Vault changes are already live." }
