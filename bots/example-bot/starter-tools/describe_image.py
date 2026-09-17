"""Eyes for a brain without them. Sends an image from /data (attachments land in /data/uploads) to a vision model and
returns what it sees, or the answer to a question about it (read this receipt, what's on the whiteboard, is the light on).
Uses whichever key the household has: xAI Grok (XAI_API_KEY), OpenAI (OPENAI_API_KEY) or Claude (ANTHROPIC_API_KEY)."""
import base64, os, mimetypes
from pathlib import Path

ROOT = Path("/data")
MAX_BYTES = 15 * 1024 * 1024
PROVIDERS = (   # in preference order; the first one with a key in the vault answers
    ("XAI_API_KEY", "xai", "https://api.x.ai/v1", os.environ.get("XAI_VISION_MODEL", "grok-4")),
    ("OPENAI_API_KEY", "openai", None, os.environ.get("OPENAI_VISION_MODEL", "gpt-5")),
    ("ANTHROPIC_API_KEY", "anthropic", None, os.environ.get("ANTHROPIC_VISION_MODEL", "claude-sonnet-5")),
)


def _load(path):
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if ROOT not in p.parents and p != ROOT:
        raise ValueError("path must be under /data (attachments are in /data/uploads)")
    if not p.is_file():
        raise FileNotFoundError(f"no file at {p}")
    if p.stat().st_size > MAX_BYTES:
        raise ValueError(f"{p.name} is {p.stat().st_size // 1048576} MB; over the {MAX_BYTES // 1048576} MB limit")
    mime = mimetypes.guess_type(p.name)[0] or ""
    data = p.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime.startswith("image/"):
        raise ValueError(f"{p.name} does not look like an image (png, jpeg, gif, webp)")
    return p, mime, data


def _openai_style(key, base_url, model, mime, data, prompt, max_tokens):
    from openai import OpenAI
    client = OpenAI(api_key=key, base_url=base_url)
    r = client.chat.completions.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}", "detail": "high"}}]}])
    return (r.choices[0].message.content or "").strip()


def _anthropic(key, model, mime, data, prompt, max_tokens):
    import anthropic
    client = anthropic.Anthropic(api_key=key)
    r = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}},
        {"type": "text", "text": prompt}]}])
    return "".join(b.text for b in r.content if b.type == "text").strip()


def run(path, question="", max_tokens=800):
    p, mime, data = _load(path)
    prompt = (question or "").strip() or ("Describe this image precisely: what it shows, any text in it (transcribe it), and anything a "
                                          "person would want to know about it. Be concrete and brief.")
    max_tokens = max(100, min(int(max_tokens or 800), 4000))
    chosen = next(((k, name, url, model) for k, name, url, model in PROVIDERS if os.environ.get(k)), None)
    if not chosen:
        raise RuntimeError("no vision key available: XAI_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY")
    key, provider, base_url, model = chosen
    key = os.environ[key]
    text = _anthropic(key, model, mime, data, prompt, max_tokens) if provider == "anthropic" else _openai_style(key, base_url, model, mime, data, prompt, max_tokens)
    return {"file": str(p), "mime": mime, "bytes": len(data), "provider": provider, "model": model,
            "question": question or None, "answer": text}
