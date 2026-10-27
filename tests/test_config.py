"""config.py: the validated settings, with a throwaway config file."""
from __future__ import annotations

import json
import threading
import time

import pytest

import config as app_config
from config import (ai_problem, config_problem, jupyter_folder, jupyter_problem, load_config,
                    read_raw_config, save_raw_config, write_private_json)


def config_file(tmp_path):
    return tmp_path / "config.json"


def test_ai_problems(monkeypatch):
    monkeypatch.delenv("KI_TOOLBOX_API_KEY", raising=False)  # the machine may have it
    base = {"ai_url": "https://some.box/api/v1", "ai_model": "m"}
    assert ai_problem(base) is None
    # The KI-Toolbox needs a token (or its environment variable).
    ki = {"ai_url": app_config.KI_TOOLBOX_URL, "ai_model": "m"}
    assert "KI-Toolbox token" in ai_problem(ki)
    assert ai_problem({**ki, "ai_token": "t"}) is None
    assert "AI URL" in ai_problem({**ki, "ai_url": " "})
    assert "AI model" in ai_problem({**ki, "ai_model": ""})


def test_config_problems(tmp_path, monkeypatch):
    monkeypatch.delenv("CHEMOTION_PASSWORD", raising=False)
    base = {"url": "https://chemotion.real.example", "user": "u", "password": "p"}
    assert config_problem(base) is None
    assert "Chemotion URL" in config_problem({**base, "url": app_config.EXAMPLE["url"]})
    assert "user" in config_problem({**base, "user": ""})
    assert "password" in config_problem({**base, "password": ""})
    # CHEMOTION_PASSWORD wins over the file.
    monkeypatch.setenv("CHEMOTION_PASSWORD", "from the environment")
    assert config_problem({**base, "password": ""}) is None


def test_jupyter_problems_and_folder():
    assert jupyter_problem({"jupyter_url": ""}) is None  # off, never a problem
    assert "token or password" in jupyter_problem({"jupyter_url": "http://j.test", "jupyter_token": ""})
    assert jupyter_problem({"jupyter_url": "http://j.test", "jupyter_token": "t"}) is None
    assert jupyter_folder({}) == app_config.DEFAULT_JUPYTER_FOLDER
    assert jupyter_folder({"jupyter_folder": "/My Lab/"}) == "My Lab"


def test_load_config_validates_and_clamps(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    monkeypatch.setattr(app_config, "CONFIG_FILE", path)
    monkeypatch.setattr(app_config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CHEMOTION_PASSWORD", raising=False)
    monkeypatch.delenv("KI_TOOLBOX_API_KEY", raising=False)
    save_raw_config({"url": "http://chemotion.test", "user": "u", "password": "p",
                     "ai_url": "http://ai.test/v1", "ai_model": "m", "ai_token": "t",
                     "execution_timeout": 5, "prompt_width": 100, "auto_run": "bogus",
                     "jupyter_url": "http://j.test", "jupyter_token": "t", "jupyter_folder": "/Lab/"})

    loaded = load_config()
    assert loaded.url == "http://chemotion.test"
    assert loaded.execution_timeout == 10  # minimum
    assert loaded.prompt_width == 360      # minimum
    assert loaded.auto_run == "off"        # unknown values are off
    assert loaded.jupyter_url == "http://j.test" and loaded.jupyter_folder == "Lab"
    assert loaded.jupyter is True
    assert loaded.ai_token == "t"


def test_load_config_raises_while_incomplete(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    monkeypatch.setattr(app_config, "CONFIG_FILE", path)
    monkeypatch.setattr(app_config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CHEMOTION_PASSWORD", raising=False)
    save_raw_config({"url": "http://chemotion.test", "user": "u"})  # no password

    try:
        load_config()
        raise AssertionError("load_config should have raised")
    except ValueError as exc:
        assert "password" in str(exc)


def test_legacy_models_are_mapped(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    monkeypatch.setattr(app_config, "CONFIG_FILE", path)
    monkeypatch.setattr(app_config, "CONFIG_DIR", tmp_path)
    write_private_json(path, {"ai_model": "ki-toolbox/claude-opus-5"})
    assert read_raw_config()["ai_model"] == app_config.LEGACY_MODELS["claude-opus-5"]


def use_config_file(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    monkeypatch.setattr(app_config, "CONFIG_FILE", path)
    monkeypatch.setattr(app_config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CHEMOTION_PASSWORD", raising=False)
    monkeypatch.delenv("KI_TOOLBOX_API_KEY", raising=False)
    return path


SETTINGS = {"url": "http://chemotion.test", "user": "u", "password": "p",
            "ai_url": "http://ai.test/v1", "ai_model": "m", "ai_token": "t",
            "jupyter_url": "http://j.test", "jupyter_token": "", "jupyter_password": "jp",
            "github_token": "gh"}


def test_secrets_go_into_the_keyring(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["secret_store"] == "keyring"
    assert all(on_disk[key] == "" for key in app_config.SECRET_KEYS)
    assert on_disk["user"] == "u"
    assert keyring.secrets == {"password": "p", "ai_token": "t", "jupyter_password": "jp", "github_token": "gh"}

    assert read_raw_config()["password"] == "p"
    loaded = load_config()
    assert (loaded.password, loaded.ai_token, loaded.jupyter_password, loaded.github_token) == ("p", "t", "jp", "gh")
    # The environment still overrides the keyring.
    monkeypatch.setenv("CHEMOTION_PASSWORD", "from the environment")
    assert load_config().password == "from the environment"

    # An emptied secret is deleted from the keyring.
    save_raw_config({**read_raw_config(), "jupyter_password": "", "jupyter_token": "jt"})
    assert keyring.secrets == {"password": "p", "ai_token": "t", "jupyter_token": "jt", "github_token": "gh"}


def test_secrets_are_read_again_when_the_file_changes(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    keyring.secrets["password"] = "changed elsewhere"
    assert read_raw_config()["password"] == "p"  # cached: config.json is unchanged
    # Another process (the settings) saved: config.json changed.
    write_private_json(app_config.CONFIG_FILE, {**json.loads(app_config.CONFIG_FILE.read_text()), "user": "v"})
    assert read_raw_config()["password"] == "changed elsewhere"


def test_a_failing_keyring_is_asked_again_later(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    keyring.fail = True
    assert read_raw_config()["password"] == ""
    assert "locked" in app_config.keyring_problem()
    keyring.fail = False
    assert read_raw_config()["password"] == ""  # not before SECRET_RETRY_S
    monkeypatch.setattr(app_config, "SECRET_RETRY_S", 0)
    assert read_raw_config()["password"] == "p"
    assert app_config.keyring_problem() == ""


def test_secrets_stay_in_the_file_without_a_keyring(tmp_path, monkeypatch, no_keyring):
    path = use_config_file(tmp_path, monkeypatch)
    save_raw_config({**SETTINGS, "secret_store": "keyring"})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "secret_store" not in on_disk and on_disk["password"] == "p"
    assert load_config().ai_token == "t"
    assert "no system keyring" in app_config.storage_location(True)
    assert app_config.migrate_secrets() is False


def test_secrets_stay_in_the_file_when_the_keyring_fails(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    keyring.fail = True
    save_raw_config(SETTINGS)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "secret_store" not in on_disk and on_disk["password"] == "p"
    keyring.fail = False
    keyring.secrets["password"] = "stale"  # not consulted without the marker
    assert read_raw_config()["password"] == "p"


def test_plain_text_secrets_are_migrated(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    write_private_json(path, SETTINGS)  # an older install
    assert app_config.migrate_secrets() is True
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["secret_store"] == "keyring" and on_disk["password"] == ""
    assert keyring.secrets["password"] == "p"
    assert load_config().password == "p"
    assert app_config.migrate_secrets() is False  # nothing left to move

    # A value typed into config.json later wins over the keyring and is moved too.
    write_private_json(path, {**on_disk, "ai_token": "new"})
    assert read_raw_config()["ai_token"] == "new"
    assert app_config.migrate_secrets() is True
    assert keyring.secrets["ai_token"] == "new" and keyring.secrets["password"] == "p"


def test_no_migration_while_the_keyring_cannot_be_read(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    write_private_json(path, {**json.loads(path.read_text()), "ai_token": "new"})
    keyring.fail = True
    assert app_config.migrate_secrets() is False  # would have deleted the password
    keyring.fail = False
    assert keyring.secrets["password"] == "p"


def test_the_config_file_is_private(tmp_path):
    path = config_file(tmp_path)
    write_private_json(path, {"password": "secret"})
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["password"] == "secret"


def test_config_is_hashable():
    """jobs.Jobs caches its JupyterBridges by (conversation, Config)."""
    config = app_config.Config(url="u", user="x", password="p", jupyter_url="http://j")
    assert hash(config) == hash(app_config.Config(url="u", user="x", password="p", jupyter_url="http://j"))
    assert {("conversation", config): 1}[("conversation", config)] == 1


def test_setup_ai_moves_the_secrets_into_the_keyring(tmp_path, monkeypatch, keyring, capsys):
    import setup_ai

    path = use_config_file(tmp_path, monkeypatch)
    write_private_json(path, {**SETTINGS, "ai_url": app_config.KI_TOOLBOX_URL})  # an older install
    setup_ai.main()
    assert "system keyring" in capsys.readouterr().out
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["secret_store"] == "keyring" and on_disk["ai_token"] == "" and on_disk["password"] == ""
    assert keyring.secrets["ai_token"] == "t" and keyring.secrets["password"] == "p"


def test_setup_ai_takes_the_token_from_the_environment(tmp_path, monkeypatch, keyring):
    import setup_ai

    use_config_file(tmp_path, monkeypatch)
    save_raw_config({**SETTINGS, "ai_token": ""})
    monkeypatch.setenv("KI_TOOLBOX_API_KEY", "from the environment")
    setup_ai.main()
    assert keyring.secrets["ai_token"] == "from the environment"


def test_setup_ai_leaves_the_config_while_the_keyring_fails(tmp_path, monkeypatch, keyring, capsys):
    import setup_ai

    path = use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    keyring.fail = True
    setup_ai.main()
    assert "could not read the system keyring" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == before
    assert keyring.secrets["password"] == "p"


def test_the_keyring_can_be_switched_off(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    assert keyring.secrets["password"] == "p"

    save_raw_config({**read_raw_config(), "use_keyring": False})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "secret_store" not in on_disk and on_disk["password"] == "p" and on_disk["ai_token"] == "t"
    assert keyring.secrets == {}  # not left behind
    assert load_config().password == "p"
    assert "switched off" in app_config.storage_location(app_config.uses_keyring(on_disk))
    assert app_config.migrate_secrets() is False  # plain text stays, as chosen

    save_raw_config({**read_raw_config(), "use_keyring": True})
    assert keyring.secrets["password"] == "p"
    assert json.loads(path.read_text(encoding="utf-8"))["password"] == ""


def test_the_main_thread_does_not_wait_for_a_locked_keyring(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    unlocked = threading.Event()
    keyring.on_read = unlocked.wait  # the keyring shows its unlock prompt
    monkeypatch.setattr(app_config, "_main_thread_wait", 0.2)

    started = time.monotonic()
    assert read_raw_config()["password"] == ""
    assert time.monotonic() - started < 2
    assert "not answered yet" in app_config.keyring_problem()
    # load_config() says why instead of asking for a password that is stored.
    with pytest.raises(ValueError, match="Could not read the passwords and tokens from the system keyring"):
        load_config()
    assert keyring.reads == 1  # still the one read, not a new one per call

    unlocked.set()
    app_config._reader.join(5)
    assert load_config().password == "p"
    assert app_config.keyring_problem() == ""


def test_keyring_failures_name_the_keyring(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    keyring.fail = True
    with pytest.raises(ValueError, match="system keyring: the keyring is locked"):
        load_config()


def test_keyring_only_changes_show_after_the_ttl(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)
    keyring.secrets["password"] = "changed in Seahorse"
    assert read_raw_config()["password"] == "p"
    monkeypatch.setattr(app_config, "SECRET_TTL_S", 0)
    assert read_raw_config()["password"] == "changed in Seahorse"


def test_the_migration_does_not_overwrite_a_save_meanwhile(tmp_path, monkeypatch, keyring):
    path = use_config_file(tmp_path, monkeypatch)
    write_private_json(path, SETTINGS)  # plain text, to be migrated
    save_raw_config(SETTINGS)
    write_private_json(path, {**json.loads(path.read_text()), "ai_token": "plain"})

    def settings_save():  # the settings process saves while the keyring is read
        keyring.on_read = None
        time.sleep(0.01)  # a new mtime
        write_private_json(path, {**json.loads(path.read_text()), "user": "saved meanwhile"})

    keyring.on_read = settings_save
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    assert app_config.migrate_secrets() is False
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["user"] == "saved meanwhile" and on_disk["ai_token"] == "plain"
    assert keyring.secrets["ai_token"] == "t"  # not overwritten with the old snapshot
    assert app_config.migrate_secrets() is True  # the next start moves it
    assert keyring.secrets["ai_token"] == "plain"


def test_a_failed_save_forgets_the_cache(tmp_path, monkeypatch, keyring):
    use_config_file(tmp_path, monkeypatch)
    save_raw_config(SETTINGS)

    def refuse(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(app_config, "write_private_json", refuse)
    with pytest.raises(OSError):
        save_raw_config({**SETTINGS, "password": "new"})
    assert app_config._secrets_cache is None


def test_a_failed_keyring_probe_is_tried_again(monkeypatch):
    import keystore

    attempts = []

    class Flaky:
        def __init__(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise keystore.KeystoreError("the keyring daemon is not up yet")

    monkeypatch.setattr(keystore, "_store", None)
    monkeypatch.setattr(keystore, "SecretService", Flaky)
    monkeypatch.setattr(keystore, "CredentialManager", Flaky)
    assert keystore.available() is False and "not up yet" in keystore.problem()
    assert keystore.available() is False and len(attempts) == 1  # not before PROBE_RETRY_S
    monkeypatch.setattr(keystore, "PROBE_RETRY_S", 0)
    assert keystore.available() is True and len(attempts) == 2
