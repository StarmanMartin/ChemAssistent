"""Lessons: one Markdown file shared by all conversations (no GTK, stdlib).

When the agent fixed a snippet that failed because of a non-obvious
behaviour of chemotion_api, a package, JupyterLab or the app, it records what
works with its `save_lesson` tool. Every new conversation gets the lessons in
its system prompt (workspace.system_prompt), so the same mistake is not made
again. The user sees every save as a note in the chat and can edit or clear
the file in the settings (tab "Lessons").

The file is plain Markdown, one entry per `## <key>` heading:

    ## sample-save-needs-collection
    *2026-10-25 · chemotion_api · chemotionapi a1b2c3d*

    What fails and what works instead.

Text before the first heading (or that does not look like an entry) is kept
as it is, so a hand edit never gets lost.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date

from config import STATE_DIR, WORKSPACE_DIR


LESSONS_FILE = STATE_DIR / "lessons.md"
# The whole file goes into every system prompt: keep it small. The oldest
# entries are dropped when a new one would exceed it.
MAX_LESSONS_CHARS = 6000
MAX_LESSON_CHARS = 600
TOPICS = ("chemotion_api", "jupyter", "package", "app", "other")
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")

LOCK = threading.Lock()


def api_commit() -> str:
    """The chemotionapi commit of the workspace (short), or ""; no git needed."""
    git = WORKSPACE_DIR / "chemotionapi" / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            path = git / ref
            if path.is_file():
                head = path.read_text(encoding="utf-8").strip()
            else:  # packed refs
                for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        head = line.split()[0]
                        break
        return head[:7] if re.fullmatch(r"[0-9a-f]{40}", head) else ""
    except OSError:
        return ""


def read_text() -> str:
    try:
        return LESSONS_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_text(text: str) -> None:
    """Atomically write the file, readable only by the user (also for the settings)."""
    LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LESSONS_FILE.with_name(LESSONS_FILE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text.strip() + "\n" if text.strip() else "")
    tmp.chmod(0o600)
    os.replace(tmp, LESSONS_FILE)


def parse(text: str) -> tuple[str, list[dict]]:
    """(text before the first entry, entries {key, block}); `block` is the
    entry's whole text including its heading."""
    parts = re.split(r"(?m)^(?=## )", text)
    head = parts[0] if parts and not parts[0].startswith("## ") else ""
    entries = []
    for part in parts:
        if part.startswith("## "):
            key = part[3:].split("\n", 1)[0].strip()
            entries.append({"key": key, "block": part.strip()})
    return head.strip(), entries


def render(head: str, entries: list[dict]) -> str:
    return "\n\n".join([head] * bool(head) + [entry["block"] for entry in entries])


def save_lesson(key: str, topic: str, text: str) -> str:
    """Add or replace (same key) a lesson; the answer for the agent."""
    key = key.strip().lower()
    text = text.strip()
    if not KEY_PATTERN.match(key):
        return "Not saved: `key` must be a short slug (lowercase letters, digits, dashes), e.g. sample-save-order."
    if topic not in TOPICS:
        return f"Not saved: `topic` must be one of {', '.join(TOPICS)}."
    if not text:
        return "Not saved: `text` is empty."
    if len(text) > MAX_LESSON_CHARS:
        return f"Not saved: `text` has {len(text)} characters, at most {MAX_LESSON_CHARS}. Shorten it."
    if "\n## " in "\n" + text:
        return "Not saved: `text` must not contain `## ` headings."
    commit = api_commit()
    meta = " · ".join([date.today().isoformat(), topic] + [f"chemotionapi {commit}"] * bool(commit))
    block = f"## {key}\n*{meta}*\n\n{text}"
    with LOCK:
        head, entries = parse(read_text())
        replaced = any(entry["key"] == key for entry in entries)
        entries = [entry for entry in entries if entry["key"] != key] + [{"key": key, "block": block}]
        dropped = []
        while len(render(head, entries)) > MAX_LESSONS_CHARS and len(entries) > 1:
            dropped.append(entries.pop(0)["key"])
        write_text(render(head, entries))
    answer = f"Lesson {key} {'replaced' if replaced else 'saved'}; later conversations will see it."
    if dropped:
        answer += f" The file was full: dropped the oldest lesson(s) {', '.join(dropped)}."
    return answer


def remove_lesson(key: str) -> str:
    key = key.strip().lower()
    with LOCK:
        head, entries = parse(read_text())
        kept = [entry for entry in entries if entry["key"] != key]
        if len(kept) == len(entries):
            return f"There is no lesson {key}."
        write_text(render(head, kept))
    return f"Lesson {key} removed."


def prompt_section() -> str:
    """The lessons for the system prompt, or "" without any."""
    text = read_text().strip()
    if not text:
        return ""
    if len(text) > MAX_LESSONS_CHARS:  # edited by hand beyond the cap
        text = text[-MAX_LESSONS_CHARS:]
    return text
