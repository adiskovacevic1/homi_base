# Installs lan-helper.ps1 as a background task that starts at logon (no window), and opens its port to Docker.
# Run once, as Administrator:   powershell -ExecutionPolicy Bypass -File .\lan-helper\install-lan-helper.ps1
# Remove with:                  ... -Uninstall
param([switch]$Uninstall, [int]$Port = 8793)
$ErrorActionPreference = "Stop"
$TaskName = "homi lan-helper"
$RuleName = "homi lan-helper ($Port)"
$Script = Join-Path $PSScriptRoot "lan-helper.ps1"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Host "Run this as Administrator (right-click PowerShell -> Run as administrator); it registers a logon task and a firewall rule."; exit 1 }

# One helper serves every install on this PC: the task's -EnvFile lists each install's bots\example-bot\.env, and the helper
# accepts any of their tokens. Installing from a second folder adds that folder; uninstalling removes only its own.
$ThisEnv = Join-Path (Split-Path -Parent $PSScriptRoot) "bots\example-bot\.env"
function Known-EnvFiles {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { return @() }
  $args = [string]$t.Actions[0].Arguments
  $list = @()
  if ($args -match '-EnvFile "([^"]+)"') { $list = $matches[1] -split ";" }
  elseif ($args -match '-File "([^"]+)\\lan-helper\\lan-helper\.ps1"') { $list = @(Join-Path $matches[1] "bots\example-bot\.env") }   # an older registration: one folder, implied
  # only folders that still exist as installs
  return @($list | ForEach-Object { $_.Trim() } | Where-Object { $_ -and (Test-Path (Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $_))) "lan-helper\lan-helper.ps1")) })
}
function Stop-Helper {
  # only the helper itself ("\lan-helper.ps1"), never this installer ("\install-lan-helper.ps1" also ends in lan-helper.ps1,
  # and matching it once made the installer kill itself right after registering the task)
  Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like "*\lan-helper.ps1*" -and $_.CommandLine -notlike "*install-lan-helper.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Register-Helper([string[]]$envFiles, [string]$scriptPath) {
  $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Port $Port -EnvFile `"$($envFiles -join ';')`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650) -StartWhenAvailable -MultipleInstances IgnoreNew
  Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "The Discord bot's presence on the real LAN: wake-on-LAN, mDNS/SSDP discovery, UDP, for the containers." | Out-Null
  Stop-Helper
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 3
  if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") { Start-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 3 }
}

if ($Uninstall) {
  $others = @(Known-EnvFiles | Where-Object { $_ -ne $ThisEnv })
  if ($others) {
    # another install on this PC still needs the helper: keep it, served from that install's copy of the script
    $otherRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $others[0]))
    Register-Helper $others (Join-Path $otherRoot "lan-helper\lan-helper.ps1")
    Write-Host "lan-helper: this folder removed from it; still running for $($others -join ', ')."; exit 0
  }
  Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
  Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Get-NetFirewallRule -DisplayName "$RuleName discovery" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Stop-Helper
  Write-Host "lan-helper removed."; exit 0
}

# Firewall: inbound on the port, private/domain profiles only (never from the internet side), for powershell.exe.
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
# Scoped to the local subnet on every profile: many home PCs have their LAN marked "Public", where profile-limited rules
# would never apply, and the local subnet is the only place these packets can legitimately come from.
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Any `
  -Program $ps -RemoteAddress LocalSubnet -Description "Docker containers reach the household LAN helper" | Out-Null
# Discovery answers arrive as UDP from each device's own address (not the multicast address the query went to), so the
# firewall does not see them as replies: allow UDP from the local subnet to the helper's PowerShell.
Get-NetFirewallRule -DisplayName "$RuleName discovery" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "$RuleName discovery" -Direction Inbound -Protocol UDP -Action Allow -Profile Any `
  -Program $ps -RemoteAddress LocalSubnet -Description "mDNS/SSDP/UDP answers from devices on the household LAN to the helper" | Out-Null
$lanRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -AddressFamily IPv4 -ErrorAction SilentlyContinue | Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
if ($lanRoute) { $prof = Get-NetConnectionProfile -InterfaceIndex $lanRoute.InterfaceIndex -ErrorAction SilentlyContinue
  if ($prof -and $prof.NetworkCategory -eq "Public") {
    Write-Host "Note: your LAN adapter '$($prof.InterfaceAlias)' is marked Public. The rules above still work, but Windows itself will not see" -ForegroundColor Yellow
    Write-Host "      printers, TVs or casting on a Public network. For a home network: Settings > Network & internet > Ethernet > Private." -ForegroundColor Yellow } }

# Logon task: PowerShell running the helper as this user with no console at all, restarted if it dies.
# "-WindowStyle Hidden" is not enough: on Windows 11 the default console host is Windows Terminal, which opens a visible
# window for the task anyway (and the owner then closes it, killing the helper). conhost --headless gives it no window.
# The env-file list keeps every other install on this PC that is still there, and adds this one.
$envFiles = @(Known-EnvFiles | Where-Object { $_ -ne $ThisEnv }) + @($ThisEnv)
if ($envFiles.Count -gt 1) { Write-Host "lan-helper: also serving $(($envFiles | Where-Object { $_ -ne $ThisEnv }) -join ', ')" }
Register-Helper $envFiles $Script
try { $h = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/health" -TimeoutSec 5; Write-Host "lan-helper is up: $($h.Content)" }
catch {
  if (Test-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "bots\example-bot\.env")) { Write-Host "registered, but the helper did not answer yet on port $Port. Check: Get-ScheduledTask '$TaskName' | Get-ScheduledTaskInfo" }
  else { Write-Host "registered; it starts answering once setup has written bots\example-bot\.env (it holds the shared token)." } }
Write-Host "Containers reach it at http://host.docker.internal:$Port with the INTERNAL_TOKEN from bots\example-bot\.env. It starts with Windows from now on."
