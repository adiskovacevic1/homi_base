import json, re, urllib.parse, httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

def _extract(obj, out):
    if isinstance(obj, dict):
        if "videoRenderer" in obj:
            v = obj["videoRenderer"]
            try:
                title = v["title"]["runs"][0]["text"]
            except Exception:
                title = (v.get("title", {}).get("simpleText") or "")
            vid = v.get("videoId")
            if vid:
                out.append({
                    "title": title,
                    "videoId": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "channel": (v.get("ownerText", {}).get("runs") or [{}])[0].get("text"),
                    "duration": v.get("lengthText", {}).get("simpleText"),
                    "views": v.get("viewCountText", {}).get("simpleText") or v.get("shortViewCountText", {}).get("simpleText"),
                    "published": v.get("publishedTimeText", {}).get("simpleText"),
                })
        for val in obj.values():
            _extract(val, out)
    elif isinstance(obj, list):
        for val in obj:
            _extract(val, out)

def run(query, limit=8):
    if not query or not str(query).strip():
        raise ValueError("query required")
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query, "hl": "en"})
    r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                  follow_redirects=True, timeout=20)
    r.raise_for_status()
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", r.text, re.S) or \
        re.search(r'ytInitialData"\]\s*=\s*(\{.*?\});', r.text, re.S)
    if not m:
        raise RuntimeError("could not find ytInitialData in response (YouTube layout change or consent wall)")
    data = json.loads(m.group(1))
    out = []
    _extract(data, out)
    seen, res = set(), []
    for v in out:
        if v["videoId"] in seen:
            continue
        seen.add(v["videoId"])
        res.append(v)
        if len(res) >= max(1, int(limit)):
            break
    return {"query": query, "count": len(res), "results": res}
