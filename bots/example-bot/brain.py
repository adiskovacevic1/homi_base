"""
brain.py: the bot's conversation model behind one small interface, so the provider is a setting.

    complete(system, messages, tools, effort, provider, model, keys) -> Turn(text, tool_calls, stop, raw)
    assistant_message(turn)              -> the message to append for the assistant's turn (keeps thinking blocks etc.)
    tool_results_message(results)        -> the message(s) to append with the tools' results
    normalize_history(messages, provider)-> the per-channel history (Anthropic-style blocks) in this provider's shape

Providers: "claude" (Anthropic Messages API with tools, adaptive thinking and server-side fallbacks - today's default),
"openai" and "deepseek" (OpenAI-compatible chat completions with function calling; DeepSeek through its base URL).
The bot keeps its history in Anthropic block form (text, image, document, tool_use, tool_result); this module converts
on the way out. Tool calls and results come back in one shape regardless of provider.

Key names: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY. A provider whose key is missing raises KeyMissing(name)
before any network call, so the bot can send its key-needed reply.
"""
import base64, json, os
from dataclasses import dataclass, field

PROVIDERS = {
    "claude":   {"key": "ANTHROPIC_API_KEY", "model": "claude-opus-5", "label": "Claude (Anthropic)"},
    "openai":   {"key": "OPENAI_API_KEY", "model": "gpt-5", "label": "OpenAI", "base_url": None},
    "deepseek": {"key": "DEEPSEEK_API_KEY", "model": "deepseek-chat", "label": "DeepSeek", "base_url": "https://api.deepseek.com"},
}
EFFORT_TOKENS = {"low": 4000, "medium": 16000, "high": 32000}


class KeyMissing(Exception):
    def __init__(self, provider, key):
        super().__init__(f"{PROVIDERS[provider]['label']} needs {key}")
        self.provider, self.key = provider, key


class KeyRejected(Exception):
    def __init__(self, provider, key, detail=""):
        super().__init__(f"{PROVIDERS[provider]['label']} rejected {key}: {detail}")
        self.provider, self.key = provider, key


@dataclass
class Turn:
    text: str = ""
    tool_calls: list = field(default_factory=list)      # [{"id", "name", "input"}]
    stop: str = "end"                                     # end | tool_use | refusal
    raw: object = None                                    # provider response, for assistant_message()
    provider: str = "claude"


_clients = {}       # (provider, key) -> client; rebuilt when the key changes


def _client(provider, key):
    ck = (provider, key)
    if ck not in _clients:
        _clients.clear()                                  # one live client at a time; a key change drops the old one
        if provider == "claude":
            import anthropic
            _clients[ck] = anthropic.Anthropic(api_key=key)
        else:
            from openai import OpenAI
            _clients[ck] = OpenAI(api_key=key, base_url=PROVIDERS[provider]["base_url"])
    return _clients[ck]


def complete(system, messages, tools, effort="medium", provider="claude", model=None, keys=None):
    """One model call. `messages` are Anthropic-style (the bot's history); `tools` are Anthropic specs
    ({name, description, input_schema}). Raises KeyMissing / KeyRejected; other API errors propagate as the SDK's."""
    provider = provider if provider in PROVIDERS else "claude"
    spec = PROVIDERS[provider]
    key = (keys or {}).get(spec["key"])
    if not key:
        raise KeyMissing(provider, spec["key"])
    model = model or spec["model"]
    if provider == "claude":
        return _complete_claude(_client(provider, key), system, messages, tools, effort, model, provider)
    return _complete_openai(_client(provider, key), system, messages, tools, effort, model, provider)


# ---------------------------------------------------------------- Claude

# Anthropic runs web searches server-side when asked to: the model searches and reads pages inside one call, with
# citations, billed per search. On by default; BRAIN_WEB_SEARCH=0 leaves Claude with the kit's web_search tool like the
# other providers. Only the conversation gets it (calls that pass no tools, like the daily ideas, do not search).
NATIVE_WEB_SEARCH = os.environ.get("BRAIN_WEB_SEARCH", "1").strip().lower() not in ("0", "off", "false", "no", "")
NATIVE_WEB_SEARCH_MAX = int(os.environ.get("BRAIN_WEB_SEARCH_MAX", "5"))
NATIVE_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": NATIVE_WEB_SEARCH_MAX}


def _claude_tools(tools):
    """The kit's tools, plus Anthropic's own search when it is on. The kit's web_search (there for the other providers)
    steps aside then: tool names must be unique in a request, and the native one is the better of the two."""
    if not tools or not NATIVE_WEB_SEARCH:
        return tools
    return [t for t in tools if t.get("name") != "web_search"] + [NATIVE_SEARCH_TOOL]


def _complete_claude(client, system, messages, tools, effort, model, provider):
    import anthropic
    try:
        r = client.beta.messages.create(model=model, max_tokens=EFFORT_TOKENS.get(effort, 16000), system=system, messages=messages,
                                        tools=_claude_tools(tools), output_config={"effort": effort},
                                        betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.AuthenticationError as e:
        raise KeyRejected(provider, PROVIDERS[provider]["key"], str(e)[:120])
    if r.stop_reason == "refusal":
        return Turn(stop="refusal", raw=r, provider=provider)
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    calls = [{"id": b.id, "name": b.name, "input": dict(b.input or {})} for b in r.content if b.type == "tool_use"]
    # pause_turn: a long server-side search loop was cut; what came back stands as the answer (the person can ask on)
    return Turn(text=text, tool_calls=calls, stop="tool_use" if r.stop_reason == "tool_use" else "end", raw=r, provider=provider)


# ---------------------------------------------------------------- OpenAI-compatible (OpenAI, DeepSeek)

def _oa_tools(tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", "")[:1024],
                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]


def _oa_content(content, images_ok):
    """Anthropic content blocks -> OpenAI content (string or parts). Images become data URLs where the model can see them,
    a memo where it cannot; documents (PDFs) become a note - the bot keeps the file under /data for tools."""
    if isinstance(content, str):
        return content
    parts, text_only = [], True
    for b in content:
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b["text"]})
        elif t == "image":
            src = b.get("source", {})
            if images_ok and src.get("type") == "base64":
                parts.append({"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"}}); text_only = False
            else:
                parts.append({"type": "text", "text": "[an image was attached; this model cannot view images - describe what you would need, or use a tool]"})
        elif t == "document":
            parts.append({"type": "text", "text": "[a PDF was attached; this model cannot read it directly - a tool can, from /data/uploads]"})
    if text_only:
        return "\n".join(p["text"] for p in parts)
    return parts


def normalize_history(messages, provider):
    """The bot's history -> this provider's message list. For Claude it is already right."""
    if provider == "claude":
        return messages
    images_ok = provider == "openai"
    out = []
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if role == "assistant" and not isinstance(content, str):
            # an assistant turn stored as Anthropic blocks (from a Claude session): keep its text and tool calls
            text = "".join(getattr(b, "text", "") if not isinstance(b, dict) else b.get("text", "") for b in content
                           if (getattr(b, "type", None) or (isinstance(b, dict) and b.get("type"))) == "text")
            calls = [b for b in content if (getattr(b, "type", None) or (isinstance(b, dict) and b.get("type"))) == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = [{"id": (b.get("id") if isinstance(b, dict) else b.id), "type": "function",
                                      "function": {"name": (b.get("name") if isinstance(b, dict) else b.name),
                                                   "arguments": json.dumps((b.get("input") if isinstance(b, dict) else b.input) or {})}} for b in calls]
            out.append(msg)
        elif role == "user" and isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            for b in content:                                   # Anthropic tool results -> one tool message each
                c = b.get("content")
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": c if isinstance(c, str) else json.dumps(c)})
        else:
            out.append({"role": role, "content": _oa_content(content, images_ok)})
    return out


def _complete_openai(client, system, messages, tools, effort, model, provider):
    import openai
    oa_messages = [{"role": "system", "content": system}] + normalize_history(messages, provider)
    kw = {"model": model, "messages": oa_messages}
    if tools:
        kw["tools"] = _oa_tools(tools)
    if provider == "openai" and model.startswith(("gpt-5", "o")):
        kw["reasoning_effort"] = effort                      # reasoning models take an effort hint; others would reject it
    try:
        r = client.chat.completions.create(**kw)
    except openai.AuthenticationError as e:
        raise KeyRejected(provider, PROVIDERS[provider]["key"], str(e)[:120])
    choice = r.choices[0]
    msg = choice.message
    calls = []
    for tc in (msg.tool_calls or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except ValueError:
            args = {"_raw": tc.function.arguments}
        calls.append({"id": tc.id, "name": tc.function.name, "input": args})
    stop = "tool_use" if calls else ("refusal" if getattr(msg, "refusal", None) else "end")
    return Turn(text=(msg.content or "").strip(), tool_calls=calls, stop=stop, raw=r, provider=provider)


# ---------------------------------------------------------------- messages to append, in the bot's (Anthropic-style) history

def assistant_message(turn):
    """What to append for this turn. Claude: the verbatim content (thinking blocks ride along with tool calls).
    OpenAI-style: the same information as Anthropic blocks, so the history stays one shape and Claude can take over later."""
    if turn.provider == "claude" and getattr(turn.raw, "content", None) is not None:
        return {"role": "assistant", "content": turn.raw.content}
    blocks = []
    if turn.text:
        blocks.append({"type": "text", "text": turn.text})
    for c in turn.tool_calls:
        blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]})
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]}


def tool_results_message(results):
    """results: Anthropic tool_result dicts ({type, tool_use_id, content, is_error?}). One user message holds them all;
    normalize_history() turns them into tool messages for the OpenAI-style providers."""
    return {"role": "user", "content": results}
