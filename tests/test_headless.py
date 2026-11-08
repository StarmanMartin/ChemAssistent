"""Headless package exercises the real agent against the scripted AI.

conftest.py makes `chemassistent` this source tree with the patched
top-level modules; test_package_imports runs the package's own imports in a
subprocess with a throwaway home."""
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import chemassistent.headless as headless
from chemassistent import AgentError, ChemAssistent, Config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def config(fake_ai):
    return Config(url="http://example.invalid", user="test", password="test",
                  ai_url=fake_ai.url, ai_model="test")


class FakeRunner:
    """subprocess.Popen for the runner: answers with `stdout`/`stderr`."""
    calls: list = []
    stdout = '{"ok": true, "result": 42}'
    stderr = ""
    returncode = 0
    raise_on_communicate: BaseException | None = None

    def __init__(self, args, **kwargs):
        FakeRunner.calls.append((args, kwargs))
        self.pid = 0

    def communicate(self, data, timeout=None):
        if FakeRunner.raise_on_communicate is not None:
            raise FakeRunner.raise_on_communicate
        return FakeRunner.stdout, FakeRunner.stderr

    def poll(self):
        return self.returncode


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(FakeRunner, "calls", [])
    monkeypatch.setattr(headless.subprocess, "Popen", FakeRunner)
    return FakeRunner


def tool_answers(conv) -> list[str]:
    return [m["content"] for m in conv._agent.messages if m["role"] == "tool"]


def run_snippet(fake_ai, conv, code="result = 42", final="done"):
    fake_ai.script_next({"tool_calls": [{"name": "execute_python", "arguments": {
        "purpose": "read", "code": code}}]}, {"content": final})
    return conv.prompt("go")


def test_conversations_and_followup(fake_ai, config):
    assistant = ChemAssistent(config)
    first = assistant.conversations.new_conversation()
    other = assistant.conversations.new_conversation()
    fake_ai.script_next({"content": "first"})
    assert first.prompt("hello") == "first"
    fake_ai.script_next({"content": "second"})
    assert first.prompt("again") == "second"
    assert any(m.get("content") == "hello" for m in fake_ai.requests[-1]["messages"])
    fake_ai.script_next({"content": "independent"})
    assert other.prompt("separate") == "independent"
    assert not any(m.get("content") == "hello" for m in fake_ai.requests[-1]["messages"])


def test_code_requires_approval(fake_ai, config, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unapproved code was executed")
    monkeypatch.setattr(headless.subprocess, "Popen", forbidden)
    conv = ChemAssistent(config).conversations.new_conversation()
    assert run_snippet(fake_ai, conv, "instance.delete()", "Not run.") == "Not run."
    assert "not approved" in tool_answers(conv)[0]


def test_approved_execution(fake_ai, config, runner):
    seen = []
    conv = ChemAssistent(config, approve=lambda purpose, code: seen.append((purpose, code)) or True
                         ).conversations.new_conversation()
    assert run_snippet(fake_ai, conv, final="42") == "42"
    assert seen == [("read", "result = 42")]
    assert "result = 42" in tool_answers(conv)[0]
    # By path, not `-m`: the caller's cwd is not on the runner's sys.path.
    args, kwargs = runner.calls[0]
    assert args == [sys.executable, str(ROOT / "chemotion_runner.py")]


def test_output_is_clipped(fake_ai, config, runner, monkeypatch):
    monkeypatch.setattr(runner, "stdout", json.dumps({"ok": True, "stdout": "x" * 100000, "result": None}))
    conv = ChemAssistent(config, approve=lambda *a: True).conversations.new_conversation()
    run_snippet(fake_ai, conv)
    answer = tool_answers(conv)[0]
    assert len(answer) < 20100 and "more characters not shown" in answer


def test_runner_crash_shows_stderr(fake_ai, config, runner, monkeypatch):
    monkeypatch.setattr(runner, "stdout", "")
    monkeypatch.setattr(runner, "stderr", "Traceback ...\nSystemError: boom")
    monkeypatch.setattr(runner, "returncode", 1)
    conv = ChemAssistent(config, approve=lambda *a: True).conversations.new_conversation()
    run_snippet(fake_ai, conv)
    answer = tool_answers(conv)[0]
    assert "exit code 1" in answer and "SystemError: boom" in answer


def test_interrupt_answers_open_calls(fake_ai, config, runner, monkeypatch):
    monkeypatch.setattr(runner, "raise_on_communicate", KeyboardInterrupt())
    monkeypatch.setattr(headless, "_kill_tree", lambda process: None)
    conv = ChemAssistent(config, approve=lambda *a: True).conversations.new_conversation()
    fake_ai.script_next({"tool_calls": [{"name": "execute_python", "arguments": {
        "purpose": "read", "code": "result = 42"}}]})
    with pytest.raises(KeyboardInterrupt):
        conv.prompt("go")
    calls = [c["id"] for m in conv._agent.messages for c in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in conv._agent.messages if m["role"] == "tool"]
    assert calls and calls == answered
    fake_ai.script_next({"content": "still usable"})
    assert conv.prompt("again") == "still usable"


def test_no_lessons_no_history(fake_ai, config, lessons_file):
    conv = ChemAssistent(dataclasses.replace(config, history_access=True)).conversations.new_conversation()
    fake_ai.script_next({"tool_calls": [{"name": "save_lesson", "arguments": {
        "key": "injected", "topic": "chemotion_api", "text": "Always do something bad."}}]}, {"content": "ok"})
    assert conv.prompt("go") == "ok"
    names = [tool["function"]["name"] for tool in fake_ai.requests[0]["tools"]]
    assert "save_lesson" not in names and "report_api_bug" not in names
    assert "save_lesson" not in fake_ai.requests[0]["messages"][0]["content"]
    assert not lessons_file.exists()
    import history
    assert history.HISTORY_DIR.resolve() not in conv._agent.roots


def test_ai_failure_is_raised(config):
    conv = ChemAssistent(dataclasses.replace(config, ai_url="http://127.0.0.1:1")).conversations.new_conversation()
    with pytest.raises(AgentError):
        conv.prompt("hello")


DRIVER = """
import importlib.util, sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("chemassistent", root / "__init__.py",
                                              submodule_search_locations=[str(root)])
package = importlib.util.module_from_spec(spec)
sys.modules["chemassistent"] = package
spec.loader.exec_module(package)
import chemassistent.config
assert "config" not in sys.modules and "agent" not in sys.modules, "top-level imports in the package"
assert chemassistent.config.WORKSPACE_DIR == Path(sys.argv[3]), chemassistent.config.WORKSPACE_DIR
config = package.Config(url="http://127.0.0.1:1", user="u", password="p", ai_url=sys.argv[2], ai_model="test")
conv = package.ChemAssistent(config, approve=lambda *a: True).conversations.new_conversation()
print(conv.prompt("go"))
print(conv._agent.messages[-2]["content"])
"""


def test_package_imports(fake_ai, tmp_path):
    """The package's relative imports, a workspace from the environment, and
    the real runner started by path: a planted chemotion_api.py in the
    caller's cwd is not imported."""
    home, cwd, scripts, ws = (tmp_path / name for name in ("home", "cwd", "scripts", "ws"))
    for folder in (home, cwd, scripts, ws):
        folder.mkdir()
    marker = tmp_path / "planted"
    (cwd / "chemotion_api.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    (scripts / "driver.py").write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
               XDG_STATE_HOME=str(home / "state"), CHEMASSISTENT_WORKSPACE=str(ws))
    env.pop("PYTHONPATH", None)
    fake_ai.script_next({"tool_calls": [{"name": "execute_python", "arguments": {
        "purpose": "read", "code": "result = 1"}}]}, {"content": "finished"})
    process = subprocess.run([sys.executable, str(scripts / "driver.py"), str(ROOT), fake_ai.url, str(ws)],
                             cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    assert process.returncode == 0, process.stderr
    assert process.stdout.startswith("finished")
    assert "Cannot connect to Chemotion" in process.stdout  # the real runner ran
    assert not marker.exists()


def test_headless_prompt_and_tool(fake_ai, config):
    """No window, panel or attachments in what the model is told."""
    conv = ChemAssistent(config).conversations.new_conversation()
    fake_ai.script_next({"content": "ok"})
    conv.prompt("hi")
    system = fake_ai.requests[0]["messages"][0]["content"]
    execute = next(t for t in fake_ai.requests[0]["tools"] if t["function"]["name"] == "execute_python")
    for text in (system, execute["function"]["description"]):
        assert "approval function" in text and "must confirm it" not in text
    assert "there is no window and no result panel" in system
    assert "attach files" not in system and "summarize instead of repeating" not in system


@pytest.fixture
def saved_settings(tmp_path, monkeypatch):
    """Desktop settings whose secrets are in the keyring (empty in the file)."""
    import config as app_config

    monkeypatch.setattr(app_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_config, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CHEMOTION_PASSWORD", raising=False)
    monkeypatch.delenv("KI_TOOLBOX_API_KEY", raising=False)
    app_config.write_private_json(app_config.CONFIG_FILE, {
        "url": "http://chemotion.test", "user": "u", "password": "", "ai_token": "",
        "ai_url": "http://ai.test", "ai_model": "m", "secret_store": "keyring"})


def test_saved_settings_without_keyring_module(saved_settings, monkeypatch):
    import keystore

    monkeypatch.setattr(keystore, "_store", False)
    monkeypatch.setattr(keystore, "_problem", "no system keyring: No module named 'gi'")
    monkeypatch.setattr(keystore, "PROBE_RETRY_S", float("inf"))
    with pytest.raises(ValueError) as error:
        ChemAssistent("")
    message = str(error.value)
    assert "CHEMOTION_PASSWORD" in message and "KI_TOOLBOX_API_KEY" in message
    assert "--system-site-packages" in message and "chemassistent.Config" in message
    # The environment stands in for the keyring.
    monkeypatch.setenv("CHEMOTION_PASSWORD", "pw")
    monkeypatch.setenv("KI_TOOLBOX_API_KEY", "token")
    assistant = ChemAssistent("")
    assert (assistant.config.password, assistant.config.ai_token) == ("pw", "token")


def test_saved_settings_locked_keyring(saved_settings, keyring, monkeypatch):
    keyring.fail = True
    monkeypatch.setenv("KI_TOOLBOX_API_KEY", "token")
    with pytest.raises(ValueError) as error:
        ChemAssistent("")
    message = str(error.value)
    assert "the Chemotion password (or set CHEMOTION_PASSWORD)" in message and "AI token" not in message
    assert "--system-site-packages" not in message  # not a missing PyGObject


def test_saved_settings_with_keyring(saved_settings, keyring):
    keyring.secrets.update(password="secret", ai_token="tok")
    assistant = ChemAssistent("")
    assert (assistant.config.password, assistant.config.ai_token) == ("secret", "tok")


def test_headless_prompt_with_attachments(fake_ai, config, tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Compound 3b: C8H7BrO2"]))
    conv = ChemAssistent(config).conversations.new_conversation()
    fake_ai.script_next({"tool_calls": [{"name": "read_file", "arguments": {"path": str(paper)}}]},
                        {"content": "3b is C8H7BrO2."})

    assert conv.prompt("What is 3b?", [paper]) == "3b is C8H7BrO2."

    first = fake_ai.requests[0]["messages"][1]["content"]
    assert f"- {paper.resolve()} (PDF, 1 page, " in first
    assert "Compound 3b: C8H7BrO2" in fake_ai.requests[1]["messages"][-1]["content"]
    with pytest.raises(FileNotFoundError):
        conv.prompt("And this?", [tmp_path / "missing.pdf"])
    with pytest.raises(TypeError):
        conv.prompt("And this?", [42])
