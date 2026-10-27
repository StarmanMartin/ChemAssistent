"""Fixtures for the tests: a scripted AI endpoint, a throwaway workspace and
state directory, and the Chemotion test instance (skipped when it is off).

Nothing here touches the user's real config, history or ELN: every test gets
tmp_path-based directories, and the Chemotion tests only run against the
local test instance from chemotionapi's test_config (docker, port 3579).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # the app's modules, imported as top-level
sys.path.insert(0, str(HERE))         # fake_ai

import agent  # noqa: E402
import config as app_config  # noqa: E402  ("config" is pytest's hook argument
from fake_ai import FakeAI  # noqa: E402


# The login of the local test instance; chemotionapi's tests/config.json.
# CHEMOTION_TEST_CONFIG overrides the path, so the tests also run where the
# checkout lives elsewhere (CI).
TEST_CONFIG_PATHS = [
    Path(os.environ["CHEMOTION_TEST_CONFIG"]) if os.environ.get("CHEMOTION_TEST_CONFIG") else None,
    Path.home() / "Deveolop" / "KIT" / "chemotionapi" / "tests" / "config.json",
    HERE.parent.parent / "chemotionapi" / "tests" / "config.json",
]
# Where the Chemotion tests put what they create (test instance only).
TEST_COLLECTION = "/AIExamples/ChemAssistent tests"


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "chemotion: needs the local Chemotion test instance "
        "(auto-skipped when it is down; deselect with -m 'not chemotion')")
    config.addinivalue_line(
        "markers",
        "jupyter: starts a throwaway Jupyter server (JUPYTER_TEST_BIN or jupyter on the PATH; "
        "auto-skipped without it; deselect with -m 'not jupyter')")


@pytest.fixture(scope="session")
def chemotion_login() -> dict:
    """URL, user and password of the test instance, or skip."""
    for path in TEST_CONFIG_PATHS:
        if path is None or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        url = str(data.get("ELN_URL") or "").strip().rstrip("/")
        # 0.0.0.0 is how the container is configured; reach it on localhost.
        url = url.replace("//0.0.0.0", "//localhost")
        if url and data.get("ELN_USER"):
            return {"url": url, "user": str(data["ELN_USER"]), "password": str(data["ELN_PASS"])}
    pytest.skip("no chemotionapi test_config found (set CHEMOTION_TEST_CONFIG)")


@pytest.fixture(scope="session")
def chemotion(chemotion_login) -> dict:
    """The login, once it is known to work; skips when the instance is down."""
    pytest.importorskip("chemotion_api", reason="chemotion_api is not installed")
    from chemotion_api import Instance

    try:
        Instance(chemotion_login["url"]).test_connection().login(chemotion_login["user"],
                                                                 chemotion_login["password"])
    except Exception as exc:  # down, not set up, other credentials
        pytest.skip(f"the Chemotion test instance is not usable: {type(exc).__name__}: {exc}")
    return chemotion_login


@pytest.fixture
def instance(chemotion):
    """A logged-in chemotion_api.Instance against the test instance."""
    from chemotion_api import Instance

    return Instance(chemotion["url"]).test_connection().login(chemotion["user"], chemotion["password"])


@pytest.fixture
def test_collection(instance):
    """The collection the Chemotion tests write into (created once, kept)."""
    instance.get_root_collection().get_or_create_collection(TEST_COLLECTION)
    return TEST_COLLECTION


@pytest.fixture
def cleanup_samples(chemotion):
    """Collects sample ids to delete afterwards (the API has no delete())."""
    import requests
    from chemotion_api import Instance

    ids: list[int] = []
    yield ids
    if not ids:
        return
    token = Instance(chemotion["url"]).test_connection().login(chemotion["user"], chemotion["password"]).token
    for sample_id in ids:
        try:
            requests.delete(f"{chemotion['url']}/api/v1/samples/{sample_id}",
                            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except Exception:  # cleanup must not fail a passing test
            pass


@pytest.fixture(autouse=True)
def lessons_file(tmp_path, monkeypatch) -> Path:
    """Every test gets its own (empty) lessons file, never the user's."""
    import lessons

    path = tmp_path / "lessons" / "lessons.md"
    monkeypatch.setattr(lessons, "LESSONS_FILE", path)
    return path


class FakeKeyring:
    """keystore's backend interface, in memory."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.fail = False  # every call raises KeystoreError

    def _check(self) -> None:
        import keystore

        if self.fail:
            raise keystore.KeystoreError("the keyring is locked")

    def read(self, key: str) -> str:
        self._check()
        return self.secrets.get(key, "")

    def write(self, key: str, value: str, label: str) -> None:
        self._check()
        self.secrets[key] = value

    def delete(self, key: str) -> None:
        self._check()
        self.secrets.pop(key, None)


@pytest.fixture(autouse=True)
def keyring(monkeypatch) -> FakeKeyring:
    """Every test gets an empty in-memory keyring, never the user's."""
    import keystore

    store = FakeKeyring()
    monkeypatch.setattr(keystore, "_store", store)
    monkeypatch.setattr(app_config, "_secrets_cache", None)
    return store


@pytest.fixture
def no_keyring(monkeypatch) -> None:
    """A system without a keyring."""
    import keystore

    monkeypatch.setattr(keystore, "_store", False)
    monkeypatch.setattr(keystore, "_problem", "no Secret Service")


@pytest.fixture
def fake_ai():
    """A scripted AI endpoint; add turns with `script_next()`."""
    server = FakeAI()
    yield server
    server.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A throwaway workspace with two files; the read-only tools see it."""
    root = tmp_path / "workspace"
    (root / "chemotionapi").mkdir(parents=True)
    (root / "CHEMOTION_API.md").write_text("# API\nInstance.login(user, password)\n", encoding="utf-8")
    (root / "chemotionapi" / "sample.py").write_text("class Sample:\n    def save(self):\n        pass\n",
                                                     encoding="utf-8")
    monkeypatch.setattr(agent, "WORKSPACE_DIR", root)
    monkeypatch.setattr(app_config, "WORKSPACE_DIR", root)
    return root


@pytest.fixture
def skills_dir(tmp_path, monkeypatch) -> Path:
    """A throwaway skills folder (Agent adds it to its roots with Jupyter on)."""
    root = tmp_path / "skills" / "jupyter-calculations"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: jupyter-calculations\n---\nxTB and DFT.\n", encoding="utf-8")
    monkeypatch.setattr(agent, "SKILLS_DIR", root.parent)
    return root.parent


@pytest.fixture
def state_dir(tmp_path, monkeypatch) -> Path:
    """history.py and jobs.py write here instead of ~/.local/state."""
    import history
    import jobs

    root = tmp_path / "state"
    (root / "history").mkdir(parents=True)
    monkeypatch.setattr(history, "HISTORY_DIR", root / "history")
    monkeypatch.setattr(history, "EXCHANGE_DIR", root / "exchange")
    monkeypatch.setattr(jobs, "JOBS_FILE", root / "jobs.json")
    return root


@pytest.fixture
def ai_config(fake_ai):
    """A Config pointing at the scripted AI (Chemotion fields are dummies)."""
    return app_config.Config(url="http://chemotion.invalid", user="tester", password="secret",
                             ai_url=fake_ai.url, ai_token="test-token", ai_model="test-model")
