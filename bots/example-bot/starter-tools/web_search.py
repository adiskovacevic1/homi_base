"""Web search without an API key, through DuckDuckGo's HTML endpoint. Returns titles, links and snippets; read a hit with
web_fetch. The Claude brain has its own server-side search and does not see this tool; DeepSeek and OpenAI do."""
import html, re, urllib.parse
import httpx

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")


def _clean(s):
    s = re.sub(r"(?s)<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _target(href):
    """DuckDuckGo wraps result links in a redirect (//duckduckgo.com/l/?uddg=<url>&rut=...); unwrap it."""
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlparse(href)
    if q.netloc.endswith("duckduckgo.com") and q.path.startswith("/l/"):
        return urllib.parse.parse_qs(q.query).get("uddg", [href])[0]
    return href


def _parse_html(body):
    out = []
    for m in re.finditer(r'(?s)<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]*class="result__a"|<div class="nav-links|$)', body):
        href, title, rest = m.groups()
        snip = re.search(r'(?s)<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', rest)
        out.append({"title": _clean(title), "url": _target(html.unescape(href)), "snippet": _clean(snip.group(1) if snip else "")})
    return out


def _parse_lite(body):
    out = []
    for m in re.finditer(r'(?s)<a[^>]*rel="nofollow"[^>]*href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>(.*?)(?=<a[^>]*class="result-link"|$)', body):
        href, title, rest = m.groups()
        snip = re.search(r'(?s)<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', rest)
        out.append({"title": _clean(title), "url": _target(html.unescape(href)), "snippet": _clean(snip.group(1) if snip else "")})
    return out


def run(query, limit=8, region="", time_range=""):
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")
    limit = max(1, min(int(limit or 8), 20))
    data = {"q": query}
    if region:
        data["kl"] = region                 # e.g. us-en, uk-en, de-de
    if time_range in ("d", "w", "m", "y"):
        data["df"] = time_range             # past day / week / month / year
    errors = []
    with httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.8"}) as c:
        for url in ENDPOINTS:
            try:
                r = c.post(url, data=data)
                if r.status_code != 200:
                    errors.append(f"{url}: HTTP {r.status_code}")
                    continue
                hits = _parse_html(r.text) if "html.duckduckgo" in url else _parse_lite(r.text)
                hits = [h for h in hits if h["url"].startswith("http")]
                if hits:
                    return {"query": query, "count": len(hits[:limit]), "results": hits[:limit], "source": "duckduckgo"}
                if "anomaly" in r.text.lower() or "captcha" in r.text.lower():
                    errors.append(f"{url}: rate limited (challenge page)")
                else:
                    errors.append(f"{url}: no results parsed")
            except httpx.HTTPError as e:
                errors.append(f"{url}: {e}")
    return {"query": query, "count": 0, "results": [], "source": "duckduckgo",
            "note": "no results; " + "; ".join(errors) + ". Try different words, or web_fetch a site you know."}
