"""Test setup: put the bot's vault module and the dev box's setup module on the path, and point setup at a throwaway
tree so nothing touches a real install."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bots" / "example-bot"))   # vault.py
sys.path.insert(0, str(ROOT / "dev"))                    # setup.py (imports vault)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A fake /lab: setup.py's paths redirected into tmp_path. Yields the setup module."""
    import setup as S
    monkeypatch.setattr(S, "LAB", tmp_path)
    monkeypatch.setattr(S, "BOT_ENV", tmp_path / "bots" / "example-bot" / ".env")
    monkeypatch.setattr(S, "VOICE_ENV", tmp_path / "bots" / "voice-bot" / ".env")
    monkeypatch.setattr(S, "ROOT_ENV", tmp_path / ".env")
    monkeypatch.setattr(S, "KITS", tmp_path / "bots" / "example-bot" / "kits")
    monkeypatch.setattr(S, "VAULT_PATH", tmp_path / "bots" / "example-bot" / "data" / "secrets_manager" / "vault.enc")
    monkeypatch.setattr(S, "RESTART_MARKER", tmp_path / ".restart-needed")
    for p in (S.BOT_ENV.parent, S.VOICE_ENV.parent, S.KITS):
        p.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("VAULT_PASSPHRASE", raising=False)
    monkeypatch.delenv("VAULT_KEY", raising=False)
    return S


@pytest.fixture
def config(lab):
    """The arguments a fresh install supplies."""
    return dict(token="x" * 60 + ".aaaaaa.bbbbbbbbbbbbbbbbbbbbbbbbbbb", anthropic="sk-ant-test-value",
                owners="111111111111111111", name="Jarvis", eleven="", voice="", auto="general",
                kit="jarvis", app_id="", kit_remote="", tz="UTC", passphrase="a-test-passphrase")
