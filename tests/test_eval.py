"""evals/run_eval.py and the agent's token counts, against the scripted AI.

The Chemotion side (setup, checks, cleanup) is a stand-in here; the agent's
snippets go to a fake runner, as in test_headless.py."""
import csv
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

import agent as agent_module
from agent import Agent
from chemassistent import Config
import chemassistent.headless as headless

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("run_eval", ROOT / "evals" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
sys.modules["run_eval"] = run_eval  # dataclasses looks the module up
spec.loader.exec_module(run_eval)

USAGE_1 = {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 200},
           "completion_tokens_details": {"reasoning_tokens": 10}, "cost": 0.001}
USAGE_2 = {"prompt_tokens": 1200, "completion_tokens": 30, "cost": 0.0015}


@pytest.fixture
def config(fake_ai):
    return Config(url="http://example.invalid", user="test", password="test", ai_url=fake_ai.url, ai_model="test")


class FakeRunner:
    """subprocess.Popen for the agent's snippets (headless.py)."""

    def __init__(self, args, **kwargs):
        self.pid = 0
        self.returncode = 0

    def communicate(self, data, timeout=None):
        return '{"ok": true, "result": 3}', ""

    def poll(self):
        return 0


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setattr(headless.subprocess, "Popen", FakeRunner)


class FakeChemotion:
    """run_eval.Chemotion: answers setup/check code by what it starts with."""

    def __init__(self, check=True):
        self.check = check
        self.codes: list[tuple[str, dict]] = []
        self.deleted_since = None

    def run(self, code, variables):
        self.codes.append((code, dict(variables)))
        if code.startswith("SETUP"):
            return {"ok": True, "result": {"label": f"{variables['run']}-x"}}
        return {"ok": True, "result": self.check, "stdout": ""}

    def snapshot(self, variables):
        return {"sample": [1, 2]}

    def delete_new(self, before, variables):
        self.deleted_since = before
        return {"deleted": {"sample": [3]}, "problems": []}


TASK = {"id": "count", "setup": "SETUP", "prompt": "Count the samples labelled {setup.label} in {collection}.",
        "expect": {"answer_regex": r"(?<!\d)3(?!\d)", "answer_contains": ["samples"], "check": "CHECK"}}


def script_two_rounds(fake_ai, final="There are 3 samples."):
    fake_ai.script_next(
        {"tool_calls": [{"name": "execute_python",
                         "arguments": {"purpose": "Count", "code": "result = 3"}}], "usage": USAGE_1},
        {"content": final, "usage": USAGE_2})


def test_agent_records_usage_per_request(fake_ai, config):
    fake_ai.script_next({"content": "hi", "usage": USAGE_1}, {"content": "again"})
    agent = Agent(config, "system", lambda *a: "", lambda *a: None)
    agent.send("hello")
    agent.send("more")
    assert fake_ai.requests[0]["stream_options"] == {"include_usage": True}
    first, second = agent.usage
    assert (first["input_tokens"], first["output_tokens"], first["cached_tokens"], first["reasoning_tokens"],
            first["cost"], first["model"]) == (1000, 50, 200, 10, 0.001, "test")
    assert second["input_tokens"] is None  # the server reported none
    total = agent_module.usage_total(agent.usage)
    assert (total["requests"], total["input_tokens"], total["output_tokens"], total["cost"]) == (2, 1000, 50, 0.001)
    assert agent_module.usage_total([])["input_tokens"] is None


def test_agent_drops_stream_options_a_server_refuses(fake_ai, config):
    fake_ai.script_next({"status": 400, "message": "Unrecognized request argument: stream_options"},
                        {"content": "hi"}, {"content": "again"})
    agent = Agent(config, "system", lambda *a: "", lambda *a: None)
    agent.send("hello")
    agent.send("more")
    assert agent.messages[-1]["content"] == "again"
    assert "stream_options" in fake_ai.requests[0]
    assert "stream_options" not in fake_ai.requests[1] and "stream_options" not in fake_ai.requests[2]


def test_plain_answer_usage(fake_ai, config):
    fake_ai.script_next({"content": "hi", "plain": True, "usage": USAGE_2})
    agent = Agent(config, "system", lambda *a: "", lambda *a: None)
    agent.send("hello")
    assert agent.usage[0]["input_tokens"] == 1200


def test_run_task_records_everything(fake_ai, config, runner):
    script_two_rounds(fake_ai)
    chemotion = FakeChemotion()
    record = run_eval.run_task(config, TASK, "test", 1, chemotion, run_eval.Options(collection="/Evals"))
    assert record["success"], record["reasons"]
    tag = record["run_tag"]
    assert record["prompts"] == [f"Count the samples labelled {tag}-x in /Evals."]
    assert record["answer"] == "There are 3 samples."
    tokens = record["tokens"]
    assert (tokens["requests"], tokens["input_tokens"], tokens["output_tokens"], tokens["cached_tokens"],
            tokens["cost"]) == (2, 2200, 80, 200, 0.0025)
    assert len(record["requests"]) == 2 and record["seconds"] >= tokens["seconds"] >= 0
    assert record["tool_calls"] == {"execute_python": 1} and record["tool_rounds"] == 1
    (execution,) = record["executions"]
    assert execution["ok"] and execution["ran"] and execution["code"] == "result = 3"
    assert record["executions_failed"] == 0
    assert [c["ok"] for c in record["checks"]] == [True, True, True]
    # The check code saw the answer and the setup result; cleanup since the snapshot.
    check_variables = chemotion.codes[-1][1]
    assert check_variables["answer"] == "There are 3 samples." and check_variables["setup"] == {"label": f"{tag}-x"}
    assert chemotion.deleted_since == {"sample": [1, 2]}
    assert record["cleanup"]["deleted"] == {"sample": [3]}
    assert record["transcript"][0] == {"role": "user", "content": record["prompts"][0]}
    assert all(body["cache"] == {"no-cache": True} for body in fake_ai.requests)  # no cached answers


def test_run_task_failure_reasons(fake_ai, config, runner):
    script_two_rounds(fake_ai, final="I found four.")
    record = run_eval.run_task(config, TASK, "test", 1, FakeChemotion(check="amount 0.1 g"), run_eval.Options())
    assert not record["success"]
    assert record["reasons"] == ["answer contains 'samples'", r"answer matches /(?<!\d)3(?!\d)/",
                                 "check code: amount 0.1 g"]


def test_run_task_rejected_snippets_and_ai_errors(fake_ai, config, runner):
    fake_ai.script_next({"tool_calls": [{"name": "execute_python", "arguments": {"purpose": "p", "code": "x"}}]},
                        {"content": "Could not run it."})
    task = {"id": "t", "prompt": "Do it."}
    record = run_eval.run_task(config, task, "test", 1, None, run_eval.Options(approve="none"))
    assert record["success"]  # no expectations
    assert record["executions_rejected"] == 1 and not record["executions"][0]["ran"]

    fake_ai.script_next({"status": 500, "message": "overloaded"})
    record = run_eval.run_task(config, task, "test", 2, None, run_eval.Options())
    assert not record["success"] and "overloaded" in record["error"] and record["reasons"] == [record["error"]]

    record = run_eval.run_task(config, TASK, "test", 3, None, run_eval.Options())
    assert "needs Chemotion" in record["error"]


def test_run_task_timeout(fake_ai, config, runner):
    release = threading.Event()
    fake_ai.script_next({"content": "a long answer that never ends", "delay": release})
    record = run_eval.run_task(config, {"id": "t", "prompt": "x", "timeout_s": 1}, "test", 1, None,
                               run_eval.Options())
    release.set()
    assert record["timed_out"] and not record["success"] and record["error"] == "timed out after 1 s"


def test_answer_checks_normalize():
    checks = run_eval.answer_checks({"answer_contains": ["c8h10n4o2"], "answer_regex": ["194[.,]1"],
                                     "answer_not_contains": ["error"]}, "Caffeine is C₈H₁₀N₄O₂, 194.19 g/mol")
    assert [c["ok"] for c in checks] == [True, True, True]


def test_fill_and_load_tasks(tmp_path):
    assert run_eval.fill("{run} {collection} {setup.a} {other}",
                         {"run": "r", "collection": "/c", "setup": {"a": 1}}) == "r /c 1 {other}"
    with pytest.raises(ValueError, match="no key 'b'"):
        run_eval.fill("{setup.b}", {"run": "r", "collection": "/c", "setup": {}})
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"collection": "/X", "tasks": [{"id": "a", "prompt": "p"},
                                                              {"id": "b", "prompt": ["p1", "p2"]}]}))
    settings, tasks = run_eval.load_tasks(path, ["b"])
    assert settings == {"collection": "/X"} and [t["id"] for t in tasks] == ["b"]
    with pytest.raises(ValueError, match="unknown task ids"):
        run_eval.load_tasks(path, ["c"])
    path.write_text(json.dumps({"tasks": [{"id": "a", "prompt": "p", "expect": {"answer_is": "x"}}]}))
    with pytest.raises(ValueError, match="unknown expect keys"):
        run_eval.load_tasks(path)


def test_shipped_tasks_load():
    settings, tasks = run_eval.load_tasks(run_eval.DEFAULT_TASKS)
    assert settings["collection"] and len(tasks) >= 4
    for task in tasks:
        compile(task.get("setup") or "", task["id"], "exec")
        compile((task.get("expect") or {}).get("check") or "", task["id"], "exec")


def test_main_writes_results(fake_ai, config, runner, tmp_path, monkeypatch):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"id": "hello", "prompt": "Say hi.",
                                            "expect": {"answer_contains": ["hi"]}}]}))
    fake_ai.script_next({"content": "hi", "usage": USAGE_1}, {"content": "nope", "usage": USAGE_2},
                        {"content": "hi there", "usage": USAGE_2}, {"content": "hi", "usage": USAGE_1})
    monkeypatch.setattr(run_eval, "build_config", lambda args: config)
    monkeypatch.setattr(run_eval, "_git_commit", lambda: "abc123")  # Popen is the fake runner
    out = tmp_path / "out"
    code = run_eval.main(["--model", "m1,m2", "--runs", "2", "--tasks", str(tasks), "--out", str(out),
                          "--no-chemotion", "--with-lessons"])
    assert code == 1  # one run failed
    data = json.loads((out / "results.json").read_text())
    assert [(r["model"], r["run"], r["success"]) for r in data["runs"]] == [
        ("m1", 1, True), ("m1", 2, False), ("m2", 1, True), ("m2", 2, True)]
    assert [body["model"] for body in fake_ai.requests] == ["m1", "m1", "m2", "m2"]
    m1 = data["summary"]["m1"]["overall"]
    assert (m1["runs"], m1["passed"], m1["success_rate"], m1["pass_at_n"], m1["pass_all_n"]) == (2, 1, 0.5, 1.0, 0.0)
    assert m1["input_tokens_total"] == 2200 and m1["output_tokens"]["mean"] == 40
    assert "cache" in fake_ai.requests[0]
    assert data["meta"]["runs"] == 2 and data["meta"]["models"] == ["m1", "m2"] and data["meta"]["system_prompt"]
    rows = list(csv.DictReader((out / "runs.csv").open()))
    assert len(rows) == 4 and rows[0]["input_tokens"] == "1000" and rows[1]["success"] == "False"
    summary = (out / "summary.md").read_text()
    assert "| m1 | hello | 1/2 | 50% |" in summary and "pass@2" in summary


def test_task_attachments(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "page.pdf").write_bytes(b"%PDF-1.4")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"id": "t", "prompt": "p", "attachments": ["assets/page.pdf"]}]}),
                     encoding="utf-8")
    _, loaded = run_eval.load_tasks(tasks)
    assert loaded[0]["attachments"] == [str((tmp_path / "assets" / "page.pdf").resolve())]

    tasks.write_text(json.dumps({"tasks": [{"id": "t", "prompt": "p", "attachments": ["gone.pdf"]}]}),
                     encoding="utf-8")
    with pytest.raises(ValueError, match="attachment not found"):
        run_eval.load_tasks(tasks)
    tasks.write_text(json.dumps({"tasks": [{"id": "t", "prompt": "p", "attachments": "x.pdf"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="list of paths"):
        run_eval.load_tasks(tasks)

    # The shipped task's PDF is there and its structure is only drawn.
    _, shipped = run_eval.load_tasks(ROOT / "evals" / "tasks.json", ["paper-structure"])
    import documents
    text = "\n".join(documents.pdf_lines(Path(shipped[0]["attachments"][0])))
    assert "Compound 3b" in text and "C8H8BrO2" in text and "bromo" not in text.lower()
