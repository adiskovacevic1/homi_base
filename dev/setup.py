"""
setup.py: first-run configuration for bot-lab, terminal version. install.sh / install.ps1 prefer the browser version
(setup_web.py, same questions on a local web page with live checks); this one is the fallback for a machine without a
browser (`install.sh --terminal`) and holds the file-writing code both share.

    docker compose run --rm --no-deps dev python /opt/forge/setup.py

Writes bots/example-bot/.env, bots/voice-bot/.env and ./.env (all git-ignored), creates the household's kit folder,
and prints the Discord invite link. Nothing is sent anywhere; the values only land in those files on this PC.
"""
import getpass, json, os, re, secrets, sys, time
from pathlib import Path

import vault as vaultlib                              # bots/example-bot/vault.py, copied next to this file in the image

LAB = Path(os.environ.get("LAB_DIR", "/lab"))         # the repo, mounted into the dev container
BOT_ENV = LAB / "bots" / "example-bot" / ".env"
VOICE_ENV = LAB / "bots" / "voice-bot" / ".env"
ROOT_ENV = LAB / ".env"                               # compose interpolation: KIT=<household> picks the kit folder to mount
KITS = LAB / "bots" / "example-bot" / "kits"
VAULT_PATH = LAB / "bots" / "example-bot" / "data" / "secrets_manager" / "vault.enc"   # the bot's /data, on the host
RESTART_MARKER = LAB / ".restart-needed"              # the console leaves this when a change needs `docker compose up -d`; the wrapper acts on it
# Administrator: it makes its own channel, manages voice, and builds tools that may need any server permission later;
# a household bot is trusted with the house, and re-inviting for every new permission is what people gave up on.
INVITE_PERMS = 8


def console_port():
    """The port the console is published on, on this PC: the installer passes CONSOLE_PORT (a free one when 8792 is taken
    by another install); a re-run keeps what the root .env says; else 8792."""
    v = os.environ.get("CONSOLE_PORT", "").strip()
    if not v.isdigit() and ROOT_ENV.exists():
        m = re.search(r"^CONSOLE_PORT=(\d+)", ROOT_ENV.read_text(encoding="utf-8"), re.M)
        v = m.group(1) if m else ""
    return int(v) if v.isdigit() else 8792
KIT_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
# the model keys setup can take; in this order the first one given becomes the brain (Claude preferred: the forge uses it)
BRAIN_KEYS = {"anthropic": ("ANTHROPIC_API_KEY", "claude"), "openai": ("OPENAI_API_KEY", "openai"), "deepseek": ("DEEPSEEK_API_KEY", "deepseek")}
LIVE_SETTINGS = LAB / "bots" / "example-bot" / "data" / "settings.json"   # the bot's live switches; setup sets brain_provider here
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


def open_vault(passphrase=None, legacy_key=None):
    return vaultlib.Vault(VAULT_PATH, passphrase=passphrase or None, legacy_key=legacy_key or None)


def current_vault():
    """The vault as the running bot sees it: opened with what the env file holds. None when there is no config yet."""
    env = env_values(BOT_ENV)
    if not env:
        return None
    return open_vault(env.get("VAULT_PASSPHRASE"), env.get("VAULT_KEY"))


def update_env(path, changes):
    """Set or add KEY=value lines in an existing env file, keeping comments and order. Returns the keys that changed."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    changed, seen = [], set()
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) in changes:
            seen.add(m.group(1))
            if m.group(2) != str(changes[m.group(1)]):
                lines[i] = f"{m.group(1)}={changes[m.group(1)]}"
                changed.append(m.group(1))
    for k, v in changes.items():
        if k not in seen:
            lines.append(f"{k}={v}")
            changed.append(k)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return changed


def validate(cfg):
    """Shared by both front ends. Returns a list of problems; empty means write_files() will accept it."""
    problems = []
    if not looks_like_discord_token(cfg.get("token", "")):
        problems.append("the Discord token does not look like one (about 70 characters with two dots)")
    if cfg.get("anthropic") and not cfg["anthropic"].startswith("sk-ant-"):
        problems.append("the Anthropic key should start with sk-ant-")
    for k, label in (("openai", "OpenAI"), ("deepseek", "DeepSeek")):
        if cfg.get(k) and not cfg[k].startswith("sk-"):
            problems.append(f"the {label} key should start with sk-")
    if not any(cfg.get(k) for k in BRAIN_KEYS):
        problems.append("at least one model key is required: Anthropic, OpenAI or DeepSeek")
    if cfg.get("passphrase") and len(cfg["passphrase"]) < 8:
        problems.append("the vault passphrase should be at least 8 characters")
    owners = [p.strip() for p in cfg.get("owners", "").split(",") if p.strip()]
    if not owners or not all(p.isdigit() for p in owners):
        problems.append("at least one owner Discord user ID is required (numbers only)")
    if not KIT_RE.match(cfg.get("kit", "")):
        problems.append("kit name: lowercase letters, digits and dashes, starting with a letter")
    if cfg.get("app_id") and not cfg["app_id"].isdigit():
        problems.append("the Application ID is a long number")
    return problems


def env_values(path):
    """{NAME: value} from an existing env file; {} if none."""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return out


def backup_existing():
    """Before overwriting: copy each existing env file to <name>.bak-<timestamp> beside it (git-ignored like the original).
    A reconfigure that goes wrong is then a copy away from undone. Returns the backup paths."""
    import shutil
    stamp = time.strftime("%Y%m%d-%H%M%S")
    made = []
    for p in (BOT_ENV, VOICE_ENV, ROOT_ENV, VAULT_PATH):
        if p.exists():
            b = p.with_name(f"{p.name}.bak-{stamp}")
            shutil.copy2(p, b)
            made.append(str(b.relative_to(LAB)))
    return made


def write_files(cfg):
    """cfg keys: token, anthropic, owners (comma-separated), name, eleven, voice, auto, kit, app_id (optional), kit_remote,
    tz, passphrase (optional: kept from the existing config on a reconfigure, else the one given, else a suggested one),
    fresh_secrets (optional, default False: the existing passphrase and INTERNAL_TOKEN are kept, so the vault the bot
    already uses survives a reconfigure). On a reconfigure, a blank token/key means "keep the one in the vault".
    Backs up existing env files and the vault, writes the bot's keys into the vault and the settings into the three
    env files, creates the kit folder (empty -> the bot seeds the starter tools), returns a summary."""
    old = env_values(BOT_ENV)
    keep = bool(old) and not cfg.get("fresh_secrets")
    notes = []
    # the passphrase: kept on a reconfigure, otherwise chosen or suggested; a legacy VAULT_KEY install upgrades to it
    if keep and old.get("VAULT_PASSPHRASE"):
        passphrase, passphrase_is_new = old["VAULT_PASSPHRASE"], False
    else:
        passphrase, passphrase_is_new = (cfg.get("passphrase") or vaultlib.suggest_passphrase()), True
    v = open_vault(passphrase, legacy_key=old.get("VAULT_KEY") if keep else None)
    existing = {}
    if v.exists():
        try:
            existing = {n: d.get("value", "") for n, d in v.load().items()}
            if v.version() == 1:
                v.rekey(passphrase)
                notes.append("the vault was re-encrypted under the passphrase (it used a random key before)")
        except vaultlib.VaultError as e:
            aside = VAULT_PATH.with_name(f"vault.enc.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
            VAULT_PATH.rename(aside)
            notes.append(f"the existing vault could not be opened ({e}); it was moved aside as {aside.name} and a new one starts")
    # blank on a reconfigure = keep what is already configured: the vault first, then the env file (which is where an
    # install made before the vault held these, and where the voice bot's ElevenLabs settings used to live)
    old_voice = env_values(VOICE_ENV)
    for env_name, key in (("DISCORD_TOKEN", "token"), ("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai"), ("DEEPSEEK_API_KEY", "deepseek"),
                          ("ELEVENLABS_API_KEY", "eleven"), ("ELEVEN_VOICE_ID", "voice")):
        if not cfg.get(key):
            cfg[key] = existing.get(env_name) or (old.get(env_name, "") if keep else "") or (old_voice.get(env_name, "") if keep else "")
    problems = validate(cfg)
    if problems:
        raise ValueError("; ".join(problems))
    name = cfg.get("name") or "the house bot"
    owners = ",".join(p.strip() for p in cfg["owners"].split(",") if p.strip())
    kit, eleven, voice, auto = cfg["kit"], cfg.get("eleven", ""), cfg.get("voice", ""), cfg.get("auto", "")
    backups = backup_existing()
    internal = old.get("INTERNAL_TOKEN") if keep and old.get("INTERNAL_TOKEN") else secrets.token_urlsafe(32)
    kept = [n for n in ("INTERNAL_TOKEN",) if keep and old.get(n)] + (["VAULT_PASSPHRASE"] if not passphrase_is_new else [])
    wake = "hey bot," + ",".join(w for w in dict.fromkeys([name.lower(), name.lower().replace(" ", "")]) if w and w != "hey bot")

    v.set("DISCORD_TOKEN", cfg["token"], "the bot's Discord token - a change needs `docker compose up -d`")
    brain_provider = None
    for k, (env_name, provider) in BRAIN_KEYS.items():
        if cfg.get(k):
            v.set(env_name, cfg[k], f"model key for {provider}; a change applies live" + (" (the forge builds tools with it)" if k == "anthropic" else ""))
            brain_provider = brain_provider or provider
    if brain_provider:                                       # the first key given answers; owners switch later in chat or the console
        try:
            live = json.loads(LIVE_SETTINGS.read_text(encoding="utf-8")) if LIVE_SETTINGS.exists() else {}
        except ValueError:
            live = {}
        if live.get("brain_provider") not in [p for k, (_, p) in BRAIN_KEYS.items() if cfg.get(k)]:
            live["brain_provider"] = brain_provider; live.pop("brain_model", None)
            LIVE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            LIVE_SETTINGS.write_text(json.dumps(live, indent=2), encoding="utf-8")
    if eleven:
        v.set("ELEVENLABS_API_KEY", eleven, "ElevenLabs, for the voice bot's hearing and speaking")
    if voice:
        v.set("ELEVEN_VOICE_ID", voice, "the ElevenLabs voice the voice bot speaks with")

    write_env(BOT_ENV, [
        "# written by bot-lab setup - git-ignored. Keys live in the vault (console.ps1 edits them); settings live here.",
        "# Edit and `docker compose up -d example-bot` to apply a settings change.",
        "# opens data/secrets_manager/vault.enc: Discord token, model key, ElevenLabs, and every secret the tools declare",
        f"VAULT_PASSPHRASE={passphrase}",
        "BOT_MODEL=claude-opus-5",
        f"BOT_SYSTEM=You are {name}, a concise, friendly assistant living in a Discord server. Answer briefly.",
        "TOOL_TIMEOUT=30",
        "# Discord user IDs allowed to make tools, run shell and install software. Never leave this empty.",
        f"TOOL_CREATORS={owners}",
        "# channels it answers in without being @mentioned, by name or ID; empty = mention it everywhere",
        "OPEN_CHANNELS=",
        "# its own channel: created when it joins a server, where it introduces itself and answers everything; empty = none",
        "HOME_CHANNEL=bot",
        "# its running log: one line per thing it does on its own (tools built, used, forge jobs, ideas, keys needed); empty = off",
        "ACTIVITY_CHANNEL=activity",
        "# who it answers at all: owners = only the people in TOOL_CREATORS (channels, DMs and voice); anyone = a shared family bot",
        "TALK_TO=owners",
        "# direct messages, within TALK_TO: owners (default); anyone; off",
        "DM_POLICY=owners",
        "# shared secret for the internal /ask and /secrets endpoints (voice bot, forge, lan-helper); compose network only",
        f"INTERNAL_TOKEN={internal}",
        "# the forge on the dev box builds tools the model can't write in one go; FORGE_URL= turns it off",
        "FORGE_URL=http://bot-dev:8791",
        "FORGE_TIMEOUT=480",
        "# the owner's console on this PC (served by the dev box); the bot links people here when it needs a key added",
        f"CONSOLE_URL=http://127.0.0.1:{console_port()}/console",
        "# this household's tool kit: bots/example-bot/kits/<KIT> is mounted at /data/tools (same name in the root .env for compose)",
        f"KIT={kit}",
        "# daily review: the bot proposes 1-3 new tools in chat at this local time; empty = off. IDEAS_CHANNEL: name or id, empty = busiest",
        "IDEAS_AT=09:00",
        "IDEAS_CHANNEL=",
    ])
    write_env(VOICE_ENV, [
        "# written by bot-lab setup - git-ignored. The Discord token and keys come from the text bot's vault at runtime.",
        "# Edit and `docker compose up -d voice-bot` to apply a settings change.",
        "BOT_MODEL=claude-opus-5",
        f"WAKE_WORDS={wake}",
        f"AUTO_JOIN={auto}",
        "# answer through the text bot's brain (same tools, same memory) and fetch secrets from it",
        "BRAIN_URL=http://example-bot:8790/ask",
        f"INTERNAL_TOKEN={internal}",
    ])
    tz = cfg.get("tz") or "UTC"
    write_env(ROOT_ENV, ["# compose variables, written by bot-lab setup - git-ignored", f"KIT={kit}",
                         "# local timezone for the containers (logs, the daily ideas post)", f"TZ={tz}",
                         "# the console's port on this PC (127.0.0.1 only); the installer picks a free one when 8792 is taken by another bot",
                         f"CONSOLE_PORT={console_port()}"])
    kit_repo = init_kit_repo(KITS / kit, cfg.get("kit_remote", ""))
    return {"files": [str(p.relative_to(LAB)) for p in (BOT_ENV, VOICE_ENV, ROOT_ENV)], "kit": kit,
            "kit_tools": len(list((KITS / kit).glob("*.json"))), "kit_repo": kit_repo, "owners": owners, "name": name,
            "speech": "ElevenLabs" if eleven else "local (whisper + Piper)", "auto_join": auto or "off",
            "invite": invite_url(cfg.get("app_id", "")), "backups": backups, "kept_secrets": kept,
            "vault": str(VAULT_PATH.relative_to(LAB)), "vault_count": len(v.names()),
            "passphrase": passphrase if passphrase_is_new else None, "notes": notes}


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
        print("There is already a configuration on this PC. Replacing it re-asks the settings; a blank token or key keeps the one\n"
              "in the vault. The old files and the vault are backed up beside the new ones (*.bak-<time>), and the vault passphrase\n"
              "and internal token are kept so the bot's stored secrets keep working. For a single key change use console.ps1 instead.")
        if ask("Type REPLACE to go on, anything else to keep what is there", "keep") != "REPLACE":
            print("Kept the existing .env files.")
            return 0
    print("""
You need a Discord bot of your own. In https://discord.com/developers/applications:
  1. New Application -> give it the name people will see.
  2. Bot tab -> Reset Token -> copy the token (shown once).
  3. Bot tab -> Privileged Gateway Intents -> turn on MESSAGE CONTENT INTENT. Without it the bot hears nothing.
  4. General Information -> copy the Application ID (used for the invite link below).
Also: your own Discord user ID (Settings -> Advanced -> Developer Mode, then right-click yourself -> Copy User ID)
and a model API key: Anthropic (https://console.anthropic.com), OpenAI or DeepSeek - one is enough. ElevenLabs is optional.

Paste with a RIGHT-CLICK in this window (Ctrl+V does not paste here). Secret values are not shown as you paste them.
""")
    cfg = {}
    cfg["app_id"] = ask("Discord Application ID", check=str.isdigit, hint="a long number")
    reconfig = config_exists()
    cfg["token"] = ask("Discord bot token" + (" (Enter to keep the current one)" if reconfig else ""), secret=True, required=not reconfig,
                       check=looks_like_discord_token, hint="a Discord token is ~70 characters with two dots; Bot tab -> Reset Token to get a fresh one")
    print("The brain: at least one model key. The first you give answers first (Claude preferred; the forge builds tools with it).")
    cfg["anthropic"] = ask("Anthropic API key" + (" (Enter to keep the current one)" if reconfig else " (Enter to skip)"), secret=True,
                           check=lambda v: v.startswith("sk-ant-"), hint="starts with sk-ant-")
    cfg["openai"] = ask("OpenAI API key (Enter to skip)", secret=True, check=lambda v: v.startswith("sk-"), hint="starts with sk-")
    cfg["deepseek"] = ask("DeepSeek API key (Enter to skip)", secret=True, check=lambda v: v.startswith("sk-"), hint="starts with sk-")
    while not reconfig and not any(cfg.get(k) for k in BRAIN_KEYS):
        print("   one model key is required")
        cfg["anthropic"] = ask("Anthropic API key (or Enter, then OpenAI/DeepSeek)", secret=True, check=lambda v: v.startswith("sk-ant-"), hint="starts with sk-ant-")
        if not cfg["anthropic"]:
            cfg["openai"] = ask("OpenAI API key", secret=True, check=lambda v: v.startswith("sk-"), hint="starts with sk-")
        if not cfg["anthropic"] and not cfg["openai"]:
            cfg["deepseek"] = ask("DeepSeek API key", secret=True, check=lambda v: v.startswith("sk-"), hint="starts with sk-")
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
    cfg["tz"] = ask("Your timezone, for the daily post and logs (e.g. America/Chicago, Europe/London)", "UTC",
                    check=lambda v: re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)*", v) is not None, hint="an IANA name like America/New_York")
    if not (env_values(BOT_ENV).get("VAULT_PASSPHRASE")):
        print("\nAll keys go into one encrypted vault file. Its passphrase is the one thing to keep safe: file + passphrase = full backup.")
        cfg["passphrase"] = ask("Vault passphrase (Enter for a suggested one)", vaultlib.suggest_passphrase(),
                                check=lambda v: len(v) >= 8, hint="at least 8 characters")

    out = write_files(cfg)
    print("\nWrote", ", ".join(out["files"]), "; tool kit:", f"bots/example-bot/kits/{out['kit']}",
          f"({out['kit_tools']} tools)" if out["kit_tools"] else "(empty - the starter tools are seeded on first start)", "-", out["kit_repo"])
    print(f"Vault: {out['vault']} ({out['vault_count']} secrets)")
    if out["passphrase"]:
        print(f"\n  VAULT PASSPHRASE:  {out['passphrase']}\n  Write it down. It is also in bots/example-bot/.env; with it and the vault file everything can be restored.")
    for n in out["notes"]:
        print("Note:", n)
    if out["backups"]:
        print("Previous configuration backed up to:", ", ".join(out["backups"]), "| kept:", ", ".join(out["kept_secrets"]) or "nothing")
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
