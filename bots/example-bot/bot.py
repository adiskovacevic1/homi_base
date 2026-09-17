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
  DISCORD_TOKEN       Discord bot token (normally in the vault). Missing -> the container idles and logs why, so `compose up` is clean.
  ANTHROPIC_API_KEY   Anthropic key (normally in the vault; a change there applies to the next message, no restart).
  BOT_MODEL           default claude-opus-5
  BOT_SYSTEM          system prompt (optional)
  TOOL_TIMEOUT        seconds one tool call may run, default 30
  TOOL_CREATORS       comma-separated Discord user IDs allowed to write tools, run shell and install
                      software. Empty = everyone who can talk to it, which is rarely what you want.
  OPEN_CHANNELS       channels it answers in without being @mentioned, by name or ID (e.g. bot-chat)
  VAULT_PASSPHRASE    opens the secret vault at /data/secrets_manager/vault.enc (see vault.py), which holds DISCORD_TOKEN,
                      ANTHROPIC_API_KEY, ELEVENLABS_API_KEY and every secret the tools declare. Lives only here, so tools
                      (which can read all of /data) cannot open the vault. Older installs: VAULT_KEY (random Fernet key) still works.
                      Any name below can also be set as a plain env var; the vault wins when both exist.
  INTERNAL_TOKEN      shared secret for the internal /ask endpoint other containers (voice-bot) call; unset = endpoint off
  BRAIN_PORT          port for that endpoint on the compose network, default 8790 (never published to the host)
  FORGE_URL           the dev box's tool builder (dev/forge.py), default http://bot-dev:8791; empty = no request_tool.
                      Needs INTERNAL_TOKEN too (the forge reads the same one from this bot's .env).
  FORGE_TIMEOUT       seconds to wait for one build, default 480
  BOT_NAME            this bot's folder name under bots/, default example-bot
  KIT                 the household's tool kit: bots/<BOT_NAME>/kits/<KIT>/ is what compose mounts at /data/tools and where the
                      forge installs. setup.py writes the same name to the root .env for compose. Default "default".
  IDEAS_AT            local time (HH:MM, container TZ) for the daily review that proposes 1-3 new tools in chat; default 09:00,
                      empty = off. IDEAS_CHANNEL (name or id) says where; empty = the day's busiest channel.

Test without Discord:
  python bot.py --ask "what's the 40th fibonacci number?"
  python bot.py --tools
  python bot.py --ideas --dry        # the daily review now, printed instead of posted (logs in to Discord to read)
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
# The owner's console (served by the dev box on the owner's PC): where the bot sends people to add a key it needs.
CONSOLE_URL = os.environ.get("CONSOLE_URL", "http://127.0.0.1:8792/console").rstrip("/")
FORGE_ON = bool(FORGE_URL and os.environ.get("INTERNAL_TOKEN"))
BOT_NAME = os.environ.get("BOT_NAME", "example-bot")
KIT = os.environ.get("KIT", "default")       # which bots/<bot>/kits/<KIT> folder compose mounted at /data/tools; the forge installs there

# ---------------------------------------------------------------- secrets: one encrypted vault (vault.py), opened with VAULT_PASSPHRASE.
# It holds the bot's own keys (Discord, Anthropic, ElevenLabs) and everything its tools declare; tools only ever see env vars.
from vault import Vault, VaultError, BOOTSTRAP as VAULT_BOOTSTRAP

SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")   # env-var style names
VAULT = Vault()                                           # /data/secrets_manager/vault.enc; passphrase (or legacy VAULT_KEY) from env


def vault_load():
    try:
        return VAULT.load()
    except VaultError as e:
        raise ToolError(f"{e} - the owner fixes this in the console or .env")


def vault_save(data):
    try:
        VAULT.save(data)
    except VaultError as e:
        raise ToolError(str(e))


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
        if vault.get(n, {}).get("value"):                   # an empty entry is a placeholder waiting for the owner, not a value
            out[n] = vault[n]["value"]
        elif os.environ.get(n):
            out[n] = os.environ[n]
    return out


KEY_REJECTED_RE = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|invalid[ _-]*(api[ _-]*)?(key|token)|(key|token)[^.\n]{0,20}(invalid|expired|revoked|rejected)|authentication|incorrect api key", re.I)


def key_needed(names, why, tool=None):
    """The one way the bot ever asks for a key: it creates a highlighted placeholder for each name in the vault and points
    the person at the console, which opens with those rows on top and empty fields ready. Never a request to paste a key
    into chat. Returns the text to relay."""
    names = [n for n in dict.fromkeys(names) if n and SECRET_NAME_RE.match(n)]
    created = []
    for n in names:
        try:
            if VAULT.placeholder(n, f"needed by {tool or 'the bot'}: {why}"[:200]):
                created.append(n)
        except (VaultError, ValueError):
            pass                                              # unreadable vault: the link still tells the owner what is missing
    link = f"{CONSOLE_URL}?need={','.join(names)}"
    what = ", ".join(f"`{n}`" for n in names)
    return (f"I need {what} for that ({why}). Add it here on the PC that runs me: {link}\n"
            f"The {'field is' if len(names) == 1 else 'fields are'} already waiting there, highlighted at the top; paste the value, save, "
            f"then tell me to try again.")


def secrets_tool(action="list", name=None, value=None, note=None):
    """Meta-tool: manage the vault by name. There is deliberately no action that returns a value."""
    if action == "list":
        try:
            vault = vault_load()
        except ToolError as e:
            return {"error": str(e), "secrets": []}
        pending = [n for n, d in vault.items() if not d.get("value")]
        return {"count": len(vault), "secrets": [{"name": n, "note": d.get("note", ""), "updated": d.get("updated"),
                                                   "length": len(d.get("value", "")), "pending": not d.get("value")} for n, d in sorted(vault.items())],
                "waiting_for_owner": pending, "console": f"{CONSOLE_URL}?need={','.join(pending)}" if pending else CONSOLE_URL,
                "note": "values are never shown; a tool that declares one of these names gets it as an environment variable. "
                        "'pending' entries are placeholders the owner has not filled yet"}
    if not name or not SECRET_NAME_RE.match(name):
        raise ToolError("name must look like an environment variable: XAI_API_KEY, NAS_PASSWORD, ...")
    if action == "placeholder":
        return {"ok": True, "name": name, "reply": key_needed([name], note or "a tool is going to need it")}
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
    raise ToolError("action must be list, set, delete or placeholder")

TOOL_GUIDE = f"""

You can extend yourself. When answering needs something you can't do yet, build the tool and do the thing:
create_tool for something small, request_tool for anything bigger. Never tell a person you can't do
something, and never ask whether they would like a tool built - a missing tool is not a limitation, it is
the next step. Say "working on it" if it will take a moment, then get on with it. Tools stay available
afterwards, including after a restart, so you build up a kit over time. When a reply used tools you built
during this request, end it with one line naming them, e.g. "New tool: `lg_tv` (LG TV control)."
You can send files: have a tool save its output under /data (an image, a chart, a CSV, a report, a log) and
call attach_file with the path; it is uploaded with your reply. Never say you cannot upload or attach files.
Once a day you post a few tool ideas drawn from the day's conversation. Owners can turn that off or on, move it,
or ask for it now through your `settings` tool. "build 2" after such a post means: build idea 2 - you will be
given its details.

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
  names your tool needs (e.g. ["XAI_API_KEY"]) and read them with os.environ when it runs. Never write a
  secret into code, never save one under /data, never echo one into a reply, and never ask for one you
  could declare instead. Already available to any tool that declares the name: DISCORD_TOKEN (this bot's own
  Discord token, for anything on the Discord API), ANTHROPIC_API_KEY and, when the house uses it, ELEVENLABS_API_KEY -
  use those names, do not invent new ones.
- When a key is missing or a service rejects one, you are handed a console link with the exact names
  (`...?need=XAI_API_KEY`): a placeholder for each is already waiting there, highlighted. Relay that link and
  those names word for word, say "add it there, then tell me to try again", and stop. Never ask anyone to paste a
  key into chat or a DM, never mention .env files, never guess at a key. Before building a tool that will need a
  key nobody has yet, create the slot yourself with `secrets` action placeholder and send the same link.
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
    "description": "The bot's encrypted vault of API keys and passwords, by name. action 'list' shows names, notes and which are "
                   "still empty placeholders; 'placeholder' creates an empty, highlighted slot for a key the owner has to add and returns "
                   "the console link to relay (use this whenever a key is needed - never ask for the value in chat); 'set' stores a value "
                   "someone insisted on giving you; 'delete' removes one. Values can never be read back here - a tool that declares the "
                   "name in its spec receives it as an environment variable when it runs.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "placeholder", "set", "delete"]},
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
META_TOOLS.append({
    "name": "attach_file",
    "description": "Send a file to the person with your reply: an image a tool made, a report, a CSV, a log. The file must be "
                   "under /data (tools write there). Call it once per file, up to ten; they go out attached to your next reply.",
    "input_schema": {"type": "object", "properties": {
        "path": {"type": "string", "description": "absolute path under /data"},
        "note": {"type": "string", "description": "optional caption, shown with the file"}},
        "required": ["path"]},
})
META_TOOLS.append({
    "name": "settings",
    "description": "Owner-only switches for the bot itself. action 'get' shows them; 'set' changes one: daily_ideas (true/false - the "
                   "daily post proposing new tools), ideas_at (HH:MM local), ideas_channel (name or id; empty = the day's busiest channel); "
                   "'run_ideas' posts the daily ideas now. Use when someone asks to turn the daily ideas on or off, move them, or see them now.",
    "input_schema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["get", "set", "run_ideas"]},
        "key": {"type": "string", "enum": ["daily_ideas", "ideas_at", "ideas_channel"]},
        "value": {"type": "string"}},
        "required": ["action"]},
})
META_NAMES = {t["name"] for t in META_TOOLS}
# Tools that act on the machine rather than the conversation; limited to TOOL_CREATORS.
PRIVILEGED = {"create_tool", "request_tool", "delete_tool", "shell", "install", "secrets", "settings"}
UPLOAD_MAX = int(os.environ.get("DISCORD_UPLOAD_MAX_MB", "10")) * 1024 * 1024   # Discord's limit for a server without boosts
UPLOAD_MAX_FILES = 10


def attach_file(path, note=None, ctx=None):
    """Queue a file for the reply being written. The actual upload happens with the reply, on the event loop."""
    if ctx is None or "files" not in ctx:
        raise ToolError("there is nowhere to send a file from here (no Discord reply in progress)")
    p = Path(str(path or "")).resolve()
    if not str(p).startswith("/data/") or not p.is_file():
        raise ToolError(f"{path} is not a file under /data; tools should save what they produce under /data/<tool>/ and pass that path")
    size = p.stat().st_size
    if size > UPLOAD_MAX:
        raise ToolError(f"{p.name} is {size / 1048576:.1f} MB; Discord accepts up to {UPLOAD_MAX // 1048576} MB here - shrink or split it")
    if len(ctx["files"]) >= UPLOAD_MAX_FILES:
        raise ToolError(f"{UPLOAD_MAX_FILES} files are already attached to this reply")
    ctx["files"].append((str(p), (note or "").strip()[:200]))
    return f"attached {p.name} ({size / 1024:.0f} KB); it goes out with your reply - mention it in one line, do not paste its contents"

_client, _client_key = None, None


def get_client():
    """The Anthropic client, built from the key in the vault (env as fallback) and rebuilt whenever that key changes, so a
    key changed in the console applies to the next message with no restart. Created on first use, so the container can
    start (and idle) without a key configured."""
    global _client, _client_key
    key = secret_env(["ANTHROPIC_API_KEY"]).get("ANTHROPIC_API_KEY")
    if _client is None or key != _client_key:
        _client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        _client_key = key
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
    declared = tool_secrets(name)
    have = secret_env(declared)
    missing = [n for n in declared if not have.get(n)]
    if missing and not selftest:                             # do not even start: the person gets the console link instead of a traceback
        raise ToolError(key_needed(missing, f"the `{name}` tool declares it and it is not in the vault", tool=name)
                        + "\nRelay that to the person as-is (link included) and stop; do not try other ways to get the key.")
    argv = [sys.executable, os.path.abspath(__file__), "--run-tool", name] + (["--selftest"] if selftest else [])
    try:
        p = subprocess.run(argv, input=json.dumps(args or {}), capture_output=True, text=True,
                           timeout=TOOL_TIMEOUT, env={**os.environ, **have, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        raise ToolError(f"the tool ran longer than {TOOL_TIMEOUT}s and was stopped")
    try:
        out = json.loads(p.stdout)
    except ValueError:
        tail = (p.stderr or p.stdout or "").strip()[-600:]
        raise ToolError(f"the tool returned nothing usable (exit {p.returncode}): {tail}")
    if not out.get("ok"):
        err = str(out["error"])
        if declared and KEY_REJECTED_RE.search(err):          # the key exists but the service refused it
            err += ("\n\nThis looks like a rejected or expired key. If the call itself was right, the key needs replacing: "
                    f"{CONSOLE_URL}?need={','.join(declared)} - relay that link to the person, do not ask them to paste a key here.")
        raise ToolError(err)
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
    available += [n for n in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY") if os.environ.get(n) and n not in available]   # the bot's own, via env fallback
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
    fctx = {**ctx, "no_forge": True, "files": []}
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
        await ctx["send"](reply, fctx["files"])
    except Exception as e:  # noqa - the send target may be gone; the history still has the answer
        print(f"forge follow-up for `{name}` could not be posted: {type(e).__name__}: {e}", flush=True)


def discord_files(files):
    """(path, note) pairs -> discord.File objects for one message. A file that vanished is skipped, not fatal."""
    import discord
    out = []
    for path, note in files or []:
        try:
            out.append(discord.File(path, description=note or None))
        except OSError as e:
            print(f"attachment skipped: {path}: {e}", flush=True)
    return out


# ---------------------------------------------------------------- daily ideas: the bot reviews its day and proposes tools to build

IDEAS_AT = os.environ.get("IDEAS_AT", "09:00").strip()          # default local time, HH:MM; empty = off. settings.json overrides
IDEAS_CHANNEL = os.environ.get("IDEAS_CHANNEL", "").strip().lstrip("#").lower()   # name or id; empty = the day's busiest channel
IDEAS_LOOKBACK_H = int(os.environ.get("IDEAS_LOOKBACK_H", "24"))
IDEAS_MAX_CHARS = 60_000
IDEAS_DIR = Path("/data/ideas")                 # latest.json + one file per day: the post and the working notes behind each idea
SETTINGS_FILE = Path("/data/settings.json")     # owner-changeable switches (the `settings` tool); env values are the defaults
IDEAS_SYSTEM = """You are a Discord bot that builds its own tools, reviewing the last day of conversation in your server to propose
new tools worth having. You can build almost anything: a tool is Python running as root in your own container on the
household's LAN with internet, and a coding agent (the forge) can build and test the involved ones for you.

Propose 1 to 3 tool ideas, grounded in what people actually asked for, tried, or hit a wall on. Also welcome: a tool that
would have made an answer better or faster, or that removes a step people keep doing by hand. Do not propose anything that
duplicates a tool you already have, do not repeat a previously proposed idea unless the day brought new evidence for it,
and do not propose vague platforms; each idea is one concrete tool with a name.

Answer with JSON only, no prose around it:
{"post": "<the Discord message people see>", "ideas": [{"n": 1, "name": "snake_name", "summary": "one line, what it does",
  "why": "one line, what in the day suggested it", "evidence": ["up to 5 short quotes or paraphrases from the transcript, with who and when"],
  "brief": "one paragraph, at most 150 words, that a coding agent could build from: what it must do, inputs and outputs, the
  devices or services involved, what was tried before and how it failed, anything learned (hosts, names, ids) - concrete"}]}

The post, plain Discord markdown, short:
**Tool ideas from the last day**
1. **`snake_name`** - what it does in one line. _Why: ..._
2. ...
Reply **build 1** (or 2, 3) and I'll make it.

If the day gives you nothing worth proposing: {"post": "NONE", "ideas": []}"""


def settings_load():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def settings_save(s):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def ideas_settings():
    """Effective switches: settings.json over env defaults."""
    s = settings_load()
    return {"daily_ideas": bool(s.get("daily_ideas", bool(IDEAS_AT))),
            "ideas_at": str(s.get("ideas_at") or IDEAS_AT or "09:00"),
            "ideas_channel": str(s.get("ideas_channel") or IDEAS_CHANNEL).lstrip("#").lower()}


def settings_tool(action="get", key=None, value=None, ctx=None):
    """Meta-tool, owner-only: the bot's own switches. get / set <key> <value> / run_ideas."""
    if action == "get":
        return {**ideas_settings(), "note": "daily_ideas true/false; ideas_at HH:MM local; ideas_channel name or id (empty = busiest); run_ideas posts now"}
    if action == "run_ideas":
        if not (ctx or {}).get("ideas_now"):
            raise ToolError("cannot run the review from here (no Discord connection in this context)")
        ctx["ideas_now"]()
        return "running the daily review now; the post lands in a minute or so (or nothing, if the day gave no ideas)"
    if action != "set":
        raise ToolError("action must be get, set or run_ideas")
    s = settings_load()
    v = str(value if value is not None else "").strip()
    if key == "daily_ideas":
        if v.lower() not in ("true", "false", "on", "off", "yes", "no"):
            raise ToolError("daily_ideas takes true or false")
        s["daily_ideas"] = v.lower() in ("true", "on", "yes")
    elif key == "ideas_at":
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", v):
            raise ToolError("ideas_at is HH:MM, 24h, local time")
        s["ideas_at"] = v
    elif key == "ideas_channel":
        s["ideas_channel"] = v.lstrip("#").lower()
    else:
        raise ToolError("key must be daily_ideas, ideas_at or ideas_channel")
    settings_save(s)
    return {"ok": True, **ideas_settings()}


async def gather_day(bot, since):
    """Recent messages from every text channel the bot can read: (channel, lines, count). Newest channels first by activity."""
    per_channel = []
    for guild in bot.guilds:
        for ch in guild.text_channels:
            me = guild.me or guild.get_member(bot.user.id)
            perms = ch.permissions_for(me) if me else None
            if not perms or not (perms.read_message_history and perms.view_channel):
                continue
            lines = []
            try:
                async for m in ch.history(after=since, limit=400, oldest_first=True):
                    text = (m.content or "").strip()
                    if m.attachments:
                        text += " " + " ".join(f"[file: {a.filename}]" for a in m.attachments)
                    if not text.strip():
                        continue
                    who = "YOU" if m.author.id == bot.user.id else m.author.display_name
                    lines.append(f"[{m.created_at.strftime('%H:%M')}] {who}: {text[:400]}")
            except Exception as e:  # noqa - one unreadable channel is not a reason to skip the day
                print(f"ideas: could not read #{ch.name}: {type(e).__name__}", flush=True)
                continue
            if lines:
                per_channel.append((ch, lines))
    per_channel.sort(key=lambda t: len(t[1]), reverse=True)
    return per_channel


def previously_proposed(days=7):
    """Names proposed in the last week, so the review does not repeat itself without new evidence."""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    names = []
    for f in sorted(IDEAS_DIR.glob("20*.json")):
        if f.stem >= cutoff:
            try:
                names += [i.get("name") for i in json.loads(f.read_text(encoding="utf-8")).get("ideas", []) if i.get("name")]
            except (OSError, ValueError):
                pass
    return sorted(set(names))


def propose_ideas(transcript):
    """One model call, no tools: the day's transcript plus the current kit -> {"post", "ideas"} or None."""
    kit = "\n".join(f"- {s['name']}: {s['description'].splitlines()[0][:120]}" for s in tool_specs()[len(META_TOOLS):])
    prev = previously_proposed()
    prompt = (f"Tools you already have:\n{kit or '(none yet)'}\n\n"
              + (f"Previously proposed this week (skip unless there is new evidence): {', '.join(prev)}\n\n" if prev else "")
              + f"Conversation of the last {IDEAS_LOOKBACK_H} hours (YOU are your own messages):\n\n{transcript[-IDEAS_MAX_CHARS:]}")
    r = get_client().messages.create(model=MODEL, max_tokens=6000, system=IDEAS_SYSTEM,
                                     messages=[{"role": "user", "content": prompt}], output_config={"effort": "medium"})
    text = "".join(b.text for b in r.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        data = salvage_ideas_json(text)
        print(f"ideas: model output was not clean JSON (stop={r.stop_reason}); salvaged post={bool(data.get('post'))} "
              f"ideas={len(data.get('ideas', []))}", flush=True)
    post = str(data.get("post") or "").strip()
    if not post or post.upper().startswith("NONE"):
        return None
    ideas = [i for i in data.get("ideas", []) if isinstance(i, dict) and i.get("name")]
    for k, i in enumerate(ideas, 1):
        i.setdefault("n", k)
    return {"post": post, "ideas": ideas}


def salvage_ideas_json(text):
    """The output was JSON until it was cut off (token cap) or decorated. Recover the post string and every complete idea object;
    a partial trailing idea is dropped. Falls back to the raw text as the post so the day is never lost entirely."""
    dec = json.JSONDecoder(strict=False)                     # lenient about raw newlines inside strings
    out = {"post": "", "ideas": []}
    m = re.search(r'"post"\s*:\s*(")', text)
    if m:
        try:
            out["post"] = dec.raw_decode(text, m.start(1))[0]
        except ValueError:
            pass
    m = re.search(r'"ideas"\s*:\s*\[', text)
    if m:
        i = m.end()
        while True:
            j = text.find("{", i)
            if j < 0:
                break
            try:
                obj, end = dec.raw_decode(text, j)
            except ValueError:
                break                                        # the cut-off one
            if isinstance(obj, dict):
                out["ideas"].append(obj)
            i = end
    if not out["post"]:
        out["post"] = text if not text.lstrip().startswith("{") else ""
    if out["ideas"] and out["post"]:                        # drop ideas the post no longer describes (it lists them by number)
        out["ideas"] = [x for x in out["ideas"] if f"`{x.get('name')}`" in out["post"] or not x.get("name")]
    return out


def find_channel(bot, wanted):
    for guild in bot.guilds:
        for c in guild.text_channels:
            if str(c.id) == wanted or c.name.lower() == wanted:
                return c
    return None


async def post_ideas(bot, histories, dry=False):
    """Gather, propose, post to the chosen channel, save the working notes. The post joins that channel's history and
    'build N' is answered from the saved notes (see build_request_extra) even after the post has scrolled out of memory."""
    import datetime
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=IDEAS_LOOKBACK_H)
    day = await gather_day(bot, since)
    if not day:
        print("ideas: nothing said in the last day; skipping", flush=True)
        return None
    transcript = "\n\n".join(f"# {ch.name}\n" + "\n".join(lines) for ch, lines in day)
    wanted = ideas_settings()["ideas_channel"]
    target = (find_channel(bot, wanted) if wanted else None) or day[0][0]     # else the busiest channel of the day
    result = await asyncio.to_thread(propose_ideas, transcript)
    if not result:
        print("ideas: the model found nothing worth proposing today", flush=True)
        return None
    post = result["post"]
    if dry:
        print(f"ideas (dry run, would post to #{target.name}):\n{post}\n\nworking notes: {json.dumps(result['ideas'], indent=1)[:3000]}", flush=True)
        return post
    for part in chunk(post):
        await target.send(part)
    histories[target.id].append({"role": "assistant", "content": post})
    record = {"date": datetime.date.today().isoformat(), "posted_at": datetime.datetime.now().isoformat(timespec="minutes"),
              "channel": target.name, "channel_id": target.id, "post": post, "ideas": result["ideas"]}
    IDEAS_DIR.mkdir(parents=True, exist_ok=True)
    (IDEAS_DIR / f"{record['date']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (IDEAS_DIR / "latest.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"ideas: posted {len(result['ideas'])} idea(s) to #{target.name}", flush=True)
    return post


BUILD_RE = re.compile(r"^\s*(?:ok(?:ay)?[,.]?\s+|yes[,.]?\s+|please\s+|go\s+)?build\s+(?:idea\s+|#|number\s+|no\.?\s*)?(\d)\b", re.I)


def build_request_extra(text):
    """'build 2' -> the saved working notes for idea 2, as extra system context for this turn; '' for any other message."""
    m = BUILD_RE.match(text or "")
    if not m:
        return ""
    n = int(m.group(1))
    try:
        rec = json.loads((IDEAS_DIR / "latest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("\n\nThe person said 'build N' but you have no saved daily ideas post to refer to. Ask what they would like built, "
                "or if the context makes it obvious, just build that.")
    idea = next((i for i in rec.get("ideas", []) if int(i.get("n", 0)) == n), None)
    if not idea:
        return (f"\n\nThe person said 'build {n}', but your latest ideas post ({rec.get('date')}) had {len(rec.get('ideas', []))} idea(s): "
                + ", ".join(f"{i.get('n')} `{i.get('name')}`" for i in rec.get("ideas", [])) + ". Ask which one they mean.")
    ev = "\n".join(f"  - {e}" for e in idea.get("evidence", [])[:8])
    return (f"\n\nBUILD REQUEST: the person wants idea {n} from your daily ideas post of {rec.get('date')}: `{idea['name']}` - "
            f"{idea.get('summary', '')}\nWhy it came up: {idea.get('why', '')}\nEvidence from that day:\n{ev or '  (none recorded)'}\n"
            f"Suggested brief for the forge:\n{idea.get('brief', '')}\n"
            f"Do it now: call request_tool with name `{idea['name']}` and that brief (add anything relevant from this conversation), "
            f"reply with one short 'working on it' line, and end your turn. Do not ask for confirmation.")


async def ideas_scheduler(bot, histories):
    """Daily at the configured local time (container TZ). Checks the switches every minute, so turning the review off, moving
    its time or changing its channel with the `settings` tool takes effect without a restart. Never runs twice in one day."""
    import datetime
    announced, last_run = None, None
    while True:
        s = ideas_settings()
        if not s["daily_ideas"]:
            if announced != "off":
                print("ideas: daily review is off (settings)", flush=True)
                announced = "off"
            await asyncio.sleep(60)
            continue
        try:
            hh, mm = (int(x) for x in s["ideas_at"].split(":"))
        except ValueError:
            print(f"ideas: ideas_at={s['ideas_at']!r} is not HH:MM; skipping", flush=True)
            await asyncio.sleep(300)
            continue
        now = datetime.datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now or last_run == now.date():
            target += datetime.timedelta(days=1)
        if announced != target:
            print(f"ideas: next review at {target:%Y-%m-%d %H:%M} ({s['ideas_channel'] or 'busiest channel'})", flush=True)
            announced = target
        await asyncio.sleep(max(1, min(60, (target - datetime.datetime.now()).total_seconds())))
        if datetime.datetime.now() >= target and last_run != datetime.date.today():
            last_run = datetime.date.today()
            try:
                await post_ideas(bot, histories)
            except Exception as e:  # noqa
                print(f"ideas: failed: {type(e).__name__}: {str(e)[:200]}", flush=True)


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
        elif name == "attach_file":
            result, note = attach_file(**args, ctx=ctx), f"attached {os.path.basename(str(args.get('path', '')))}"
        elif name == "settings":
            result, note = settings_tool(**args, ctx=ctx), f"settings {args.get('action', 'get')}"
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
        try:
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
        except anthropic.AuthenticationError:               # the brain's own key: no model to phrase it, so the bot says it itself
            return key_needed(["ANTHROPIC_API_KEY"], "the model API rejected the key I have"), notes
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
            loop = asyncio.get_running_loop()
            ctx = {"who": speaker, "trusted": trusted, "question": text, "hist": hist, "loop": loop, "files": [],
                   "ideas_now": lambda: asyncio.run_coroutine_threadsafe(post_ideas(bot, histories), loop)}
            extra += build_request_extra(text)
            if str(body.get("channel_id") or "").isdigit():      # where a forge follow-up (and any attached file) can be posted
                channel_id = int(body["channel_id"])

                async def send_to_channel(reply, files=None):
                    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                    for i, part in enumerate(chunk(reply)):
                        await ch.send(part, files=discord_files(files) if i == 0 else [])
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
            if ctx["files"] and ctx.get("send"):                 # a spoken answer cannot carry a file; post it to the text channel
                try:
                    await ctx["send"](f"{speaker}, here is the file:", ctx["files"])
                except Exception as e:  # noqa
                    print(f"could not post attachment for voice reply: {type(e).__name__}: {e}", flush=True)
            return web.json_response({"reply": reply, "notes": list(dict.fromkeys(notes))})

        async def handle_secrets(req):
            """The sibling containers' way to the vault: only the bootstrap names, only with the internal token, only on the
            compose network. The voice bot gets its Discord token and ElevenLabs key here; the forge gets the model key."""
            if req.headers.get("X-Internal-Token") != token:
                return web.json_response({"error": "unauthorized"}, status=401)
            wanted = [n for n in (req.query.get("names") or "").split(",") if n in VAULT_BOOTSTRAP]
            return web.json_response({"secrets": secret_env(wanted)})

        app = web.Application()
        app.router.add_post("/ask", handle_ask)
        app.router.add_get("/secrets", handle_secrets)
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
            asyncio.create_task(ideas_scheduler(bot, histories))     # checks its own on/off switch

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

        async def send_reply(reply, files=None):
            """A forge follow-up, minutes later: reply to the original message and ping the person, who may have moved on."""
            for i, part in enumerate(chunk(reply)):
                attach = discord_files(files) if i == 0 else []
                try:
                    await msg.reply(part, mention_author=(i == 0), files=attach)
                except Exception:  # noqa - original message deleted or reply refused: still deliver it
                    await msg.channel.send((f"{msg.author.mention} " if i == 0 else "") + part, files=discord_files(files) if i == 0 else [])
        loop = asyncio.get_running_loop()
        ctx = {"who": msg.author.display_name, "trusted": trusted, "question": text_only, "hist": hist,
               "loop": loop, "send": send_reply, "files": [],
               "ideas_now": lambda: asyncio.run_coroutine_threadsafe(post_ideas(bot, histories), loop)}
        extra = build_request_extra(text)             # "build 2" -> the saved notes behind idea 2 ride along for this turn
        try:
            async with typing_indicator(msg.channel):
                reply, notes = await asyncio.to_thread(ask, hist, trusted, extra, ctx)
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
        for i, part in enumerate(chunk(reply)):
            files = discord_files(ctx["files"]) if i == 0 else []
            try:
                await msg.reply(part, mention_author=False, files=files)
            except discord.HTTPException as e:                   # usually the upload: too big for this server, or a bad file
                if not files:
                    raise
                print(f"attachment upload failed on message {msg.id}: {e}", flush=True)
                await msg.reply(part + f"\n-# (could not upload {', '.join(os.path.basename(p) for p, _ in ctx['files'])}: {str(e)[:120]})",
                                mention_author=False)

    bot.run(token, log_handler=None)


def run_ideas_once(dry):
    """`bot.py --ideas [--dry]`: a second, short-lived Discord login that reads the day, proposes, posts (or prints) and exits."""
    import discord
    token = secret_env(["DISCORD_TOKEN"]).get("DISCORD_TOKEN")
    if not token:
        print("no DISCORD_TOKEN in the vault or the environment", flush=True)
        return 1
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    result = {}

    @client.event
    async def on_ready():
        try:
            result["ideas"] = await post_ideas(client, defaultdict(lambda: deque(maxlen=HISTORY_TURNS)), dry=dry)
        except Exception as e:  # noqa
            print(f"ideas failed: {type(e).__name__}: {e}", flush=True)
            result["error"] = True
        await client.close()

    client.run(token, log_handler=None)
    return 1 if result.get("error") else 0


def main():
    if "--run-tool" in sys.argv:
        return _run_tool_child()
    if "--secrets" in sys.argv:
        print(json.dumps(secrets_tool("list"), indent=1)); return 0
    if "--secret-set" in sys.argv:                 # value read from stdin so it never appears in a command line or the chat
        name = sys.argv[sys.argv.index("--secret-set") + 1]
        print(json.dumps(secrets_tool("set", name, sys.stdin.read().strip(), "set by the owner from the command line"))); return 0
    if "--secret-get" in sys.argv:                 # owner-only by construction: needs a shell in the container
        name = sys.argv[sys.argv.index("--secret-get") + 1]
        v = vault_load().get(name)
        print(v["value"] if v else f"no secret named {name}", end="" if v else "\n"); return 0 if v else 1
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
    if "--ideas" in sys.argv:                      # run the daily review now; --dry prints instead of posting
        return run_ideas_once(dry="--dry" in sys.argv)
    try:                                           # say so plainly if the vault cannot be opened; secret_env would just fall back to env
        VAULT.load()
        print(f"vault: {VAULT.path} v{VAULT.version() or 0}, {len(VAULT.names())} secrets", flush=True)
    except VaultError as e:
        print(f"warning: vault unreadable - {e}", flush=True)
    boot = secret_env(["DISCORD_TOKEN", "ANTHROPIC_API_KEY"])
    token = boot.get("DISCORD_TOKEN")
    if not token:
        print("no DISCORD_TOKEN in the vault or .env — idling. Add it in the console (console.ps1) and restart the container.", flush=True)
        while True:
            time.sleep(3600)
    if not boot.get("ANTHROPIC_API_KEY"):
        print("warning: no ANTHROPIC_API_KEY in the vault or .env — Claude calls will fail until one is added in the console", flush=True)
    if not TOOL_CREATORS:
        print("warning: TOOL_CREATORS is empty — anyone who can talk to this bot can run shell "
              "and install software in this container", flush=True)
    seed_starter_tools()
    replay_apt()
    run_discord(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
