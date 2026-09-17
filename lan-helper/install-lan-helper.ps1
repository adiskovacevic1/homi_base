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

if ($Uninstall) {
  Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
  Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object { $_.CommandLine -like "*lan-helper.ps1*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
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
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\conhost.exe" -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Port $Port"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650) -StartWhenAvailable -MultipleInstances IgnoreNew
Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "The Discord bot's presence on the real LAN: wake-on-LAN, mDNS/SSDP discovery, UDP, for the containers." | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
try { $h = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$Port/health" -TimeoutSec 5; Write-Host "lan-helper is up: $($h.Content)" }
catch {
  if (Test-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "bots\example-bot\.env")) { Write-Host "registered, but the helper did not answer yet on port $Port. Check: Get-ScheduledTask '$TaskName' | Get-ScheduledTaskInfo" }
  else { Write-Host "registered; it starts answering once setup has written bots\example-bot\.env (it holds the shared token)." } }
Write-Host "Containers reach it at http://host.docker.internal:$Port with the INTERNAL_TOKEN from bots\example-bot\.env. It starts with Windows from now on."
