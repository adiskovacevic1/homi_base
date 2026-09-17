"""lan: the household network as seen from the PC itself, via the lan-helper service running on Windows.

Containers sit behind Docker's NAT and cannot broadcast, multicast, or be found by mDNS/SSDP. lan-helper runs natively
on the host and does those things on request: neighbour table (MAC lookup), wake-on-LAN, ping, mDNS/DNS-SD service
discovery, SSDP discovery, raw UDP send/receive. Authenticated with INTERNAL_TOKEN (declared secret).
"""
import os
import httpx

HELPER = os.environ.get("LAN_HELPER_URL", "http://host.docker.internal:8793").rstrip("/")


def _call(method, path, **kw):
    token = os.environ.get("INTERNAL_TOKEN")
    if not token:
        raise RuntimeError("INTERNAL_TOKEN is not available to this tool")
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.request(method, HELPER + path, headers={"X-LAN-Token": token}, **kw)
    except httpx.HTTPError as e:
        raise RuntimeError(f"lan-helper is not reachable at {HELPER} ({type(e).__name__}). It runs on the Windows host; "
                           "the owner installs it once with lan-helper\\install-lan-helper.ps1 (as administrator).")
    if r.status_code == 401:
        raise PermissionError("lan-helper rejected the token; the bot's INTERNAL_TOKEN and the helper's must match (restart the helper after changing .env)")
    if r.status_code >= 400:
        raise RuntimeError(f"lan-helper answered {r.status_code}: {r.text[:300]}")
    return r.json()


def run(action="arp", mac=None, ip=None, service_type=None, st=None, port=None, text=None, hex=None, broadcast=False, wait_ms=0, timeout=3):
    action = (action or "arp").lower()
    if action == "health":
        return _call("GET", "/health")
    if action == "arp":
        out = _call("GET", "/arp")
        if mac:
            m = mac.lower().replace("-", ":")
            out["neighbours"] = [n for n in out["neighbours"] if n["mac"] == m]
        if ip:
            out["neighbours"] = [n for n in out["neighbours"] if n["ip"] == ip]
        out["count"] = len(out["neighbours"])
        return out
    if action == "wake":
        if not mac:
            if not ip:
                raise ValueError("wake needs mac, or ip (looked up in the neighbour table)")
            hit = [n for n in _call("GET", "/arp")["neighbours"] if n["ip"] == ip]
            if not hit:
                raise ValueError(f"no MAC known for {ip}; the device must have been seen on the LAN recently (ping it while it is on, then save its MAC)")
            mac = hit[0]["mac"]
        return _call("POST", "/wake", json={"mac": mac, "ip": ip})
    if action == "ping":
        if not ip:
            raise ValueError("ping needs ip")
        return _call("POST", "/ping", json={"ip": ip, "timeout_ms": int(timeout * 1000)})
    if action == "mdns":
        return _call("GET", "/mdns", params={"type": service_type or "_services._dns-sd._udp.local", "timeout": int(timeout)})
    if action == "ssdp":
        return _call("GET", "/ssdp", params={"st": st or "ssdp:all", "timeout": int(timeout)})
    if action == "udp":
        if not ip or not port or (text is None and not hex):
            raise ValueError("udp needs ip, port and text or hex")
        return _call("POST", "/udp", json={"ip": ip, "port": int(port), "text": text, "hex": hex, "broadcast": bool(broadcast), "wait_ms": int(wait_ms)})
    raise ValueError("action must be health, arp, wake, ping, mdns, ssdp or udp")
