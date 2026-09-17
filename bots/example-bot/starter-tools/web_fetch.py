import re, html
import httpx

UA = "Mozilla/5.0 (compatible; AssistantBot/1.0)"

def _strip(body):
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n\s*", "\n\n", body)
    return body.strip()

def run(url, mode="text", max_chars=20000):
    if not re.match(r"^https?://", url or "", re.I):
        raise ValueError("url must start with http:// or https://")
    max_chars = int(max_chars or 20000)
    with httpx.Client(follow_redirects=True, timeout=25.0,
                      headers={"User-Agent": UA, "Accept": "*/*"}) as c:
        r = c.get(url)
    ctype = r.headers.get("content-type", "")
    body = r.text
    if mode == "text" and ("html" in ctype.lower() or "<html" in body[:2000].lower()):
        out = _strip(body)
    else:
        out = body
    truncated = len(out) > max_chars
    return {
        "url": str(r.url),
        "status": r.status_code,
        "content_type": ctype,
        "truncated": truncated,
        "content": out[:max_chars],
    }
