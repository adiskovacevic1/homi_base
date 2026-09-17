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
import asyncio, os, re, secrets, sys
from pathlib import Path

import aiohttp
from aiohttp import web

import setup as cfgfile

PORT = int(os.environ.get("SETUP_PORT", "8792"))
KEY = os.environ.get("SETUP_KEY") or secrets.token_urlsafe(16)
SKIP_CHECKS = os.environ.get("SETUP_SKIP_CHECKS") == "1"
PAGE = (Path(__file__).parent / "setup.html").read_text(encoding="utf-8")
DISCORD = "https://discord.com/api/v10"
MSG_CONTENT_FLAGS = (1 << 18) | (1 << 19)        # GATEWAY_MESSAGE_CONTENT, GATEWAY_MESSAGE_CONTENT_LIMITED
DONE = asyncio.Event()


def authorized(req):
    return req.query.get("key") == KEY or req.headers.get("X-Setup-Key") == KEY


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


async def check_owners(token, ids):
    out = []
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as s:
        for i in ids[:10]:
            st, u = await fetch_json(s, "GET", f"{DISCORD}/users/{i}")
            out.append({"id": i, "ok": st == 200, "name": (u.get("global_name") or u.get("username")) if st == 200 else "not found"})
    return out


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
            return web.json_response({"error": "bad or missing setup key - use the link the installer printed"}, status=403)
        try:
            return await handler(req)
        except aiohttp.ClientError as e:
            return web.json_response({"ok": False, "error": f"could not reach the service: {type(e).__name__}", "offline": True})
        except Exception as e:  # noqa - shown on the page, never a blank failure
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}, status=500)
    return inner


@guarded
async def page(req):
    return web.Response(text=PAGE.replace("__KEY__", KEY), content_type="text/html")


@guarded
async def state(req):
    return web.json_response({"kits": cfgfile.existing_kits(), "config_exists": cfgfile.config_exists(), "skip_checks": SKIP_CHECKS})


@guarded
async def check(req):
    what, body = req.match_info["what"], await req.json()
    if SKIP_CHECKS:
        fake = {"discord": {"ok": True, "name": "test-bot", "id": "0", "avatar": "", "app_id": "123456789012345678", "message_content": True,
                            "invite": cfgfile.invite_url("123456789012345678")},
                "anthropic": {"ok": True}, "eleven": {"ok": True, "voices": []},
                "owners": [{"id": i, "ok": True, "name": "tester"} for i in body.get("ids", [])]}
        return web.json_response(fake[what])
    if what == "discord":
        return web.json_response(await check_discord(cfgfile.clean(body.get("token"))))
    if what == "anthropic":
        return web.json_response(await check_anthropic(cfgfile.clean(body.get("key"))))
    if what == "eleven":
        return web.json_response(await check_eleven(cfgfile.clean(body.get("key"))))
    if what == "owners":
        ids = [i for i in (cfgfile.clean(x) for x in body.get("ids", [])) if i.isdigit()]
        return web.json_response(await check_owners(cfgfile.clean(body.get("token")), ids))
    return web.json_response({"error": "unknown check"}, status=404)


@guarded
async def write(req):
    body = await req.json()
    cfg = {k: cfgfile.clean(str(body.get(k) or "")) for k in ("token", "anthropic", "owners", "name", "eleven", "voice", "auto", "kit", "app_id", "kit_remote", "tz")}
    if not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)*", cfg["tz"] or ""):
        cfg["tz"] = "UTC"
    problems = cfgfile.validate(cfg)
    kits = cfgfile.existing_kits()
    if kits.get(cfg["kit"]) and not body.get("keep_kit"):
        problems.append(f"kits/{cfg['kit']} already has {kits[cfg['kit']]} tools; confirm it is this household's kit or pick another name")
    if problems:
        return web.json_response({"ok": False, "problems": problems}, status=400)
    if not SKIP_CHECKS:
        d = await check_discord(cfg["token"])
        if not d.get("ok"):
            return web.json_response({"ok": False, "problems": [d.get("error", "Discord token check failed")]}, status=400)
        cfg["app_id"] = cfg["app_id"] or d.get("app_id", "")
    out = cfgfile.write_files(cfg)
    asyncio.get_running_loop().call_later(1.5, DONE.set)       # let the response leave, then let the installer continue
    return web.json_response({"ok": True, **out})


@guarded
async def cancel(req):
    asyncio.get_running_loop().call_later(0.5, DONE.set)
    return web.json_response({"ok": True})


async def main():
    if not cfgfile.LAB.exists():
        sys.exit("the repo has to be mounted at /lab - run this through install.sh or install.ps1")
    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/setup", page)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/check/{what}", check)
    app.router.add_post("/api/write", write)
    app.router.add_post("/api/cancel", cancel)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()      # the installer publishes this port on 127.0.0.1 only
    print(f"SETUP_URL=http://localhost:{PORT}/setup?key={KEY}", flush=True)
    print("open that link in a browser on this PC; this waits until the form is submitted (Ctrl+C to abort)", flush=True)
    await DONE.wait()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ncancelled")
        sys.exit(130)
