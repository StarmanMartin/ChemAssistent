"""A whole conversation: the scripted AI drives the agent, and the code it
asks for really runs against the Chemotion test instance.

This is the path app.py takes, without GTK: the agent asks for
execute_python, the "user" confirms (the execute callback here), the snippet
runs in chemotion_runner.py like the result panel would run it, and its
output goes back to the model as the tool answer. The answer is built the
way app.py's _answer_text() builds it, so the model sees what it would see
in the app.

Everything these tests create in the ELN is deleted again.
"""
from __future__ import annotations

import json
import threading

import pytest

import history
from agent import Agent
from chemotion_runner import run as run_snippet
from jobs import REPORT_HEADER, Jobs, duration, report

pytestmark = pytest.mark.chemotion

MAX_ANSWER_CHARS = 20000


class User:
    """The user in front of the result panel: confirms, edits or rejects."""

    def __init__(self, chemotion: dict, exchange=None) -> None:
        self.chemotion = chemotion
        self.exchange = exchange
        self.cards: list[dict] = []
        # Set to a reason to reject the next card, or to code to edit it.
        self.reject: str | None = None
        self.edit: str | None = None
        self.results: list[dict] = []

    def execute(self, purpose: str, code: str, cancelled: threading.Event, target: str) -> str:
        self.cards.append({"purpose": purpose, "code": code, "target": target})
        if self.reject is not None:
            reason, self.reject = self.reject, None
            return f"The user rejected the code. Their reason: {reason}"
        edited = self.edit is not None
        if edited:
            code, self.edit = self.edit, None
        request = {**self.chemotion, "code": code}
        if self.exchange is not None:
            request["exchange"] = str(self.exchange)
        answer = run_snippet(request)
        self.results.append(answer)
        return self.answer_text(answer, code, edited)

    @staticmethod
    def answer_text(answer: dict, code: str, edited: bool) -> str:
        """What app.py sends back to the model for one finished card."""
        parts = []
        if edited:
            parts.append(f"The user edited the code before running it:\n```python\n{code}\n```")
        parts.append(f"Output:\n{answer['stdout'].rstrip() or '(nothing printed)'}")
        if answer["error"]:
            parts.append(f"Error:\n{answer['error']}")
        if answer["result"] is not None:
            parts.append("result:\n" + json.dumps(answer["result"], indent=2, default=str))
        return "\n\n".join(parts)[:MAX_ANSWER_CHARS]


def drive(fake_ai, config, user, message: str, **kwargs) -> tuple[Agent, list]:
    events: list[tuple[str, object]] = []
    agent = Agent(config, "You are ChemAssistent.", user.execute, lambda kind, data: events.append((kind, data)),
                  **kwargs)
    agent.send(message)
    return agent, events


def answer_text(events) -> str:
    return "".join(str(data) for kind, data in events if kind == "text")


def test_creates_a_sample_the_user_confirms(fake_ai, ai_config, workspace, chemotion, test_collection,
                                            cleanup_samples):
    """Two tool rounds and a final answer, as a real conversation runs."""
    create = (f"col = instance.get_root_collection().get_collection('{test_collection}')\n"
              "sample = col.new_sample_smiles('CCO')\n"
              "sample.properties['external_label'] = 'ChemAssistent e2e'\n"
              "sample.save()\n"
              "print('saved', sample.id)\n"
              "result = sample\n")
    fake_ai.script_next(
        {"content": "I will create the sample.",
         "tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Create an ethanol sample",
                                                                 "code": create}}]},
        {"content": "The ethanol sample is in your collection."},
    )
    user = User(chemotion)

    agent, events = drive(fake_ai, ai_config, user, "Please add an ethanol sample to my test collection.")

    assert [kind for kind, _ in events][-1] == "done"
    assert user.cards[0]["purpose"] == "Create an ethanol sample"
    assert user.cards[0]["target"] == "chemotion"
    created = user.results[0]
    assert created["ok"] is True, created["error"]
    sample_id = created["result"]["id"]
    cleanup_samples.append(sample_id)

    # The model saw the printed output and the described element.
    tool_answer = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert f"saved {sample_id}" in tool_answer
    assert "ethanol" in tool_answer.lower() or "sample" in tool_answer
    assert answer_text(events).endswith("in your collection.")

    # It is really in the ELN.
    check = run_snippet({**chemotion, "code": f"result = instance.get_sample({sample_id}).properties"
                                              f"['external_label']"})
    assert check["result"] == "ChemAssistent e2e"


def test_user_rejects_the_code(fake_ai, ai_config, workspace, chemotion):
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python",
                         "arguments": {"purpose": "Delete all samples", "code": "for s in col.get_samples(): "
                                                                                "s.delete()"}}]},
        {"content": "Understood, I will not delete anything."},
    )
    user = User(chemotion)
    user.reject = "Never delete my samples."

    agent, events = drive(fake_ai, ai_config, user, "Clean up my collection.")

    assert user.results == []  # nothing ran
    tool_answer = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "Never delete my samples." in tool_answer
    assert "not delete anything" in answer_text(events)


def test_user_edits_the_code_before_running(fake_ai, ai_config, workspace, chemotion, test_collection,
                                            cleanup_samples):
    proposed = (f"col = instance.get_root_collection().get_collection('{test_collection}')\n"
                "sample = col.new_sample_smiles('CCO')\n"
                "sample.properties['external_label'] = 'wrong label'\n"
                "sample.save()\nresult = sample.id\n")
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Create a sample",
                                                                 "code": proposed}}]},
        {"content": "I used your label."},
    )
    user = User(chemotion)
    user.edit = proposed.replace("wrong label", "label the user chose")

    agent, events = drive(fake_ai, ai_config, user, "Add a sample.")

    cleanup_samples.append(user.results[0]["result"])
    tool_answer = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "The user edited the code" in tool_answer
    assert "label the user chose" in tool_answer
    check = run_snippet({**chemotion, "code": f"result = instance.get_sample({user.results[0]['result']})"
                                              f".properties['external_label']"})
    assert check["result"] == "label the user chose"


def test_agent_recovers_from_a_failing_snippet(fake_ai, ai_config, workspace, chemotion):
    """The traceback goes back to the model, which then tries something else."""
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Read a sample",
                                                                 "code": "result = instance.get_sample(0).name"}}]},
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Count collections",
                                                                 "code": "result = len(instance."
                                                                         "get_root_collection().children)"}}]},
        {"content": "Sample 0 does not exist; you have collections, though."},
    )
    user = User(chemotion)

    agent, events = drive(fake_ai, ai_config, user, "Show me sample 0.")

    answers = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "Error:" in answers[0]
    assert isinstance(user.results[1]["result"], int)
    assert [kind for kind, _ in events][-1] == "done"


def test_read_only_snippet_passes_the_auto_run_guard(fake_ai, ai_config, workspace, chemotion):
    """What auto-run mode "readonly" would run without asking."""
    from agent import looks_read_only

    code = "result = [c.label for c in instance.get_root_collection().children][:3]"
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "List collections", "code": code}}]},
        {"content": "Here are your collections."},
    )
    ok, why = looks_read_only(code)
    assert ok, why
    user = User(chemotion)

    agent, events = drive(fake_ai, ai_config, user, "Which collections do I have?")

    assert user.results[0]["ok"] is True
    assert isinstance(user.results[0]["result"], list)


def test_exchange_folder_is_shared_with_the_conversation(fake_ai, ai_config, workspace, skills_dir, chemotion,
                                                         tmp_path):
    """execute_python writes into `exchange`; the agent can read it back."""
    exchange = tmp_path / "exchange" / "conv-1"
    code = ("names = [c.label for c in instance.get_root_collection().children][:3]\n"
            "(exchange / 'collections.json').write_text(__import__('json').dumps(names))\n"
            "result = 'written'\n")
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Export collections", "code": code}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": str(exchange / "collections.json")}}]},
        {"content": "I exported and checked the file."},
    )
    user = User(chemotion, exchange=exchange)

    agent, events = drive(fake_ai, ai_config, user, "Export my collections.", exchange=exchange)

    assert (exchange / "collections.json").is_file()
    answers = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "collections.json" in answers[1]  # the agent read it inside the exchange folder
    assert [kind for kind, _ in events][-1] == "done"


def test_background_job_report_continues_the_conversation(fake_ai, ai_config, workspace, skills_dir, state_dir,
                                                          chemotion, tmp_path):
    """A finished job's report is a user message that starts a new turn."""
    exchange = tmp_path / "exchange" / "conv-2"
    jobs = Jobs(on_change=lambda: None)
    conversation = "conv-2"
    job = {"id": "job-abc123", "conversation": conversation, "purpose": "Optimise the geometry",
           "server": "http://jupyter.test", "folder": "ChemAssistent/conv-2", "kernel": "k1",
           "notebook_url": "http://jupyter.test/lab/tree/jobs/job-abc123.ipynb",
           "started": 1000.0, "state": "running", "finished": None, "report": "", "delivered": False,
           "problem": "", "wake": True}
    jobs.add(job)

    # The job ends: the watcher would write exactly this report.
    job.update(state="done", finished=1000.0 + 3725)
    text = report(job, {"started": 1000.0, "finished": 1000.0 + 3725, "output": "step 1\nstep 2\ndone\n",
                        "result": "-154.21 Hartree", "figures": ["job-abc123_figure_1.png"]},
                  {"downloaded": ["energies.json"]})
    jobs.update(job["id"], state="done", finished=job["finished"], report=text)

    assert "finished successfully after 1 h 2 min" in text
    assert "step 2" in text and "-154.21 Hartree" in text
    assert "energies.json" in text and "job-abc123_figure_1.png" in text
    assert jobs.undelivered(conversation)[0]["id"] == "job-abc123"

    # The app delivers it as a user message; the model answers the user.
    user = User(chemotion, exchange=exchange)
    fake_ai.script_next({"content": "The optimisation finished: -154.21 Hartree."})
    agent = Agent(ai_config, "You are ChemAssistent.", user.execute, lambda kind, data: None,
                  exchange=exchange, jobs=lambda: jobs.info(conversation))
    agent.send(REPORT_HEADER + text)

    sent = fake_ai.requests[0]["messages"][-1]["content"]
    assert sent.startswith(REPORT_HEADER)
    assert "Optimise the geometry" in sent
    assert agent.messages[-1]["content"] == "The optimisation finished: -154.21 Hartree."

    jobs.mark_delivered(jobs.undelivered(conversation))
    assert jobs.undelivered(conversation) == []


def test_history_round_trip_continues_a_conversation(fake_ai, ai_config, workspace, state_dir, chemotion,
                                                     test_collection, cleanup_samples):
    """Save a conversation like app.py does, then continue it with restore()."""
    code = (f"col = instance.get_root_collection().get_collection('{test_collection}')\n"
            "sample = col.new_sample_smiles('CCO')\n"
            "sample.properties['external_label'] = 'ChemAssistent history'\n"
            "sample.save()\nresult = sample.id\n")
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "Create a sample", "code": code}}]},
        {"content": "Created."},
    )
    user = User(chemotion)
    first, _ = drive(fake_ai, ai_config, user, "Add an ethanol sample.")
    sample_id = user.results[0]["result"]
    cleanup_samples.append(sample_id)

    conversation = history.new_id()
    history.save(conversation, "Add an ethanol sample", "test-model", first.messages[1:], [])
    saved = history.load(conversation)
    assert saved["title"] == "Add an ethanol sample"
    assert [entry.id for entry in history.entries()] == [conversation]

    # A new agent continues where the old one stopped.
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python",
                         "arguments": {"purpose": "Read the sample back",
                                       "code": f"result = instance.get_sample({sample_id}).properties"
                                               f"['external_label']"}}]},
        {"content": "It is still there."},
    )
    second = Agent(ai_config, "You are ChemAssistent.", user.execute, lambda kind, data: None)
    second.restore(saved["messages"], saved["roots"])
    second.send("Is it still there?")

    assert user.results[-1]["result"] == "ChemAssistent history"
    # The old turn is part of the new request.
    assert any("Add an ethanol sample" in str(m.get("content")) for m in fake_ai.requests[-1]["messages"])


def test_duration_wording():
    assert duration(5) == "5 s"
    assert duration(125) == "2 min"
    assert duration(3725) == "1 h 2 min"
    assert duration(90000) == "1 d 1 h"
