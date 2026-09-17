# Uninstaller (Windows PowerShell): stops and removes this install's containers, network, volumes and images, and the
# LAN helper if it was registered for this folder. Your configuration, vault and tool kit stay unless you ask:
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1            containers, images, helper; keeps .env, data/, kits/
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge     also deletes .env files, data/ (the vault) and kits/
# The folder itself is left for you to delete. Re-running install.ps1 afterwards is a fresh install (or, without -Purge,
# the same bot again: same vault, same kit, same token).
param([switch]$Purge, [switch]$Yes)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$here = $PSScriptRoot
$project = (Split-Path -Leaf $here).ToLower() -replace '[^a-z0-9_-]', ''      # compose names the stack after the folder

Write-Host "Uninstalling the bot in $here"
Write-Host ""

# 1. containers, network, the dev box's home volume, and the images built for this folder
$dockerUp = $false
if (Get-Command docker -ErrorAction SilentlyContinue) { docker info *> $null; $dockerUp = $? }
if ($dockerUp) {
  $running = @(docker compose ps -q 2>$null)
  Write-Host "Containers: stopping and removing ($($running.Count) running)..."
  # compose reports progress on stderr, which PowerShell would treat as an error here; cmd merges the streams for us
  cmd /c "docker compose down -v --rmi local --remove-orphans 2>&1" | Where-Object { $_ -match 'Removed|Stopped|Error' } | ForEach-Object { "  $($_.Trim())" }
  # exactly this stack's images (compose names them <folder>-<service>); a prefix match would also hit "<folder>-something"
  $mine = @(cmd /c "docker compose config --images 2>nul" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $have = @(docker images --format '{{.Repository}}')
  foreach ($img in $mine) { if ($img -in $have) { docker rmi -f $img *> $null; Write-Host "  image $img removed" } }
  Write-Host "Containers: gone."
} else {
  Write-Host "Docker is not running; skipping containers and images (run 'docker compose down -v --rmi local' here later)." -ForegroundColor Yellow
}
Write-Host ""

# 2. the LAN helper, only if its task points at this folder (another install may own it)
$task = Get-ScheduledTask -TaskName "homi lan-helper" -ErrorAction SilentlyContinue
if ($task -and $task.Actions[0].Arguments -like "*$here\lan-helper\lan-helper.ps1*") {
  Write-Host "LAN helper: registered for this folder; removing (Windows will ask for administrator approval once)..."
  try {
    $p = Start-Process -PassThru -Wait -Verb RunAs powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$here\lan-helper\install-lan-helper.ps1`" -Uninstall"
    if ($p.ExitCode -eq 0) { Write-Host "LAN helper: removed (task, firewall rules, process)." }
    else { Write-Host "LAN helper: removal returned exit $($p.ExitCode); run lan-helper\install-lan-helper.ps1 -Uninstall as administrator." -ForegroundColor Yellow }
  } catch { Write-Host "LAN helper: approval declined; run lan-helper\install-lan-helper.ps1 -Uninstall as administrator to remove it." -ForegroundColor Yellow }
} elseif ($task) {
  Write-Host "LAN helper: registered for another folder ($($task.Actions[0].Arguments -replace '.*-File "([^"]+)".*', '$1')); left alone."
} else {
  Write-Host "LAN helper: not installed."
}
Write-Host ""

# 3. what belongs to the household: keep by default
$mine = @(".env", "bots\example-bot\.env", "bots\voice-bot\.env", "bots\example-bot\data", "bots\example-bot\kits", ".restart-needed") | Where-Object { Test-Path $_ }
if (-not $Purge) {
  if ($mine) {
    Write-Host "Kept (your configuration, vault and tool kit):"
    $mine | ForEach-Object { Write-Host "  $_" }
    Write-Host "Delete them too with:  .\uninstall.ps1 -Purge   (the vault and the kit cannot be recovered afterwards)"
  }
} elseif ($mine) {
  Write-Host "About to delete for good:" -ForegroundColor Yellow
  $mine | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
  Write-Host "That is the Discord token, the vault with every key, and the tool kit the bot built." -ForegroundColor Yellow
  if (-not $Yes) { $ans = Read-Host "Type DELETE to confirm"; if ($ans -ne "DELETE") { Write-Host "Left in place."; $mine = @() } }
  foreach ($p in $mine) { Remove-Item -Recurse -Force $p; Write-Host "  deleted $p" }
}
Write-Host ""
Write-Host "Done. The folder $here can be deleted now, or kept for a reinstall with install.ps1."
