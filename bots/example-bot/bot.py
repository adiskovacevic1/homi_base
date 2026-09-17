"""
example-bot: a Discord + Claude bot that writes its own tools, as a template for new bots.

  @mention it (or DM it) and it answers with Claude, keeping a short per-channel history.
  If answering needs something it can't do, it writes a tool for itself, calls it, and keeps it.
  Attachments: images and PDFs are shown to Claude, text-like files are read inline, anything else is
  saved under /data/uploads and the path is passed on so a tool can act on it.

Tools live in /data/tools (a Docker volume, so they survive restarts and are readable from Windows):
  <name>.json   the spec sent to the model     <name>.py   the code, defining run(**kwargs)
Generated code is never imported by the bot process; each call runs in a separate, time-limited python.

Env (from .env):
  DISCORD_TOKEN       Discord bot token. Missing -> the container idles and logs why, so `compose up` is clean.
  ANTHROPIC_API_KEY   Anthropic key.
  BOT_MODEL           default claude-opus-5
  BOT_SYSTEM          system prompt (optional)
  TOOL_TIMEOUT        seconds one tool call may run, default 30
  TOOL_CREATORS       comma-separated Discord user IDs allowed to write tools, run shell and install
                      software. Empty = everyone who can talk to it, which is rarely what you want.
  OPEN_CHANNELS       channels it answers in without being @mentioned, by name or ID (e.g. bot-chat)
  VAULT_KEY           Fernet key for the secret vault at /data/secrets_manager/vault.enc. Lives only here, so tools
                      (which can read all of /data) cannot decrypt the vault. Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  INTERNAL_TOKEN      shared secret for the internal /ask endpoint other containers (voice-bot) call; unset = endpoint off
  BRAIN_PORT          port for that endpoint on the compose network, default 8790 (never published to the host)
  FORGE_URL           the dev box's tool builder (dev/forge.py), default http://bot-dev:8791; empty = no request_tool.
                      Needs INTERNAL_TOKEN too (the forge reads the same one from this bot's .env).
  FORGE_TIMEOUT       seconds to wait for one build, default 480
  BOT_NAME            this bot's folder name under bots/, default example-bot
  KIT                 the household's tool kit: bots/<BOT_NAME>/kits/<KIT>/ is what compose mounts at /data/tools and where the
                      forge installs. setup.py writes the same name to the root .env for compose. Default "default".

Test without Discord:
  python bot.py --ask "what's the 40th fibonacci number?"
  python bot.py --tools
"""
import asyncio, base64, contextlib, importlib.util, json, os, re, shutil, subprocess, sys, threading, time, traceback
from collections import defaultdict, deque
from pathlib import Path

import anthropic
import httpx

MODEL = os.environ.get("BOT_MODEL", "claude-opus-5")
SYSTEM = os.environ.get("BOT_SYSTEM", "You are a concise, friendly assistant living in a Discord server. Answer briefly.")
HISTORY_TURNS = 10          # user+assistant messages kept per channel
MAX_REPLY = 1900            # Discord hard limit is 2000

TOOLS_DIR = Path(os.environ.get("TOOLS_DIR", "/data/tools"))
TOOL_TIMEOUT = int(os.environ.get("TOOL_TIMEOUT", "30"))
TOOL_CREATORS = {s.strip() for s in os.environ.get("TOOL_CREATORS", "").split(",") if s.strip()}
OPEN_CHANNELS = {s.strip().lstrip("#").lower() for s in os.environ.get("OPEN_CHANNELS", "").split(",") if s.strip()}
MAX_TOOLS = 60              # tools the model may keep at once (every spec is sent on every turn)
MAX_TOOL_OUTPUT = 4000      # characters of a tool result fed back to the model
MAX_STEPS = 16              # tool rounds per message, before it has to answer with what it has
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,47}$")

SHELL_TIMEOUT_MAX = 600     # seconds a single shell command or install may take
PYLIBS = Path("/data/pylibs")            # pip --user target; on the volume, so installs persist
APT_MANIFEST = Path("/data/apt-packages.txt")   # apt can't persist, so we replay it at startup
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")   # one package name, no shell metacharacters

# The forge (dev/forge.py): Claude Code on the dev box builds and tests a tool from a brief, then installs it here.
FORGE_URL = os.environ.get("FORGE_URL", "http://bot-dev:8791").rstrip("/")
FORGE_TIMEOUT = int(os.environ.get("FORGE_TIMEOUT", "480"))     # the forge's own limit for one build is 360s
FORGE_ON = bool(FORGE_URL and os.environ.get("INTERNAL_TOKEN"))
BOT_NAME = os.environ.get("BOT_NAME", "example-bot")
KIT = os.environ.get("KIT", "default")       # which bots/<bot>/kits/<KIT> folder compose mounted at /data/tools; the forge installs there

# ---------------------------------------------------------------- secrets: the bot process owns the vault, tools only ever see env vars
VAULT_FILE = Path("/data/secrets_manager/vault.enc")   # {name: {"value", "note", "created", "updated"}}, Fernet-encrypted
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")   # env-var style names


def _fernet():
    key = os.environ.get("VAULT_KEY")
    if not key:
        raise ToolError("no VAULT_KEY in the bot's environment - the owner has to add one to .env before secrets can be stored")
    from cryptography.fernet import Fernet
    return Fernet(key.strip().encode())


def vault_load():
    if not VAULT_FILE.exists() or not VAULT_FILE.read_bytes().strip():
        return {}
    return json.loads(_fernet().decrypt(VAULT_FILE.read_bytes()).decode())


def vault_save(data):
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAULT_FILE.write_bytes(_fernet().encrypt(json.dumps(data).encode()))


def secret_env(names):
    """The environment additions for one tool run: only the secrets it declared, from the vault first, else the bot's own env."""
    if not names:
        return {}
    try:
        vault = vault_load()
    except Exception:  # noqa - no key or unreadable vault: fall back to plain env, tool will complain if missing
        vault = {}
    out = {}
    for n in names:
        if n in vault:
            out[n] = vault[n]["value"]
        elif n in os.environ:
            out[n] = os.environ[n]
    return out


def secrets_tool(action="list", name=None, value=None, note=None):
    """Meta-tool: manage the vault by name. There is deliberately no action that returns a value."""
    if action == "list":
        try:
            vault = vault_load()
        except ToolError as e:
            return {"error": str(e), "secrets": []}
        return {"count": len(vault), "secrets": [{"name": n, "note": d.get("note", ""), "updated": d.get("updated"),
                                                   "length": len(d.get("value", ""))} for n, d in sorted(vault.items())],
                "note": "values are never shown; a tool that declares one of these names gets it as an environment variable"}
    if not name or not SECRET_NAME_RE.match(name):
        raise ToolError("name must look like an environment variable: XAI_API_KEY, NAS_PASSWORD, ...")
    if action == "set":
        if value is None or value == "":
            raise ToolError("set needs a value")
        vault = vault_load()
        prev = vault.get(name, {})
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        vault[name] = {"value": value, "note": note if note is not None else prev.get("note", ""),
                       "created": prev.get("created", now), "updated": now}
        vault_save(vault)
        return {"ok": True, "name": name, "length": len(value),
                "note": "stored. If the value was pasted in a public channel, suggest the person rotate it; secrets are best sent by DM."}
    if action == "delete":
        vault = vault_load()
        if name not in vault:
            raise ToolError(f"no secret named {name}; have: {sorted(vault)}")
        del vault[name]
        vault_save(vault)
        return {"ok": True, "deleted": name}
    raise ToolError("action must be list, set or delete")

TOOL_GUIDE = f"""

You can extend yourself. When answering needs something you can't do yet, build the tool and do the thing:
create_tool for something small, request_tool for anything bigger. Never tell a person you can't do
something, and never ask whether they would like a tool built - a missing tool is not a limitation, it is
the next step. Say "working on it" if it will take a moment, then get on with it. Tools stay available
afterwards, including after a restart, so you build up a kit over time. When a reply used tools you built
during this request, end it with one line naming them, e.g. "New tool: `lg_tv` (LG TV control)."

Writing one:
- `code` must define a top-level `run(**kwargs)` taking your input_schema's properties and *returning* a
  JSON-serialisable value. Return it - anything printed is discarded.
- Module-level code runs on every call, so keep it to imports and constants; do the work inside run().
- Each call runs in its own process, limited to {TOOL_TIMEOUT}s, with no access to this conversation.
- Raise an exception with a clear message on bad input; you will see it and can fix the tool.
- Prefer an existing tool to a near-duplicate. To repair one, read_tool it and create_tool the same name.

Your machine - you are root in your own Debian container, and `shell` and `install` are real:
- Start from the Python 3.12 standard library, httpx and anthropic. Need more? `install` it: pip packages
  your tools can import, apt packages for system commands. Both persist across restarts and rebuilds.
- `shell` runs any bash command here. Use it to look around, not to install (installs made by hand are
  lost when the container is rebuilt; `install` is the one that survives).
- Only /data survives a rebuild - it is the volume. Anything you write elsewhere is temporary, so keep
  files a tool needs in /data/<tool_name>/.
- You are on the household network and can reach the internet. Stay inside your own container unless
  asked; you have access to things outside it that you are not meant to touch on your own initiative.
- Secrets (API keys, passwords) are handled for you: give create_tool a `secrets` list of environment-variable
  names your tool needs (e.g. ["XAI_API_KEY"]) and read them with os.environ when it runs. Users store values
  with the `secrets` tool (set) - ideally by DM - or the owner puts them in the bot's .env. Never write a
  secret into code, never save one under /data, never echo one into a reply, and never ask for one you
  could declare instead.
When you make a tool or install something, say so in one short line.
"""
FORGE_GUIDE = """
Bigger tools: create_tool is for something you can get right in one go. When a tool must talk to a device or a
service, needs auth, or should be tested against the real thing, use request_tool instead - a coding agent on
the dev box writes it, runs it against the real target, fixes it and installs it, usually in one to three
minutes. request_tool starts that build in the background and returns at once: reply with one short line like
"Working on it - give me a couple of minutes" (do not explain that you lack a tool or ask permission to build one),
then end your turn - do not wait, poll or try to write the tool yourself meanwhile. When the build finishes you get
a follow-up turn with the result, the notes on using the tool and the person's original message; do what they asked
then, and end that reply with a line naming the new tool. Give request_tool a real brief (what,
when, what to return, anything you already learned) and the example_call you want to make. If the forge is
unreachable, fall back to create_tool.
"""
if FORGE_ON:
    TOOL_GUIDE += FORGE_GUIDE

META_TOOLS = [
    {
        "name": "create_tool",
        "description": "Write a new tool, or replace one of yours with the same name, and make it callable "
                       "from your next turn onwards. The code is checked and imported once before it is kept.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case, 3-48 characters"},
                "description": {"type": "string",
                                "description": "What it does and when to use it. Your future self reads only this."},
                "input_schema": {"type": "object",
                                 "description": "JSON Schema (type: object) for run()'s keyword arguments"},
                "code": {"type": "string", "description": "Python source defining a top-level run(**kwargs)"},
                "secrets": {"type": "array", "items": {"type": "string"},
                            "description": "environment-variable names the tool needs (e.g. XAI_API_KEY); injected at run time from the vault"},
            },
            "required": ["name", "description", "input_schema", "code"],
        },
    },
    {
        "name": "read_tool",
        "description": "Read back the source of a tool you wrote, to fix or extend it.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "delete_tool",
        "description": "Forget a tool that is wrong or no longer useful.",
        "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "shell",
        "description": "Run a bash command as root inside your own container, to look around the system, "
                       "inspect files, check processes or test something. Returns exit code, stdout and stderr. "
                       "Changes outside /data are lost when the container is rebuilt - to add software, use install.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "bash, run from /data"},
                "timeout": {"type": "integer", "description": f"seconds, default {TOOL_TIMEOUT}, max {SHELL_TIMEOUT_MAX}"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "install",
        "description": "Install software permanently: pip packages your tools can then import, or apt packages "
                       "for system commands. Survives restarts and rebuilds, unlike installing by hand in shell.",
        "input_schema": {
            "type": "object",
            "properties": {
                "manager": {"type": "string", "enum": ["pip", "apt"]},
                "packages": {"type": "array", "items": {"type": "string"}, "description": "package names"},
            },
            "required": ["manager", "packages"],
        },
    },
]
META_TOOLS.append({
    "name": "secrets",
    "description": "The bot's encrypted vault of API keys and passwords, by name. action 'list' shows names and notes only; "
                   "'set' stores or replaces a value (name like XAI_API_KEY); 'delete' removes one. Values can never be read back "
                   "here - a tool that declares the name in its spec receives it as an environment variable when it runs.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "set", "delete"]},
        "name": {"type": "string"}, "value": {"type": "string"}, "note": {"type": "string"}},
        "required": ["action"]},
})
if FORGE_ON:
    META_TOOLS.append({
        "name": "request_tool",
        "description": "Have a tool built and tested for you by a coding agent on the dev box, for anything too involved to get "
                       "right in one create_tool call: device or service integrations, auth, multi-step logic. Starts the build in "
                       "the background and returns immediately; when it is done (one to three minutes) you are woken with a follow-up "
                       "turn carrying the result and the person's original question. Replaces a tool of the same name.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "snake_case, 3-48 characters"},
            "brief": {"type": "string", "description": "What it must do, when to use it, what it should return, and anything you "
                                                       "already learned (hosts, ports, API shape, quirks)"},
            "inputs": {"type": "string", "description": "the arguments it should take, in words"},
            "secrets": {"type": "array", "items": {"type": "string"},
                        "description": "environment-variable names it will need (existing vault names or new ones)"},
            "example_call": {"type": "object", "description": "the arguments you want to call it with right now; the agent tests with these"}},
            "required": ["name", "brief"]},
    })
META_NAMES = {t["name"] for t in META_TOOLS}
# Tools that act on the machine rather than the conversation; limited to TOOL_CREATORS.
PRIVILEGED = {"create_tool", "request_tool", "delete_tool", "shell", "install", "secrets"}

_client = None


def get_client():
    """Created on first use, so the container can start (and idle) without an API key configured."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class ToolError(Exception):
    """Anything the model should see and be able to correct itself."""


# ---------------------------------------------------------------- the tool kit

def code_path(name):
    return TOOLS_DIR / f"{name}.py"


def spec_path(name):
    return TOOLS_DIR / f"{name}.json"


def tool_specs():
    """The tools sent to the model: the meta-tools, plus every tool it has written.
    Only the JSON specs are read here - generated code never enters this process."""
    specs = list(META_TOOLS)
    for f in sorted(TOOLS_DIR.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        spec.pop("secrets", None)            # bot-side field, not part of the API schema
        specs.append(spec)
    return specs


def tool_secrets(name):
    """Secret names a generated tool declared in its spec."""
    try:
        return [n for n in json.loads(spec_path(name).read_text(encoding="utf-8")).get("secrets", []) if isinstance(n, str)]
    except (OSError, ValueError):
        return []


def create_tool(name, description, input_schema, code, secrets=None):
    if not NAME_RE.match(name or ""):
        raise ToolError("name must be snake_case, 3-48 chars, starting with a letter")
    if name in META_NAMES:
        raise ToolError(f"{name} is one of your built-in tools; pick another name")
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise ToolError('input_schema must be a JSON Schema object: {"type": "object", "properties": {...}}')
    if not re.search(r"^def run\(", code or "", re.M):
        raise ToolError("code must define a top-level run(**kwargs)")
    if len(list(TOOLS_DIR.glob("*.json"))) >= MAX_TOOLS and not spec_path(name).exists():
        raise ToolError(f"you already have {MAX_TOOLS} tools; delete one first")
    try:
        compile(code, f"{name}.py", "exec")
    except SyntaxError as e:
        raise ToolError(f"syntax error on line {e.lineno}: {e.msg}")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    kept = code_path(name).read_text(encoding="utf-8") if code_path(name).exists() else None
    code_path(name).write_text(code, encoding="utf-8")
    try:
        run_tool(name, {}, selftest=True)          # imports it once, in a child process
    except ToolError as e:
        if kept is None:
            code_path(name).unlink(missing_ok=True)
        else:
            code_path(name).write_text(kept, encoding="utf-8")   # keep the version that worked
        raise ToolError(f"the tool was not kept - it failed to import: {e}")
    secrets = [n for n in (secrets or []) if isinstance(n, str) and SECRET_NAME_RE.match(n)]
    spec_path(name).write_text(
        json.dumps({"name": name, "description": description, "input_schema": input_schema, "secrets": secrets}, indent=2),
        encoding="utf-8")
    return f"tool `{name}` is ready; you can call it now"


def read_tool(name):
    if not code_path(name).exists():
        raise ToolError(f"you have no tool named {name}")
    return code_path(name).read_text(encoding="utf-8")


def delete_tool(name):
    if not code_path(name).exists():
        raise ToolError(f"you have no tool named {name}")
    code_path(name).unlink(missing_ok=True)
    spec_path(name).unlink(missing_ok=True)
    return f"forgot `{name}`"


def run_tool(name, args, selftest=False):
    """Run one generated tool in a separate python, so a crash, a hang or a runaway loop
    costs one subprocess instead of the bot."""
    if not code_path(name).exists():
        raise ToolError(f"you have no tool named {name}")
    argv = [sys.executable, os.path.abspath(__file__), "--run-tool", name] + (["--selftest"] if selftest else [])
    try:
        p = subprocess.run(argv, input=json.dumps(args or {}), capture_output=True, text=True,
                           timeout=TOOL_TIMEOUT, env={**os.environ, **secret_env(tool_secrets(name)), "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise ToolError(f"the tool ran longer than {TOOL_TIMEOUT}s and was stopped")
    try:
        out = json.loads(p.stdout)
    except ValueError:
        tail = (p.stderr or p.stdout or "").strip()[-600:]
        raise ToolError(f"the tool returned nothing usable (exit {p.returncode}): {tail}")
    if not out.get("ok"):
        raise ToolError(out["error"])
    return out["result"]


def _run_tool_child():
    """`bot.py --run-tool <name>`: import the tool, call run() with JSON args from stdin, print JSON to stdout.
    The tool's own stdout is pointed at stderr so a stray print() can't corrupt the result."""
    name = sys.argv[sys.argv.index("--run-tool") + 1]
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        spec = importlib.util.spec_from_file_location(f"tool_{name}", code_path(name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "run", None)):
            raise TypeError("no top-level run() after import")
        if "--selftest" in sys.argv:
            out = {"ok": True, "result": "imports cleanly"}
        else:
            out = {"ok": True, "result": mod.run(**json.loads(sys.stdin.read() or "{}"))}
    except Exception:
        out = {"ok": False, "error": traceback.format_exc(limit=3).strip()}
    sys.stdout = real_stdout
    print(json.dumps(out, default=str))
    return 0


def clip(text):
    return text if len(text) <= MAX_TOOL_OUTPUT else text[:MAX_TOOL_OUTPUT] + f"\n… [{len(text)} chars, truncated]"


# ---------------------------------------------------------------- the machine

def shell(command, timeout=None):
    """Any bash command, as root, in this container."""
    if not str(command or "").strip():
        raise ToolError("command is empty")
    timeout = min(int(timeout or TOOL_TIMEOUT), SHELL_TIMEOUT_MAX)
    try:
        p = subprocess.run(["bash", "-lc", command], capture_output=True, text=True,
                           timeout=timeout, cwd="/data")
    except subprocess.TimeoutExpired:
        raise ToolError(f"the command ran longer than {timeout}s and was stopped")
    return {"exit_code": p.returncode, "stdout": clip(p.stdout), "stderr": clip(p.stderr)}


def install(manager, packages):
    """pip installs into /data so it persists; apt can't, so we record it and replay at startup."""
    if manager not in ("pip", "apt"):
        raise ToolError("manager must be 'pip' or 'apt'")
    packages = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if not packages:
        raise ToolError("no packages given")
    bad = [p for p in packages if not PKG_RE.match(p)]
    if bad:
        raise ToolError(f"these aren't plain package names: {bad}")

    if manager == "pip":
        PYLIBS.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, "-m", "pip", "install", "--user", "--no-input", "-q", *packages]
    else:
        argv = ["bash", "-lc", "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install "
                               "-y -qq --no-install-recommends " + " ".join(packages)]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=SHELL_TIMEOUT_MAX,
                           env={**os.environ, "PYTHONUSERBASE": str(PYLIBS)})
    except subprocess.TimeoutExpired:
        raise ToolError(f"the install ran longer than {SHELL_TIMEOUT_MAX}s and was stopped")
    if p.returncode != 0:
        raise ToolError(f"install failed: {clip((p.stderr or p.stdout).strip())}")

    if manager == "apt":                       # remember, so a rebuilt container reinstalls it
        have = set(APT_MANIFEST.read_text().split()) if APT_MANIFEST.exists() else set()
        APT_MANIFEST.write_text("\n".join(sorted(have | set(packages))) + "\n")
    return (f"installed {', '.join(packages)}. "
            + ("Import it in a tool as usual." if manager == "pip" else "The command is on PATH now."))


PENDING_BUILDS = {}          # tool name -> time the forge was asked; one build per name at a time


def request_tool(name, brief, inputs=None, secrets=None, example_call=None, ctx=None):
    """Hand a brief to the forge on the dev box (dev/forge.py): a headless Claude Code writes the tool, tests it with a
    harness that behaves like this bot, installs the pair into /data/tools and commits it.

    Non-blocking: the build runs in a background thread and this returns at once, so the model can tell the person
    it is being built and end its turn. When the forge answers, forge_followup() wakes the model in the same channel
    with the result and the person's original message, and posts the reply to them (see run_discord for ctx)."""
    if not FORGE_ON:
        raise ToolError("no forge configured (FORGE_URL and INTERNAL_TOKEN); write it yourself with create_tool")
    ctx = ctx or {}
    if ctx.get("no_forge"):
        raise ToolError("the forge already had its turn on this request; write it with create_tool or tell the person what is missing")
    if not NAME_RE.match(name or ""):
        raise ToolError("name must be snake_case, 3-48 chars, starting with a letter")
    if name in META_NAMES:
        raise ToolError(f"{name} is one of your built-in tools; pick another name")
    if len(list(TOOLS_DIR.glob("*.json"))) >= MAX_TOOLS and not spec_path(name).exists():
        raise ToolError(f"you already have {MAX_TOOLS} tools; delete one first")
    if name in PENDING_BUILDS:
        raise ToolError(f"`{name}` is already being built (started {round(time.time() - PENDING_BUILDS[name])}s ago); "
                        "tell the person it is on its way")
    try:
        r = httpx.get(f"{FORGE_URL}/health", timeout=5)      # fail now, while the model can still fall back
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ToolError(f"the forge on the dev box is not reachable ({type(e).__name__}); write it yourself with create_tool")
    try:
        available = sorted(vault_load())
    except Exception:  # noqa - no vault is fine, the forge just won't know the names
        available = []
    job = {"name": name, "bot": BOT_NAME, "kit": KIT, "brief": str(brief), "inputs": str(inputs or ""),
           "secrets": [s for s in (secrets or []) if isinstance(s, str)],
           "example_call": example_call if isinstance(example_call, dict) else {},
           "requested_by": ctx.get("who", ""), "available_secrets": available}
    PENDING_BUILDS[name] = time.time()
    threading.Thread(target=_forge_worker, args=(name, job, ctx), daemon=True, name=f"forge-{name}").start()
    who = ctx.get("who") or "the person"
    if ctx.get("send"):
        return (f"build of `{name}` started in the background; it usually takes one to three minutes. Reply to {who} with one "
                f"short line - \"working on it, a couple of minutes\" - not an explanation of what you lack, then end your turn. "
                f"You will be woken with the result and {who}'s original message.")
    return (f"build of `{name}` started in the background (one to three minutes). There is no channel to follow up in from "
            f"here, so tell {who} to ask again in a few minutes; the tool will be in your kit by then if the build succeeds.")


def _forge_worker(name, job, ctx):
    """Blocks on the forge for one build, then hands the result to the event loop for the follow-up turn."""
    try:
        r = httpx.post(f"{FORGE_URL}/forge", json=job, headers={"X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
                       timeout=FORGE_TIMEOUT)
        out = r.json() if r.status_code == 200 else {"ok": False, "failure": f"the forge refused the request ({r.status_code}): {r.text[:300]}"}
    except httpx.TimeoutException:
        out = {"ok": False, "failure": f"the forge did not answer within {FORGE_TIMEOUT}s; the tool may still land later"}
    except Exception as e:  # noqa - anything else is reported to the model, never lost
        out = {"ok": False, "failure": f"could not talk to the forge: {type(e).__name__}: {str(e)[:200]}"}
    finally:
        PENDING_BUILDS.pop(name, None)
    print(f"forge: `{name}` {'ready' if out.get('ok') else 'failed'} for {job.get('requested_by') or 'someone'}"
          + ("" if out.get("ok") else f": {str(out.get('failure'))[:160]}"), flush=True)
    if ctx.get("loop") and ctx.get("send"):
        asyncio.run_coroutine_threadsafe(forge_followup(name, out, ctx), ctx["loop"])


def forge_result_text(name, out, who, question):
    """The follow-up turn: not a person speaking, so it says so, and carries everything the model needs to act."""
    status = "READY" if out.get("ok") else "FAILED"
    lines = [f"[Follow-up from the forge - this is not a person speaking. The tool `{name}` you requested while answering {who} is {status}.]"]
    if out.get("ok"):
        lines += [f"Summary: {out.get('summary')}", f"Tested: {out.get('tested')}", f"Notes for using it: {out.get('notes_for_model')}"]
        if out.get("apt_needed"):
            lines.append(f"It needs apt packages - `install` these before calling it: {', '.join(out['apt_needed'])}")
        if out.get("pip_installed"):
            lines.append(f"pip installed for it: {', '.join(out['pip_installed'])}")
        if out.get("replaced"):
            lines.append("It replaced an earlier tool of the same name.")
    else:
        lines.append(f"Why: {out.get('failure')}")
        if out.get("notes_for_model"):
            lines.append(f"Notes: {out.get('notes_for_model')}")
    lines.append(f"{who}'s original message was: \"{(question or '').strip()[:1500]}\"")
    if out.get("ok"):
        lines.append(f"Now call `{name}` to do what {who} originally asked and reply to them by name, as a normal message - lead "
                     f"with the result, not with the fact that a tool was built. End the reply with one line naming what is new, "
                     f"e.g. \"New tool: `{name}` ({(out.get('summary') or 'what it does')[:60]})\". If the tool errors, say what "
                     f"happened. Do not use request_tool in this turn.")
    else:
        lines.append(f"Tell {who} briefly that the build did not work out and why; answer another way if you can "
                     f"(create_tool for a simpler version, or existing tools), otherwise say what would be needed. "
                     f"Do not use request_tool in this turn.")
    return "\n".join(lines)


async def forge_followup(name, out, ctx):
    """Runs on the event loop after a build: append the result to the channel's history as a turn the model must act on,
    run the model with the same trust as the original request, and post its reply to the person who asked."""
    hist, who = ctx["hist"], ctx.get("who") or "the person"
    hist.append({"role": "user", "content": forge_result_text(name, out, who, ctx.get("question"))})
    fctx = {**ctx, "no_forge": True}
    try:
        reply, notes = await asyncio.to_thread(ask, hist, ctx.get("trusted", False), "", fctx)
    except anthropic.APIError as e:
        reply, notes = (f"The tool `{name}` is ready but I hit a model API error ({getattr(e, 'status_code', '?')}) writing "
                        f"the answer - ask me again, {who}." if out.get("ok")
                        else f"Building `{name}` failed ({str(out.get('failure'))[:200]}), and I hit a model API error explaining it."), []
    hist.append({"role": "assistant", "content": reply})
    if notes:
        reply += "\n-# " + ", ".join(dict.fromkeys(notes))
    try:
        await ctx["send"](reply)
    except Exception as e:  # noqa - the send target may be gone; the history still has the answer
        print(f"forge follow-up for `{name}` could not be posted: {type(e).__name__}: {e}", flush=True)


STARTER_TOOLS = Path(os.environ.get("STARTER_TOOLS", "/app/starter-tools"))   # baked into the image from bots/example-bot/starter-tools


def seed_starter_tools():
    """First start on a fresh install: /data/tools is empty, so copy in the generic starter set. Never touches a kit that
    has anything in it, so deleting a starter tool later sticks."""
    if any(TOOLS_DIR.glob("*.json")) or not STARTER_TOOLS.is_dir():
        return
    names = sorted(p.stem for p in STARTER_TOOLS.glob("*.json") if (STARTER_TOOLS / f"{p.stem}.py").exists())
    if not names:
        return
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    for n in names:
        for ext in (".py", ".json"):
            shutil.copyfile(STARTER_TOOLS / f"{n}{ext}", TOOLS_DIR / f"{n}{ext}")
    print(f"first start: seeded {len(names)} starter tools - {', '.join(names)}", flush=True)


def replay_apt():
    """Reinstall apt packages recorded by install() - the filesystem outside /data is new on every rebuild."""
    if not APT_MANIFEST.exists():
        return
    wanted = APT_MANIFEST.read_text().split()
    missing = [p for p in wanted
               if subprocess.run(["dpkg", "-s", p], capture_output=True).returncode != 0]
    if not missing:
        return
    print(f"reinstalling apt packages after rebuild: {', '.join(missing)}", flush=True)
    r = subprocess.run(["bash", "-lc", "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get "
                                       "install -y -qq --no-install-recommends " + " ".join(missing)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"warning: could not reinstall {missing}: {r.stderr.strip()[-300:]}", flush=True)


def dispatch(block, trusted, ctx=None):
    """Run one tool_use block. Returns (tool_result, short note for the user).
    ctx carries what request_tool needs to follow up later: who asked (who, trusted), what they said (question),
    the channel's history (hist), the event loop, and an async send(text) that posts to them."""
    name, args = block.name, dict(block.input or {})
    try:
        if name in PRIVILEGED and not trusted:
            raise ToolError("this person isn't allowed to change your tools or your machine; "
                            "answer with what you already have")
        if name == "create_tool":
            result, note = create_tool(**args), f"wrote `{args.get('name')}`"
        elif name == "request_tool":
            result, note = request_tool(**args, ctx=ctx), f"forged `{args.get('name')}`"
        elif name == "read_tool":
            result, note = read_tool(**args), f"read `{args.get('name')}`"
        elif name == "delete_tool":
            result, note = delete_tool(**args), f"deleted `{args.get('name')}`"
        elif name == "shell":
            result, note = shell(**args), "shell"
        elif name == "install":
            result, note = install(**args), f"installed {', '.join(args.get('packages') or [])}"
        elif name == "secrets":
            result, note = secrets_tool(**args), f"secrets {args.get('action', 'list')}"
        else:
            result, note = run_tool(name, args), f"`{name}`"
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        return {"type": "tool_result", "tool_use_id": block.id, "content": clip(content)}, note
    except ToolError as e:
        detail = str(e)
    except TypeError as e:                       # wrong arguments for a meta-tool
        detail = f"bad arguments: {e}"
    except Exception:
        detail = traceback.format_exc(limit=2).strip()
    return ({"type": "tool_result", "tool_use_id": block.id, "content": clip(detail), "is_error": True},
            f"`{name}` failed")


# ---------------------------------------------------------------- talking to Claude

def ask(history, trusted=True, system_extra="", ctx=None):
    """Claude, plus as many tool rounds as the answer needs. Returns (reply, notes).
    Opus 5 thinks adaptively by default; medium effort keeps chat replies quick and cheap.
    Server-side fallbacks re-run a safety-declined request on another model instead of returning nothing."""
    messages = [dict(m) for m in history]
    notes = []
    for _ in range(MAX_STEPS):
        response = get_client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM + TOOL_GUIDE + system_extra,
            messages=messages,
            tools=tool_specs(),
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            return "I can't help with that one.", notes
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return text or "…", notes
        # The assistant turn goes back verbatim: it carries the thinking blocks with the tool calls.
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                result, note = dispatch(block, trusted, ctx)
                results.append(result)
                notes.append(note)
        messages.append({"role": "user", "content": results})   # all results in one message
    return "I kept reaching for tools and ran out of steps — try narrowing the question.", notes


def chunk(text, n=MAX_REPLY):
    parts = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        parts.append(text[:cut]); text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


# ---------------------------------------------------------------- attachments: images and PDFs go to Claude, text inline, the rest to /data/uploads

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_EXT = {".txt", ".md", ".json", ".csv", ".tsv", ".log", ".yaml", ".yml", ".toml", ".ini", ".xml", ".html", ".css",
            ".js", ".mjs", ".ts", ".py", ".sh", ".ps1", ".bat", ".sql", ".c", ".h", ".cpp", ".java", ".go", ".rs", ".rb", ".php", ".env.example", ".nfo", ".srt"}
MAX_IMAGES = 4                       # per message
MAX_ATTACH_BYTES = 10 * 1024 * 1024  # per attachment sent to the model or saved
MAX_IMAGE_BYTES = 5 * 1000 * 1000    # the API's per-image cap; bigger images are shrunk (Pillow) or handed to the tools
MAX_IMAGE_SIDE = 1568                # px; the model downsamples anything larger anyway, so shrink first and save tokens


def sniff_image_type(data):
    """The real format from the bytes. Discord's content_type is often wrong (iPhone uploads arrive as
    'IMG_1234.png' labelled image/jpeg); the API rejects a media_type that doesn't match the bytes with a 400."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")): return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"): return "image/heic"   # not supported by the API
    return None


def shrink_image(data, media_type):
    """Re-encode an image that is over the API's size cap or needlessly large. Returns (bytes, media_type)
    or None when Pillow is not installed or the image can't be read."""
    try:
        from PIL import Image
        import io
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa
        return None
    if getattr(im, "is_animated", False):
        return None                                  # keep animated GIFs as they are
    w, h = im.size
    scale = min(1.0, MAX_IMAGE_SIDE / max(w, h))
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    out = io.BytesIO()
    if has_alpha:
        im.convert("RGBA").save(out, "PNG", optimize=True); mt = "image/png"
    else:
        im.convert("RGB").save(out, "JPEG", quality=85, optimize=True); mt = "image/jpeg"
    if out.tell() > MAX_IMAGE_BYTES:                 # still too big (huge flat PNG): force JPEG
        out = io.BytesIO(); im.convert("RGB").save(out, "JPEG", quality=75, optimize=True); mt = "image/jpeg"
    return out.getvalue(), mt
MAX_TEXT_CHARS = 60_000              # of an inline text file
UPLOADS = Path("/data/uploads")      # other files land here; tools can act on them by path
SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def content_from_attachments(text, author, files, msg_id="x"):
    """Build the user turn for one Discord message.
    files: list of (filename, content_type, data bytes). Returns (blocks, text_only, notes):
      blocks    what Claude sees now (text + image/document blocks + inline files)
      text_only what stays in channel history afterwards, so images aren't resent on every later turn
      notes     one-liners for the Discord footer (what was done with each attachment)"""
    head = f"{author}: {text}" if text else f"{author} sent attachment(s) without text — look at them and respond to what they seem to want."
    blocks, memo, notes, images = [{"type": "text", "text": head}], [head], [], 0
    for name, ctype, data in files:
        ctype = (ctype or "").split(";")[0].strip().lower()
        ext = os.path.splitext(name)[1].lower()
        size = len(data)
        if size > MAX_ATTACH_BYTES:
            blocks.append({"type": "text", "text": f"[attachment {name} skipped: {size // 1048576} MB is over the {MAX_ATTACH_BYTES // 1048576} MB limit]"})
            notes.append(f"{name}: too big"); continue
        real = sniff_image_type(data)
        if real == "image/heic":
            shrunk = shrink_image(data, real)        # Pillow can only open HEIC with an extra plugin; usually None
            if shrunk:
                data, real = shrunk
            else:
                ctype = "image/heic"                 # falls through to "keep it for the tools" below
        if real in IMAGE_TYPES and images < MAX_IMAGES:
            images += 1
            media_type = real                        # never trust Discord's label over the bytes
            if size > MAX_IMAGE_BYTES or (ctype == "image/png" and size > 1_500_000):
                shrunk = shrink_image(data, media_type)
                if shrunk:
                    data, media_type = shrunk
            if len(data) > MAX_IMAGE_BYTES:          # no Pillow and too big for the API: hand it to the tools instead
                UPLOADS.mkdir(parents=True, exist_ok=True)
                path = UPLOADS / f"{msg_id}-{SAFE_RE.sub('_', name)[:80]}"
                path.write_bytes(data)
                line = f"[image {name} is {size / 1e6:.1f} MB, over the 5 MB the model accepts — saved to {path}; a tool could shrink it]"
                blocks.append({"type": "text", "text": line}); memo.append(line); notes.append(f"{name}: too big to view, saved"); continue
            blocks.append({"type": "text", "text": f"[image attached: {name}]"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()}})
            memo.append(f"[image: {name}]"); notes.append(f"{name}: seen"); continue
        if ctype == "application/pdf" or ext == ".pdf":
            blocks.append({"type": "text", "text": f"[PDF attached: {name}]"})
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(data).decode()}})
            memo.append(f"[pdf: {name}]"); notes.append(f"{name}: read"); continue
        if ext in TEXT_EXT or ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            body = data.decode("utf-8", "replace")
            clipped = body[:MAX_TEXT_CHARS] + ("\n[... clipped]" if len(body) > MAX_TEXT_CHARS else "")
            blocks.append({"type": "text", "text": f"--- file {name} ({size} bytes) ---\n{clipped}\n--- end {name} ---"})
            memo.append(f"[file {name}: {body[:400]}{'…' if len(body) > 400 else ''}]"); notes.append(f"{name}: read"); continue
        # anything else: keep it for the tools
        UPLOADS.mkdir(parents=True, exist_ok=True)
        path = UPLOADS / f"{msg_id}-{SAFE_RE.sub('_', name)[:80]}"
        path.write_bytes(data)
        line = f"[file saved: {path} ({size} bytes, {ctype or 'unknown type'}) — use your tools to act on it]"
        blocks.append({"type": "text", "text": line}); memo.append(line); notes.append(f"{name}: saved to /data/uploads")
    return blocks, "\n".join(memo), notes


@contextlib.asynccontextmanager
async def typing_indicator(channel):
    """The "is typing" hint, but never at the cost of the answer.

    channel.typing() sends its own HTTP request on entry and re-sends every ~9s to keep the hint alive, on a
    different endpoint from the one that delivers the reply. Discord can refuse it (429/500/503) while still
    happily accepting messages, and an unguarded `async with` turns that into a lost answer — here it would
    escape the anthropic-only handlers below and leave the person with no reply at all."""
    cm = channel.typing()
    started = False
    try:
        await cm.__aenter__()
        started = True
    except Exception as e:  # noqa — decoration only
        print(f"typing indicator unavailable ({type(e).__name__}); answering anyway", flush=True)
    try:
        yield
    finally:
        if started:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa
                pass


def is_open_channel(channel):
    """True in a channel listed in OPEN_CHANNELS, matched by name (bot-chat) or ID.
    Names are what people actually know; an ID still works and survives a rename."""
    return bool(OPEN_CHANNELS) and (
        str(getattr(channel, "id", "")) in OPEN_CHANNELS
        or (getattr(channel, "name", "") or "").lower() in OPEN_CHANNELS
    )


def run_discord(token):
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    bot = discord.Client(intents=intents)
    histories = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))

    async def brain_endpoint():
        """Internal HTTP endpoint for sibling containers (the voice bot). POST /ask with
        {"text": ..., "speaker": ..., "speaker_id": ..., "key": <history bucket>, "mode": "voice"|"text"} and the
        X-Internal-Token header. Runs the same ask() with the same tools; history is kept per key so the voice
        channel has its own thread. Only reachable on the compose network."""
        from aiohttp import web
        token = os.environ.get("INTERNAL_TOKEN")
        if not token:
            return
        VOICE_EXTRA = ("\n\nVOICE MODE: your reply will be read aloud by text-to-speech in a voice channel. Answer in one to "
                       "three short spoken sentences, plain words, no markdown, no lists, no URLs, no emoji; spell out numbers "
                       "under ten. Do the work with tools as usual, just keep the spoken summary short.")

        async def handle_ask(req):
            if req.headers.get("X-Internal-Token") != token:
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                body = await req.json()
            except Exception:  # noqa
                return web.json_response({"error": "bad json"}, status=400)
            text = (body.get("text") or "").strip()
            if not text:
                return web.json_response({"error": "text required"}, status=400)
            speaker = body.get("speaker") or "someone"
            hist = histories["brain:" + str(body.get("key") or "default")]
            hist.append({"role": "user", "content": f"{speaker}: {text}"})
            trusted = not TOOL_CREATORS or str(body.get("speaker_id") or "") in TOOL_CREATORS
            extra = VOICE_EXTRA if body.get("mode") == "voice" else ""
            ctx = {"who": speaker, "trusted": trusted, "question": text, "hist": hist, "loop": asyncio.get_running_loop()}
            if str(body.get("channel_id") or "").isdigit():      # where a forge follow-up can be posted (the voice session's text channel)
                channel_id = int(body["channel_id"])

                async def send_to_channel(reply):
                    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                    for part in chunk(reply):
                        await ch.send(part)
                ctx["send"] = send_to_channel
            try:
                reply, notes = await asyncio.to_thread(ask, hist, trusted, extra, ctx)
            except anthropic.RateLimitError:
                reply, notes = "I'm being rate limited, try again in a moment.", []
            except anthropic.APIStatusError as e:
                print(f"model API error {e.status_code} on /ask: {str(getattr(e, 'message', '') or e)[:300]}", flush=True)
                reply, notes = f"The model API returned an error, {e.status_code}.", []
            except anthropic.APIConnectionError:
                reply, notes = "I couldn't reach the model API.", []
            hist.append({"role": "assistant", "content": reply})
            return web.json_response({"reply": reply, "notes": list(dict.fromkeys(notes))})

        app = web.Application()
        app.router.add_post("/ask", handle_ask)
        app.router.add_get("/health", lambda r: web.json_response({"ok": True, "tools": len(tool_specs()) - len(META_TOOLS)}))
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("BRAIN_PORT", "8790"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"brain endpoint listening on :{port} (compose network only)", flush=True)

    @bot.event
    async def on_ready():
        print(f"online as {bot.user} | model={MODEL} | tools={len(tool_specs()) - len(META_TOOLS)}"
              f" | no mention needed in: {', '.join(sorted(OPEN_CHANNELS)) or 'nowhere'}", flush=True)
        if not getattr(bot, "_brain_started", False):
            bot._brain_started = True
            asyncio.create_task(brain_endpoint())

    @bot.event
    async def on_message(msg):
        if msg.author.bot:
            return
        is_dm = msg.guild is None
        if not (is_dm or bot.user in msg.mentions or is_open_channel(msg.channel)):
            return
        text = msg.content.replace(f"<@{bot.user.id}>", "").strip()
        if text.startswith("!"):           # !join / !leave / !voice are voice-bot commands (same Discord identity)
            return
        if not text and not msg.attachments:
            return
        files = []
        for a in msg.attachments[:8]:
            try:
                files.append((a.filename, a.content_type, await a.read() if a.size <= MAX_ATTACH_BYTES else b"\0" * (MAX_ATTACH_BYTES + 1)))
            except Exception as e:  # noqa
                files.append((a.filename, "error", f"[could not download: {e}]".encode()))
        blocks, text_only, attach_notes = content_from_attachments(text, msg.author.display_name, files, msg.id)
        hist = histories[msg.channel.id]
        entry = {"role": "user", "content": blocks}
        hist.append(entry)
        notes = []
        trusted = not TOOL_CREATORS or str(msg.author.id) in TOOL_CREATORS

        async def send_reply(reply):
            """A forge follow-up, minutes later: reply to the original message and ping the person, who may have moved on."""
            for i, part in enumerate(chunk(reply)):
                try:
                    await msg.reply(part, mention_author=(i == 0))
                except Exception:  # noqa - original message deleted or reply refused: still deliver it
                    await msg.channel.send((f"{msg.author.mention} " if i == 0 else "") + part)
        ctx = {"who": msg.author.display_name, "trusted": trusted, "question": text_only, "hist": hist,
               "loop": asyncio.get_running_loop(), "send": send_reply}
        try:
            async with typing_indicator(msg.channel):
                reply, notes = await asyncio.to_thread(ask, hist, trusted, "", ctx)
        except anthropic.RateLimitError:
            reply = "I'm being rate limited — try again in a moment."
        except anthropic.APIStatusError as e:
            detail = str(getattr(e, "message", "") or e)[:300]
            print(f"model API error {e.status_code} on message {msg.id}: {detail}", flush=True)
            reply = f"The model API returned an error ({e.status_code}): {detail}"
        except anthropic.APIConnectionError:
            reply = "I couldn't reach the model API."
        entry["content"] = text_only          # images/PDFs are not resent on later turns; a text memo stands in
        hist.append({"role": "assistant", "content": reply})
        notes = attach_notes + notes
        if notes:
            reply += "\n-# " + ", ".join(dict.fromkeys(notes))   # Discord small text
        for part in chunk(reply):
            await msg.reply(part, mention_author=False)

    bot.run(token, log_handler=None)


def main():
    if "--run-tool" in sys.argv:
        return _run_tool_child()
    if "--secrets" in sys.argv:
        print(json.dumps(secrets_tool("list"), indent=1)); return 0
    if "--secret-set" in sys.argv:                 # value read from stdin so it never appears in a command line or the chat
        name = sys.argv[sys.argv.index("--secret-set") + 1]
        print(json.dumps(secrets_tool("set", name, sys.stdin.read().strip(), "set by the owner from the command line"))); return 0
    if "--tools" in sys.argv:
        for spec in tool_specs()[len(META_TOOLS):]:
            print(f"{spec['name']:<24} {spec['description'].splitlines()[0]}")
        return 0
    if "--ask" in sys.argv:
        reply, notes = ask([{"role": "user", "content": sys.argv[sys.argv.index("--ask") + 1]}])
        print(reply)
        if notes:
            print("used:", ", ".join(dict.fromkeys(notes)), file=sys.stderr)
        return 0
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not set — idling. Put it in bots/example-bot/.env and restart the container.", flush=True)
        while True:
            time.sleep(3600)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("warning: ANTHROPIC_API_KEY not set — Claude calls will fail", flush=True)
    if not TOOL_CREATORS:
        print("warning: TOOL_CREATORS is empty — anyone who can talk to this bot can run shell "
              "and install software in this container", flush=True)
    seed_starter_tools()
    replay_apt()
    run_discord(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
