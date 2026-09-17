"""
setup.py: first-run configuration for bot-lab, terminal version. install.sh / install.ps1 prefer the browser version
(setup_web.py, same questions on a local web page with live checks); this one is the fallback for a machine without a
browser (`install.sh --terminal`) and holds the file-writing code both share.

    docker compose run --rm --no-deps dev python /opt/forge/setup.py

Writes bots/example-bot/.env, bots/voice-bot/.env and ./.env (all git-ignored), creates the household's kit folder,
and prints the Discord invite link. Nothing is sent anywhere; the values only land in those files on this PC.
"""
import base64, getpass, os, re, secrets, sys
from pathlib import Path

LAB = Path(os.environ.get("LAB_DIR", "/lab"))         # the repo, mounted into the dev container
BOT_ENV = LAB / "bots" / "example-bot" / ".env"
VOICE_ENV = LAB / "bots" / "voice-bot" / ".env"
ROOT_ENV = LAB / ".env"                               # compose interpolation: KIT=<household> picks the kit folder to mount
KITS = LAB / "bots" / "example-bot" / "kits"
# View Channels, Send Messages, Read Message History, Attach Files, Embed Links, Add Reactions, Connect, Speak, Use Voice Activity
INVITE_PERMS = 1024 | 2048 | 65536 | 32768 | 16384 | 64 | 1048576 | 2097152 | 33554432
KIT_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x1f\x7f]")   # terminal escape sequences (bracketed-paste markers) and control chars


def clean(v):
    """A paste into a Docker TTY on Windows can arrive wrapped in bracketed-paste markers (ESC[200~ ... ESC[201~) or with a
    stray CR; a token with those in it fails later as 'invalid Authorization header'. Strip anything unprintable."""
    return CONTROL.sub("", v or "").strip()


def looks_like_discord_token(v):
    return len(v) > 50 and v.count(".") == 2


def kit_default_for(name):
    return re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "home"


def existing_kits():
    """{kit name: number of tools} for every kit folder in the repo."""
    if not KITS.exists():
        return {}
    return {p.name: len(list(p.glob("*.json"))) for p in sorted(KITS.iterdir()) if p.is_dir()}


def config_exists():
    return BOT_ENV.exists() or VOICE_ENV.exists()


def invite_url(app_id):
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions={INVITE_PERMS}" if app_id else ""


def write_env(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def validate(cfg):
    """Shared by both front ends. Returns a list of problems; empty means write_files() will accept it."""
    problems = []
    if not looks_like_discord_token(cfg.get("token", "")):
        problems.append("the Discord token does not look like one (about 70 characters with two dots)")
    if not cfg.get("anthropic", "").startswith("sk-ant-"):
        problems.append("the Anthropic key should start with sk-ant-")
    owners = [p.strip() for p in cfg.get("owners", "").split(",") if p.strip()]
    if not owners or not all(p.isdigit() for p in owners):
        problems.append("at least one owner Discord user ID is required (numbers only)")
    if not KIT_RE.match(cfg.get("kit", "")):
        problems.append("kit name: lowercase letters, digits and dashes, starting with a letter")
    if cfg.get("app_id") and not cfg["app_id"].isdigit():
        problems.append("the Application ID is a long number")
    return problems


def write_files(cfg):
    """cfg keys: token, anthropic, owners (comma-separated), name, eleven, voice, auto, kit, app_id (optional).
    Writes the three env files, creates the kit folder (empty -> the bot seeds the starter tools), returns a summary."""
    problems = validate(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    name = cfg.get("name") or "the house bot"
    owners = ",".join(p.strip() for p in cfg["owners"].split(",") if p.strip())
    kit, eleven, voice, auto = cfg["kit"], cfg.get("eleven", ""), cfg.get("voice", ""), cfg.get("auto", "")
    internal = secrets.token_urlsafe(32)
    vault = base64.urlsafe_b64encode(os.urandom(32)).decode()   # a Fernet key
    wake = "hey bot," + ",".join(w for w in dict.fromkeys([name.lower(), name.lower().replace(" ", "")]) if w and w != "hey bot")

    write_env(BOT_ENV, [
        "# written by bot-lab setup - git-ignored. Edit and `docker compose up -d example-bot` to apply.",
        f"DISCORD_TOKEN={cfg['token']}",
        f"ANTHROPIC_API_KEY={cfg['anthropic']}",
        "BOT_MODEL=claude-opus-5",
        f"BOT_SYSTEM=You are {name}, a concise, friendly assistant living in a Discord server. Answer briefly.",
        "TOOL_TIMEOUT=30",
        "# Discord user IDs allowed to make tools, run shell and install software. Never leave this empty.",
        f"TOOL_CREATORS={owners}",
        "# channels it answers in without being @mentioned, by name or ID; empty = mention it everywhere",
        "OPEN_CHANNELS=",
        "# shared secret for the internal /ask endpoint (voice bot) and the forge (dev box); compose network only",
        f"INTERNAL_TOKEN={internal}",
        "# key for the encrypted secret vault the bot's tools draw from",
        f"VAULT_KEY={vault}",
        "# the forge on the dev box builds tools the model can't write in one go; FORGE_URL= turns it off",
        "FORGE_URL=http://bot-dev:8791",
        "FORGE_TIMEOUT=480",
        "# this household's tool kit: bots/example-bot/kits/<KIT> is mounted at /data/tools (same name in the root .env for compose)",
        f"KIT={kit}",
        "# daily review: the bot proposes 1-3 new tools in chat at this local time; empty = off. IDEAS_CHANNEL: name or id, empty = busiest",
        "IDEAS_AT=09:00",
        "IDEAS_CHANNEL=",
    ])
    write_env(VOICE_ENV, [
        "# written by bot-lab setup - git-ignored. Edit and `docker compose up -d voice-bot` to apply.",
        f"DISCORD_TOKEN={cfg['token']}",
        f"ANTHROPIC_API_KEY={cfg['anthropic']}",
        "BOT_MODEL=claude-opus-5",
        f"WAKE_WORDS={wake}",
        f"AUTO_JOIN={auto}",
        "# ElevenLabs for hearing and speaking; empty key = fully local (faster-whisper + Piper)",
        f"ELEVENLABS_API_KEY={eleven}",
        f"ELEVEN_VOICE_ID={voice}",
        "# answer through the text bot's brain: same tools, same memory",
        "BRAIN_URL=http://example-bot:8790/ask",
        f"INTERNAL_TOKEN={internal}",
    ])
    write_env(ROOT_ENV, ["# compose variables, written by bot-lab setup - git-ignored", f"KIT={kit}"])
    kit_repo = init_kit_repo(KITS / kit, cfg.get("kit_remote", ""))
    return {"files": [str(p.relative_to(LAB)) for p in (BOT_ENV, VOICE_ENV, ROOT_ENV)], "kit": kit,
            "kit_tools": len(list((KITS / kit).glob("*.json"))), "kit_repo": kit_repo, "owners": owners, "name": name,
            "speech": "ElevenLabs" if eleven else "local (whisper + Piper)", "auto_join": auto or "off",
            "invite": invite_url(cfg.get("app_id", ""))}


def init_kit_repo(kit_dir, remote=""):
    """A kit is a folder the code repo ignores. Making it a git repo of its own lets the forge commit every tool it builds
    and the owner back the kit up wherever they like; `remote` (optional) becomes its origin. Returns a short status."""
    import subprocess
    kit_dir.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(kit_dir), "-c", "safe.directory=*"]
    try:
        if not (kit_dir / ".git").exists():
            subprocess.run(git + ["init", "-q", "-b", "main"], check=True, capture_output=True, timeout=30)
            (kit_dir / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8", newline="\n")
            (kit_dir / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8", newline="\n")   # tools run in Linux
        if remote:
            subprocess.run(git + ["remote", "remove", "origin"], capture_output=True, timeout=30)
            subprocess.run(git + ["remote", "add", "origin", remote], check=True, capture_output=True, timeout=30)
            return f"git repo, origin {remote} (push from bots/example-bot/kits/{kit_dir.name} to back it up)"
        return "git repo (local only; add a remote to back it up)"
    except Exception as e:  # noqa - the kit works as a plain folder; only the commit history is lost
        return f"plain folder (git init failed: {type(e).__name__})"


# ---------------------------------------------------------------- the terminal front end

def ask(prompt, default="", secret=False, required=False, check=None, hint=""):
    while True:
        shown = f" [{default}]" if default and not secret else ""
        line = f"{prompt}{shown}: "
        raw = getpass.getpass(line) if secret else input(line)
        v = clean(raw) or default
        if "\x16" in raw and not v:
            print("   that was the Ctrl+V key itself - this window does not paste with Ctrl+V. Right-click to paste, then Enter.")
            continue
        if required and not v:
            print("   this one is required" + (f" - {hint}" if hint else ""))
            continue
        if v and check and not check(v):
            print("   that doesn't look right" + (f" - {hint}" if hint else ""))
            continue
        return v


def main():
    if not LAB.exists():
        sys.exit("run this through install.sh or install.ps1 - the repo has to be mounted at /lab")
    print("\nbot-lab setup\n" + "-" * 60)
    if config_exists():
        if ask("There is already a configuration here. Overwrite it? (yes/no)", "no").lower() not in ("y", "yes"):
            print("Kept the existing .env files.")
            return 0
    print("""
You need a Discord bot of your own. In https://discord.com/developers/applications:
  1. New Application -> give it the name people will see.
  2. Bot tab -> Reset Token -> copy the token (shown once).
  3. Bot tab -> Privileged Gateway Intents -> turn on MESSAGE CONTENT INTENT. Without it the bot hears nothing.
  4. General Information -> copy the Application ID (used for the invite link below).
Also: your own Discord user ID (Settings -> Advanced -> Developer Mode, then right-click yourself -> Copy User ID)
and an Anthropic API key from https://console.anthropic.com. ElevenLabs is optional.

Paste with a RIGHT-CLICK in this window (Ctrl+V does not paste here). Secret values are not shown as you paste them.
""")
    cfg = {}
    cfg["app_id"] = ask("Discord Application ID", check=str.isdigit, hint="a long number")
    cfg["token"] = ask("Discord bot token", secret=True, required=True, check=looks_like_discord_token,
                       hint="a Discord token is ~70 characters with two dots; Bot tab -> Reset Token to get a fresh one")
    cfg["anthropic"] = ask("Anthropic API key", secret=True, required=True, check=lambda v: v.startswith("sk-ant-"), hint="starts with sk-ant-")
    cfg["owners"] = ask("Your Discord user ID(s), comma-separated - the only people allowed to give the bot new tools and a shell",
                        required=True, check=lambda v: all(p.strip().isdigit() for p in v.split(",")), hint="numbers only")
    cfg["name"] = ask("What should the bot call itself", "the house bot")
    cfg["eleven"] = ask("ElevenLabs API key (Enter to skip; hearing and speaking then stay local)", secret=True)
    cfg["voice"] = ask("ElevenLabs voice ID (Enter for the default voice; `node index.js --voices` lists yours later)") if cfg["eleven"] else ""
    cfg["auto"] = ask("Voice channel the voice bot joins by itself when someone arrives (name or ID; Enter for none)", "general")
    kits = existing_kits()
    while True:
        kit = ask(f"Name for this household's tool kit (a folder under bots/example-bot/kits; existing: {', '.join(kits) or 'none'})",
                  kit_default_for(cfg["name"]), check=lambda v: KIT_RE.match(v) is not None, hint="lowercase letters, digits, dashes")
        if not kits.get(kit):
            break
        # An existing, non-empty kit is right when it is yours (reconfiguring this PC) and wrong when it is another household's:
        # their tools would run here and this bot's new tools would land in their folder.
        if ask(f"   kits/{kit} already has {kits[kit]} tools. Is that this household's kit, to keep using? (yes/no)", "no").lower() in ("y", "yes"):
            break
        print("   pick a different name, then")
    cfg["kit"] = kit
    cfg["kit_remote"] = ask("Git URL to back the kit up to (optional, e.g. a private GitHub repo; Enter to skip)")

    out = write_files(cfg)
    print("\nWrote", ", ".join(out["files"]), "; tool kit:", f"bots/example-bot/kits/{out['kit']}",
          f"({out['kit_tools']} tools)" if out["kit_tools"] else "(empty - the starter tools are seeded on first start)", "-", out["kit_repo"])
    if out["invite"]:
        print(f"\nInvite the bot to your server with this link:\n  {out['invite']}")
    print(f"""
Then: @mention the bot in a channel. It starts with a few basic tools and builds the rest as people ask.
Speech: {out['speech']}. Voice bot auto-join: {out['auto_join']}.
Owner(s): {out['owners']}. Everyone else can talk to the bot and use its tools, but cannot add tools or run commands.
""")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled")
        sys.exit(130)
