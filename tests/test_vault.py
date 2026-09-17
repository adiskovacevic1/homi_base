"""The vault: one encrypted file, one passphrase. These are the checks that matter if it ever has to be trusted with
the only copy of a household's keys."""
import json

import pytest
from cryptography.fernet import Fernet

import vault as V


def test_roundtrip_and_listing(tmp_path):
    v = V.Vault(tmp_path / "v.enc", passphrase="correct horse battery staple")
    v.set("XAI_API_KEY", "xai-value", "for images")
    v.set("DISCORD_TOKEN", "t" * 70)
    assert v.get("XAI_API_KEY") == "xai-value"
    assert v.names() == ["DISCORD_TOKEN", "XAI_API_KEY"]
    listing = {e["name"]: e for e in v.listing()}
    assert "value" not in listing["XAI_API_KEY"], "listing must never carry values"
    assert listing["XAI_API_KEY"]["note"] == "for images"
    assert listing["DISCORD_TOKEN"]["needs_restart"] is True
    assert listing["XAI_API_KEY"]["needs_restart"] is False


def test_file_is_opaque_without_the_passphrase(tmp_path):
    v = V.Vault(tmp_path / "v.enc", passphrase="the passphrase")
    v.set("XAI_API_KEY", "a-very-distinctive-value")
    raw = (tmp_path / "v.enc").read_bytes()
    assert b"a-very-distinctive-value" not in raw
    assert b"XAI_API_KEY" not in raw
    assert json.loads(raw)["v"] == 2


def test_wrong_passphrase_is_refused(tmp_path):
    V.Vault(tmp_path / "v.enc", passphrase="right one").set("A_KEY", "x")
    with pytest.raises(V.VaultError):
        V.Vault(tmp_path / "v.enc", passphrase="wrong one").load()
    v = V.Vault(tmp_path / "v.enc", passphrase="right one")
    assert v.check_passphrase("right one") and not v.check_passphrase("wrong one")


def test_missing_passphrase_is_refused(tmp_path):
    V.Vault(tmp_path / "v.enc", passphrase="p").set("A_KEY", "x")
    with pytest.raises(V.VaultError):
        V.Vault(tmp_path / "v.enc", passphrase=None, legacy_key=None).load()
    with pytest.raises(V.VaultError):                      # and nothing can be written without one
        V.Vault(tmp_path / "w.enc", passphrase=None, legacy_key=None).save({})


def test_salt_changes_every_save(tmp_path):
    v = V.Vault(tmp_path / "v.enc", passphrase="p")
    v.set("A_KEY", "1")
    first = json.loads((tmp_path / "v.enc").read_bytes())["salt"]
    v.set("B_KEY", "2")
    assert json.loads((tmp_path / "v.enc").read_bytes())["salt"] != first


def test_legacy_v1_opens_and_upgrades(tmp_path):
    """Installs made before the passphrase existed used a random Fernet key; they must keep working and move over."""
    key = Fernet.generate_key().decode()
    v1 = V.Vault(tmp_path / "v.enc", passphrase=None, legacy_key=key)
    v1.set("PLEX_TOKEN", "plex-value")
    assert v1.version() == 1
    v1.rekey("a new passphrase")
    assert v1.version() == 2
    assert V.Vault(tmp_path / "v.enc", passphrase="a new passphrase").get("PLEX_TOKEN") == "plex-value"


def test_values_skips_missing_and_empty(tmp_path):
    v = V.Vault(tmp_path / "v.enc", passphrase="p")
    v.set("A_KEY", "1")
    assert v.values(["A_KEY", "NOT_THERE"]) == {"A_KEY": "1"}


def test_delete(tmp_path):
    v = V.Vault(tmp_path / "v.enc", passphrase="p")
    v.set("A_KEY", "1")
    v.delete("A_KEY")
    assert v.names() == []
    with pytest.raises(KeyError):
        v.delete("A_KEY")


def test_missing_file_is_empty_not_an_error(tmp_path):
    assert V.Vault(tmp_path / "nothing.enc", passphrase="p").load() == {}


def test_suggested_passphrase_is_not_predictable():
    a, b = V.suggest_passphrase(), V.suggest_passphrase()
    assert a != b and len(a.split("-")) == 5
