"""
setup_web.py: the browser version of first-run setup. install.sh / install.ps1 run it in the dev container with port 8792
published on localhost only, open http://localhost:8792/setup?key=<one-time key> in the browser, and wait for it to exit.

The page asks the same questions as setup.py, but checks them live before anything is written:
  - the Discord token is tried against Discord's API, and the page shows the bot's name, its Application ID (so it is not
    asked for) and whether the Message Content intent is enabled
  - the Anthropic key is tried with a models request
  - an ElevenLabs key turns the voice field into a list of the account's voices with previews
  - owner IDs are resolved to usernames
Submit writes the same files through setup.write_files() and the server exits, so the installer can continue.

Env: SETUP_KEY (the one-time key; generated if missing), SETUP_PORT (8792), LAB_DIR (/lab), SETUP_SKIP_CHECKS=1 (tests only).
Nothing here is stored or sent anywhere except the checks against the services whose keys you are entering.
"""
import asyncio, json, os, re, secrets, sys, time
from pathlib import Path

import aiohttp
from aiohttp import web

import setup as cfgfile
import vault as vaultlib

PORT = int(os.environ.get("SETUP_PORT", "8792"))
KEY = os.environ.get("SETUP_KEY") or secrets.token_urlsafe(16)
SKIP_CHECKS = os.environ.get("SETUP_SKIP_CHECKS") == "1"
MODE = "console" if os.environ.get("SETUP_MODE") == "console" or "--console" in sys.argv else "setup"   # console: edit keys and settings of an existing install
# PERSISTENT: the console the dev box serves all the time (forge.py starts it). No one-time key then; the page asks for the
# vault passphrase and gets a session cookie. The bot's "add this key" links point here.
PERSISTENT = os.environ.get("SETUP_PERSISTENT") == "1"
PAGE = (Path(__file__).parent / "setup.html").read_text(encoding="utf-8")
DISCORD = "https://discord.com/api/v10"
MSG_CONTENT_FLAGS = (1 << 18) | (1 << 19)        # GATEWAY_MESSAGE_CONTENT, GATEWAY_MESSAGE_CONTENT_LIMITED
DONE = asyncio.Event()
SESSIONS = {}                                    # cookie token -> expiry (persistent console logins)
SESSION_TTL = 12 * 3600
LOGIN_FAILS = {}                                 # remote ip -> [timestamps] for the 5-a-minute limit
COOKIE = "homi_console"


def session_ok(req):
    tok = req.cookies.get(COOKIE)
    exp = SESSIONS.get(tok)
    if not tok or not exp:
        return False
    if exp < time.time():
        SESSIONS.pop(tok, None)
        return False
    SESSIONS[tok] = time.time() + SESSION_TTL   # sliding
    return True


def authorized(req):
    if not PERSISTENT and (req.query.get("key") == KEY or req.headers.get("X-Setup-Key") == KEY):
        return True
    return session_ok(req)


def console_passphrase_ok(passphrase):
    """The console login: the vault passphrase (or, on an install still on a random VAULT_KEY, that key)."""
    env = cfgfile.env_values(cfgfile.BOT_ENV)
    if not env:
        return False
    if env.get("VAULT_PASSPHRASE"):
        return secrets.compare_digest(passphrase, env["VAULT_PASSPHRASE"])
    return bool(env.get("VAULT_KEY")) and secrets.compare_digest(passphrase, env["VAULT_KEY"])


async def login(req):
    ip = req.remote or "?"
    now = time.time()
    LOGIN_FAILS[ip] = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 60]
    if len(LOGIN_FAILS[ip]) >= 5:
        return web.json_response({"ok": False, "error": "too many tries; wait a minute"}, status=429)
    try:
        body = await req.json()
    except Exception:  # noqa
        body = {}
    if not console_passphrase_ok(cfgfile.clean(str(body.get("passphrase") or ""))):
        LOGIN_FAILS[ip].append(now)
        return web.json_response({"ok": False, "error": "wrong passphrase"}, status=401)
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = now + SESSION_TTL
    resp = web.json_response({"ok": True})
    resp.set_cookie(COOKIE, tok, httponly=True, samesite="Strict", path="/", max_age=SESSION_TTL)
    return resp


async def logout(req):
    SESSIONS.pop(req.cookies.get(COOKIE), None)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def fetch_json(session, method, url, **kw):
    async with session.request(method, url, timeout=aiohttp.ClientTimeout(total=15), **kw) as r:
        body = await r.json(content_type=None) if r.content_length != 0 else {}
        return r.status, body


async def check_discord(token):
    """Bot identity, application id and the message-content intent flag, all from the token alone."""
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
        st, me = await fetch_json(s, "GET", f"{DISCORD}/users/@me")
        if st == 401:
            return {"ok": False, "error": "Discord rejected this token. Bot tab -> Reset Token, and paste the new one."}
        if st != 200:
            return {"ok": False, "error": f"Discord answered {st}"}
        st, app = await fetch_json(s, "GET", f"{DISCORD}/oauth2/applications/@me")
        flags = int(app.get("flags") or 0) if st == 200 else 0
        avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png?size=64" if me.get("avatar") else ""
        return {"ok": True, "name": me.get("username"), "id": me.get("id"), "avatar": avatar,
                "app_id": app.get("id") if st == 200 else "", "message_content": bool(flags & MSG_CONTENT_FLAGS),
                "invite": cfgfile.invite_url(app.get("id", "")) if st == 200 else ""}


async def check_guilds(token):
    """Servers the bot has been invited to - the setup page polls this after showing the invite link."""
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
        st, guilds = await fetch_json(s, "GET", f"{DISCORD}/users/@me/guilds")
        if st != 200:
            return {"ok": False, "error": f"Discord answered {st}", "guilds": []}
        return {"ok": True, "guilds": [{"id": g["id"], "name": g["name"],
                                        "icon": f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png?size=64" if g.get("icon") else ""}
                                       for g in guilds]}


async def search_members(token, guild_id, query):
    """Members of a server whose name starts with `query`, via the bot: how the owner picks themselves without knowing their id."""
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
        st, members = await fetch_json(s, "GET", f"{DISCORD}/guilds/{guild_id}/members/search", params={"query": query, "limit": 10})
        if st != 200:
            msg = (members.get("message") if isinstance(members, dict) else "") or f"Discord answered {st}"
            return {"ok": False, "error": msg, "members": []}
        out = []
        for m in members:
            u = m.get("user") or {}
            if u.get("bot"):
                continue
            avatar = (f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=64" if u.get("avatar")
                      else f"https://cdn.discordapp.com/embed/avatars/{int(u.get('id', '0')) >> 22 & 5}.png")
            out.append({"id": u.get("id"), "name": m.get("nick") or u.get("global_name") or u.get("username"),
                        "username": u.get("username"), "avatar": avatar})
        return {"ok": True, "members": out}


# Note: a bot cannot create a server (POST /guilds answers "Bots cannot use this endpoint"), so the "no server yet?" step on the
# page is guidance that opens Discord for the person; the bot is invited afterwards like any other server.


async def check_owners(token, ids):
    out = []
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
        for i in ids[:10]:
            st, u = await fetch_json(s, "GET", f"{DISCORD}/users/{i}")
            out.append({"id": i, "ok": st == 200, "name": (u.get("global_name") or u.get("username")) if st == 200 else "not found"})
    return out


async def check_openai_compat(key, base, label):
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {key}"}) as s:
        st, body = await fetch_json(s, "GET", f"{base}/models")
        if st == 200:
            return {"ok": True}
        msg = (body.get("error") or {}).get("message") if isinstance(body, dict) and isinstance(body.get("error"), dict) else ""
        return {"ok": False, "error": f"{label} rejected this key" if st in (401, 403) else f"{label} answered {st}: {msg or ''}".strip()}


async def check_anthropic(key):
    async with aiohttp.ClientSession(headers={"x-api-key": key, "anthropic-version": "2023-06-01"}) as s:
        st, body = await fetch_json(s, "GET", "https://api.anthropic.com/v1/models?limit=1")
        if st == 200:
            return {"ok": True}
        msg = (body.get("error") or {}).get("message") if isinstance(body, dict) else ""
        return {"ok": False, "error": "Anthropic rejected this key" if st in (401, 403) else f"Anthropic answered {st}: {msg or ''}".strip()}


async def check_eleven(key):
    async with aiohttp.ClientSession(headers={"xi-api-key": key}) as s:
        st, body = await fetch_json(s, "GET", "https://api.elevenlabs.io/v1/voices")
        if st != 200:
            return {"ok": False, "error": "ElevenLabs rejected this key" if st in (401, 403) else f"ElevenLabs answered {st}"}
        voices = [{"id": v["voice_id"], "name": v.get("name", ""), "category": v.get("category", ""),
                   "labels": ", ".join(str(x) for x in (v.get("labels") or {}).values()), "preview": v.get("preview_url", "")}
                  for v in body.get("voices", [])]
        return {"ok": True, "voices": voices}


def guarded(handler):
    async def inner(req):
        if not authorized(req):
            if PERSISTENT:
                return web.json_response({"error": "login required", "login": True}, status=401)
            return web.json_response({"error": "bad or missing setup key - use the link the installer printed"}, status=403)
        try:
            return await handler(req)
        except aiohttp.ClientError as e:
            return web.json_response({"ok": False, "error": f"could not reach the service: {type(e).__name__}", "offline": True})
        except Exception as e:  # noqa - shown on the page, never a blank failure
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, status=500)
    return inner


async def page(req):
    """The page itself is public on the persistent console (it is only reachable on the owner's PC) and carries no key there;
    every API call behind it needs the session. The one-time setup keeps the key in the page."""
    if PERSISTENT:
        return web.Response(text=PAGE.replace("__KEY__", "persistent"), content_type="text/html")
    if not authorized(req):
        return web.json_response({"error": "bad or missing setup key - use the link the installer printed"}, status=403)
    return web.Response(text=PAGE.replace("__KEY__", KEY), content_type="text/html")


@guarded
async def state(req):
    env = cfgfile.env_values(cfgfile.BOT_ENV)
    v = cfgfile.current_vault()
    vault_info = {"exists": bool(v and v.exists()), "version": v.version() if v else None, "has_passphrase": bool(env.get("VAULT_PASSPHRASE")),
                  "legacy_key": bool(env.get("VAULT_KEY")), "path": str(cfgfile.VAULT_PATH.relative_to(cfgfile.LAB))}
    if v and v.exists():
        try:
            vault_info["count"] = len(v.names()); vault_info["readable"] = True
        except Exception as e:  # noqa
            vault_info["readable"] = False; vault_info["error"] = str(e)
    return web.json_response({"mode": MODE, "persistent": PERSISTENT, "kits": cfgfile.existing_kits(), "config_exists": cfgfile.config_exists(),
                              "skip_checks": SKIP_CHECKS, "vault": vault_info, "suggested_passphrase": vaultlib.suggest_passphrase(),
                              "restart_needed": cfgfile.RESTART_MARKER.read_text(encoding="utf-8").strip() if cfgfile.RESTART_MARKER.exists() else ""})


def _vault_or_400():
    v = cfgfile.current_vault()
    if v is None:
        raise web.HTTPBadRequest(text=json.dumps({"error": "no configuration on this PC yet - run the installer first"}), content_type="application/json")
    return v


@guarded
async def vault_list(req):
    v = _vault_or_400()
    try:
        return web.json_response({"secrets": v.listing(), "path": str(cfgfile.VAULT_PATH.relative_to(cfgfile.LAB)), "version": v.version(),
                                  "bootstrap": list(vaultlib.BOOTSTRAP), "needs_restart": list(vaultlib.NEEDS_RESTART)})
    except vaultlib.VaultError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)


@guarded
async def vault_set(req):
    v = _vault_or_400()
    body = await req.json()
    name, value, note = cfgfile.clean(body.get("name")).upper(), cfgfile.clean(body.get("value")), cfgfile.clean(body.get("note"))[:200]
    if not re.fullmatch(vaultlib.NAME_RE, name):
        return web.json_response({"ok": False, "error": "name must look like an environment variable: XAI_API_KEY"}, status=400)
    if not value:
        return web.json_response({"ok": False, "error": "value is empty"}, status=400)
    if name == "DISCORD_TOKEN" and not cfgfile.looks_like_discord_token(value):
        return web.json_response({"ok": False, "error": "that does not look like a Discord bot token"}, status=400)
    if name == "ANTHROPIC_API_KEY" and not value.startswith("sk-ant-"):
        return web.json_response({"ok": False, "error": "Anthropic keys start with sk-ant-"}, status=400)
    try:
        out = v.set(name, value, note or None)
    except vaultlib.VaultError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)
    restart = name in vaultlib.NEEDS_RESTART
    if restart:
        cfgfile.RESTART_MARKER.write_text(f"{name} changed\n", encoding="utf-8")
    return web.json_response({"ok": True, **out, "needs_restart": restart,
                              "applies": "after `docker compose up -d` (the wrapper does it when you finish)" if restart else "live - the bot reads it on its next use"})


@guarded
async def vault_delete(req):
    v = _vault_or_400()
    name = cfgfile.clean((await req.json()).get("name")).upper()
    try:
        v.delete(name)
    except KeyError:
        return web.json_response({"ok": False, "error": f"no secret named {name}"}, status=404)
    except vaultlib.VaultError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)
    return web.json_response({"ok": True, "deleted": name})


@guarded
async def vault_reveal(req):
    """A value, only after the passphrase is typed again: the page is local and the owner holds the passphrase, so this is
    a deliberate act rather than something a glance at the screen gives away."""
    v = _vault_or_400()
    body = await req.json()
    name, passphrase = cfgfile.clean(body.get("name")).upper(), cfgfile.clean(body.get("passphrase"))
    if not passphrase or not v.check_passphrase(passphrase):
        return web.json_response({"ok": False, "error": "wrong passphrase"}, status=403)
    value = v.get(name)
    if value is None:
        return web.json_response({"ok": False, "error": f"no secret named {name}"}, status=404)
    return web.json_response({"ok": True, "name": name, "value": value})


SETTINGS = {"bot": ("TOOL_CREATORS", "OPEN_CHANNELS", "HOME_CHANNEL", "ACTIVITY_CHANNEL", "DM_POLICY", "IDEAS_AT", "IDEAS_CHANNEL", "BOT_SYSTEM", "FORGE_URL"),
            "voice": ("WAKE_WORDS", "AUTO_JOIN"), "root": ("TZ", "KIT")}
# "live" settings are the bot's own switches in data/settings.json (what the `settings` tool edits): read on use, no restart.
LIVE_FILE = cfgfile.LAB / "bots" / "example-bot" / "data" / "settings.json"
LIVE_KEYS = ("brain_provider", "brain_model", "daily_ideas", "ideas_at", "ideas_channel")
BRAINS = {"claude": "claude-opus-5", "openai": "gpt-5", "deepseek": "deepseek-chat"}


def live_load():
    try:
        return json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@guarded
async def settings_get(req):
    live = live_load()
    env = cfgfile.env_values(cfgfile.BOT_ENV)
    live_view = {"brain_provider": live.get("brain_provider") or env.get("BRAIN_PROVIDER", "claude"),
                 "brain_model": live.get("brain_model") or "",
                 "daily_ideas": str(live.get("daily_ideas", "true")).lower(),
                 "ideas_at": live.get("ideas_at") or env.get("IDEAS_AT", "09:00"), "ideas_channel": live.get("ideas_channel") or env.get("IDEAS_CHANNEL", "")}
    return web.json_response({"live": live_view, "brains": BRAINS,
                              "bot": {k: v for k, v in env.items() if k in SETTINGS["bot"]},
                              "voice": {k: v for k, v in cfgfile.env_values(cfgfile.VOICE_ENV).items() if k in SETTINGS["voice"]},
                              "root": {k: v for k, v in cfgfile.env_values(cfgfile.ROOT_ENV).items() if k in SETTINGS["root"]}})


@guarded
async def settings_set(req):
    body = await req.json()
    changed, live_changed = [], []
    live_wanted = {k: cfgfile.clean(str(v)) for k, v in (body.get("live") or {}).items() if k in LIVE_KEYS}
    if live_wanted:
        if "brain_provider" in live_wanted and live_wanted["brain_provider"] not in BRAINS:
            return web.json_response({"ok": False, "error": f"brain_provider must be one of {', '.join(BRAINS)}"}, status=400)
        if "ideas_at" in live_wanted and live_wanted["ideas_at"] and not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", live_wanted["ideas_at"]):
            return web.json_response({"ok": False, "error": "ideas_at is HH:MM"}, status=400)
        live = live_load()
        for k, v in live_wanted.items():
            if k == "daily_ideas":
                v = v.lower() in ("true", "on", "yes", "1")
            if k == "brain_model" and not v:
                live.pop(k, None); live_changed.append(k); continue
            if live.get(k) != v:
                live[k] = v; live_changed.append(k)
        if "brain_provider" in live_changed:
            live.pop("brain_model", None)                     # a new provider starts on its default model
        LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LIVE_FILE.write_text(json.dumps(live, indent=2), encoding="utf-8")
    for group, path in (("bot", cfgfile.BOT_ENV), ("voice", cfgfile.VOICE_ENV), ("root", cfgfile.ROOT_ENV)):
        wanted = {k: cfgfile.clean(str(v)) for k, v in (body.get(group) or {}).items() if k in SETTINGS[group]}
        if "TOOL_CREATORS" in wanted and not all(p.strip().isdigit() for p in wanted["TOOL_CREATORS"].split(",") if p.strip()):
            return web.json_response({"ok": False, "error": "TOOL_CREATORS is a comma-separated list of Discord user ids"}, status=400)
        if wanted and path.exists():
            changed += [f"{group}:{k}" for k in cfgfile.update_env(path, wanted)]
    if changed:
        cfgfile.RESTART_MARKER.write_text("settings changed: " + ", ".join(changed) + "\n", encoding="utf-8")
    return web.json_response({"ok": True, "changed": changed, "live_changed": live_changed, "needs_restart": bool(changed)})


@guarded
async def vault_export(req):
    if not cfgfile.VAULT_PATH.exists():
        return web.json_response({"error": "no vault file yet"}, status=404)
    return web.Response(body=cfgfile.VAULT_PATH.read_bytes(), content_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="vault-{time.strftime("%Y%m%d")}.enc"'})


@guarded
async def vault_import(req):
    """Restore a vault file: it must open with the passphrase given; that passphrase then becomes this install's."""
    import base64
    body = await req.json()
    passphrase, content = cfgfile.clean(body.get("passphrase")), body.get("content") or ""
    try:
        raw = base64.b64decode(content.split(",", 1)[-1])
    except Exception:  # noqa
        return web.json_response({"ok": False, "error": "could not read the file"}, status=400)
    tmp = cfgfile.VAULT_PATH.with_name("vault.import.tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)
    try:
        n = len(vaultlib.Vault(tmp, passphrase=passphrase).load())
    except vaultlib.VaultError as e:
        tmp.unlink(missing_ok=True)
        return web.json_response({"ok": False, "error": f"that file does not open with that passphrase ({e})"}, status=403)
    cfgfile.backup_existing()
    os.replace(tmp, cfgfile.VAULT_PATH)
    cfgfile.update_env(cfgfile.BOT_ENV, {"VAULT_PASSPHRASE": passphrase})
    cfgfile.RESTART_MARKER.write_text("vault imported\n", encoding="utf-8")
    return web.json_response({"ok": True, "count": n, "needs_restart": True})


@guarded
async def check(req):
    what, body = req.match_info["what"], await req.json()
    if SKIP_CHECKS:
        fake = {"discord": {"ok": True, "name": "test-bot", "id": "0", "avatar": "", "app_id": "123456789012345678", "message_content": True,
                            "invite": cfgfile.invite_url("123456789012345678")},
                "anthropic": {"ok": True}, "openai": {"ok": True}, "deepseek": {"ok": True}, "eleven": {"ok": True, "voices": []},
                "owners": [{"id": i, "ok": True, "name": "tester"} for i in body.get("ids", [])],
                "guilds": {"ok": True, "guilds": [{"id": "1", "name": "Test Server", "icon": ""}]},
                "members": {"ok": True, "members": [{"id": "111111111111111111", "name": "Tester", "username": "tester", "avatar": ""},
                                                     {"id": "222222222222222222", "name": "Testina", "username": "testina", "avatar": ""}]
                            if body.get("query") else []}}
        return web.json_response(fake[what])
    if what == "guilds":
        return web.json_response(await check_guilds(cfgfile.clean(body.get("token"))))
    if what == "members":
        gid, q = cfgfile.clean(body.get("guild_id")), cfgfile.clean(body.get("query"))[:40]
        if not gid.isdigit() or not q:
            return web.json_response({"ok": False, "error": "guild_id and query required", "members": []})
        return web.json_response(await search_members(cfgfile.clean(body.get("token")), gid, q))
    if what == "discord":
        return web.json_response(await check_discord(cfgfile.clean(body.get("token"))))
    if what == "anthropic":
        return web.json_response(await check_anthropic(cfgfile.clean(body.get("key"))))
    if what == "openai":
        return web.json_response(await check_openai_compat(cfgfile.clean(body.get("key")), "https://api.openai.com/v1", "OpenAI"))
    if what == "deepseek":
        return web.json_response(await check_openai_compat(cfgfile.clean(body.get("key")), "https://api.deepseek.com/v1", "DeepSeek"))
    if what == "eleven":
        return web.json_response(await check_eleven(cfgfile.clean(body.get("key"))))
    if what == "owners":
        ids = [i for i in (cfgfile.clean(x) for x in body.get("ids", [])) if i.isdigit()]
        return web.json_response(await check_owners(cfgfile.clean(body.get("token")), ids))
    return web.json_response({"error": "unknown check"}, status=404)


@guarded
async def write(req):
    body = await req.json()
    cfg = {k: cfgfile.clean(str(body.get(k) or "")) for k in ("token", "anthropic", "openai", "deepseek", "owners", "name", "eleven", "voice", "auto", "kit", "app_id", "kit_remote", "tz", "passphrase")}
    if not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)*", cfg["tz"] or ""):
        cfg["tz"] = "UTC"
    cfg["fresh_secrets"] = bool(body.get("fresh_secrets"))
    reconfig = cfgfile.config_exists()
    problems = [] if reconfig else cfgfile.validate(cfg)      # on a reconfigure a blank token/key means keep; write_files validates after filling
    if reconfig and not body.get("replace"):
        problems.append("a configuration already exists on this PC; tick 'replace the existing configuration' to overwrite it "
                        "(it is backed up first, and the vault passphrase is kept)")
    kits = cfgfile.existing_kits()
    if kits.get(cfg["kit"]) and not body.get("keep_kit"):
        problems.append(f"kits/{cfg['kit']} already has {kits[cfg['kit']]} tools; confirm it is this household's kit or pick another name")
    if problems:
        return web.json_response({"ok": False, "problems": problems}, status=400)
    if not SKIP_CHECKS and cfg["token"]:
        d = await check_discord(cfg["token"])
        if not d.get("ok"):
            return web.json_response({"ok": False, "problems": [d.get("error", "Discord token check failed")]}, status=400)
        cfg["app_id"] = cfg["app_id"] or d.get("app_id", "")
    try:
        out = cfgfile.write_files(cfg)
    except ValueError as e:
        return web.json_response({"ok": False, "problems": str(e).split("; ")}, status=400)
    asyncio.get_running_loop().call_later(1.5, DONE.set)       # let the response leave, then let the installer continue
    return web.json_response({"ok": True, **out})


@guarded
async def cancel(req):
    """Setup/one-off console: finish or abort ends the server so the installer continues. The persistent console just stays."""
    if not PERSISTENT:
        asyncio.get_running_loop().call_later(0.5, DONE.set)
    marker = cfgfile.RESTART_MARKER.read_text(encoding="utf-8").strip() if cfgfile.RESTART_MARKER.exists() else ""
    return web.json_response({"ok": True, "restart_needed": marker})


@guarded
async def restart_ack(req):
    """The owner ran `docker compose up -d` themselves: clear the marker."""
    cfgfile.RESTART_MARKER.unlink(missing_ok=True)
    return web.json_response({"ok": True})


def build_app():
    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/setup", page)
    app.router.add_get("/console", page)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/restart-done", restart_ack)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/check/{what}", check)
    app.router.add_post("/api/write", write)
    app.router.add_post("/api/cancel", cancel)
    app.router.add_post("/api/done", cancel)                 # the console's "finish": same shutdown, the wrapper then restarts if needed
    app.router.add_get("/api/vault", vault_list)
    app.router.add_post("/api/vault/set", vault_set)
    app.router.add_post("/api/vault/delete", vault_delete)
    app.router.add_post("/api/vault/reveal", vault_reveal)
    app.router.add_get("/api/vault/export", vault_export)
    app.router.add_post("/api/vault/import", vault_import)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_post("/api/settings", settings_set)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True, "mode": MODE, "persistent": PERSISTENT}))
    assets = Path(__file__).parent / "assets"                # the console's sounds; small, and nothing secret in them
    if assets.is_dir():
        app.router.add_static("/assets/", assets, show_index=False)
    return app


async def serve_console(port=PORT):
    """The always-on console, started by forge.py inside the dev container: console mode, persistent, passphrase login.
    Compose publishes the port on 127.0.0.1 only, so it is reachable from the owner's PC and nowhere else."""
    global MODE, PERSISTENT
    MODE, PERSISTENT = "console", True
    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"console listening on :{port} (published on the PC as http://127.0.0.1:{port}/console; vault passphrase to log in)", flush=True)
    while True:
        await asyncio.sleep(3600)


async def main():
    if not cfgfile.LAB.exists():
        sys.exit("the repo has to be mounted at /lab - run this through install.sh or install.ps1")
    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()      # the installer publishes this port on 127.0.0.1 only
    print(f"SETUP_URL=http://localhost:{PORT}/setup?key={KEY}", flush=True)
    print(f"[{MODE}] open that link in a browser on this PC; this waits until you finish there (Ctrl+C to abort)", flush=True)
    await DONE.wait()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ncancelled")
        sys.exit(130)
