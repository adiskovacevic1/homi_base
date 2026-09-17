"""
vault.py: the bot's encrypted secret store, shared by the bot (which reads it) and the setup/console server (which edits it).

One file, one passphrase. /data/secrets_manager/vault.enc holds every key the household's bot uses - its Discord token,
Anthropic and ElevenLabs keys, and whatever its tools declare - as {name: {"value", "note", "created", "updated"}},
encrypted with a Fernet key derived from VAULT_PASSPHRASE (scrypt, salt stored in the file). The file plus the passphrase
is a complete backup; the file alone is safe to keep anywhere.

Format v2:  {"v": 2, "salt": <base64>, "data": <fernet token>}
Format v1:  a bare Fernet token encrypted with a random VAULT_KEY (older installs); readable while VAULT_KEY is set and
            rewritten as v2 on the first save once a passphrase exists.
"""
import base64, functools, json, os, secrets, time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

DEFAULT_FILE = Path(os.environ.get("VAULT_FILE", "/data/secrets_manager/vault.enc"))
NAME_RE = r"^[A-Z][A-Z0-9_]{1,63}$"
# The keys the bot itself and its sibling containers need; the brain's /secrets route hands out only these.
BOOTSTRAP = ("DISCORD_TOKEN", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "ELEVEN_VOICE_ID")
NEEDS_RESTART = ("DISCORD_TOKEN",)              # everything else is read on use, so a change applies live

WORDS = ("amber basil cedar coral delta ember fable flint gale grove harbor iris jade juniper kelp lark lemon lunar maple marsh "
         "meadow mesa nectar north oak olive onyx opal orbit otter pearl pine plume quartz raven reef ridge river robin rosin "
         "sage sand shale slate spruce summit tide timber topaz trail tulip umber vale velvet violet wave willow winter zephyr").split()


class VaultError(Exception):
    """Something the owner has to fix: missing or wrong passphrase, unreadable file."""


@functools.lru_cache(maxsize=16)
def derive_key(passphrase, salt):
    """scrypt is deliberately slow (~50 ms); the bot opens the vault on every tool call, so the result is cached per salt."""
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def suggest_passphrase():
    """Four words and a number: memorable, and about 40 bits from a 64-word list plus the number, which is plenty for a
    file that never leaves the house and is only ever attacked by someone who already has it."""
    return "-".join(secrets.choice(WORDS) for _ in range(4)) + "-" + str(secrets.randbelow(90) + 10)


class Vault:
    def __init__(self, path=None, passphrase=None, legacy_key=None):
        self.path = Path(path or DEFAULT_FILE)
        self.passphrase = passphrase if passphrase is not None else (os.environ.get("VAULT_PASSPHRASE") or None)
        self.legacy_key = legacy_key if legacy_key is not None else (os.environ.get("VAULT_KEY") or None)

    # ---- file
    def exists(self):
        try:
            return self.path.exists() and self.path.read_bytes().strip() != b""
        except OSError:
            return False

    def version(self):
        if not self.exists():
            return None
        return 2 if self.path.read_bytes().strip().startswith(b"{") else 1

    def load(self):
        """The whole store as a dict. {} when there is no file yet."""
        if not self.exists():
            return {}
        raw = self.path.read_bytes().strip()
        if raw.startswith(b"{"):
            hdr = json.loads(raw)
            if not self.passphrase:
                raise VaultError("the vault is locked with a passphrase and VAULT_PASSPHRASE is not set")
            try:
                return json.loads(Fernet(derive_key(self.passphrase, base64.b64decode(hdr["salt"]))).decrypt(hdr["data"].encode()).decode())
            except (InvalidToken, KeyError, ValueError):
                raise VaultError("wrong vault passphrase")
        if not self.legacy_key:
            raise VaultError("this vault was encrypted with a random VAULT_KEY that is no longer set; its contents cannot be read")
        try:
            return json.loads(Fernet(self.legacy_key.strip().encode()).decrypt(raw).decode())
        except (InvalidToken, ValueError):
            raise VaultError("VAULT_KEY does not open this vault")

    def save(self, data):
        """Always writes v2 when a passphrase is set (so a v1 file upgrades on its first save); v1 otherwise."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.passphrase:
            salt = os.urandom(16)
            token = Fernet(derive_key(self.passphrase, salt)).encrypt(json.dumps(data).encode()).decode()
            body = json.dumps({"v": 2, "salt": base64.b64encode(salt).decode(), "data": token}).encode()
        elif self.legacy_key:
            body = Fernet(self.legacy_key.strip().encode()).encrypt(json.dumps(data).encode())
        else:
            raise VaultError("no VAULT_PASSPHRASE set - the owner sets one in setup or the console before secrets can be stored")
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, self.path)

    # ---- entries
    def names(self):
        return sorted(self.load())

    def get(self, name):
        return (self.load().get(name) or {}).get("value")

    def values(self, names):
        """{name: value} for the names present, for injecting into a tool's environment."""
        data = self.load()
        return {n: data[n]["value"] for n in names if n in data and data[n].get("value")}

    def set(self, name, value, note=None):
        data = self.load()
        prev = data.get(name, {})
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        data[name] = {"value": value, "note": note if note is not None else prev.get("note", ""),
                      "created": prev.get("created", now), "updated": now}
        self.save(data)
        return {"name": name, "length": len(value), "updated": now, "replaced": bool(prev)}

    def delete(self, name):
        data = self.load()
        if name not in data:
            raise KeyError(name)
        del data[name]
        self.save(data)

    def listing(self):
        """Names, notes, timing and lengths - never values. What a UI or the model may see."""
        data = self.load()
        return [{"name": n, "note": d.get("note", ""), "created": d.get("created"), "updated": d.get("updated"),
                 "length": len(d.get("value", "")), "needs_restart": n in NEEDS_RESTART} for n, d in sorted(data.items())]

    # ---- passphrase
    def check_passphrase(self, passphrase):
        """True when `passphrase` opens the file (used before revealing a value)."""
        try:
            Vault(self.path, passphrase=passphrase, legacy_key=self.legacy_key).load()
            return True
        except VaultError:
            return False

    def rekey(self, new_passphrase):
        """Re-encrypt under a new passphrase (also how a v1 file becomes v2)."""
        data = self.load()
        self.passphrase = new_passphrase
        self.save(data)
