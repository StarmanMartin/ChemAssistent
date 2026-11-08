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
        "execute_python", "read_file", "grep", "list_files", "view_page", "save_lesson"]
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


def test_saved_conversations_are_readable_when_allowed(fake_ai, ai_config, workspace, recorder, state_dir):
    import dataclasses
    import history
    import workspace as workspace_module

    history.save("20261020-101500-abcdef", "Samples of Main Test", "test-model",
                 [{"role": "user", "content": "Which samples are in Main Test?"},
                  {"role": "assistant", "content": "MS-17 Benzene is the only one."}], [])
    folder = str(state_dir / "history")
    turns = (
        {"tool_calls": [{"name": "grep", "arguments": {"pattern": '^  "title":', "path": folder}},
                        {"name": "read_file", "arguments": {"path": f"{folder}/20261020-101500-abcdef.json"}}]},
        {"content": "Last time: MS-17."},
    )
    fake_ai.script_next(*turns)
    agent = make_agent(dataclasses.replace(ai_config, history_access=True), recorder)

    agent.send("What did we find last week?")

    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "Samples of Main Test" in results[0]
    assert "MS-17 Benzene" in results[1]
    # The prompt says where they are and that they are read only on request.
    prompt = workspace_module.system_prompt(history=True)
    assert folder in prompt and "only when the user asks" in prompt

    # The default: the saved conversations are no root, and the prompt does not mention them.
    fake_ai.script_next(*turns)
    agent = make_agent(ai_config, recorder)
    agent.send("What did we find last week?")
    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert len(results) == 2 and all("outside the workspace" in result for result in results)
    assert "MS-17 Benzene" not in "".join(results)
    assert folder not in workspace_module.system_prompt()


def test_attachments_are_kept_apart_from_the_agents_own_roots(fake_ai, ai_config, workspace, recorder, tmp_path,
                                                             skills_dir):
    """The app saves agent.attachments with the conversation, never the
    workspace, the exchange folder or the skills."""
    import dataclasses

    note = tmp_path / "note.txt"
    note.write_text("see MS-17", encoding="utf-8")
    fake_ai.script_next({"content": "Noted."})
    exchange = tmp_path / "exchange"
    agent = make_agent(dataclasses.replace(ai_config, history_access=True), recorder, exchange=exchange)
    assert agent.attachments == []

    agent.send("Keep this", attachments=[str(note)])

    assert agent.attachments == [note.resolve()]
    assert {workspace.resolve(), exchange.resolve(), skills_dir.resolve()} < set(agent.roots)
    fake_ai.script_next({"content": "Back."})
    again = make_agent(ai_config, recorder)
    again.restore(agent.messages[1:], [str(path) for path in agent.attachments] + [str(tmp_path / "gone.txt")])
    assert again.attachments == [note.resolve()]  # what exists; the rest of the roots are its own


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
        "execute_python", "read_file", "grep", "list_files", "view_page", "save_lesson"]

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


def test_skill_references_resolve_relative_to_the_skill(workspace, skills_dir):
    """A SKILL.md points to `references/x.md`, relative to its own folder."""
    api_skill = workspace / "chemotionapi" / "skills" / "chemotion-api"
    (api_skill / "references").mkdir(parents=True)
    (api_skill / "SKILL.md").write_text("---\nname: chemotion-api\n---\nRead `references/api.md`.\n",
                                        encoding="utf-8")
    (api_skill / "references" / "api.md").write_text("Sample.save()\n", encoding="utf-8")
    jupyter_skill = skills_dir / "jupyter-calculations"
    (jupyter_skill / "references").mkdir()
    (jupyter_skill / "references" / "xtb.md").write_text("GFN2-xTB\n", encoding="utf-8")

    roots = [workspace.resolve()]
    assert "Sample.save()" in agent_module.read_file("references/api.md", roots)
    assert "api.md" in agent_module.list_files(roots, "references")
    # The app's skills only with SKILLS_DIR among the roots (JupyterLab on).
    with pytest.raises(AgentError):
        agent_module.read_file("references/xtb.md", roots)
    roots.append(skills_dir.resolve())
    assert "GFN2-xTB" in agent_module.read_file("references/xtb.md", roots)

    # Workspace paths win; the same name in two skills is ambiguous; no escape.
    (workspace / "references").mkdir()
    (workspace / "references" / "api.md").write_text("workspace copy\n", encoding="utf-8")
    assert "workspace copy" in agent_module.read_file("references/api.md", roots)
    (jupyter_skill / "references" / "setup.md").write_text("a\n", encoding="utf-8")
    (api_skill / "references" / "setup.md").write_text("b\n", encoding="utf-8")
    with pytest.raises(AgentError, match="ambiguous"):
        agent_module.read_file("references/setup.md", roots)
    (skills_dir.parent / "secret.txt").write_text("no\n", encoding="utf-8")
    with pytest.raises(AgentError):
        agent_module.read_file("../../secret.txt", roots)


def test_describe_attachment(tmp_path):
    file = tmp_path / "a.txt"
    file.write_text("x" * 2048, encoding="utf-8")
    assert "text file, 2.0 kB" in agent_module.describe_attachment(file)
    assert "folder, 1 files" in agent_module.describe_attachment(tmp_path)


def test_describe_attachment_pdf_and_picture(tmp_path):
    import documents
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["a", "b"]))
    assert agent_module.describe_attachment(paper).startswith(f"- {paper} (PDF, 2 pages, ")
    picture = tmp_path / "scheme.png"
    picture.write_bytes(documents.png_encode(4, 3, 12, b"\x00" * 36))
    assert agent_module.describe_attachment(picture).startswith(f"- {picture} (PNG image, 4×3, ")


def test_merge_call_without_index():
    """Some servers send whole tool calls without an index."""
    calls: dict[int, dict] = {}
    Agent._merge_call(calls, {"id": "call_a", "function": {"name": "read_file", "arguments": '{"path"'}})
    Agent._merge_call(calls, {"id": "call_a", "function": {"name": "", "arguments": ': "a.md"}'}})
    assert len(calls) == 1
    call = calls[0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.md"}


# --- PDFs ---------------------------------------------------------------------
def test_read_file_gives_the_text_of_a_pdf(tmp_path):
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Title\nScheme 1", "Results\n" + "\n".join(f"row {n}" for n in range(1, 6))]))
    roots = [tmp_path.resolve()]
    text = agent_module.read_file(str(paper), roots)
    assert f"{paper.resolve()}, lines 1-10 of 10:" in text
    assert "     1\t--- page 1 of 2 ---" in text and "     4\t--- page 2 of 2 ---" in text
    assert "     3\tScheme 1" in text
    # Paged like a text file.
    page_two = agent_module.read_file(str(paper), roots, offset=4, limit=3)
    assert "lines 4-6 of 10" in page_two and "row 1" in page_two and "read_file with offset 7" in page_two

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 nothing")
    with pytest.raises(AgentError, match="cannot be read as a PDF"):
        agent_module.read_file(str(broken), roots)


def test_read_file_pdf_through_the_agent(fake_ai, ai_config, workspace, recorder, tmp_path):
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Compound 8a: C12H10O2, m/z 186"]))
    fake_ai.script_next({"tool_calls": [{"name": "read_file", "arguments": {"path": str(paper)}}]},
                        {"content": "8a is C12H10O2."})
    agent = make_agent(ai_config, recorder)

    agent.send("What is 8a?", [str(paper)])

    answer = next(m for m in agent.messages if m.get("role") == "tool")
    assert "Compound 8a: C12H10O2, m/z 186" in answer["content"]


def test_grep_searches_pdfs_with_pages(tmp_path, monkeypatch):
    from pdf_maker import make_pdf

    (tmp_path / "paper.pdf").write_bytes(make_pdf(["Introduction", "Scheme 2: cyclisation of 7a\nScheme 3"]))
    (tmp_path / "notes.txt").write_text("see Scheme 2\n", encoding="utf-8")
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    roots = [tmp_path.resolve()]
    paper = (tmp_path / "paper.pdf").resolve()

    text = agent_module.grep("scheme", roots, str(tmp_path))
    assert f"{paper}:4 (page 2): Scheme 2: cyclisation of 7a" in text
    assert f"{paper}:5 (page 2): Scheme 3" in text
    assert f"{(tmp_path / 'notes.txt').resolve()}:1: see Scheme 2" in text
    # The line number is read_file's: offset 4 starts at that line.
    assert "4\tScheme 2" in agent_module.read_file(str(paper), roots, offset=4, limit=1)
    # Page markers themselves are no matches.
    assert "--- page" not in agent_module.grep("page", roots, str(paper)).replace("No matches.", "")

    monkeypatch.setattr(agent_module, "MAX_GREP_PDFS", 1)
    text = agent_module.grep("scheme", roots, str(tmp_path))
    assert "1 more PDFs not searched" in text


def test_model_infos_reads_the_vision_capability(fake_ai, ai_config):
    fake_ai.models = [
        {"id": "glm", "info": {"meta": {"capabilities": {"vision": True}}}},
        {"id": "deepseek", "info": {"meta": {"capabilities": {"vision": False, "file_upload": True}}}},
        {"id": "plain"},
        {"id": "odd", "info": {"meta": {"capabilities": "yes"}}},
        {"id": "hidden", "info": {"meta": {"hidden": True, "capabilities": {"vision": True}}}},
    ]

    infos = agent_module.model_infos(ai_config)
    assert list(infos) == ["deepseek", "glm", "odd", "plain"]
    assert infos["glm"] == {"vision": True}
    assert infos["deepseek"] == {"vision": False}
    assert infos["plain"] == {"vision": None} and infos["odd"] == {"vision": None}
    assert list_models(ai_config) == ["deepseek", "glm", "odd", "plain"]


def test_agent_vision_setting(ai_config, recorder):
    agent = make_agent(ai_config, recorder)
    assert agent.vision is None
    agent.set_vision(False)
    assert agent.vision is False


# --- view_page -----------------------------------------------------------------
def test_view_page_function(tmp_path):
    import documents
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Scheme 1", "Figure 2"]))
    picture = tmp_path / "scheme.png"
    picture.write_bytes(documents.png_encode(8, 6, 24, b"\x40" * 144))
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    roots = [tmp_path.resolve()]

    text, image, target = agent_module.view_page(str(paper), roots, 2, "top")
    assert text.startswith(f"Page 2 of 2 of {paper.resolve()} (") and ", top); the image follows" in text
    assert image.mime == "image/png" and target == paper.resolve()
    text, image, _ = agent_module.view_page(str(picture), roots)
    assert text.startswith(f"{picture.resolve()} (8×6 px)") and image.data == picture.read_bytes()

    with pytest.raises(AgentError, match="give the page"):
        agent_module.view_page(str(paper), roots)
    with pytest.raises(AgentError, match="page 3 does not exist"):
        agent_module.view_page(str(paper), roots, 3)
    with pytest.raises(AgentError, match="neither a PDF nor a picture"):
        agent_module.view_page(str(tmp_path / "notes.txt"), roots)
    with pytest.raises(AgentError, match="outside the workspace"):
        agent_module.view_page("/etc/hostname", roots)


def test_view_page_is_offered_unless_the_model_takes_no_images(ai_config, recorder):
    def names(agent):
        return [tool["function"]["name"] for tool in agent.tools]

    assert "view_page" in names(make_agent(ai_config, recorder))
    assert "view_page" in names(make_agent(ai_config, recorder, vision=True))
    agent = make_agent(ai_config, recorder, vision=False)
    assert "view_page" not in names(agent)
    agent.set_vision(None)
    assert "view_page" in names(agent)
    agent.set_auto_run("always")
    assert "view_page" in names(agent)
    assert tool_summary("view_page", {"path": "/a/x.pdf", "page": 3, "region": "top"}) == "Looked at page 3 of x.pdf (top)"
    assert tool_summary("view_page", {"path": "/a/s.png"}) == "Looked at s.png"


@pytest.fixture
def paper(tmp_path):
    from pdf_maker import make_pdf

    path = tmp_path / "paper.pdf"
    path.write_bytes(make_pdf([f"Page {n}" for n in range(1, 9)]))
    return path


def view(path, page, region=None, call_id=None):
    call = {"name": "view_page", "arguments": {"path": str(path), "page": page}}
    if region:
        call["arguments"]["region"] = region
    if call_id:
        call["id"] = call_id
    return call


def image_parts(messages):
    return [part for message in messages if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]


def test_view_page_images_follow_the_tool_answers(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next(
        {"tool_calls": [view(paper, 1, call_id="c1"), view(paper, 2, "top", call_id="c2"),
                        {"name": "read_file", "arguments": {"path": str(paper)}, "id": "c3"}]},
        {"content": "Scheme 1 shows 8a."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("Draw 8a from the paper", [str(paper)])

    sent = fake_ai.requests[1]["messages"]
    roles = [message["role"] for message in sent]
    assert roles[-5:] == ["assistant", "tool", "tool", "tool", "user"]  # the tool answers stay together
    images = sent[-1]["content"]
    assert images[0] == {"type": "text", "text": agent_module.IMAGES_NOTE}
    assert images[1]["text"] == f"The image of page 1 of {paper.resolve()}:"
    assert images[2]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert images[3]["text"] == f"The image of page 2 of {paper.resolve()} (top):"
    assert all("_source" not in part for part in images)  # private
    assert sent[-4]["content"].startswith("Page 1 of 8 of ")
    shown = [data for kind, data in recorder.events if kind == "image"]
    assert [item["summary"] for item in shown] == ["Looked at page 1 of paper.pdf", "Looked at page 2 of paper.pdf (top)"]
    assert shown[0]["data"].startswith(b"\x89PNG") and shown[0]["path"] == str(paper.resolve())
    # Kept with its source for pruning and the history.
    assert image_parts(agent.messages)[1]["_source"] == {"path": str(paper.resolve()), "page": 2, "region": "top"}
    assert agent_module.message_text(agent.messages[-2]["content"]).startswith(agent_module.IMAGES_NOTE)


def test_only_the_newest_images_are_sent(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next(
        {"tool_calls": [view(paper, 1), view(paper, 2), view(paper, 3)]},
        {"tool_calls": [view(paper, 4), view(paper, 5), view(paper, 6), view(paper, 7), view(paper, 8)]},
        {"content": "Done."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("Look at every page", [str(paper)])

    assert len(image_parts(fake_ai.requests[1]["messages"])) == 3
    last = fake_ai.requests[2]["messages"]
    assert len(image_parts(last)) == agent_module.MAX_INLINE_IMAGES
    # The fifth view_page of one round is refused; pages 1-3 became text notes.
    assert "at most 4 images per step" in last[-2]["content"]
    rounds = [message for message in last
              if agent_module.message_text(message["content"]).startswith(agent_module.IMAGES_NOTE)]
    assert isinstance(rounds[0]["content"], str)  # all its images pruned: sent as text
    texts = rounds[0]["content"]
    assert f"[The image of page 1 of {paper.resolve()} was shown here; call view_page again to see it.]" in texts


def test_images_refused_by_the_model(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next(
        {"tool_calls": [view(paper, 1)]},
        {"status": 400, "message": "litellm.BadRequestError: deepseek-v4-flash is not a multimodal model"},
        {"content": "From the text alone: 8a."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("What is 8a?", [str(paper)])

    assert recorder.text == "From the text alone: 8a."
    assert ("warning", "This model cannot see images; view_page is off for this conversation.") in recorder.events
    retried = fake_ai.requests[2]
    assert not image_parts(retried["messages"])
    assert "this model cannot see images" in agent_module.message_text(retried["messages"][-1]["content"])
    assert "view_page" not in [tool["function"]["name"] for tool in retried["tools"]]
    assert agent.vision is False


def test_other_errors_with_images_stay_errors(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next({"tool_calls": [view(paper, 1)]}, {"status": 500, "message": "overloaded"})
    agent = make_agent(ai_config, recorder)

    agent.send("What is 8a?", [str(paper)])

    assert recorder.kinds[-1] == "error" and agent.vision is None
    assert image_parts(agent.messages)  # kept for retry()


def test_no_images_for_a_model_without_vision(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next({"tool_calls": [view(paper, 1)]}, {"content": "Seen."}, {"content": "Text only."})
    agent = make_agent(ai_config, recorder)
    agent.send("Look", [str(paper)])
    agent.set_vision(False)  # switched to a model without vision

    agent.send("And now?")

    assert not image_parts(fake_ai.requests[2]["messages"])


def test_message_text_and_without_images():
    content = [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "data:x"},
                                               "_source": {"path": "/p.pdf", "page": 2, "region": "full"}}]
    assert agent_module.message_text(content) == "a"
    assert agent_module.message_text("plain") == "plain" and agent_module.message_text(None) == ""
    stripped = agent_module.without_images([{"role": "user", "content": content}, {"role": "user", "content": "x"}])
    assert stripped[0]["content"] == "a\n[The image of page 2 of /p.pdf was shown here; call view_page again to see it.]"
    assert content[1]["type"] == "image_url"  # the original is unchanged
    assert not agent_module.has_images(stripped)


def test_a_saved_conversation_keeps_no_images(fake_ai, ai_config, workspace, recorder, paper, state_dir):
    """The app saves without_images(messages); restore() and the CLI agents'
    transcript get text notes, the chat replay skips them."""
    import json as json_module

    import history
    from cli_agent import CliAgent

    fake_ai.script_next({"tool_calls": [view(paper, 1)]}, {"content": "Seen."}, {"content": "Again."})
    agent = make_agent(ai_config, recorder)
    agent.send("Look", [str(paper)])
    history.save("20261108-120000-abcdef", "Look", "test-model", agent_module.without_images(agent.messages[1:]),
                 [str(paper)])
    text = (state_dir / "history" / "20261108-120000-abcdef.json").read_text(encoding="utf-8")
    assert "base64" not in text
    saved = json_module.loads(text)["messages"]
    note = saved[-2]["content"]
    assert note.startswith(agent_module.IMAGES_NOTE) and "was shown here; call view_page again" in note

    restored = make_agent(ai_config, recorder)
    restored.restore(saved, [str(paper)])
    restored.send("And now?")
    assert not image_parts(fake_ai.requests[-1]["messages"])

    cli = CliAgent(ai_config, "You are ChemAssistent.", recorder.on_event)
    cli.restore(saved, [str(paper)])
    assert "Look" in cli._context and "Seen." in cli._context
    assert agent_module.IMAGES_NOTE not in cli._context


def test_pruned_messages_are_sent_as_text(fake_ai, ai_config, workspace, recorder, paper):
    fake_ai.script_next({"tool_calls": [view(paper, 1)]}, {"content": "Seen."}, {"content": "Fine."})
    agent = make_agent(ai_config, recorder)
    agent.send("Look", [str(paper)])
    agent.set_vision(False)
    agent.send("More")
    images = [message for message in fake_ai.requests[-1]["messages"]
              if agent_module.message_text(message["content"]).startswith(agent_module.IMAGES_NOTE)]
    assert len(images) == 1 and isinstance(images[0]["content"], str)


def test_chat_replay_skips_image_messages(tmp_path):
    """app.replay_items() (GTK; in a subprocess, as the app sets up its display)."""
    import subprocess
    import sys

    pytest.importorskip("gi")
    root = agent_module.Path(agent_module.__file__).resolve().parent
    code = (
        "import json, sys, app, agent\n"
        "messages = json.loads(sys.stdin.read())\n"
        "print(json.dumps(app.replay_items(messages)))\n"
    )
    messages = [
        {"role": "user", "content": "Look at the scheme"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "view_page", "arguments": json.dumps({"path": "/a/paper.pdf", "page": 2})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Page 2 of 3 ..."},
        {"role": "user", "content": [{"type": "text", "text": agent_module.IMAGES_NOTE},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
        {"role": "user", "content": agent_module.IMAGES_NOTE + "\n[The image of page 2 of /a/paper.pdf was shown]"},
        {"role": "assistant", "content": "It shows 8a."},
    ]
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps(messages), capture_output=True, text=True,
                            cwd=root, timeout=60)
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)
    assert [item[0] for item in items] == ["user", "note", "assistant"]
    assert items[1][1] == "Looked at page 2 of paper.pdf"


def test_the_prompt_says_to_look_at_drawn_structures(workspace):
    import workspace as workspace_module

    prompt = workspace_module.system_prompt()
    assert "--- page n of m ---" in prompt and "`view_page`" in prompt
    assert "Never present a structure you derived from the text alone" in prompt
    # Headless conversations have no attachments.
    assert "view_page" not in workspace_module.system_prompt(headless=True)


def test_a_stopped_round_sends_no_images(fake_ai, ai_config, workspace, recorder, paper):
    """Stopped while a card waits: the round's images are dropped, the tool
    calls are answered and stay right after their assistant message."""
    fake_ai.script_next(
        {"tool_calls": [view(paper, 1, call_id="c1"),
                        {"name": "execute_python", "arguments": {"purpose": "p", "code": "x = 1"}, "id": "c2"},
                        view(paper, 2, call_id="c3")]},
        {"content": "Next."},
    )
    agent = make_agent(ai_config, recorder)

    def stop():
        agent.cancel()
        return "Not run."

    recorder.answer = stop
    agent.send("Look", [str(paper)])

    assert recorder.kinds[-1] == "stopped"
    assert not image_parts(agent.messages) and agent._pending_images == []
    assert [m["role"] for m in agent.messages[-4:]] == ["assistant", "tool", "tool", "tool"]
    recorder.answer = "Output:\n(nothing)"
    agent.send("Go on")
    assert recorder.text.endswith("Next.")


def test_a_failed_view_page_queues_nothing(fake_ai, ai_config, workspace, recorder, paper, tmp_path):
    fake_ai.script_next(
        {"tool_calls": [view(paper, 99), {"name": "view_page", "arguments": {"path": "/etc/hostname"}}]},
        {"content": "That page does not exist."},
    )
    agent = make_agent(ai_config, recorder)

    agent.send("Look at page 99", [str(paper)])

    answers = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert answers[0] == "Error: paper.pdf has 8 pages; page 99 does not exist."
    assert answers[1].startswith("Error: /etc/hostname is outside the workspace")
    assert not image_parts(agent.messages) and "image" not in recorder.kinds
