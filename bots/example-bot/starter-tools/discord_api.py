"""discord_api: talk to Discord's REST API as this bot, with its own token (declared secret DISCORD_TOKEN).

Everyone who talks to the bot can trigger its tools, so this one is deliberately scoped: read anything the bot can
see; post messages, files, reactions, pins, threads, polls and DMs; edit or delete only messages the bot itself
wrote. Guild administration (channels, roles, members, bans, webhooks, invites) is refused here - an owner can have
a dedicated tool built for that.
"""
import json, mimetypes, os, re, time
import httpx

API = "https://discord.com/api/v10"
UA = "DiscordBot (homi_base, 1.0)"
MAX_TEXT = 20000

# (methods, path regex). Anything not matched is refused.
ALLOW = [
    ({"GET"}, r"^/(channels|guilds|users)/[^?]*$"),                     # read: messages, channels, members, guild info, users
    ({"GET"}, r"^/users/@me(/.*)?$"),
    ({"POST"}, r"^/channels/\d+/messages$"),                             # send a message (text, embed, poll, file)
    ({"PATCH", "DELETE"}, r"^/channels/\d+/messages/\d+$"),              # edit/delete - the bot's own messages only (checked below)
    ({"PUT", "DELETE"}, r"^/channels/\d+/messages/\d+/reactions/[^/]+/@me$"),   # react / unreact as the bot
    ({"DELETE"}, r"^/channels/\d+/messages/\d+/reactions/[^/]+$"),      # clear one emoji's reactions (needs Manage Messages)
    ({"PUT", "DELETE"}, r"^/channels/\d+/pins/\d+$"),                    # pin / unpin
    ({"POST"}, r"^/channels/\d+/messages/\d+/threads$"),                 # thread from a message
    ({"POST"}, r"^/channels/\d+/threads$"),                              # new thread
    ({"PUT", "DELETE"}, r"^/channels/\d+/thread-members/@me$"),          # join / leave a thread
    ({"POST"}, r"^/channels/\d+/typing$"),
    ({"POST"}, r"^/users/@me/channels$"),                                # open a DM with a user
    ({"GET", "POST"}, r"^/channels/\d+/polls/\d+/(answers/\d+|expire)$"),
    ({"POST"}, r"^/channels/\d+/messages/bulk-delete$"),                 # refused below unless every id is the bot's own
]
OWN_MESSAGE_RE = re.compile(r"^/channels/(\d+)/messages/(\d+)$")


def _headers():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not available to this tool - it should be declared in the tool's secrets (it is)")
    return {"Authorization": f"Bot {token}", "User-Agent": UA}


def _request(client, method, path, **kw):
    for attempt in range(3):
        r = client.request(method, API + path, headers=_headers(), **kw)
        if r.status_code == 429:                                 # rate limited: Discord says how long to wait
            try:
                wait = float(r.json().get("retry_after", 1))
            except ValueError:
                wait = 1.0
            time.sleep(min(wait, 10))
            continue
        return r
    return r


def _own_message(client, channel_id, message_id):
    me = _request(client, "GET", "/users/@me").json().get("id")
    m = _request(client, "GET", f"/channels/{channel_id}/messages/{message_id}")
    if m.status_code != 200:
        raise ValueError(f"message {message_id} not found in channel {channel_id} (HTTP {m.status_code})")
    return (m.json().get("author") or {}).get("id") == me


def run(method="GET", path="/users/@me", body=None, query=None, file_path=None, file_name=None):
    method = str(method or "GET").upper()
    path = "/" + str(path or "").strip().lstrip("/")
    path = re.sub(r"^/api(/v\d+)?", "", path)                      # accept a full-ish path too
    if "?" in path:
        raise ValueError("put query parameters in `query`, not in the path")
    if not any(method in methods and re.match(rx, path) for methods, rx in ALLOW):
        raise PermissionError(f"{method} {path} is outside what this tool may do (reads; the bot's own messages, files, reactions, pins, "
                              "threads, polls, DMs). Guild administration needs a dedicated tool built by an owner.")
    if isinstance(body, str):
        body = json.loads(body) if body.strip() else None
    with httpx.Client(timeout=25.0) as client:
        m = OWN_MESSAGE_RE.match(path)
        if m and method in ("PATCH", "DELETE") and not _own_message(client, m.group(1), m.group(2)):
            raise PermissionError("that message was not written by this bot; it can only edit or delete its own")
        if path.endswith("/bulk-delete"):
            channel = path.split("/")[2]
            ids = (body or {}).get("messages") or []
            if not ids or not all(_own_message(client, channel, i) for i in ids):
                raise PermissionError("bulk-delete here is limited to messages this bot wrote")
        kw = {"params": query or None}
        if file_path:
            p = os.path.realpath(str(file_path))
            if not p.startswith("/data/") or not os.path.isfile(p):
                raise ValueError("file_path must be an existing file under /data")
            if os.path.getsize(p) > 10 * 1024 * 1024:
                raise ValueError("file is over 10 MB, Discord's default upload limit")
            name = file_name or os.path.basename(p)
            with open(p, "rb") as fh:
                kw["files"] = {"files[0]": (name, fh.read(), mimetypes.guess_type(name)[0] or "application/octet-stream")}
            kw["data"] = {"payload_json": json.dumps(body or {})}
        elif body is not None:
            kw["json"] = body
        r = _request(client, method, path, **kw)
    out = {"status": r.status_code, "ok": 200 <= r.status_code < 300}
    if r.status_code == 204 or not r.content:
        return out
    try:
        out["data"] = r.json()
    except ValueError:
        out["data"] = r.text[:MAX_TEXT]
    if not out["ok"]:
        msg = out["data"].get("message") if isinstance(out["data"], dict) else str(out["data"])[:200]
        hints = {401: "the bot token was rejected", 403: "the bot lacks permission in that channel or server",
                 404: "no such channel/message/user, or the bot cannot see it", 400: "Discord rejected the request body"}
        out["error"] = f"{msg} - {hints.get(r.status_code, '')}".strip(" -")
    return out
