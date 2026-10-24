"""config.py: the validated settings, with a throwaway config file."""
from __future__ import annotations

import json

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
