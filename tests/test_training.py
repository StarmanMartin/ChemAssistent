"""training.py: records, curation, the dataset formats and scrubbing (no GTK)."""
from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import pytest

import agent
import training


@pytest.fixture(autouse=True)
def training_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(training, "TRAINING_DIR", tmp_path / "training")
    monkeypatch.setattr(training, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(training, "WORKSPACE_DIR", tmp_path / "app" / "workspace")
    monkeypatch.setattr(training, "SKILLS_DIR", tmp_path / "app" / "skills")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path / "training"


def call(cid, code="print(1)", name="execute_python", **extra):
    args = {"purpose": "p", "code": code, **extra}
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def assistant(*calls, content=None, **extra):
    message = {"role": "assistant", "content": content, **extra}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def tool(cid, text=agent.RAN_TEXT):
    return {"role": "tool", "tool_call_id": cid, "content": text}


def event(cid, decision="run", ok=True, original="print(1)", executed=None, tool_name="execute_python"):
    ev = training.new_event(cid, tool_name, "chemotion", "p", original)
    return training.decide(ev, decision, executed_code=original if executed is None else executed,
                           ok=None if decision in ("rejected", "abandoned") else ok,
                           error="" if ok else "boom")


def rec(cid, messages, events=(), ratings=None, turns=("done",), meta=None):
    training.record(cid, messages=[{"role": "system", "content": "SYS"}] + messages, tools=[{"type": "function"}],
                    events={e["call_id"]: e for e in events}, ratings=ratings or {},
                    turns=[{"outcome": t, "at": 1.0} for t in turns], meta=meta or {"agent": "api"})


def read(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def simple(cid="c1", code="print(1)"):
    rec(cid, [{"role": "user", "content": "hi " + cid}, assistant(call("t1", code), content="ok"), tool("t1"),
              assistant(content="done")], [event("t1", original=code)])


# --- recording -------------------------------------------------------------

def test_recording_enabled():
    c = types.SimpleNamespace
    assert not training.recording_enabled(c(training_data=False, agent="api"))
    assert training.recording_enabled(c(training_data=True, agent="api"))
    assert training.recording_enabled(c(training_data=True, agent="gemini"))
    assert not training.recording_enabled(c(training_data=True, agent="claude"))
    assert not training.recording_enabled(c(training_data=True, agent="codex"))


def test_new_event_and_decide():
    ev = training.new_event("a", "execute_python", "chemotion", "why", "x=1")
    assert ev["decision"] == "abandoned" and ev["ok"] is None and ev["executed_code"] == ""
    assert set(ev) == {"call_id", "tool", "target", "purpose", "original_code", "executed_code", "decision",
                       "auto_mode", "reject_reason", "ok", "error", "duration_s", "at"}
    assert training.decide(ev, "auto", executed_code="x=1", ok=False, error="e" * 900, duration_s=1.5,
                           auto_mode="always") is ev
    assert ev["decision"] == "auto" and len(ev["error"]) == 500 and ev["auto_mode"] == "always"
    with pytest.raises(ValueError):
        training.decide(ev, "maybe")


def test_record_load_roundtrip(training_dir):
    rec("abc", [{"role": "user", "content": "x"}], ratings={3: 1, 5: -1})
    data = training.load("abc")
    assert data["ratings"] == {3: 1, 5: -1}
    assert data["version"] == 1 and data["messages"][0]["role"] == "system"
    raw = json.loads((training_dir / "abc.json").read_text())
    assert raw["ratings"] == {"3": 1, "5": -1}
    assert oct((training_dir / "abc.json").stat().st_mode & 0o777) == "0o600"
    assert training.load("missing") is None
    (training_dir / "bad.json").write_text("{nope")
    assert training.load("bad") is None


def test_created_kept():
    rec("abc", [])
    first = training.load("abc")
    rec("abc", [{"role": "user", "content": "more"}])
    second = training.load("abc")
    assert second["created"] == first["created"] and second["updated"] >= first["updated"]
    assert len(second["messages"]) == 2


def test_count_and_clear():
    assert training.count() == 0 and training.clear() == 0
    rec("a", []); rec("b", [])
    assert training.count() == 2
    assert training.clear() == 2 and training.count() == 0


def test_bad_id():
    with pytest.raises(ValueError):
        training.record("../x", messages=[], tools=[], events={}, ratings={}, turns=[], meta={})


def test_conversation_meta():
    config = types.SimpleNamespace(agent="api")
    meta = training.conversation_meta(config=config, model="m2", models=["m1", "m2", "m1"], auto_run="off",
                                      jupyter=1, system_prompt="SYS")
    assert meta["models"] == ["m1", "m2"] and meta["teacher_model"] == "m2" and meta["jupyter"] is True
    assert len(meta["system_prompt_sha256"]) == 64 and meta["agent"] == "api"
    assert meta["platform"]
    for key in ("app_commit", "chemotionapi", "chemotionapi_commit"):
        assert key in meta


def test_app_commit(tmp_path):
    assert training.app_commit(tmp_path) == ""
    sha = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
    git = tmp_path / "r" / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    assert training.app_commit(tmp_path / "r") == sha[:7]
    # a worktree: .git is a file, refs live in the common dir
    wt = tmp_path / "wt"
    (git / "worktrees" / "wt").mkdir(parents=True)
    (git / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "worktrees" / "wt" / "commondir").write_text("../..\n")
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {git / 'worktrees' / 'wt'}\n")
    assert training.app_commit(wt) == sha[:7]
    (wt / ".git").write_text("garbage")
    assert training.app_commit(wt) == ""


def test_app_commit_of_real_checkout_is_hex_or_empty():
    assert training.app_commit() == "" or len(training.app_commit()) == 7


# --- curation ----------------------------------------------------------------

def export(tmp_path, **kw):
    out = tmp_path / "out"
    summary = training.export(out, **kw)
    return summary, out


def test_simple_export_shapes(tmp_path):
    simple()
    summary, out = export(tmp_path)
    assert (summary.conversations, summary.used, summary.skipped) == (1, 1, {})
    (row,) = read(out / "sft.jsonl")
    assert row["id"] == "c1" and row["split"] in training.SPLITS
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert row["tools"] == [{"type": "function"}] and row["meta"] == {"agent": "api"}
    assert sum(summary.sft.values()) == 1
    assert (out / "README.md").read_text().startswith("# ChemAssistent teaching data")
    (meta,) = read(out / "meta.jsonl")
    assert meta["used"] is True and meta["skipped"] is None
    assert {p.name for p in summary.files} == {"sft.jsonl", "preference.jsonl", "kto.jsonl", "meta.jsonl", "README.md"}
    (kto,) = read(out / "kto.jsonl")
    assert kto["label"] is True and kto["source"] == "run" and kto["completion"][0]["role"] == "assistant"
    assert read(out / "preference.jsonl") == []
    assert "1 of 1" in summary.text()


def test_unreadable_empty_and_no_done(tmp_path, training_dir):
    training_dir.mkdir(parents=True)
    (training_dir / "junk.json").write_text("{nope")
    (training_dir / "old.json").write_text(json.dumps({"version": 99}))
    rec("empty", [])
    rec("running", [{"role": "user", "content": "q"}, assistant(content="a")], turns=("error",))
    rec("nooutcome", [{"role": "user", "content": "q"}], turns=())
    summary, out = export(tmp_path)
    assert summary.conversations == 5 and summary.used == 0
    assert summary.skipped == {"unreadable": 2, "empty": 1, "no_done_turn": 2}
    metas = {m["id"]: m for m in read(out / "meta.jsonl")}
    assert metas["empty"]["skipped"] == "empty" and not metas["junk"]["used"]


def test_cut_after_last_done_turn(tmp_path):
    rec("c", [{"role": "user", "content": "one"}, assistant(content="a1"),
              {"role": "user", "content": "two"}, assistant(content="a2"),
              {"role": "user", "content": "three"}, assistant(content="a3")], turns=("done", "done", "error"))
    export(tmp_path)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    assert [m["content"] for m in row["messages"]] == ["SYS", "one", "a1", "two", "a2"]


def test_cleanup_reasoning_reminder_private_keys(tmp_path):
    messages = [{"role": "user", "content": "q"},
                assistant(call("t1"), content="x", reasoning_content="think", _extra="z"),
                tool("t1", agent.RAN_TEXT + "\n\n" + agent.LESSON_REMINDER),
                {"role": "user", "content": [{"type": "text", "text": "hi", "_source": {"a": 1}}]},
                assistant(content="end", reasoning_content="more")]
    rec("c", messages, [event("t1")], turns=("done", "done"))
    export(tmp_path)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    dumped = json.dumps(row["messages"])
    assert "think" not in dumped and "_extra" not in dumped and "_source" not in dumped
    assert agent.LESSON_REMINDER not in dumped
    assert row["messages"][3]["content"] == agent.RAN_TEXT
    export(tmp_path, include_reasoning=True)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    assert "think" in json.dumps(row["messages"])


def test_edited_code_is_gold(tmp_path):
    note = f"{training.EDITED_PREFIX}print(2)\n```"
    rec("c", [{"role": "user", "content": "q"}, assistant(call("t1", "print(1)")), tool("t1", note + "\n\n" + agent.RAN_TEXT),
              assistant(content="fin")],
        [event("t1", "edited", original="print(1)", executed="print(2)")])
    summary, out = export(tmp_path)
    (row,) = read(out / "sft.jsonl")
    args = json.loads(row["messages"][2]["tool_calls"][0]["function"]["arguments"])
    assert args["code"] == "print(2)" and args["purpose"] == "p"
    assert row["messages"][3]["content"] == agent.RAN_TEXT
    (pair,) = read(out / "preference.jsonl")
    assert pair["source"] == "edited"
    assert json.loads(pair["chosen"][0]["tool_calls"][0]["function"]["arguments"])["code"] == "print(2)"
    assert json.loads(pair["rejected"][0]["tool_calls"][0]["function"]["arguments"])["code"] == "print(1)"
    assert [m["role"] for m in pair["prompt"]] == ["system", "user"]
    assert len(pair["chosen"]) == len(pair["rejected"]) == 1
    (kto,) = read(out / "kto.jsonl")
    assert kto["label"] is True


def test_edited_but_failed_keeps_original_code(tmp_path):
    rec("c", [{"role": "user", "content": "q"}, assistant(call("t1", "print(1)")),
              tool("t1", f"{training.EDITED_PREFIX}1/0\n```\n\n{agent.FAILED_TEXT}\nboom"),
              assistant(call("t2", "print(3)")), tool("t2"), assistant(content="fin")],
        [event("t1", "edited", ok=False, original="print(1)", executed="1/0"), event("t2", original="print(3)")])
    export(tmp_path)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    # the failed call was fixed later in the turn: removed
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert "print(3)" in json.dumps(row["messages"])


def test_failed_then_fixed_is_removed_and_paired(tmp_path):
    rec("c", [{"role": "user", "content": "q"},
              assistant(call("t1", "bad = 1")), tool("t1", agent.FAILED_TEXT + "\nNameError"),
              assistant(call("t2", "good = 1")), tool("t2", agent.RAN_TEXT + "\n\n" + agent.LESSON_REMINDER),
              assistant(content="ok")],
        [event("t1", ok=False, original="bad = 1"), event("t2", original="good = 1")])
    summary, out = export(tmp_path)
    (row,) = read(out / "sft.jsonl")
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant", "tool", "assistant"]
    assert "bad = 1" not in json.dumps(row["messages"])
    assert row["messages"][2]["tool_calls"][0]["id"] == "t2"
    (pair,) = read(out / "preference.jsonl")
    assert pair["source"] == "fixed"
    assert [m["role"] for m in pair["prompt"]] == ["system", "user"]
    assert pair["chosen"][0]["tool_calls"][0]["id"] == "t2" and pair["rejected"][0]["tool_calls"][0]["id"] == "t1"
    labels = sorted((r["source"], r["label"]) for r in read(out / "kto.jsonl"))
    assert labels == [("run", False), ("run", True)]


def test_rejected_then_ok_removes_but_keeps_text_of_message(tmp_path):
    rec("c", [{"role": "user", "content": "q"},
              assistant(call("t1", "a = 1"), call("t2", "b = 2", name="execute_python"), content="two"),
              tool("t1", "The user rejected"), tool("t2"), assistant(call("t3", "a = 1")), tool("t3"),
              assistant(content="ok")],
        [event("t1", "rejected", original="a = 1"), event("t2", original="b = 2"), event("t3", original="a = 1")])
    export(tmp_path)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    first = row["messages"][2]
    assert [c["id"] for c in first["tool_calls"]] == ["t2"] and first["content"] == "two"
    assert [m.get("tool_call_id") for m in row["messages"] if m["role"] == "tool"] == ["t2", "t3"]


def test_recovery_keeps_failures(tmp_path):
    rec("c", [{"role": "user", "content": "q"},
              assistant(call("t1", "bad = 1")), tool("t1", agent.FAILED_TEXT), assistant(call("t2", "good = 1")),
              tool("t2"), assistant(content="ok")],
        [event("t1", ok=False, original="bad = 1"), event("t2", original="good = 1")])
    export(tmp_path, recovery=True)
    (row,) = read(tmp_path / "out" / "sft.jsonl")
    assert len(row["messages"]) == 7


def test_unresolved_failure_cuts_turn(tmp_path):
    rec("c", [{"role": "user", "content": "one"}, assistant(content="a1"),
              {"role": "user", "content": "two"}, assistant(call("t1", "x = 1")), tool("t1", agent.FAILED_TEXT),
              assistant(content="giving up")],
        [event("t1", ok=False, original="x = 1")], turns=("done", "done"))
    summary, out = export(tmp_path)
    (row,) = read(out / "sft.jsonl")
    assert [m.get("content") for m in row["messages"]] == ["SYS", "one", "a1"]
    # nothing left
    training.clear()
    rec("d", [{"role": "user", "content": "two"}, assistant(call("t1", "x = 1")), tool("t1", agent.FAILED_TEXT)],
        [event("t1", "rejected")], turns=("done",))
    summary, _ = export(tmp_path)
    assert summary.skipped == {"unresolved_failure": 1}
    summary, _ = export(tmp_path, recovery=True)
    assert summary.used == 1


def test_failure_of_other_tool_does_not_resolve(tmp_path):
    rec("c", [{"role": "user", "content": "q"}, assistant(call("t1", "x = 1")), tool("t1", agent.FAILED_TEXT),
              assistant(call("t2", "y = 1", name="execute_jupyter")), tool("t2"), assistant(content="z")],
        [event("t1", ok=False, original="x = 1"), event("t2", original="y = 1", tool_name="execute_jupyter")])
    summary, _ = export(tmp_path)
    assert summary.skipped == {"unresolved_failure": 1}


def test_abandoned_cuts_even_with_recovery(tmp_path):
    rec("c", [{"role": "user", "content": "one"}, assistant(content="a1"),
              {"role": "user", "content": "two"}, assistant(call("t1", "x = 1")), tool("t1", "stopped"),
              assistant(content="end")],
        [event("t1", "abandoned")], turns=("done", "done"))
    summary, out = export(tmp_path, recovery=True)
    (row,) = read(out / "sft.jsonl")
    assert len(row["messages"]) == 3


def test_format_checks(tmp_path):
    rec("syntax", [{"role": "user", "content": "q"}, assistant(call("t1", "def (:")), tool("t1"), assistant(content="x")],
        [event("t1", original="def (:")])
    rec("args", [{"role": "user", "content": "q"},
                 assistant({"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{oops"}}),
                 tool("t1"), assistant(content="x")])
    rec("list", [{"role": "user", "content": "q"},
                 assistant({"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "[1]"}}),
                 tool("t1"), assistant(content="x")])
    rec("magic", [{"role": "user", "content": "q"},
                  assistant(call("t1", "%matplotlib inline\nx = 1", name="execute_jupyter")), tool("t1"),
                  assistant(content="x")], [event("t1", original="x", tool_name="execute_jupyter")])
    summary, _ = export(tmp_path)
    assert summary.skipped == {"format": 3} and summary.used == 1


def test_duplicates(tmp_path):
    simple("a")
    simple("b")
    for cid in ("a", "b"):  # same first message and code
        data = training.load(cid)
        data["messages"][1]["content"] = "same"
        training.record(cid, messages=data["messages"], tools=[], events=data["events"], ratings={},
                        turns=data["turns"], meta={})
    simple("c", code="print(9)")
    summary, _ = export(tmp_path)
    assert summary.skipped == {"duplicate": 1} and summary.used == 2


def test_ratings_become_kto_rows(tmp_path):
    rec("c", [{"role": "user", "content": "q"}, assistant(content="good one"),
              {"role": "user", "content": "q2"}, assistant(content="bad one"),
              {"role": "user", "content": "q3"}, assistant(content="cut")],
        ratings={2: 1, 4: -1, 6: 1}, turns=("done", "done", "error"))
    _, out = export(tmp_path)
    rows = read(out / "kto.jsonl")
    assert [(r["source"], r["label"], r["completion"][0]["content"]) for r in rows] == [
        ("rating", True, "good one"), ("rating", False, "bad one")]
    assert [m["role"] for m in rows[0]["prompt"]] == ["system", "user"]
    assert rows[0]["tools"] == [{"type": "function"}]


def test_rejected_without_fix_in_recovery_is_false_label(tmp_path):
    rec("c", [{"role": "user", "content": "q"}, assistant(call("t1", "x = 1")), tool("t1", "rejected"),
              assistant(content="ok")], [event("t1", "rejected", original="x = 1")])
    _, out = export(tmp_path, recovery=True)
    (row,) = read(out / "kto.jsonl")
    assert row["label"] is False and row["source"] == "run"


def test_split_stable_and_whole_conversations(tmp_path):
    for n in range(30):
        simple(f"conv{n}", code=f"print({n})")
    s1, out = export(tmp_path)
    first = {r["id"]: r["split"] for r in read(out / "sft.jsonl")}
    s2, out = export(tmp_path)
    assert first == {r["id"]: r["split"] for r in read(out / "sft.jsonl")}
    assert set(first.values()) == {"train", "validation", "test"}
    assert sum(s1.sft.values()) == 30 and s1.sft["train"] > s1.sft["test"]
    kto = {r["id"]: r["split"] for r in read(out / "kto.jsonl")}
    assert kto == first
    assert training.split_of("x", (1, 0, 0)) == "train"
    assert training.split_of("x", (0, 0, 1)) == "test"


def test_export_never_raises_for_a_bad_record(tmp_path, training_dir):
    simple("ok")
    training_dir.mkdir(exist_ok=True)
    (training_dir / "weird.json").write_text(json.dumps(
        {"version": 1, "messages": [{"role": "user", "content": 5}, "str"], "turns": [{"outcome": "done"}]}))
    (training_dir / "weird2.json").write_text(json.dumps(
        {"version": 1, "messages": [{"role": "user", "content": "q"}, {"role": "assistant", "tool_calls": [5]}],
         "turns": [{"outcome": "done"}], "events": []}))
    summary, _ = export(tmp_path)
    assert summary.used >= 1 and summary.conversations == 3


# --- scrubbing -----------------------------------------------------------------

def test_scrub(tmp_path):
    state = tmp_path / "state"
    home = tmp_path / "home"
    s = training.Scrubber({"url": "https://eln.kit.edu/", "user": "martin"}, ["s3cr3tTOKEN", "ab"])
    value = {"k martin": [f"login martin at https://eln.kit.edu/api and eln.kit.edu, mail a.b@kit.edu,"
                          f" token s3cr3tTOKEN, file {state}/exchange/x.png, {tmp_path}/app/workspace/y,"
                          f" {tmp_path}/app/skills/z, {home}/doc, martinez", 5, None]}
    out = training.scrub(value, s)
    assert list(out) == ["k martin"]  # keys untouched
    text = out["k martin"][0]
    assert text == ("login <user> at https://eln.example.org/api and eln.example.org, mail <email>,"
                    " token <secret>, file <state>/exchange/x.png, <workspace>/y, <skills>/z, ~/doc, martinez")
    assert out["k martin"][1:] == [5, None]
    assert s.secret_hits == 1 and s.replacements == 9
    assert value["k martin"][0].startswith("login martin")  # original untouched


def test_export_scrubs_everything_and_counts(tmp_path):
    token = "tok-123456"
    rec("c", [{"role": "user", "content": f"use https://eln.kit.edu as martin ({token})"},
              assistant(call("t1", f"print('{tmp_path}/home/x')")), tool("t1", f"{tmp_path}/state/y {token}"),
              assistant(content="done")],
        [event("t1", original="print('x')")], meta={"agent": "api", "who": "martin@kit.edu"})
    summary, out = export(tmp_path, private={"url": "https://eln.kit.edu", "user": "martin"}, secrets=[token])
    for name in ("sft", "preference", "kto", "meta"):
        text = (out / f"{name}.jsonl").read_text()
        for bad in (token, "eln.kit.edu", "martin", str(tmp_path)):
            assert bad not in text, (name, bad)
    assert summary.secret_hits >= 2 and summary.replacements >= 6
    assert "WARNING" in summary.text()
    assert "<user>" in (out / "sft.jsonl").read_text()


def test_no_private_given_still_scrubs_email_and_paths(tmp_path):
    rec("c", [{"role": "user", "content": f"mail x@y.org {tmp_path}/home/f"}, assistant(content="a")])
    _, out = export(tmp_path)
    (row,) = read(out / "sft.jsonl")
    assert row["messages"][1]["content"] == "mail <email> ~/f"


def test_export_overwrites(tmp_path):
    simple("a")
    export(tmp_path)
    training.clear()
    summary, out = export(tmp_path)
    assert summary.used == 0 and read(out / "sft.jsonl") == []
