"""lessons.py: the shared lessons file, the agent's save_lesson tool, the
reminder after a fixed failure, and the lessons in the system prompt."""
from __future__ import annotations

import stat

import lessons
import workspace as workspace_module
from agent import LESSON_REMINDER, RAN_TEXT, FAILED_TEXT, Agent
from lessons import MAX_LESSON_CHARS, prompt_section, remove_lesson, save_lesson


def entries() -> list[str]:
    return [entry["key"] for entry in lessons.parse(lessons.read_text())[1]]


# --- the file ---------------------------------------------------------------------
def test_save_replace_and_remove(lessons_file):
    assert "saved" in save_lesson("sample-save-order", "chemotion_api", "Call save() before attach().")
    assert "saved" in save_lesson("rdkit-import", "package", "Import Chem from rdkit.")
    assert entries() == ["sample-save-order", "rdkit-import"]
    text = lessons_file.read_text(encoding="utf-8")
    assert text.startswith("## sample-save-order\n*") and "· chemotion_api" in text

    # The same key replaces the entry (and moves it to the end: it is fresh).
    assert "replaced" in save_lesson("sample-save-order", "chemotion_api", "Better: save() first.")
    assert entries() == ["rdkit-import", "sample-save-order"]
    assert "Better: save() first." in lessons_file.read_text(encoding="utf-8")
    assert "Call save() before" not in lessons_file.read_text(encoding="utf-8")

    assert remove_lesson("rdkit-import") == "Lesson rdkit-import removed."
    assert remove_lesson("rdkit-import").startswith("There is no lesson")
    assert entries() == ["sample-save-order"]
    assert stat.S_IMODE(lessons_file.stat().st_mode) == 0o600


def test_invalid_lessons_are_refused(lessons_file):
    assert save_lesson("Not A Slug!", "package", "x").startswith("Not saved")
    assert save_lesson("ok-key", "weather", "x").startswith("Not saved")
    assert save_lesson("ok-key", "package", " ").startswith("Not saved")
    assert save_lesson("ok-key", "package", "x" * (MAX_LESSON_CHARS + 1)).startswith("Not saved")
    assert save_lesson("ok-key", "package", "a\n## injected").startswith("Not saved")
    assert not lessons_file.exists()


def test_the_oldest_lessons_go_when_the_file_is_full(lessons_file, monkeypatch):
    monkeypatch.setattr(lessons, "MAX_LESSONS_CHARS", 400)
    for number in range(5):
        answer = save_lesson(f"lesson-{number}", "other", f"Lesson number {number}: " + "x" * 60)
    assert "dropped the oldest" in answer
    kept = entries()
    assert kept[-1] == "lesson-4" and "lesson-0" not in kept
    assert len(lessons_file.read_text(encoding="utf-8")) <= 400


def test_hand_edits_survive(lessons_file):
    lessons_file.parent.mkdir(parents=True)
    lessons_file.write_text("My own notes {with braces}\n\n## kept\nfree text\n", encoding="utf-8")
    save_lesson("new-one", "app", "Something.")
    text = lessons_file.read_text(encoding="utf-8")
    assert text.startswith("My own notes {with braces}")
    assert "## kept\nfree text" in text and "## new-one" in text


# --- the prompt ---------------------------------------------------------------------
def test_lessons_reach_the_system_prompt(lessons_file, workspace):
    assert prompt_section() == ""
    prompt = workspace_module.system_prompt()
    assert "save_lesson" in prompt and "Lessons from earlier conversations" not in prompt

    save_lesson("braces-ok", "package", "Use {'a': 1} and {0}.")  # no str.format on lessons
    prompt = workspace_module.system_prompt()
    assert "Lessons from earlier conversations" in prompt
    assert "Use {'a': 1} and {0}." in prompt


# --- the agent's tool -------------------------------------------------------------
class Recorder:
    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.events: list[tuple[str, object]] = []

    def execute(self, purpose, code, cancelled, target):
        return self.answers.pop(0)

    def on_event(self, kind, data):
        self.events.append((kind, data))


def test_save_lesson_tool(fake_ai, ai_config, workspace, lessons_file):
    recorder = Recorder([])
    agent = Agent(ai_config, "system", recorder.execute, recorder.on_event)
    fake_ai.script_next(
        {"tool_calls": [{"name": "save_lesson", "arguments": {
            "key": "collection-path", "topic": "chemotion_api", "text": "Paths start with a slash."}},
            {"name": "save_lesson", "arguments": {"key": "BAD KEY", "topic": "other", "text": "x"}}]},
        {"content": "Noted."},
    )
    agent.send("go")
    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert results[0].startswith("Lesson collection-path saved")
    assert results[1].startswith("Not saved")
    assert ("tool", "Saved a lesson: collection-path") in recorder.events
    assert sum(kind == "tool" for kind, _ in recorder.events) == 1  # the refusal is not shown
    assert entries() == ["collection-path"]

    fake_ai.script_next(
        {"tool_calls": [{"name": "save_lesson", "arguments": {"key": "collection-path", "remove": True}}]},
        {"content": "Removed."},
    )
    agent.send("it was wrong")
    assert entries() == []
    assert ("tool", "Removed a lesson: collection-path") in recorder.events


def test_reminder_after_a_fixed_failure(fake_ai, ai_config, workspace):
    recorder = Recorder([f"{FAILED_TEXT}\nAttributeError", RAN_TEXT, RAN_TEXT,
                         RAN_TEXT])
    agent = Agent(ai_config, "system", recorder.execute, recorder.on_event)
    call = {"name": "execute_python", "arguments": {"purpose": "p", "code": "x"}}
    fake_ai.script_next({"tool_calls": [call]}, {"tool_calls": [call]}, {"tool_calls": [call]},
                        {"content": "Done."})
    agent.send("go")
    results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert LESSON_REMINDER not in results[0]
    assert results[1].endswith(LESSON_REMINDER)  # the run after the failure
    assert LESSON_REMINDER not in results[2]  # only once

    # A new message starts fresh: a success alone gets no reminder.
    fake_ai.script_next({"tool_calls": [call]}, {"content": "Done."})
    agent.send("again")
    assert LESSON_REMINDER not in agent.messages[-2]["content"]
