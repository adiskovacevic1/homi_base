"""Setup writes a household's configuration. The cases below are the ones that have gone wrong or would hurt if they
did: a reconfigure must never lose the keys or the passphrase, and an older install must migrate intact."""
import pytest
from cryptography.fernet import Fernet


def test_fresh_install_puts_keys_in_the_vault_not_the_env(lab, config):
    out = lab.write_files(config)
    env = lab.env_values(lab.BOT_ENV)
    assert env["VAULT_PASSPHRASE"] == "a-test-passphrase"
    for name in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY"):
        assert name not in env, f"{name} must live in the vault, not the env file"
    v = lab.current_vault()
    assert v.get("DISCORD_TOKEN") == config["token"]
    assert v.get("ANTHROPIC_API_KEY") == config["anthropic"]
    assert out["passphrase"] == "a-test-passphrase", "a new passphrase is shown once"
    assert out["vault_count"] == 2


def test_voice_env_carries_no_keys(lab, config):
    lab.write_files(dict(config, eleven="el-value", voice="voice-id"))
    voice = lab.env_values(lab.VOICE_ENV)
    for name in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        assert name not in voice, "the voice bot fetches keys from the brain, it must not hold copies"
    assert voice["INTERNAL_TOKEN"] == lab.env_values(lab.BOT_ENV)["INTERNAL_TOKEN"], "both sides share one token"
    assert lab.current_vault().get("ELEVENLABS_API_KEY") == "el-value"


def test_reconfigure_keeps_passphrase_internal_token_and_blank_fields(lab, config):
    lab.write_files(config)
    before = lab.env_values(lab.BOT_ENV)
    out = lab.write_files(dict(config, token="", anthropic="", passphrase="", name="Jarvis Two"))
    after, v = lab.env_values(lab.BOT_ENV), lab.current_vault()
    assert after["VAULT_PASSPHRASE"] == before["VAULT_PASSPHRASE"]
    assert after["INTERNAL_TOKEN"] == before["INTERNAL_TOKEN"]
    assert v.get("DISCORD_TOKEN") == config["token"], "a blank field keeps the key already configured"
    assert v.get("ANTHROPIC_API_KEY") == config["anthropic"]
    assert "Jarvis Two" in after["BOT_SYSTEM"], "settings still change"
    assert out["passphrase"] is None, "an unchanged passphrase is not shown again"
    assert any("vault.enc.bak-" in b for b in out["backups"]), "the vault is backed up before a rewrite"


def test_rotating_one_key_leaves_the_others(lab, config):
    lab.write_files(config)
    lab.write_files(dict(config, anthropic="sk-ant-rotated", token="", passphrase=""))
    v = lab.current_vault()
    assert v.get("ANTHROPIC_API_KEY") == "sk-ant-rotated"
    assert v.get("DISCORD_TOKEN") == config["token"]


def test_legacy_install_migrates_with_its_tool_secrets(lab, config):
    """The shape of a real install made before the vault held the bot's own keys."""
    import vault as V
    key = Fernet.generate_key().decode()
    lab.BOT_ENV.write_text(f"DISCORD_TOKEN={config['token']}\nANTHROPIC_API_KEY=sk-ant-old\n"
                           f"VAULT_KEY={key}\nINTERNAL_TOKEN=shared-token\nTOOL_CREATORS=111111111111111111\n")
    lab.VOICE_ENV.write_text("ELEVENLABS_API_KEY=el-old\nELEVEN_VOICE_ID=voice-old\n")
    old = V.Vault(lab.VAULT_PATH, passphrase=None, legacy_key=key)
    old.set("XAI_API_KEY", "xai-old", "written by the bot")
    out = lab.write_files(dict(config, token="", anthropic="", eleven="", voice="", passphrase=""))
    v = lab.current_vault()
    assert v.version() == 2 and any("re-encrypted" in n for n in out["notes"])
    assert v.get("XAI_API_KEY") == "xai-old", "a tool's secret must survive the migration"
    assert v.get("DISCORD_TOKEN") == config["token"], "keys move out of the env file into the vault"
    assert v.get("ANTHROPIC_API_KEY") == "sk-ant-old"
    assert v.get("ELEVENLABS_API_KEY") == "el-old" and v.get("ELEVEN_VOICE_ID") == "voice-old"
    assert lab.env_values(lab.BOT_ENV)["INTERNAL_TOKEN"] == "shared-token"


def test_fresh_secrets_starts_a_new_vault(lab, config):
    lab.write_files(config)
    lab.write_files(dict(config, passphrase="a-different-passphrase", fresh_secrets=True))
    assert lab.env_values(lab.BOT_ENV)["VAULT_PASSPHRASE"] == "a-different-passphrase"
    assert lab.current_vault().get("DISCORD_TOKEN") == config["token"]


def test_validation_rejects_bad_input(lab, config):
    with pytest.raises(ValueError, match="passphrase"):
        lab.write_files(dict(config, passphrase="short"))
    with pytest.raises(ValueError, match="Discord token"):
        lab.write_files(dict(config, token="short"))
    with pytest.raises(ValueError, match="sk-ant-"):
        lab.write_files(dict(config, anthropic="nope"))
    with pytest.raises(ValueError, match="owner"):
        lab.write_files(dict(config, owners=""))
    with pytest.raises(ValueError, match="kit name"):
        lab.write_files(dict(config, kit="Not A Kit"))


def test_kit_folder_is_created_as_its_own_repo(lab, config):
    out = lab.write_files(config)
    kit = lab.KITS / "jarvis"
    assert kit.is_dir() and out["kit_tools"] == 0, "a new kit is empty; the bot seeds the starters on first start"
    assert "git repo" in out["kit_repo"] or "plain folder" in out["kit_repo"]


def test_update_env_keeps_comments_and_changes_only_what_it_should(lab, config):
    lab.write_files(config)
    before = lab.BOT_ENV.read_text()
    changed = lab.update_env(lab.BOT_ENV, {"IDEAS_AT": "20:30", "TOOL_CREATORS": "111111111111111111"})
    after = lab.BOT_ENV.read_text()
    assert changed == ["IDEAS_AT"], "a value that is already right is not reported as changed"
    assert after.count("#") == before.count("#"), "comments survive"
    assert "IDEAS_AT=20:30" in after
