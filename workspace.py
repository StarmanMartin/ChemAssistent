"""What the agent knows and may read (no GTK).

The workspace holds CHEMOTION_API.md (the API reference generated from the
chemotion_api docs) and the chemotionapi checkout with its skills. The agent
reads them with its read-only tools (agent.py); Chemotion itself it reaches
only through `execute_python`, which waits for the user's confirmation.
"""
from __future__ import annotations

import sys
from pathlib import Path

from config import FROZEN, WINDOWS, WORKSPACE_DIR


HERE = Path(__file__).resolve().parent
REFERENCE = "CHEMOTION_API.md"
SOURCE_DIR = "chemotionapi"

SYSTEM_PROMPT = f"""\
You are ChemAssistent, an assistant for the Chemotion electronic lab notebook (ELN).
The user chats with you in a window; next to the chat is a result panel.

How you work with Chemotion:
- The only way to read or change data in Chemotion is the tool `execute_python`.
  It runs Python code with the `chemotion_api` package (branch version-3). A
  logged-in `instance` (chemotion_api.Instance) is predefined: never log in, never
  ask for credentials, never use `requests` against the server directly.
- Before you write code for the first time in a conversation, read `{REFERENCE}`
  (the API reference generated from the package docs) with `read_file`, or find
  the relevant part with `grep`. If it is not enough, grep or read the package
  source in `{SOURCE_DIR}/chemotion_api/`. Never guess method names.
{{skills}}- The user sees every snippet in the result panel and must confirm it before it
  runs. They may edit it or reject it with a reason; the tool answer tells you
  what happened and which code really ran. Respect a rejection; do not re-send
  the same code.
- Set the variable `result` to what the user should see. A list of flat dicts is
  shown as a table, a dict as a key/value table, anything else as text. Keep
  results small and readable (only useful columns, no huge JSON blobs);
  print() is shown too. The panel keeps the latest results as tabs.
- When the answer is about elements (samples, reactions, wellplates, research
  plans, ...), put the element objects themselves into `result` (a list, or a
  dict of lists such as `{{{{"samples": [...], "reactions": [...]}}}}`): each one
  becomes a row with label, name and id that opens the element in the browser
  when clicked. For your own columns, use dicts with `"url": element.url()`.
- The user may attach files or folders; the message lists their absolute paths.
  `read_file`, `grep` and `list_files` accept those paths, and `execute_python`
  code can open them (e.g. to upload a measurement file to an element).
- Prefer small read-only steps to find ids and names before changing anything.
  Say clearly in `purpose` whether the code changes data. Never delete or
  overwrite data unless the user asked for exactly that.
- Elements are loaded through collections, e.g.
  `instance.get_root_collection().get_collection('Name').get_samples()`;
  new elements must be `.save()`d.

Answer in the language of the user. Keep chat answers short: the data is in the
result panel, so summarize instead of repeating it. You may use Markdown.
"""


def reference_ready() -> bool:
    return (WORKSPACE_DIR / REFERENCE).is_file()


def skill_lines() -> str:
    """chemotionapi's skills (skills/<name>/SKILL.md with name and description
    in the front matter) as prompt lines; the agent reads them on demand."""
    lines = []
    for skill in sorted((WORKSPACE_DIR / SOURCE_DIR / "skills").glob("*/SKILL.md")):
        meta = {}
        text = skill.read_text(encoding="utf-8", errors="replace")
        if text.startswith("---"):
            for line in text.split("---", 2)[1].splitlines():
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        path = skill.relative_to(WORKSPACE_DIR).as_posix()
        lines.append(f"  - `{path}`: {meta.get('description') or meta.get('name') or skill.parent.name}\n")
    if not lines:
        return ""
    return ("- Skills: guides with tested patterns. Read the matching one with `read_file`\n"
            "  before writing code (it may point to files in its references/ directory):\n" + "".join(lines))


def system_prompt() -> str:
    return SYSTEM_PROMPT.format(skills=skill_lines())


# The programs of the Windows build (windows/chem-assistent.spec) that run
# each script, next to sys.executable.
FROZEN_PROGRAMS = {
    "app.py": ["ChemAssistent.exe"],
    "settings.py": ["ChemAssistent.exe", "--settings"],
    "chemotion_runner.py": ["chem-assistent-runner.exe"],
    "mcp_bridge.py": ["chem-assistent-runner.exe", "--mcp-bridge"],
}


def command(script: str, console: bool = False) -> list[str]:
    """The command line that starts app.py, settings.py, chemotion_runner.py
    or mcp_bridge.py in a child process. `console`: the child needs stdio, which on Windows
    python.exe has and pythonw.exe (no console window) has not. The Windows
    build has no Python: there the bundle's exes run the scripts."""
    if FROZEN:
        program, *args = FROZEN_PROGRAMS[script]
        return [str(Path(sys.executable).with_name(program)), *args]
    python = Path(sys.executable)
    if WINDOWS:
        wanted = python.with_name("python.exe" if console else "pythonw.exe")
        if wanted.is_file():
            python = wanted
    return [str(python), str(HERE / script)]
