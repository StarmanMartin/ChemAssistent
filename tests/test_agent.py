"""The agent loop against the scripted AI endpoint (tests/fake_ai.py).

No real model and no network: every answer comes from the script, so the
tests pin down what the agent sends, how it merges streamed deltas and what
it does with tool calls, errors and cancellation.
"""
from __future__ import annotations

import json
import threading

import pytest

import agent as agent_module
from agent import Agent, AgentError, build_tools, list_models, looks_read_only, tool_summary


class Recorder:
    """Collects the agent's events and the tool calls it makes."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.executions: list[dict] = []
        self.answer = "Output:\n(nothing)"

    def on_event(self, kind: str, data) -> None:
        self.events.append((kind, data))

    def execute(self, purpose: str, code: str, cancelled, target: str) -> str:
        self.executions.append({"purpose": purpose, "code": code, "target": target, "cancelled": cancelled})
        return self.answer() if callable(self.answer) else self.answer

    @property
    def text(self) -> str:
        return "".join(str(data) for kind, data in self.events if kind == "text")

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


def make_agent(ai_config, recorder, workspace=None, **kwargs) -> Agent:
    return Agent(ai_config, "You are ChemAssistent.", recorder.execute, recorder.on_event, **kwargs)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


# --- the plain answer ---------------------------------------------------------
def test_streamed_answer(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"content": "Hello from the lab."})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")

    assert recorder.text == "Hello from the lab."
    assert recorder.kinds[-1] == "done"
    assert agent.messages[-1] == {"role": "assistant", "content": "Hello from the lab."}
    request = fake_ai.requests[0]
    assert request["model"] == "test-model"
    assert request["stream"] is True
    assert request["messages"][0] == {"role": "system", "content": "You are ChemAssistent."}
    assert request["messages"][1] == {"role": "user", "content": "Hi"}
    assert [tool["function"]["name"] for tool in request["tools"]] == [
        "execute_python", "read_file", "grep", "list_files"]
    assert fake_ai.headers_seen[0]["Authorization"] == "Bearer test-token"


def test_answer_without_streaming(fake_ai, ai_config, workspace, recorder):
    """A server that ignores "stream" and sends one JSON answer."""
    fake_ai.script_next({"content": "One shot.", "plain": True})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")

    assert recorder.text == "One shot."
    assert recorder.kinds[-1] == "done"


def test_reasoning_is_kept_and_sent_back(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"reasoning": "The user says hi.", "content": "Hello."},
                        {"content": "Again."})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")
    assert "thinking" in recorder.kinds
    assert agent.messages[-1]["reasoning_content"] == "The user says hi."

    agent.send("Again?")
    sent = fake_ai.requests[1]["messages"]
    assert any(message.get("reasoning_content") == "The user says hi." for message in sent)


def test_set_model_switches_and_drops_reasoning(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"reasoning": "hmm", "content": "First."}, {"content": "Second."})
    agent = make_agent(ai_config, recorder)
    agent.send("Hi")

    agent.set_model("other-model")
    agent.send("More")

    assert fake_ai.requests[1]["model"] == "other-model"
    assert all("reasoning_content" not in message for message in fake_ai.requests[1]["messages"])


# --- the read-only tools ------------------------------------------------------
def test_read_file_round_trip(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next(
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "CHEMOTION_API.md"}}]},
        {"content": "The reference explains login()."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("What does the reference say?")

    assert ("tool", "Read CHEMOTION_API.md") in recorder.events
    answer = next(m for m in agent.messages if m.get("role") == "tool")
    assert "Instance.login(user, password)" in answer["content"]
    # The tool result goes back with the next request.
    assert any(m.get("role") == "tool" for m in fake_ai.requests[1]["messages"])
    assert recorder.text == "The reference explains login()."


def test_grep_and_list_files(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next(
        {"tool_calls": [{"name": "grep", "arguments": {"pattern": "def save", "glob": "*.py"}},
                        {"name": "list_files", "arguments": {}}]},
        {"content": "Found it."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("Where is save()?")

    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "chemotionapi/sample.py:2:" in results[0]
    assert "CHEMOTION_API.md" in results[1] and "chemotionapi/" in results[1]


def test_paths_outside_the_workspace_are_refused(fake_ai, ai_config, workspace, recorder, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("password", encoding="utf-8")
    fake_ai.script_next({"tool_calls": [{"name": "read_file", "arguments": {"path": str(outside)}}]},
                        {"content": "I cannot read that."})
    agent = make_agent(ai_config, recorder)

    agent.send("Read my secret")

    result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "outside the workspace" in result
    assert "password" not in result


def test_attachments_become_readable(fake_ai, ai_config, workspace, recorder, tmp_path):
    attachment = tmp_path / "measurement.csv"
    attachment.write_text("t,value\n1,2\n", encoding="utf-8")
    fake_ai.script_next({"tool_calls": [{"name": "read_file", "arguments": {"path": str(attachment)}}]},
                        {"content": "Two columns."})
    agent = make_agent(ai_config, recorder)

    agent.send("What is in this file?", [str(attachment)])

    assert agent_module.ATTACHMENT_NOTE.strip() in fake_ai.requests[0]["messages"][1]["content"]
    assert str(attachment) in fake_ai.requests[0]["messages"][1]["content"]
    result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "t,value" in result


def test_unknown_tool_and_broken_arguments(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next(
        {"tool_calls": [{"name": "delete_everything", "arguments": {}},
                        {"name": "read_file", "arguments": "{not json"}]},
        {"content": "Sorry."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("Do something odd")

    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "There is no tool delete_everything" in results[0]
    assert "Invalid JSON arguments" in results[1]


# --- execute_python -----------------------------------------------------------
def test_execute_python_is_handed_to_the_app(fake_ai, ai_config, workspace, recorder):
    code = "result = [c.label for c in instance.get_root_collection().children]"
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python",
                         "arguments": {"purpose": "List the collections", "code": code}}]},
        {"content": "You have two collections."},
    )
    recorder.answer = 'Output:\n(nothing)\n\nresult:\n["A", "B"]'
    agent = make_agent(ai_config, recorder)

    agent.send("Which collections do I have?")

    assert len(recorder.executions) == 1
    execution = recorder.executions[0]
    assert execution["purpose"] == "List the collections"
    assert execution["code"] == code
    assert execution["target"] == "chemotion"
    assert isinstance(execution["cancelled"], threading.Event)
    result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert result == recorder.answer


def test_empty_code_is_not_executed(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"tool_calls": [{"name": "execute_python",
                                         "arguments": {"purpose": "nothing", "code": "   "}}]},
                        {"content": "I need code."})
    agent = make_agent(ai_config, recorder)

    agent.send("Run nothing")

    assert recorder.executions == []
    assert next(m["content"] for m in agent.messages if m.get("role") == "tool") == "No code given."


def test_tool_description_follows_the_auto_run_mode(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"content": "a"}, {"content": "b"})
    agent = make_agent(ai_config, recorder, auto_run="off")

    agent.send("one")
    agent.set_auto_run("always")
    agent.send("two")

    def description(request):
        return next(tool["function"]["description"] for tool in request["tools"]
                    if tool["function"]["name"] == "execute_python")

    assert "must confirm it before it runs" in description(fake_ai.requests[0])
    assert "runs automatically" in description(fake_ai.requests[1])


# --- JupyterLab tools ---------------------------------------------------------
def test_jupyter_tools_only_with_an_exchange_folder(fake_ai, ai_config, workspace, skills_dir, recorder, tmp_path):
    assert [tool["function"]["name"] for tool in build_tools("off")] == [
        "execute_python", "read_file", "grep", "list_files"]

    exchange = tmp_path / "exchange" / "conversation"
    jobs_text = "- job-abc (A long run): running (5 min)"
    agent = make_agent(ai_config, recorder, exchange=exchange, jobs=lambda: jobs_text)
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_jupyter", "arguments": {"purpose": "Plot", "code": "print(1)"}},
                        {"name": "start_jupyter_job", "arguments": {"purpose": "DFT", "code": "print(2)"}},
                        {"name": "jupyter_jobs", "arguments": {}}]},
        {"content": "Started."},
    )

    agent.send("Analyse this")

    assert [execution["target"] for execution in recorder.executions] == ["jupyter", "job"]
    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert results[2] == jobs_text
    assert ("tool", "Checked the background jobs") in recorder.events
    assert exchange.resolve() in agent.roots and skills_dir.resolve() in agent.roots
    names = [tool["function"]["name"] for tool in fake_ai.requests[0]["tools"]]
    assert names[:4] == ["execute_python", "execute_jupyter", "start_jupyter_job", "jupyter_jobs"]


def test_jupyter_tools_are_refused_without_jupyter(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"tool_calls": [{"name": "execute_jupyter",
                                         "arguments": {"purpose": "Plot", "code": "print(1)"}}]},
                        {"content": "No JupyterLab."})
    agent = make_agent(ai_config, recorder)

    agent.send("Plot something")

    assert recorder.executions == []
    assert "There is no tool execute_jupyter" in next(m["content"] for m in agent.messages if m.get("role") == "tool")


# --- errors, retry, cancel, limits --------------------------------------------
def test_http_error_then_retry(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"status": 503, "message": "model is overloaded"})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")

    assert recorder.kinds[-1] == "error"
    assert "model is overloaded" in str(recorder.events[-1][1])

    fake_ai.script_next({"content": "Now it works."})
    agent.retry()

    assert recorder.kinds[-1] == "done"
    assert recorder.text == "Now it works."
    # retry() does not add a second user message.
    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant"]


def test_error_inside_the_stream(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"error": {"message": "context length exceeded"}})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")

    assert recorder.kinds[-1] == "error"
    assert "context length exceeded" in str(recorder.events[-1][1])


def test_unauthorized_mentions_the_token(fake_ai, ai_config, workspace, recorder):
    fake_ai.script_next({"status": 401, "message": "no key"})
    agent = make_agent(ai_config, recorder)

    agent.send("Hi")

    assert "check the AI token in the settings" in str(recorder.events[-1][1])


def test_cancel_while_the_answer_streams(fake_ai, ai_config, workspace, recorder):
    gate = threading.Event()
    fake_ai.script_next({"content": "This answer is interrupted.", "delay": gate})
    agent = make_agent(ai_config, recorder)
    worker = threading.Thread(target=agent.send, args=("Hi",))

    worker.start()
    assert fake_ai.sending.wait(10)
    agent.cancel()
    gate.set()
    worker.join(timeout=15)

    assert not worker.is_alive()
    assert recorder.kinds[-1] == "stopped"


def test_cancelled_tool_calls_are_answered(fake_ai, ai_config, workspace, recorder):
    """Every tool call needs an answer, or the next request is refused."""
    def stop_instead_of_running(purpose, code, cancelled, target):
        agent.cancel()
        return "Not run."

    fake_ai.script_next({"tool_calls": [{"name": "execute_python",
                                         "arguments": {"purpose": "p", "code": "result = 1"}},
                                        {"name": "read_file", "arguments": {"path": "CHEMOTION_API.md"}}]})
    recorder.execute = stop_instead_of_running
    agent = make_agent(ai_config, recorder)
    agent.execute = stop_instead_of_running

    agent.send("Hi")

    assert recorder.kinds[-1] == "stopped"
    calls = [call["id"] for m in agent.messages for call in m.get("tool_calls") or []]
    answered = [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"]
    assert sorted(calls) == sorted(answered)


def test_stops_after_max_rounds(fake_ai, ai_config, workspace, recorder, monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_ROUNDS", 3)
    for _ in range(5):
        fake_ai.script_next({"tool_calls": [{"name": "list_files", "arguments": {}}]})
    agent = make_agent(ai_config, recorder)

    agent.send("Loop forever")

    assert recorder.kinds[-1] == "error"
    assert "Stopped after 3 tool rounds" in str(recorder.events[-1][1])
    assert agent.messages[-1] == {"role": "user", "content": agent_module.STOPPED_NOTE}


def test_unreachable_endpoint(workspace, recorder):
    import config as config_module

    broken = config_module.Config(url="http://chemotion.invalid", user="u", password="p",
                                  ai_url="http://127.0.0.1:1/v1", ai_model="test-model")
    agent = Agent(broken, "prompt", recorder.execute, recorder.on_event)

    agent.send("Hi")

    assert recorder.kinds[-1] == "error"
    assert "Cannot reach" in str(recorder.events[-1][1])


# --- restore ------------------------------------------------------------------
def test_restore_continues_a_saved_conversation(fake_ai, ai_config, workspace, recorder, tmp_path):
    attachment = tmp_path / "old.csv"
    attachment.write_text("a,b\n", encoding="utf-8")
    saved = [
        {"role": "system", "content": "an older system prompt"},
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_7", "type": "function",
                         "function": {"name": "execute_python", "arguments": "{}"}}]},
    ]
    fake_ai.script_next({"content": "Carrying on."})
    agent = make_agent(ai_config, recorder)

    agent.restore(saved, [str(attachment), str(tmp_path / "gone.csv")])
    agent.send("And now?")

    assert agent.messages[0]["content"] == "You are ChemAssistent."
    assert not any(m.get("content") == "an older system prompt" for m in agent.messages)
    # The open tool call of the saved conversation was answered.
    assert any(m.get("tool_call_id") == "call_7" for m in agent.messages)
    assert attachment.resolve() in [path.resolve() for path in agent.roots]
    assert all("gone.csv" not in str(path) for path in agent.roots)


# --- list_models --------------------------------------------------------------
def test_list_models_sorts_and_drops_hidden(fake_ai, ai_config):
    fake_ai.models = [
        {"id": "zeta"},
        {"id": "alpha"},
        {"id": "embedding", "info": {"meta": {"hidden": True}}},
        {"no_id": True},
    ]

    assert list_models(ai_config) == ["alpha", "zeta"]


def test_list_models_reports_errors(ai_config):
    import dataclasses

    with pytest.raises(AgentError):
        list_models(dataclasses.replace(ai_config, ai_url="http://127.0.0.1:1/v1"))


# --- the auto-run guard -------------------------------------------------------
@pytest.mark.parametrize("code", [
    "result = instance.get_sample(1).properties",
    "result = [s.short_label for s in col.get_samples()]",
    "hits = instance.search_sample().add_search_condition(match, conn, name='X').request()",
    "with open('/tmp/x.csv') as handle:\n    result = handle.read()",
    "result = open(exchange / 'data.json', 'r').read()",
])
def test_looks_read_only_accepts_reading(code):
    ok, why = looks_read_only(code)
    assert ok, why


@pytest.mark.parametrize("code, hint", [
    ("sample.save()", "save()"),
    ("sample.delete()", "delete()"),
    ("col.new_sample_smiles('CCO')", "new_sample_smiles()"),
    ("sample.set_amount(1, unit)", "set_amount()"),
    ("import os\nresult = os.listdir('.')", "imports os"),
    ("import subprocess", "imports subprocess"),
    ("open('out.csv', 'w').write('x')", "write()"),
    ("handle = open(exchange / 'out.csv', 'w')", 'mode "w"'),
    ("handle = open('out.csv', mode=chosen)", "computed mode"),
    ("del sample.properties['name']", "deletes something"),
    ("getattr(instance, 'login')()", "not a plain name or method"),
    ("result = (", "does not parse"),
])
def test_looks_read_only_refuses_writing(code, hint):
    ok, why = looks_read_only(code)
    assert not ok
    assert hint in why


def test_tool_summary_texts():
    assert tool_summary("read_file", {"path": "a.md"}) == "Read a.md"
    assert tool_summary("grep", {"pattern": "x", "path": "src"}) == "Searched “x” in src"
    assert tool_summary("list_files", {}) == "Listed the workspace"
    assert tool_summary("jupyter_jobs", {}) == "Checked the background jobs"


def test_read_file_limits_and_binary(workspace):
    roots = [workspace.resolve()]
    long_file = workspace / "long.txt"
    long_file.write_text("\n".join(f"line {n}" for n in range(1, 51)), encoding="utf-8")
    text = agent_module.read_file("long.txt", roots, offset=1, limit=10)
    assert "lines 1-10 of 50" in text and "read_file with offset 11" in text

    binary = workspace / "image.png"
    binary.write_bytes(b"\x89PNG\x00\x00binary")
    assert "binary file" in agent_module.read_file("image.png", roots)

    with pytest.raises(AgentError):
        agent_module.read_file("nope.txt", roots)


def test_describe_attachment(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("x" * 2048, encoding="utf-8")
    assert "text file, 2.0 kB" in agent_module.describe_attachment(file)
    assert "folder, 1 files" in agent_module.describe_attachment(tmp_path)


def test_merge_call_without_index():
    """Some servers send whole tool calls without an index."""
    calls: dict[int, dict] = {}
    Agent._merge_call(calls, {"id": "call_a", "function": {"name": "read_file", "arguments": '{"path"'}})
    Agent._merge_call(calls, {"id": "call_a", "function": {"name": "", "arguments": ': "a.md"}'}})
    assert len(calls) == 1
    call = calls[0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.md"}
