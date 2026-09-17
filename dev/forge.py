"""
forge: the dev box builds tools for the bots, with Claude Code.

A bot that needs a tool it can't write in one shot posts a brief here (POST /forge, compose network only).
The forge gives the brief to a headless Claude Code in a fresh work directory under /root/work, where it
writes the tool, runs it against the real thing with a harness that behaves like the bot, iterates, and
leaves a RESULT.json. The forge then checks the pair the same way the bot would, moves it into the bot's
/data/tools in one rename per file (the bot rescans that folder every turn, so nothing half-written is ever
seen), commits it if the kit folder is a git repo of its own, and returns a short summary the bot relays in chat.

What Claude Code may do here is fixed by an allowlist, not by a permission bypass: read anywhere, write only
inside the work directory, and run two commands - the harness and `pip install --user`. Anything else it tries
is refused by Claude Code itself. It probes devices and services through the tool it is writing, via the
harness, which is also the only way the bot will ever call it.

  POST /forge   X-Internal-Token: <INTERNAL_TOKEN>
                {"name", "brief", "inputs", "secrets": [...], "example_call": {...}, "requested_by", "bot": "example-bot",
                 "kit": "<household>", "available_secrets": [names]}
The tool lands in /bots/<bot>/kits/<kit>/, the folder compose mounts at the bot's /data/tools.
  GET  /health

Env: INTERNAL_TOKEN (else read from /bots/example-bot/.env, so the bots and the forge share one), FORGE_PORT (8791),
     FORGE_WORK (/root/work), FORGE_TIMEOUT (360s for one build), FORGE_MAX_TURNS (60), FORGE_KEEP_DAYS (7).
Without a token the forge idles and the dev box is just a dev box, as before.
Claude Code authenticates with the dev box's login if someone has logged in here (it persists in the /root volume),
else with ANTHROPIC_API_KEY from the environment or example-bot's .env - the bot's own key, billed per token.

Work directories are kept FORGE_KEEP_DAYS so a bad tool can be understood: the brief, the prompt, Claude Code's
output, RESULT.json and the previous version of a replaced tool are all there.
"""
import asyncio, json, os, re, shutil, subprocess, sys, time, traceback
from pathlib import Path

PORT = int(os.environ.get("FORGE_PORT", "8791"))
WORK = Path(os.environ.get("FORGE_WORK", "/root/work"))
BOTS = Path("/bots")
LAB = Path("/lab")                                    # the repo root, when mounted; enables the git commit
CLAUDE_TIMEOUT = int(os.environ.get("FORGE_TIMEOUT", "360"))
MAX_TURNS = int(os.environ.get("FORGE_MAX_TURNS", "60"))
KEEP_DAYS = int(os.environ.get("FORGE_KEEP_DAYS", "7"))
TOOL_TIMEOUT = 30                                     # what the bot gives one call; the harness matches it
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")
BOT_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

# Everything Claude Code is allowed to do while building. Write/Edit are limited to the working directory by
# Claude Code; Bash is limited to these two command shapes.
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash(python harness.py *)", "Bash(pip install --user *)"]

LOCK = asyncio.Lock()                                 # one build at a time; a second request waits


def env_value(name):
    """From the forge's own environment, else from example-bot's .env, so the bots and the forge share one setting."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env = BOTS / "example-bot" / ".env"
    if env.exists():
        m = re.search(rf"^{name}=(.+)$", env.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""


def token():
    return env_value("INTERNAL_TOKEN")


BRAIN = os.environ.get("BRAIN_URL", "http://example-bot:8790").replace("/ask", "").rstrip("/")


def brain_secret(name):
    """A bootstrap secret from the bot's vault, through its internal /secrets route (compose network, shared token)."""
    import urllib.request
    tok = token()
    if not tok:
        return ""
    try:
        req = urllib.request.Request(f"{BRAIN}/secrets?names={name}", headers={"X-Internal-Token": tok})
        with urllib.request.urlopen(req, timeout=5) as r:
            return (json.loads(r.read().decode()).get("secrets") or {}).get(name, "")
    except Exception:  # noqa - the bot may be down; callers fall back
        return ""


def claude_auth(env):
    """How the headless Claude Code authenticates: the dev box's own login when someone has run `claude` and logged in
    here (the credentials live in the /root volume), else the bot's API key from its vault (via the brain) or from the
    environment, billed per token like the bot itself."""
    if (Path.home() / ".claude" / ".credentials.json").exists():
        env.pop("ANTHROPIC_API_KEY", None)
        return "dev box login"
    key = brain_secret("ANTHROPIC_API_KEY")
    if key:
        env["ANTHROPIC_API_KEY"] = key
        return "the bot's API key (vault)"
    key = env_value("ANTHROPIC_API_KEY")
    if key:
        env["ANTHROPIC_API_KEY"] = key
        return "the bot's API key (env)"
    return "none - add ANTHROPIC_API_KEY in the console, or log in with `docker compose exec dev claude`"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- what Claude Code gets

HARNESS = '''"""Run a tool the way the bot does: a fresh python, JSON args on stdin, JSON out, %(timeout)ss limit.
  python harness.py NAME.py --selftest              import only, like the bot's check at creation
  python harness.py NAME.py '{"arg": "value"}'      call run(**args)
"""
import importlib.util, json, subprocess, sys, traceback

TIMEOUT = %(timeout)s


def child(path, args, selftest):
    real, sys.stdout = sys.stdout, sys.stderr          # a stray print() must not corrupt the result
    try:
        spec = importlib.util.spec_from_file_location("tool_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "run", None)):
            raise TypeError("no top-level run() after import")
        out = {"ok": True, "result": "imports cleanly" if selftest else mod.run(**args)}
    except Exception:
        out = {"ok": False, "error": traceback.format_exc(limit=3).strip()}
    sys.stdout = real
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    if "--child" in sys.argv:
        child(sys.argv[2], json.loads(sys.stdin.read() or "{}"), "--selftest" in sys.argv)
        sys.exit(0)
    path, selftest = sys.argv[1], "--selftest" in sys.argv
    args = next((a for a in sys.argv[2:] if a.startswith("{")), "{}")
    try:
        p = subprocess.run([sys.executable, __file__, "--child", path] + (["--selftest"] if selftest else []),
                           input=args, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": f"ran longer than {TIMEOUT}s - the bot would have stopped it"}))
        sys.exit(1)
    if p.stderr.strip():
        print(p.stderr.strip()[-3000:], file=sys.stderr)
    print(p.stdout.strip() or json.dumps({"ok": False, "error": f"no output, exit {p.returncode}"}))
    sys.exit(0 if '"ok": true' in p.stdout else 1)
''' % {"timeout": TOOL_TIMEOUT}

PROMPT = """You are building one tool for a Discord bot that extends itself. Work only in this directory ({work}).
The forge service that started you will validate the result and install it; you do not copy anything anywhere.

## The request
Tool name: {name}
Requested by: {requested_by}
Brief: {brief}
Inputs: {inputs}
Example call the bot wants to make as soon as the tool exists: {example_call}
Secrets it may declare (names only - the values live in the bot's vault and you do not have them): {secrets}
Secret names already in the vault: {available_secrets}

## The contract
A tool is two files. Create both here:
- `{name}.py` defines a top-level `run(**kwargs)` whose keyword arguments are the spec's properties. It RETURNS a
  JSON-serialisable value (a dict is best); printed output is discarded. Module level is imports and constants only;
  do the work inside run(). Raise an exception with a clear message on bad input. Every call runs in a fresh python
  with a {tool_timeout}s limit: no background threads, nothing kept in memory between calls. State the tool must keep goes
  under /data/{name}/ (create it) - only /data survives in the bot's container.
- `{name}.json` is {{"name": "{name}", "description": ..., "input_schema": ..., "secrets": [...]}}. The description is the
  ONLY thing the calling model reads to decide when and how to use the tool: say what it does, when to use it, what it
  returns, units and ranges. input_schema is JSON Schema with type object and described properties. secrets lists
  the environment-variable names the tool reads.
Read two or three of the existing tools in {tools_dir} first for the house style: web_fetch, and anything related to
this request. They are read-only for you.

## What you can do here
You can read files anywhere, write only in this directory, and run exactly two commands: `python harness.py ...`
and `pip install --user ...`. Everything else is refused, so investigate devices and services through the tool you
are writing, by calling it with the harness - that is also the only way the bot will ever run it.

## Where it will run
- The bot's own container: Debian 12 slim, Python 3.12, root, on the household LAN, with internet.
- Importable: the standard library, httpx, anthropic, and everything in {pylibs} (packages the bot installed for
  itself). PYTHONUSERBASE already points there for you, so `pip install --user <pkg>` lands in the same place and
  the bot can import it too. Record anything you install in RESULT.json.
- apt packages are NOT shared between this box and the bot's. If the tool needs a system command that a slim Debian
  lacks, prefer a pure-Python route; if there is none, list the package in RESULT.json apt_needed.
- Secrets arrive as environment variables named in the spec's secrets. Read them with os.environ inside run(),
  fail with a clear message when one is missing. Never hard-code a value, never write one to disk, never print one.

## Test it
`python harness.py {name}.py --selftest` imports it exactly as the bot does at creation.
`python harness.py {name}.py '{{"arg": "value"}}'` calls run() in a fresh process with the same {tool_timeout}s limit.
You are on the same LAN as the bot, so test against the real device or service when you can, starting with the
example call. Be careful with actions that change the world (switching things off, sending messages, deleting):
validate arguments and dry-run those unless the example call clearly asks for the real thing. If a live test needs a
secret you do not have, test everything around it and say so.

## Budget
You have about {budget} minutes of wall clock in total; at the limit the forge stops you and nothing is kept. Use the
first minutes to get the example call working end to end, then harden. If a data source fights you (blocks, captchas,
empty or javascript-only pages) after two attempts, switch to another source or a public API, or write RESULT.json
with status failed and say exactly why - a clear failure in three minutes beats a timeout in six.

## When you are done
Write `RESULT.json` here:
{{"status": "ready" or "failed",
 "summary": "one line: what the tool does",
 "tested": "what you actually verified, one or two lines",
 "notes_for_model": "what the bot must know to call it well: units, ranges, quirks, and if a live test could not run, how to tell whether it works",
 "pip_installed": [], "apt_needed": [],
 "failure": "why, if failed"}}
Keep every field short; the bot relays this to a person in chat. Nobody is watching, so do not ask questions: make a
reasonable choice and note it in notes_for_model.
"""


# ---------------------------------------------------------------- the job

def harness_run(work, path, selftest=False, args=None, env=None):
    argv = [sys.executable, str(work / "harness.py"), str(path)] + (["--selftest"] if selftest else [json.dumps(args or {})])
    p = subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT + 10, cwd=work, env=env)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (p.stderr or p.stdout).strip()[-600:] or f"no output, exit {p.returncode}"}


def check_pair(work, name, env):
    """The same checks the bot's create_tool makes, before anything is installed. Returns (spec, code) or raises."""
    spec_file, code_file = work / f"{name}.json", work / f"{name}.py"
    if not code_file.exists() or not spec_file.exists():
        raise RuntimeError(f"expected {name}.py and {name}.json in the work directory; have {sorted(p.name for p in work.glob(name + '.*'))}")
    code = code_file.read_text(encoding="utf-8")
    if not re.search(r"^def run\(", code, re.M):
        raise RuntimeError("the code has no top-level run()")
    compile(code, f"{name}.py", "exec")
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except ValueError as e:
        raise RuntimeError(f"{name}.json is not valid JSON: {e}")
    if spec.get("name") != name:
        raise RuntimeError(f"the spec's name is {spec.get('name')!r}, not {name!r}")
    if not isinstance(spec.get("description"), str) or len(spec["description"]) < 20:
        raise RuntimeError("the spec needs a real description")
    if not isinstance(spec.get("input_schema"), dict) or spec["input_schema"].get("type") != "object":
        raise RuntimeError("input_schema must be a JSON Schema object with type object")
    secrets = [s for s in spec.get("secrets", []) if isinstance(s, str) and SECRET_NAME_RE.match(s)]
    spec = {"name": name, "description": spec["description"], "input_schema": spec["input_schema"], "secrets": secrets}
    r = harness_run(work, code_file, selftest=True, env=env)
    if not r.get("ok"):
        raise RuntimeError(f"it does not import: {r.get('error')}")
    return spec, code


def install_pair(work, name, spec, code, tools_dir):
    """Into the bot's tools folder, whole or not at all: write beside, then rename. Code first, so a spec the bot
    picks up always has its code. The previous version of a replaced tool is kept in the work directory."""
    tools_dir.mkdir(parents=True, exist_ok=True)
    prev = work / "previous"
    for ext in (".py", ".json"):
        old = tools_dir / f"{name}{ext}"
        if old.exists():
            prev.mkdir(exist_ok=True)
            shutil.copy2(old, prev / old.name)
    for ext, body in ((".py", code), (".json", json.dumps(spec, indent=2) + "\n")):
        tmp = tools_dir / f".{name}{ext}.forge-tmp"
        tmp.write_text(body, encoding="utf-8", newline="\n")
        os.replace(tmp, tools_dir / f"{name}{ext}")
    return prev.exists()


def git_commit(kit_dir, name, result, requested_by):
    """The kit is its own git repo when the household chose to back it up (setup makes it one); commit the whole kit there.
    The code repo never sees kits. Best effort: a failure here never fails the build."""
    if not (kit_dir / ".git").exists():
        return False, "kit is not a git repo (setup can make it one; the tool is installed regardless)"
    base = ["git", "-C", str(kit_dir), "-c", "safe.directory=*", "-c", "core.filemode=false", "-c", "core.autocrlf=false",
            "-c", "user.name=forge", "-c", "user.email=forge@bot-dev"]
    msg = (f"{name}: {result.get('summary', '')}\n\nBuilt by the forge for {requested_by or 'unknown'}. "
           f"Tested: {result.get('tested', 'not recorded')}\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>\n")
    try:
        # The whole kit, not just this pair: the seeded starters and anything the bot wrote itself with create_tool are
        # otherwise never committed, since the bot's container has no git.
        subprocess.run(base + ["add", "-A", "."], check=True, capture_output=True, text=True, timeout=60)
        p = subprocess.run(base + ["commit", "-q", "-m", msg], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout).strip()[-300:]
        sha = subprocess.run(base + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30).stdout.strip()
        return True, sha
    except Exception as e:  # noqa
        return False, f"{type(e).__name__}: {str(e)[-300:]}"


def cleanup():
    if not WORK.exists():
        return
    cutoff = time.time() - KEEP_DAYS * 86400
    for d in WORK.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def build(job):
    """One build, start to finish, in a worker thread. Always returns a dict for the bot."""
    name, bot, kit = job["name"], job["bot"], job["kit"]
    bot_dir = BOTS / bot
    tools_dir, pylibs = bot_dir / "kits" / kit, bot_dir / "data" / "pylibs"   # the kit is what compose mounts at /data/tools
    work = WORK / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}"
    work.mkdir(parents=True)
    started = time.time()
    (work / "harness.py").write_text(HARNESS, encoding="utf-8")
    (work / "BRIEF.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    repair = job.get("mode") == "repair" and job.get("existing_code")
    if repair:                                               # the broken tool is the starting point, already in the work directory
        (work / f"{name}.py").write_text(job["existing_code"], encoding="utf-8")
        (work / f"{name}.json").write_text(json.dumps(job.get("existing_spec") or {"name": name}, indent=2), encoding="utf-8")
    prompt = PROMPT.format(work=work, name=name, requested_by=job.get("requested_by") or "someone in Discord",
                           brief=job["brief"], inputs=job.get("inputs") or ("keep the current inputs unless they are the problem" if repair else "your call - keep them minimal"),
                           example_call=json.dumps(job.get("example_call") or {}),
                           secrets=", ".join(job.get("secrets") or []) or ("as declared in the existing spec" if repair else "none"),
                           available_secrets=", ".join(job.get("available_secrets") or []) or "none",
                           tools_dir=tools_dir, pylibs=pylibs, tool_timeout=TOOL_TIMEOUT, budget=max(2, CLAUDE_TIMEOUT // 60 - 1))
    if repair:
        prompt = (f"## REPAIR, not a new build\n`{name}.py` and `{name}.json` in this directory are the tool as it is now, and it is broken. "
                  f"The failure the bot saw:\n```\n{job.get('error') or '(not recorded)'}\n```\nFix the tool. Keep its name, and keep its "
                  f"inputs and outputs unless they are the cause; other tools and the bot's habits depend on them. Reproduce the failure "
                  f"with the harness first if you can, then fix, then re-test with the example call. In RESULT.json say what was wrong "
                  f"and what changed.\n\n") + prompt
    (work / "PROMPT.md").write_text(prompt, encoding="utf-8")
    pylibs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONUSERBASE": str(pylibs), "PYTHONDONTWRITEBYTECODE": "1"}
    auth = claude_auth(env)

    out = {"ok": False, "status": "failed", "name": name, "work_dir": str(work), "mode": job.get("mode", "build")}
    argv = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", str(MAX_TURNS),
            "--allowedTools", *ALLOWED_TOOLS]
    log(f"building `{name}` for {bot} in {work.name} (auth: {auth})")
    try:
        p = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
        (work / "claude-output.json").write_text(p.stdout, encoding="utf-8")
        (work / "claude-stderr.log").write_text(p.stderr, encoding="utf-8")
        try:
            meta = json.loads(p.stdout)
            out["cost_usd"] = round(float(meta.get("total_cost_usd") or 0), 3)
            out["turns"] = meta.get("num_turns")
        except ValueError:
            meta = {}
        if p.returncode != 0 and not (work / "RESULT.json").exists():
            said = (meta.get("result") or "").strip() if isinstance(meta, dict) else ""
            out["failure"] = (f"Claude Code could not run: {said[:300]}" if said
                              else f"Claude Code exited {p.returncode}: {(p.stderr or p.stdout).strip()[-400:]}")
            if "log" in said.lower() and "in" in said.lower():
                out["failure"] += " - the dev box's Claude Code login is missing or expired; the owner has to run `docker compose exec dev claude` once and log in"
            return out
    except subprocess.TimeoutExpired:
        out["failure"] = (f"the build ran longer than {CLAUDE_TIMEOUT}s and was stopped before it finished - the job is probably "
                          "harder than it looks (hostile data source, no usable API); a narrower brief or a different source may work")
        return out
    finally:
        out["seconds"] = round(time.time() - started)
        if out.get("failure"):
            log(f"`{name}` failed after {out['seconds']}s: {out['failure'][:200]}")

    try:
        result = json.loads((work / "RESULT.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"status": "failed", "failure": "Claude Code finished without a usable RESULT.json",
                  "notes_for_model": (meta.get("result") or "")[-400:] if isinstance(meta, dict) else ""}
    out.update({k: result.get(k) for k in ("summary", "tested", "notes_for_model", "pip_installed", "apt_needed", "failure")})
    if result.get("status") != "ready":
        out["failure"] = out.get("failure") or "Claude Code reported the build as failed"
        log(f"`{name}` failed after {out['seconds']}s: {out['failure'][:200]}")
        return out

    try:
        spec, code = check_pair(work, name, env)
    except Exception as e:  # noqa - anything here is a build failure, reported not raised
        out["failure"] = f"built, but rejected before install: {e}"
        log(f"`{name}` rejected after {out['seconds']}s: {e}")
        return out
    out["replaced"] = install_pair(work, name, spec, code, tools_dir)
    out["ok"], out["status"], out["mode"] = True, "ready", job.get("mode", "build")
    out["committed"], out["commit"] = git_commit(tools_dir, name, result, job.get("requested_by"))
    log(f"`{name}` ready in {out['seconds']}s, ${out.get('cost_usd', '?')}, {out['turns']} turns, commit={out['commit']}")
    return out


# ---------------------------------------------------------------- the endpoint

async def main():
    from aiohttp import web
    tok = token()
    if not tok:
        log("no INTERNAL_TOKEN (env or /bots/example-bot/.env) - forge idle; the dev box is still a dev box")
        while True:
            await asyncio.sleep(3600)
    WORK.mkdir(parents=True, exist_ok=True)
    cleanup()

    async def handle_forge(req):
        if req.headers.get("X-Internal-Token") != tok:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            job = await req.json()
        except Exception:  # noqa
            return web.json_response({"error": "bad json"}, status=400)
        name, bot, kit = str(job.get("name") or ""), str(job.get("bot") or "example-bot"), str(job.get("kit") or "default")
        if not NAME_RE.match(name):
            return web.json_response({"error": "name must be snake_case, 3-48 chars, starting with a letter"}, status=400)
        if not BOT_RE.match(bot) or not (BOTS / bot / "Dockerfile").exists():
            return web.json_response({"error": f"no bot named {bot} under /bots"}, status=400)
        if not BOT_RE.match(kit) or not (BOTS / bot / "kits" / kit).is_dir():
            return web.json_response({"error": f"no kit named {kit} under /bots/{bot}/kits"}, status=400)
        if not str(job.get("brief") or "").strip():
            return web.json_response({"error": "brief required"}, status=400)
        job = {"name": name, "bot": bot, "kit": kit, "brief": str(job["brief"]), "inputs": str(job.get("inputs") or ""),
               "secrets": [s for s in job.get("secrets") or [] if isinstance(s, str) and SECRET_NAME_RE.match(s)],
               "example_call": job.get("example_call") if isinstance(job.get("example_call"), dict) else {},
               "requested_by": str(job.get("requested_by") or ""),
               "available_secrets": [s for s in job.get("available_secrets") or [] if isinstance(s, str)][:50],
               "mode": "repair" if job.get("mode") == "repair" else "build",
               "existing_code": str(job.get("existing_code") or "")[:60000], "error": str(job.get("error") or "")[:4000],
               "existing_spec": job.get("existing_spec") if isinstance(job.get("existing_spec"), dict) else {}}
        async with LOCK:
            try:
                out = await asyncio.to_thread(build, job)
            except Exception:  # noqa
                out = {"ok": False, "status": "failed", "name": name, "failure": traceback.format_exc(limit=2).strip()[-600:]}
                log(f"build of `{name}` crashed: {out['failure']}")
            cleanup()
        return web.json_response(out)

    async def handle_health(req):
        return web.json_response({"ok": True, "busy": LOCK.locked(), "work": str(WORK),
                                  "kept": len([d for d in WORK.iterdir() if d.is_dir()])})

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/forge", handle_forge)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log(f"forge listening on :{PORT} (compose network only); work in {WORK}, kept {KEEP_DAYS} days; "
        f"Claude Code auth: {claude_auth({})}")
    try:                                                     # the owner's always-on console rides in the same process
        sys.path.insert(0, str(Path(__file__).parent))
        import setup_web
        asyncio.create_task(setup_web.serve_console(int(os.environ.get("CONSOLE_PORT", "8792"))))
    except Exception as e:  # noqa - the forge must not die because the console cannot start
        log(f"console not started: {type(e).__name__}: {e}")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
