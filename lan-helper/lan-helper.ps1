# lan-helper: the bot's presence on the real LAN.
#
# Docker Desktop on Windows runs containers inside a VM, so nothing in a container can send a broadcast, join a multicast
# group or be found by mDNS/SSDP on the household network. This small server runs natively on Windows, where the real
# network card is, and does those few things on the containers' behalf over a tiny HTTP API:
#
#   GET  /health                         who I am, my LAN addresses
#   GET  /arp                            the neighbour table: ip, mac, state (how a tool finds a MAC for wake-on-LAN)
#   POST /wake   {mac, ip?}              wake-on-LAN magic packets: broadcast, subnet broadcast, and unicast to ip if given
#   POST /ping   {ip, timeout_ms?}       one ICMP echo
#   GET  /mdns?type=_googlecast._tcp.local&timeout=3      mDNS/DNS-SD service query (Chromecast, AirPlay, Sonos, printers...)
#   GET  /ssdp?st=ssdp:all&timeout=3     SSDP M-SEARCH (LG/Samsung TVs, Roku, Plex, UPnP anything)
#   POST /udp    {ip, port, text|hex, broadcast?, wait_ms?}   send a UDP datagram, optionally collect replies (Govee LAN, etc.)
#
# Every request needs X-LAN-Token: <INTERNAL_TOKEN>, the same shared secret the bots use, read from bots/example-bot/.env.
# Containers reach it at http://host.docker.internal:8793. install-lan-helper.ps1 registers it to run at logon.
param([int]$Port = 8793, [string]$EnvFile = "")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $Root "bots\example-bot\.env" }

function Read-Token {
  if (-not (Test-Path $EnvFile)) { return "" }
  $m = Select-String -Path $EnvFile -Pattern '^INTERNAL_TOKEN=(.+)$' | Select-Object -First 1
  if ($m) { return $m.Matches[0].Groups[1].Value.Trim().Trim('"') } else { return "" }
}
$Token = Read-Token
if (-not $Token) { Write-Host "no INTERNAL_TOKEN in $EnvFile - refusing to start without a shared secret"; exit 2 }

function Log($m) { Write-Host ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $m) }
function ToJson($o) { $o | ConvertTo-Json -Depth 6 -Compress }

# ---------------------------------------------------------------- the network bits
function Get-LanAddresses {
  Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    ForEach-Object { @{ ip = $_.IPAddress; prefix = $_.PrefixLength; interface = $_.InterfaceAlias } }
}
function Get-LanPrimary {
  # The address on the interface that holds the default route: the real LAN, not WSL's vEthernet or a VPN.
  $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -AddressFamily IPv4 -ErrorAction SilentlyContinue | Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
  $ip = $null
  if ($route) { $ip = (Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress }
  if (-not $ip) { $ip = (Get-LanAddresses | Where-Object { $_.ip -like "192.168.*" -or $_.ip -like "10.*" } | Select-Object -First 1).ip }
  return $ip
}
function New-LanUdp([int]$localPort = 0) {
  # A UDP socket bound to the LAN interface, so multicast and broadcast leave through the right card.
  $lan = Get-LanPrimary
  $u = New-Object System.Net.Sockets.UdpClient
  $u.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::Socket, [Net.Sockets.SocketOptionName]::ReuseAddress, $true)
  $bindIp = if ($lan) { [System.Net.IPAddress]::Parse($lan) } else { [System.Net.IPAddress]::Any }
  $u.Client.Bind((New-Object System.Net.IPEndPoint($bindIp, $localPort)))
  if ($lan) { $u.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::IP, [Net.Sockets.SocketOptionName]::MulticastInterface, [System.Net.IPAddress]::Parse($lan).GetAddressBytes()) }
  return $u
}
function Get-Arp {
  Get-NetNeighbor -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.State -ne "Unreachable" -and $_.LinkLayerAddress -and $_.LinkLayerAddress -ne "00-00-00-00-00-00" -and $_.IPAddress -notlike "224.*" -and $_.IPAddress -notlike "239.*" -and $_.IPAddress -ne "255.255.255.255" } |
    ForEach-Object { @{ ip = $_.IPAddress; mac = ($_.LinkLayerAddress -replace "-", ":").ToLower(); state = "$($_.State)"; interface = $_.InterfaceAlias } }
}
function Send-Wake([string]$mac, [string]$ip) {
  $clean = ($mac -replace "[^0-9A-Fa-f]", "")
  if ($clean.Length -ne 12) { throw "mac must be 6 bytes like AA:BB:CC:DD:EE:FF" }
  $macBytes = 0..5 | ForEach-Object { [Convert]::ToByte($clean.Substring($_ * 2, 2), 16) }
  $packet = New-Object byte[] 102
  for ($i = 0; $i -lt 6; $i++) { $packet[$i] = 0xFF }
  for ($i = 0; $i -lt 16; $i++) { [Array]::Copy($macBytes, 0, $packet, 6 + $i * 6, 6) }
  $sent = @()
  $targets = @("255.255.255.255")
  foreach ($a in Get-LanAddresses) {                                  # each interface's subnet broadcast, one octet at a time
    if ([int]$a.prefix -ge 31) { continue }
    $ipb = [System.Net.IPAddress]::Parse($a.ip).GetAddressBytes(); $oct = @()
    for ($i = 0; $i -lt 4; $i++) {
      $bits = [Math]::Max(0, [Math]::Min(8, [int]$a.prefix - 8 * $i))       # network bits within this octet
      $maskOct = (0xFF -shl (8 - $bits)) -band 0xFF
      $oct += (([int]$ipb[$i]) -bor (0xFF -bxor $maskOct))
    }
    $targets += ($oct -join ".")
  }
  if ($ip) { $targets += $ip }
  foreach ($t in ($targets | Select-Object -Unique)) {
    foreach ($port in 9, 7) {
      try { $u = New-LanUdp; $u.EnableBroadcast = $true; $null = $u.Send($packet, $packet.Length, $t, $port); $u.Close(); $sent += "$t`:$port" } catch {}
    }
  }
  return @{ ok = $true; mac = ($clean -replace "(..)(?!$)", '$1:').ToLower(); sent_to = $sent; note = "3 bursts are usual; a device needs WoL enabled in its own settings" }
}
function Test-Ping([string]$ip, [int]$timeoutMs) {
  $p = New-Object System.Net.NetworkInformation.Ping
  try { $r = $p.Send($ip, $timeoutMs); return @{ ip = $ip; alive = ($r.Status -eq "Success"); ms = $r.RoundtripTime; status = "$($r.Status)" } }
  catch { return @{ ip = $ip; alive = $false; status = $_.Exception.Message } }
}

# --- mDNS: build a PTR query, listen for answers, parse PTR/SRV/TXT/A (with name compression)
function Encode-Name([string]$name) {
  $out = New-Object System.Collections.Generic.List[byte]
  foreach ($label in ($name.TrimEnd(".") -split "\.")) { $b = [Text.Encoding]::UTF8.GetBytes($label); $out.Add([byte]$b.Length); $out.AddRange([byte[]]$b) }
  $out.Add(0); return ,$out.ToArray()                              # the comma keeps PowerShell from unrolling the byte[]
}
function Read-Name([byte[]]$buf, [ref]$pos) {
  $labels = @(); $p = $pos.Value; $jumped = $false; $end = $p; $hops = 0
  while ($true) {
    if ($p -ge $buf.Length) { break }
    $len = $buf[$p]
    if ($len -eq 0) { $p++; break }
    if (($len -band 0xC0) -eq 0xC0) {
      $ptr = (($len -band 0x3F) -shl 8) -bor $buf[$p + 1]
      if (-not $jumped) { $end = $p + 2 }
      $p = $ptr; $jumped = $true; if (++$hops -gt 20) { break }; continue
    }
    $labels += [Text.Encoding]::UTF8.GetString($buf, $p + 1, $len); $p += 1 + $len
  }
  if (-not $jumped) { $end = $p }
  $pos.Value = $end
  return ($labels -join ".")
}
function Parse-Dns([byte[]]$buf, [string]$from) {
  $recs = @()
  try {
    $qd = ($buf[4] -shl 8) -bor $buf[5]; $an = ($buf[6] -shl 8) -bor $buf[7]; $ns = ($buf[8] -shl 8) -bor $buf[9]; $ar = ($buf[10] -shl 8) -bor $buf[11]
    $pos = 12
    for ($i = 0; $i -lt $qd; $i++) { $null = Read-Name $buf ([ref]$pos); $pos += 4 }
    for ($i = 0; $i -lt ($an + $ns + $ar); $i++) {
      $name = Read-Name $buf ([ref]$pos)
      $type = ($buf[$pos] -shl 8) -bor $buf[$pos + 1]; $rdlen = ($buf[$pos + 8] -shl 8) -bor $buf[$pos + 9]; $pos += 10
      $rd = $pos
      $rec = @{ name = $name; from = $from }
      switch ($type) {
        1  { $rec.type = "A"; $rec.ip = "$($buf[$rd]).$($buf[$rd+1]).$($buf[$rd+2]).$($buf[$rd+3])" }
        12 { $rec.type = "PTR"; $q = $rd; $rec.target = Read-Name $buf ([ref]$q) }
        16 { $rec.type = "TXT"; $t = @(); $q = $rd; while ($q -lt $rd + $rdlen) { $l = $buf[$q]; if ($l -gt 0) { $t += [Text.Encoding]::UTF8.GetString($buf, $q + 1, $l) }; $q += 1 + $l }; $rec.txt = $t }
        33 { $rec.type = "SRV"; $rec.port = ($buf[$rd + 4] -shl 8) -bor $buf[$rd + 5]; $q = $rd + 6; $rec.target = Read-Name $buf ([ref]$q) }
        default { $rec.type = "$type" }
      }
      $pos = $rd + $rdlen
      if ($rec.type -in "A", "PTR", "TXT", "SRV") { $recs += $rec }
    }
  } catch {}
  return $recs
}
function Query-Mdns([string]$type, [int]$timeoutSec) {
  $q = New-Object System.Collections.Generic.List[byte]
  $q.AddRange([byte[]](0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0)); $q.AddRange([byte[]](Encode-Name $type)); $q.AddRange([byte[]](0, 12, 0x80, 1))   # PTR, IN, unicast-response
  $lan = Get-LanPrimary
  $u = New-Object System.Net.Sockets.UdpClient
  $u.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::Socket, [Net.Sockets.SocketOptionName]::ReuseAddress, $true)
  $u.Client.Bind((New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 5353)))      # Any:5353 to receive the group; send via the LAN card
  if ($lan) { $u.Client.SetSocketOption([Net.Sockets.SocketOptionLevel]::IP, [Net.Sockets.SocketOptionName]::MulticastInterface, [System.Net.IPAddress]::Parse($lan).GetAddressBytes()) }
  try { if ($lan) { $u.JoinMulticastGroup([System.Net.IPAddress]::Parse("224.0.0.251"), [System.Net.IPAddress]::Parse($lan)) } else { $u.JoinMulticastGroup([System.Net.IPAddress]::Parse("224.0.0.251")) } } catch {}
  $u.Client.ReceiveTimeout = 500
  $pkt = $q.ToArray(); $null = $u.Send($pkt, $pkt.Length, "224.0.0.251", 5353)
  $recs = @(); $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    try { $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0); $data = $u.Receive([ref]$ep); $recs += Parse-Dns $data $ep.Address.ToString() } catch { }
  }
  $u.Close()
  # stitch PTR -> SRV/TXT/A into one entry per service instance
  $byName = @{}
  foreach ($r in $recs) { if (-not $byName[$r.name]) { $byName[$r.name] = @{ name = $r.name; from = $r.from } }; $e = $byName[$r.name]
    switch ($r.type) { "SRV" { $e.host = $r.target; $e.port = $r.port } "TXT" { $e.txt = $r.txt } "A" { $e.ip = $r.ip } "PTR" { $e.instance = $r.target } } }
  $services = @()
  foreach ($r in ($recs | Where-Object { $_.type -eq "PTR" })) {                 # ($Host is reserved in PowerShell, hence hostName)
    $inst = $byName[$r.target]; $hostName = if ($inst) { $inst.host } else { $null }; $a = if ($hostName -and $byName[$hostName]) { $byName[$hostName].ip } else { $null }
    $services += @{ instance = $r.target; type = $r.name; host = $hostName; port = $(if ($inst) { $inst.port }); ip = $(if ($a) { $a } else { $r.from }); txt = $(if ($inst) { $inst.txt }) }
  }
  return @{ type = $type; count = $services.Count; services = ($services | Sort-Object { $_.instance } -Unique); raw_records = $recs.Count }
}
function Query-Ssdp([string]$st, [int]$timeoutSec) {
  $msg = "M-SEARCH * HTTP/1.1`r`nHOST: 239.255.255.250:1900`r`nMAN: `"ssdp:discover`"`r`nMX: $([Math]::Max(1, [Math]::Min(5, $timeoutSec)))`r`nST: $st`r`n`r`n"
  $u = New-LanUdp; $u.Client.ReceiveTimeout = 500
  $b = [Text.Encoding]::ASCII.GetBytes($msg); $null = $u.Send($b, $b.Length, "239.255.255.250", 1900); Start-Sleep -Milliseconds 150; $null = $u.Send($b, $b.Length, "239.255.255.250", 1900)
  $found = @{}; $deadline = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $deadline) {
    try {
      $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0); $data = $u.Receive([ref]$ep); $text = [Text.Encoding]::ASCII.GetString($data)
      $h = @{ ip = $ep.Address.ToString() }
      foreach ($line in ($text -split "`r`n")) { if ($line -match "^([A-Za-z-]+):\s*(.*)$") { $h[$matches[1].ToUpper()] = $matches[2] } }
      $key = if ($h.USN) { $h.USN } else { "$($h.ip)|$($h.LOCATION)" }
      $found[$key] = @{ ip = $h.ip; location = $h.LOCATION; st = $h.ST; usn = $h.USN; server = $h.SERVER }
    } catch { }
  }
  $u.Close()
  return @{ st = $st; count = $found.Count; devices = @($found.Values) }
}
function Send-Udp([string]$ip, [int]$port, [string]$text, [string]$hex, [bool]$broadcast, [int]$waitMs) {
  $bytes = if ($hex) { $clean = $hex -replace "[^0-9A-Fa-f]", ""; 0..($clean.Length / 2 - 1) | ForEach-Object { [Convert]::ToByte($clean.Substring($_ * 2, 2), 16) } } else { [Text.Encoding]::UTF8.GetBytes($text) }
  $u = New-LanUdp; $u.EnableBroadcast = $broadcast; $u.Client.ReceiveTimeout = 300
  $null = $u.Send([byte[]]$bytes, $bytes.Count, $ip, $port)
  $replies = @()
  if ($waitMs -gt 0) { $deadline = (Get-Date).AddMilliseconds($waitMs)
    while ((Get-Date) -lt $deadline) { try { $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0); $d = $u.Receive([ref]$ep)
      $replies += @{ from = "$($ep.Address):$($ep.Port)"; text = [Text.Encoding]::UTF8.GetString($d); hex = [BitConverter]::ToString($d).Replace("-", "").ToLower() } } catch { } } }
  $u.Close()
  return @{ ok = $true; sent_bytes = $bytes.Count; to = "$ip`:$port"; replies = $replies }
}

# ---------------------------------------------------------------- a very small HTTP server (no URL ACL needed, unlike HttpListener)
function Send-Response($stream, [int]$code, [string]$body, [string]$ctype = "application/json") {
  $reason = @{ 200 = "OK"; 400 = "Bad Request"; 401 = "Unauthorized"; 404 = "Not Found"; 500 = "Internal Server Error" }[$code]
  $bytes = [Text.Encoding]::UTF8.GetBytes($body)
  $head = "HTTP/1.1 $code $reason`r`nContent-Type: $ctype`r`nContent-Length: $($bytes.Length)`r`nConnection: close`r`n`r`n"
  $hb = [Text.Encoding]::ASCII.GetBytes($head); $stream.Write($hb, 0, $hb.Length); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush()
}
function Handle($method, $path, $query, $headers, $body) {
  if ($path -eq "/health" -and -not $headers["x-lan-token"]) { return 200, (ToJson @{ ok = $true; helper = "lan-helper"; auth = "required" }) }
  if ($headers["x-lan-token"] -ne $Token) { return 401, (ToJson @{ error = "bad or missing X-LAN-Token" }) }
  $j = @{}; if ($body) { try { $j = $body | ConvertFrom-Json } catch { return 400, (ToJson @{ error = "body is not JSON" }) } }
  switch ("$method $path") {
    "GET /health" { $r = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -AddressFamily IPv4 -ErrorAction SilentlyContinue | Sort-Object RouteMetric, InterfaceMetric | Select-Object -First 1
                    return 200, (ToJson @{ ok = $true; helper = "lan-helper"; host = $env:COMPUTERNAME; lan_ip = (Get-LanPrimary); gateway = $(if ($r) { $r.NextHop }); addresses = @(Get-LanAddresses) }) }
    "GET /arp"    { $a = @(Get-Arp); return 200, (ToJson @{ count = $a.Count; neighbours = $a }) }
    "POST /wake"  { if (-not $j.mac) { return 400, (ToJson @{ error = "mac required" }) }; return 200, (ToJson (Send-Wake $j.mac $j.ip)) }
    "POST /ping"  { if (-not $j.ip) { return 400, (ToJson @{ error = "ip required" }) }; $t = if ($j.timeout_ms) { [int]$j.timeout_ms } else { 1500 }; return 200, (ToJson (Test-Ping $j.ip $t)) }
    "GET /mdns"   { $t = if ($query["type"]) { $query["type"] } else { "_services._dns-sd._udp.local" }; $s = if ($query["timeout"]) { [int]$query["timeout"] } else { 3 }; return 200, (ToJson (Query-Mdns $t ([Math]::Min(10, $s)))) }
    "GET /ssdp"   { $st = if ($query["st"]) { $query["st"] } else { "ssdp:all" }; $s = if ($query["timeout"]) { [int]$query["timeout"] } else { 3 }; return 200, (ToJson (Query-Ssdp $st ([Math]::Min(10, $s)))) }
    "POST /udp"   { if (-not $j.ip -or -not $j.port) { return 400, (ToJson @{ error = "ip and port required" }) }
                    $w = if ($j.wait_ms) { [Math]::Min(5000, [int]$j.wait_ms) } else { 0 }
                    return 200, (ToJson (Send-Udp $j.ip ([int]$j.port) ([string]$j.text) ([string]$j.hex) ([bool]$j.broadcast) $w)) }
    default       { return 404, (ToJson @{ error = "no such route"; routes = @("GET /health", "GET /arp", "POST /wake", "POST /ping", "GET /mdns", "GET /ssdp", "POST /udp") }) }
  }
}

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
$listener.Start()
Log "lan-helper listening on :$Port (token from $EnvFile); addresses: $((Get-LanAddresses | ForEach-Object { $_.ip }) -join ', ')"
while ($true) {
  $client = $null
  try {
    $client = $listener.AcceptTcpClient(); $client.ReceiveTimeout = 5000
    $stream = $client.GetStream(); $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $false, 65536, $true)
    $reqLine = $reader.ReadLine(); if (-not $reqLine) { $client.Close(); continue }
    $parts = $reqLine -split " "; $method = $parts[0]; $target = $parts[1]
    $headers = @{}; while (($line = $reader.ReadLine()) -and $line -ne "") { if ($line -match "^([^:]+):\s*(.*)$") { $headers[$matches[1].ToLower()] = $matches[2] } }
    $body = ""; if ($headers["content-length"]) { $len = [int]$headers["content-length"]; $buf = New-Object char[] $len; $read = 0; while ($read -lt $len) { $n = $reader.Read($buf, $read, $len - $read); if ($n -le 0) { break }; $read += $n }; $body = -join $buf[0..($read - 1)] }
    $path, $qs = $target -split "\?", 2; $query = @{}
    if ($qs) { foreach ($kv in ($qs -split "&")) { $k, $v = $kv -split "=", 2; $query[[Uri]::UnescapeDataString($k)] = [Uri]::UnescapeDataString("$v") } }
    $code, $out = Handle $method $path $query $headers $body
    Send-Response $stream $code $out
    if ($path -ne "/health") { Log "$method $path -> $code" }
  } catch {
    Log "request failed: $($_.Exception.Message)"
    try { if ($client -and $client.Connected) { Send-Response $client.GetStream() 500 (ToJson @{ error = $_.Exception.Message }) } } catch {}
  } finally { if ($client) { $client.Close() } }
}
