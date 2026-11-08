"""The ChemAssistent agent: a tool-calling loop over an OpenAI-compatible
chat completions API (no GTK, standard library only).

Only standard endpoints are used: GET <ai_url>/models and POST
<ai_url>/chat/completions with `stream`, `tools` and `tool_choice`, so the
KIT KI-Toolbox, OpenAI, Ollama, vLLM, LiteLLM, ... all work.

The model gets exactly four tools: `execute_python` (the app provides it as a
callback; whether the user must confirm first depends on the auto-run mode:
"off" always asks, "readonly" runs read-only snippets automatically —
`looks_read_only()` decides — and "always" runs everything)
and read-only `read_file`, `grep` and `list_files`, which cannot leave the
workspace (the API reference and the chemotionapi checkout) and the files
and folders the user attached. With JupyterLab configured there are also
`execute_jupyter` (jupyter.py; same callback with target "jupyter"),
`start_jupyter_job` (a background job in a kernel of its own, jobs.py; same
callback with target "job", it returns at once and the report arrives later
as a new message) and `jupyter_jobs` (their state), and the conversation's
exchange folder is readable too.

MCP servers the user added (mcp_client.py; the app passes the conversation's
McpHub as `mcp`) bring their tools as mcp__<server>__<tool>; a call goes to
the same callback with target "mcp" (a card with the JSON arguments). In a
turn whose message starts with "/mcp" (mcp_command()) the agent also gets
MCP_ADMIN_TOOLS to list, add and remove servers; adding and removing go to
the callback as targets "mcp-add"/"mcp-remove", so the user confirms them.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__:
    from . import issues, lessons, history
    from .config import AUTO_RUN_MODES, SKILLS_DIR, WORKSPACE_DIR, Config
else:
    import issues
    import lessons
    import history
    from config import AUTO_RUN_MODES, SKILLS_DIR, WORKSPACE_DIR, Config


REQUEST_TIMEOUT_S = 300
# What the agent gets back from one snippet (app.py, headless.py); the panel
# shows everything.
MAX_ANSWER_CHARS = 20000
# Tool rounds per user message before the agent stops and asks the user.
MAX_ROUNDS = 40
MAX_READ_LINES = 400
# chemotionapi's skills in the workspace (workspace.SOURCE_DIR + "/skills").
CHEMOTIONAPI_SKILLS = Path("chemotionapi", "skills")
MAX_TOOL_CHARS = 40000
MAX_GREP_MATCHES = 100
MAX_LIST_ENTRIES = 300
# Files grep looks at below one directory (an attached folder may be huge).
MAX_GREP_FILES = 5000
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
# Appended to a user message with attachments; the history shows the text before it.
ATTACHMENT_NOTE = ("\n\nAttached by the user (read them with read_file / list_files / grep by these "
                   "absolute paths; execute_python code can open them, e.g. to upload a file to "
                   "Chemotion):\n")
STOPPED_NOTE = "(Stopped after many tool calls.)"


def execute_python_tool(mode: str, headless: bool = False) -> dict:
    """The execute_python tool for one auto-run mode (config.AUTO_RUN_MODES):
    what the description says about running depends on it. `headless`:
    headless.py's approval function decides, nobody sees a panel."""
    if headless:
        confirm = ("The calling program's approval function decides whether the code runs; it may "
                   "reject it.")
    elif mode == "readonly":
        confirm = (
            "Read-only code (fetching data only) runs automatically; code that creates, changes "
            "or deletes data, or writes files, waits for the user to confirm it in the result "
            "panel; they may edit it first or reject it with a reason."
        )
    elif mode == "always":
        confirm = (
            "The code runs automatically; the user still sees it in the result panel. Prefer "
            "read-only code, and be careful with anything that changes or deletes data."
        )
    else:
        confirm = (
            "The user sees the code in the result panel and must confirm it before it runs; "
            "they may edit it first or reject it with a reason."
        )
    return {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Run Python code against the user's Chemotion ELN with the chemotion_api package. "
                "A logged-in `instance` (chemotion_api.Instance) is predefined; never log in. "
                "Only chemotion_api and the standard library are available: never install packages "
                "or start other programs (pip, subprocess) here. "
                + confirm
                + (" Assign the data you need to `result`. The answer contains the printed output, "
                   "`result` as JSON, the error traceback, or that the code was not approved."
                   if headless else
                   " Assign what the user should see to `result`. The answer contains the printed "
                   "output, `result` as JSON, the error traceback, or the user's reason for rejecting "
                   "the code.")
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {
                        "type": "string",
                        "description": "One short sentence for the user: what the code does and "
                                       "whether it changes data.",
                    },
                    "code": {"type": "string", "description": "Python source."},
                },
                "required": ["purpose", "code"],
            },
        },
    }


def execute_jupyter_tool(mode: str) -> dict:
    """The execute_jupyter tool for one auto-run mode: only "always" runs it
    without confirmation (looks_read_only() knows only Chemotion code)."""
    if mode == "always":
        confirm = "The code runs automatically; the user still sees it in the result panel."
    else:
        confirm = ("The user sees the code in the result panel and must confirm it before it runs; "
                   "they may edit it first or reject it with a reason.")
    return {
        "type": "function",
        "function": {
            "name": "execute_jupyter",
            "description": (
                "Run Python code in the user's JupyterLab, in the kernel of this conversation's "
                "notebook (variables persist between calls), for analyses Chemotion cannot do: "
                "RDKit, pandas, numpy, plots, spectra. `exchange` (pathlib.Path) is predefined: the "
                "folder shared with execute_python; read input from it, save files (figures, CSV, "
                "JSON) there to hand them back. Never redefine `exchange` or use paths of the "
                "user's computer: JupyterLab may run elsewhere. No access to Chemotion from here. "
                + confirm +
                " The answer contains printed output, the value of the last expression, the error "
                "traceback, which figures the user saw, and which files were exchanged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "description": "One short sentence for the user: what the code does."},
                    "code": {"type": "string", "description": "Python source (IPython: %pip and display() work)."},
                },
                "required": ["purpose", "code"],
            },
        },
    }


def start_jupyter_job_tool(mode: str) -> dict:
    """The start_jupyter_job tool; confirmation as for execute_jupyter."""
    if mode == "always":
        confirm = "It starts automatically; the user still sees the code in the result panel."
    else:
        confirm = ("The user sees the code in the result panel and must confirm it before it starts; "
                   "they may edit it first or reject it with a reason.")
    return {
        "type": "function",
        "function": {
            "name": "start_jupyter_job",
            "description": (
                "Start a long calculation in JupyterLab as a background job: for anything that may take "
                "longer than a few minutes (hours or days are fine). It runs in a NEW kernel of its own, "
                "so variables of execute_jupyter runs do not exist there: the code must import "
                "everything and read its input from files in `exchange` (predefined pathlib.Path, as in "
                "execute_jupyter) and save every result there (JSON/CSV, figures as PNG). Printed output "
                "is kept as the job's log, so print progress. " + confirm +
                " The answer only says that the job started. Do not wait for it or poll it: finish your "
                "answer to the user (say what runs and that you continue when it is done). When the job "
                "ends you get a message with its outcome, printed output and new files, and continue then."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "description": "One short sentence for the user: what the job computes."},
                    "code": {"type": "string", "description": "Self-contained Python source (IPython)."},
                },
                "required": ["purpose", "code"],
            },
        },
    }


JUPYTER_JOBS_TOOL = {
    "type": "function",
    "function": {
        "name": "jupyter_jobs",
        "description": "The background jobs of this conversation: state, run time and, for running jobs, "
                       "their latest printed output. Only when the user asks how a job is doing; you are "
                       "told anyway when a job ends.",
        "parameters": {"type": "object", "properties": {}},
    },
}


# How the app's answer to an execute_* call starts its verdict (app.py
# RequestCard._answer_text); the agent reminds itself of save_lesson after a
# failure that a later run in the same turn fixed.
RAN_TEXT = "The code ran successfully."
FAILED_TEXT = "The code failed:"
LESSON_REMINDER = ("An earlier snippet in this turn failed and this one worked. If the failure came from a "
                   "non-obvious behaviour of chemotion_api, a package, JupyterLab or this app (not a typo "
                   "or a wrong guess), record what works with save_lesson.")

SAVE_LESSON_TOOL = {
    "type": "function",
    "function": {
        "name": "save_lesson",
        "description": (
            "Record a lesson for all later conversations (they get it in their instructions): after a "
            "snippet failed because of a non-obvious behaviour of chemotion_api, a package, JupyterLab or "
            "this app, and you found what works. Not for your own typos. Only how the tools behave, never "
            "data from Chemotion or about the user. The same key replaces a lesson; `remove` deletes one. "
            "The user sees every lesson and can edit them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string",
                        "description": "Short slug naming the lesson, e.g. sample-save-needs-collection."},
                "topic": {"type": "string", "enum": list(lessons.TOPICS)},
                "text": {"type": "string",
                         "description": f"What fails, why, and the working call (at most "
                                        f"{lessons.MAX_LESSON_CHARS} characters, Markdown)."},
                "remove": {"type": "boolean", "description": "Delete the lesson `key` instead (it was wrong)."},
            },
            "required": ["key"],
        },
    },
}


# Developer mode only (config.developer_mode, issues.py); mcp_bridge.py offers it too.
REPORT_API_BUG_TOOL = {
    "type": "function",
    "function": {
        "name": "report_api_bug",
        "description": (
            "Report a bug in chemotion_api as an issue of its GitHub repository (developer mode). Use it "
            "for every bug you find in chemotion_api (wrong results, exceptions from correct calls, "
            "methods that do not work as documented), once you are sure it is the package's fault and not "
            "your code. The app adds the date, the AI model, the Chemotion instance (URL, version) and the "
            "chemotion_api branch itself. The issue is public: no data from Chemotion (names, ids, "
            "values of the user's elements), no credentials, nothing about the user; use placeholders."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short summary of the bug."},
                "description": {"type": "string",
                                "description": "Markdown: what you did, a minimal code snippet that reproduces "
                                               "it, the expected and the actual behaviour (with the traceback), "
                                               "and the cause in the source if you found it."},
            },
            "required": ["title", "description"],
        },
    },
}


# MCP (mcp_client.py): the agent's names of MCP tools start with this.
MCP_TOOL_PREFIX = "mcp__"
MCP_COMMAND = re.compile(r"\s*/mcp(?![\w-])", re.IGNORECASE)


def mcp_command(text: str) -> bool:
    """The user's message asks to manage MCP servers ("/mcp ..."): that turn
    gets MCP_ADMIN_TOOLS."""
    return bool(MCP_COMMAND.match(text or ""))


MCP_ADMIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_mcp_servers",
            "description": "List the MCP servers saved in the settings: name, command or URL, and whether "
                           "they run in this conversation with which tools. Values of env and headers are "
                           "not shown.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_mcp_server",
            "description": (
                "Add an MCP server to the settings (the same name replaces one). Either `command` (+ `args`, "
                "`env`) for a server program speaking MCP on stdio, or `url` (+ `headers`) for a remote "
                "server (Streamable HTTP). You cannot install software: use a command that fetches the "
                "server itself, e.g. command `npx` with args [\"-y\", \"<npm package>\", ...] (Node.js), "
                "`uvx` with [\"<PyPI package>\", ...] (Python, uv) or `docker` with [\"run\", \"-i\", \"--rm\", "
                "\"<image>\"]. The user sees the entry in the result panel and must confirm it (they may edit "
                "it); the app then starts the server and saves it only if it starts and lists its tools. "
                "The answer names its tools (mcp__<name>__<tool>), or the error, e.g. the server's output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short name: letters, digits, - and _ (no "
                                                              "double underscore), e.g. filesystem."},
                    "command": {"type": "string", "description": "The program, e.g. npx, uvx, docker."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Its arguments."},
                    "env": {"type": "object", "additionalProperties": {"type": "string"},
                            "description": "Environment variables, e.g. an API key the user gave you."},
                    "url": {"type": "string", "description": "Instead of command: the server's URL."},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"},
                                "description": "HTTP headers for `url`, e.g. Authorization."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_mcp_server",
            "description": "Remove an MCP server from the settings and from this conversation; the user "
                           "confirms it in the result panel.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
]
MCP_ADMIN_NAMES = {tool["function"]["name"] for tool in MCP_ADMIN_TOOLS}
# What add_mcp_server takes into the settings entry (config.normalize_mcp_server).
MCP_SERVER_KEYS = ("name", "command", "args", "env", "url", "headers")


def mcp_server_entry(arguments: dict) -> str:
    """The settings entry add_mcp_server shows on its card (JSON, editable)."""
    entry = {key: arguments[key] for key in MCP_SERVER_KEYS if arguments.get(key)}
    return json.dumps(entry, indent=2, ensure_ascii=False)


def clip(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters not shown)"


def build_tools(mode: str, jupyter: bool = False, developer: bool = False,
                headless: bool = False) -> list[dict]:
    """The tools with the execute_* descriptions for `mode`; the JupyterLab
    tools only with JupyterLab configured, report_api_bug in developer mode,
    save_lesson except for `headless` (headless.py: no prompt may change the
    lessons every other conversation reads)."""
    if not jupyter:
        tools = [execute_python_tool(mode, headless)] + READ_ONLY_TOOLS
    else:
        tools = ([execute_python_tool(mode), execute_jupyter_tool(mode), start_jupyter_job_tool(mode),
                  JUPYTER_JOBS_TOOL] + READ_ONLY_TOOLS)
    if not headless:
        tools.append(SAVE_LESSON_TOOL)
    return tools + [REPORT_API_BUG_TOOL] if developer else tools

# The read-only tools; build_tools() prepends execute_python with the
# description for the current auto-run mode.
READ_ONLY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": f"Read a text file of the workspace or one the user attached, with line "
                           f"numbers, at most {MAX_READ_LINES} lines per call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace, or the "
                                                              "absolute path of an attachment."},
                    "offset": {"type": "integer", "description": "First line (1-based), default 1."},
                    "limit": {"type": "integer", "description": f"Number of lines, default {MAX_READ_LINES}."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the workspace or an attachment with a regular expression (Python "
                           "syntax, case-insensitive). Returns path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "File or directory, default the whole workspace. "
                                                              "Attachments by absolute path."},
                    "glob": {"type": "string", "description": "File name filter, e.g. *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List a directory of the workspace or an attached folder (directories end with /).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Default: the workspace root. "
                                                                         "Attachments by absolute path."}},
            },
        },
    },
]


class AgentError(Exception):
    pass


# --- HTTP ---------------------------------------------------------------------
def _request(config: Config, path: str, body: dict | None = None):
    url = config.ai_url.rstrip("/") + path
    headers = {"Accept": "application/json, text/event-stream"}
    if config.ai_token:
        headers["Authorization"] = f"Bearer {config.ai_token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S, context=ssl.create_default_context())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        try:
            error = json.loads(detail).get("error", detail)
            detail = error.get("message", detail) if isinstance(error, dict) else str(error)
        except (ValueError, AttributeError):
            pass
        hint = " (check the AI token in the settings)" if exc.code in (401, 403) else ""
        raise AgentError(f"{url}: HTTP {exc.code}{hint}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise AgentError(f"Cannot reach {url}: {getattr(exc, 'reason', exc)}") from None


def list_models(config: Config) -> list[str]:
    """The chat model ids the endpoint offers (GET /models), sorted. Models an
    Open WebUI (KI-Toolbox) marks as hidden, e.g. embeddings, are left out."""
    with _request(config, "/models") as response:
        try:
            data = json.loads(response.read().decode("utf-8"))
        except ValueError:
            raise AgentError(f"{config.ai_url}/models did not return JSON") from None
    models = []
    for item in data.get("data", []) if isinstance(data, dict) else []:
        meta = ((item.get("info") or {}).get("meta") or {}) if isinstance(item, dict) else {}
        if isinstance(item, dict) and item.get("id") and not meta.get("hidden"):
            models.append(str(item["id"]))
    return sorted(models)


# --- tools without the user ----------------------------------------------------
# They read only below `roots`: the workspace and what the user attached.
def base_roots(history_access: bool) -> list[Path]:
    """What every conversation may read before any attachment: the workspace
    and, with the setting on, the saved conversations (workspace.HISTORY_PROMPT
    says when). Agent, CliAgent and so mcp_bridge.py start from it."""
    roots = [WORKSPACE_DIR.resolve()]
    if history_access:
        roots.append(history.HISTORY_DIR.resolve())
    return roots


def _allowed(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


def _skill_dirs(roots: list[Path]) -> list[Path]:
    """The folders of the skills workspace.skill_lines() lists (chemotionapi's in
    the workspace, the app's in SKILLS_DIR) that `roots` allow."""
    found = (sorted((WORKSPACE_DIR / CHEMOTIONAPI_SKILLS).glob("*/SKILL.md"))
             + sorted(SKILLS_DIR.glob("*/SKILL.md")))
    return [skill.parent.resolve() for skill in found if _allowed(skill.parent.resolve(), roots)]


def _resolve(path: str | None, roots: list[Path]) -> Path:
    """An absolute path inside one of `roots`, or a path relative to the workspace.
    A relative path that is not in the workspace may be relative to a skill's
    folder: a SKILL.md points to `references/api.md` that way."""
    workspace = WORKSPACE_DIR.resolve()
    text = (path or ".").strip()
    absolute = Path(text).expanduser().is_absolute()
    if absolute and _allowed(Path(text).expanduser().resolve(), roots):
        target = Path(text).expanduser().resolve()
    else:
        # Relative paths, and "/CHEMOTION_API.md"-style ones, are workspace paths.
        target = (workspace / text.lstrip("/\\")).resolve()
        if not absolute and not target.exists():
            in_skills = [found for found in ((skill / text).resolve() for skill in _skill_dirs(roots))
                         if found.exists() and _allowed(found, roots)]
            if len(in_skills) > 1:
                raise AgentError(f"{path} is ambiguous: " + ", ".join(_shown(found) for found in in_skills))
            if in_skills:
                return in_skills[0]
        if not _allowed(target, roots) or (absolute and not target.exists()):
            raise AgentError(f"{path} is outside the workspace and the user's attachments.")
    if not target.exists():
        raise AgentError(f"{path} does not exist.")
    return target


def _shown(path: Path) -> str:
    """Workspace files relative, attachments with their absolute path."""
    workspace = WORKSPACE_DIR.resolve()
    if path == workspace or workspace in path.parents:
        return path.relative_to(workspace).as_posix() or "."
    return str(path)


def _is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\0" in handle.read(8192)


def read_file(path: str, roots: list[Path], offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    target = _resolve(path, roots)
    if target.is_dir():
        raise AgentError(f"{path} is a directory; use list_files.")
    if _is_binary(target):
        return (f"{_shown(target)} is a binary file ({target.stat().st_size} bytes). It cannot be shown "
                f"as text; execute_python code can open it by its absolute path {target}.")
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or MAX_READ_LINES), MAX_READ_LINES))
    chunk = [f"{number:6}\t{line}" for number, line in enumerate(lines[start - 1:start - 1 + count], start)]
    text = "\n".join(chunk)[:MAX_TOOL_CHARS]
    end = start + len(chunk) - 1
    more = f"\n... lines {end + 1}-{len(lines)} not shown (read_file with offset {end + 1})" if end < len(lines) else ""
    return f"{_shown(target)}, lines {start}-{end} of {len(lines)}:\n{text}{more}"


def _files(root: Path, glob: str | None):
    if root.is_file():
        yield root
        return
    for index, path in enumerate(sorted(root.rglob(glob or "*"))):
        if index >= MAX_GREP_FILES:
            return
        if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def grep(pattern: str, roots: list[Path], path: str | None = None, glob: str | None = None) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise AgentError(f"Invalid regular expression: {exc}") from None
    matches = []
    for file in _files(_resolve(path, roots), glob):
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary
        for number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                matches.append(f"{_shown(file)}:{number}: {line.strip()[:300]}")
                if len(matches) >= MAX_GREP_MATCHES:
                    return "\n".join(matches) + f"\n... more than {MAX_GREP_MATCHES} matches; narrow the search."
    return "\n".join(matches) or "No matches."


def list_files(roots: list[Path], path: str | None = None) -> str:
    target = _resolve(path, roots)
    if not target.is_dir():
        return f"{_shown(target)} ({target.stat().st_size} bytes)"
    entries = [f"{entry.name}/" if entry.is_dir() else entry.name
               for entry in sorted(target.iterdir()) if entry.name not in SKIP_DIRS]
    more = f"\n... {len(entries) - MAX_LIST_ENTRIES} more" if len(entries) > MAX_LIST_ENTRIES else ""
    return "\n".join(entries[:MAX_LIST_ENTRIES]) + more


def tool_summary(name: str, arguments: dict) -> str:
    """The note the chat shows for a read-only tool call."""
    if name == "read_file":
        return f"Read {arguments.get('path')}"
    if name == "grep":
        where = f" in {arguments['path']}" if arguments.get("path") else ""
        return f"Searched “{arguments.get('pattern')}”{where}"
    if name == "jupyter_jobs":
        return "Checked the background jobs"
    if name == "save_lesson":
        return f"{'Removed' if arguments.get('remove') else 'Saved'} a lesson: {arguments.get('key')}"
    if name == "report_api_bug":
        return f"Reported a chemotion_api bug: {arguments.get('title')}"
    if name == "list_mcp_servers":
        return "Listed the MCP servers"
    if name in ("add_mcp_server", "remove_mcp_server"):
        return f"{'Add' if name == 'add_mcp_server' else 'Remove'} the MCP server {arguments.get('name')}"
    if name.startswith(MCP_TOOL_PREFIX):
        server, _, tool = name.removeprefix(MCP_TOOL_PREFIX).partition("__")
        return f"MCP {server} · {tool}"
    return f"Listed {arguments.get('path') or 'the workspace'}"


# --- the auto-run guard ---------------------------------------------------------
# Auto-run executes a snippet without the user's confirmation, so it may only
# run when it looks strictly read-only. This is a best-effort static check,
# not a sandbox: anything it does not recognize counts as writing (fail
# closed, it just falls back to a confirmation card).

# Methods that change data in Chemotion or on this machine; grounded in the
# chemotionapi checkout (save, delete, update, set_*, upload_*, add_*, ...).
MUTATING_CALLS = {
    "save", "delete", "destroy", "restore", "undelete", "copy", "move", "move_to", "move_elements",
    "assign_to", "assign_elements", "remove", "remove_elements", "remove_from", "update",
    "upload", "login", "login_token", "commit", "revert", "abort",
    # files, processes and code execution
    "write", "writelines", "write_text", "write_bytes", "unlink", "truncate", "touch", "mkdir",
    "makedirs", "rmdir", "rmtree", "rename", "replace", "removedirs", "symlink", "link",
    "chmod", "chown", "system", "popen", "spawn", "execv", "execve", "kill", "terminate",
    "send_signal", "Popen", "run", "check_call", "check_output", "eval", "exec", "compile",
    "__import__",
}
MUTATING_PREFIXES = (
    "create_", "new_", "add_", "delete_", "remove_", "update_", "set_", "assign_", "upload_",
    "move_", "replace_", "write_", "save_", "insert_", "unassign_",
)
# Methods whose name looks mutating but that only build a local search query
# (chemotionapi's Search; see the "Search" section of the skill).
ALLOWED_CALLS = {"add_search_condition"}
# Modules a read-only snippet has no business importing.
BANNED_MODULES = {"os", "subprocess", "shutil", "ctypes", "importlib", "multiprocessing",
                  "pickle", "socket"}


def looks_read_only(code: str) -> tuple[bool, str]:
    """(True, "") if the snippet only reads data, else (False, why).

    Used by auto-run mode ("readonly"): code the check does not clear waits
    for the user's confirmation like in "off" mode.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"the code does not parse ({exc.msg}, line {exc.lineno})"
    except (ValueError, RecursionError):
        return False, "the code could not be parsed"

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    banned = sorted(modules & BANNED_MODULES)
    if banned:
        return False, f"it imports {', '.join(banned)}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Delete):
            return False, "it deletes something"
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            # getattr(...)() and friends: not recognizable, so not read-only.
            return False, "it calls something that is not a plain name or method"
        if name in ALLOWED_CALLS:
            continue
        if name == "open":
            mode_arg = None
            if len(node.args) > 1:
                mode_arg = node.args[1]
            else:
                mode_arg = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
            if mode_arg is None:
                continue  # open(path) defaults to reading
            if not isinstance(mode_arg, ast.Constant) or not isinstance(mode_arg.value, str):
                return False, "it opens a file with a computed mode"
            if set(mode_arg.value) & set("wax+"):
                return False, f'it opens a file for writing (mode "{mode_arg.value}")'
            continue
        if name in MUTATING_CALLS:
            return False, f"it calls {name}()"
        if any(name.startswith(prefix) for prefix in MUTATING_PREFIXES):
            return False, f"it calls {name}()"
    return True, ""


def describe_attachment(path: Path) -> str:
    if path.is_dir():
        count = sum(1 for _ in _files(path, None))
        return f"- {path}{os.sep} (folder, {count}{'+' if count >= MAX_GREP_FILES else ''} files)"
    size = path.stat().st_size
    kind = "binary file" if _is_binary(path) else "text file"
    return f"- {path} ({kind}, {size / 1024:.1f} kB)"


def usage_entry(model: str, seconds: float, usage: dict | None) -> dict:
    """One chat completion's cost in OpenAI's usage fields (prompt_tokens =
    input, completion_tokens = output, which includes reasoning tokens)."""
    usage = usage or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {"model": model, "seconds": round(seconds, 3),
            "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
            "cached_tokens": prompt_details.get("cached_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
            "cost": usage.get("cost")}


def usage_total(entries: list[dict]) -> dict:
    """The sum of usage_entry()s; a count is None if no request reported it."""
    total: dict = {"requests": len(entries), "seconds": round(sum(e["seconds"] for e in entries), 3)}
    for key in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost"):
        values = [e[key] for e in entries if isinstance(e.get(key), (int, float))]
        total[key] = (round(sum(values), 6) if key == "cost" else sum(values)) if values else None
    return total


# --- the conversation ------------------------------------------------------------
class Agent:
    """One conversation. send() runs in a worker thread and reports through
    `on_event(kind, data)` (also from that thread):
        ("text", delta)        streamed answer text
        ("thinking", None)     the model reasons before answering
        ("tool", summary)      a read-only tool ran
        ("done", None)         the answer to this message is complete
        ("error", message)
        ("stopped", None)      cancel() ended the turn
    After an error, retry() repeats the request (set_model() may switch the
    model first) without a new user message.
    `execute(purpose, code, cancelled, target)` asks the user and returns the
    text for the model; it must return when the `cancelled` event is set.
    `target` is "chemotion" (execute_python), "jupyter" (execute_jupyter) or
    "job" (start_jupyter_job).
    `exchange`: the conversation's exchange folder when JupyterLab is on;
    `jobs()` returns the text of the jupyter_jobs tool.
    `mcp`: the conversation's mcp_client.McpHub; its tool calls go to
    `execute(purpose, arguments_json, cancelled, "mcp", tool)`, and in a
    "/mcp" turn add/remove_mcp_server to targets "mcp-add"/"mcp-remove".
    """

    def __init__(self, config: Config, system_prompt: str, execute, on_event,
                 auto_run: str = "off", exchange: Path | None = None, jobs=None,
                 headless: bool = False, mcp=None) -> None:
        self.config = config
        # headless.py: no save_lesson, the approval function instead of a panel.
        self.headless = headless
        self.jupyter = exchange is not None
        self.execute = execute
        self.jobs = jobs
        self.mcp = mcp
        self.mcp_admin = False  # this turn started with "/mcp": MCP_ADMIN_TOOLS
        self.on_event = on_event
        self.auto_run = auto_run if auto_run in AUTO_RUN_MODES else "off"
        # The tool descriptions depend on the auto-run mode (execute_python).
        self.tools = build_tools(self.auto_run, self.jupyter, self.config.developer_mode, self.headless)
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # What the read-only tools may read: base_roots() and every attachment.
        self.roots: list[Path] = base_roots(config.history_access)
        # The files and folders the user attached (a part of roots); the app
        # saves them with the conversation and restore() brings them back.
        self.attachments: list[Path] = []
        if exchange is not None:
            exchange.mkdir(parents=True, exist_ok=True)
            self.roots.append(exchange.resolve())
            # The calculation skills are about execute_jupyter code.
            if SKILLS_DIR.is_dir():
                self.roots.append(SKILLS_DIR.resolve())
        self.cancelled = threading.Event()
        self.busy = False
        self.failed_run = False  # an execute_* run failed since the user's last message
        self._response = None
        self._call_counter = 0
        self._drop_reasoning = False  # set_model(): strip reasoning_content before the next request
        # One entry per chat completion: model, seconds and the server's token
        # counts (usage_entry(); None where the server reports none).
        self.usage: list[dict] = []
        self._stream_usage = True  # False after a server refused stream_options
        # Further request fields (evals/run_eval.py: bypass a gateway's response cache).
        self.extra_body: dict = {}

    def set_auto_run(self, mode: str) -> None:
        """Switch the auto-run mode mid-conversation (the header menu); the
        changed tool description goes out with the next request."""
        self.auto_run = mode if mode in AUTO_RUN_MODES else "off"
        self.tools = build_tools(self.auto_run, self.jupyter, self.config.developer_mode, self.headless)

    def set_model(self, model: str) -> None:
        """Switch the model mid-conversation (the header menu); the next
        request uses it. Reasoning only goes back to the model that wrote it
        (Deept,Seek wants it), other backends may refuse the field. The worker
        thread drops it (it may be sending the messages right now)."""
        self.config = dataclasses.replace(self.config, ai_model=model)
        self._drop_reasoning = True

    def restore(self, messages: list[dict], roots: list[str]) -> None:
        """Continue a saved conversation (history.py): its messages after the
        current system prompt, and the attachments that still exist."""
        self.messages = self.messages[:1] + [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        for item in roots:
            path = Path(item)
            if path.exists():
                self.attach(path)
        self._close_open_calls()

    def attach(self, path: Path) -> None:
        """A file or folder the user attached: readable from now on."""
        if path not in self.roots:
            self.roots.append(path)
            self.attachments.append(path)

    def cancel(self) -> None:
        self.cancelled.set()
        response = self._response
        if response is not None:
            try:
                response.close()  # ends a blocking read of the stream
            except Exception:
                pass

    def current_tools(self) -> list[dict]:
        """What the next request offers: the built-in tools, the MCP servers'
        and in a "/mcp" turn the MCP admin tools."""
        tools = list(self.tools)
        if self.mcp is not None:
            tools += self.mcp.openai_tools()
            if self.mcp_admin:
                tools += MCP_ADMIN_TOOLS
        return tools

    def send(self, text: str, attachments: list[str] | tuple[str, ...] = (), mcp_admin: bool = False) -> None:
        """`attachments`: files and folders the user attached to this message;
        `mcp_admin`: the user's message started with "/mcp" (mcp_command())."""
        self.busy = True
        self.cancelled.clear()
        self.failed_run = False
        self.mcp_admin = mcp_admin
        lines = []
        for item in attachments:
            path = Path(item).resolve()
            if path.exists():
                self.attach(path)
                lines.append(describe_attachment(path))
        if lines:
            text += ATTACHMENT_NOTE + "\n".join(lines)
        self.messages.append({"role": "user", "content": text})
        self._run()

    def retry(self) -> None:
        """Repeat the request after an error: the messages end with the user's
        message or with tool results, which still wait for an answer."""
        self.busy = True
        self.cancelled.clear()
        self._run()

    def _start_mcp(self) -> None:
        """Start the MCP servers not tried yet (npx may download for a while)."""
        if self.mcp is None or not self.mcp.waiting():
            return
        self.on_event("status", "Starting the MCP servers…")
        for problem in self.mcp.start(self.cancelled):
            self.on_event("warning", problem)

    def _run(self) -> None:
        try:
            self._start_mcp()
            for _ in range(MAX_ROUNDS):
                message = self._complete()
                if self.cancelled.is_set():
                    break
                calls = message.get("tool_calls") or []
                if not calls:
                    self.on_event("done", None)
                    return
                for call in calls:
                    if self.cancelled.is_set():
                        break
                    self._run_tool(call)
                if self.cancelled.is_set():
                    break
            else:
                self.messages.append({"role": "user", "content": STOPPED_NOTE})
                self.on_event("error", f"Stopped after {MAX_ROUNDS} tool rounds. Send a message to continue.")
                return
            self._close_open_calls()
            self.on_event("stopped", None)
        except AgentError as exc:
            self._close_open_calls()
            if self.cancelled.is_set():
                self.on_event("stopped", None)
            else:
                self.on_event("error", str(exc))
        except Exception as exc:  # a bug must not leave the panel waiting forever
            self._close_open_calls()
            self.on_event("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._response = None
            self.busy = False

    def _close_open_calls(self) -> None:
        """Every tool call needs an answer, or the next request is refused."""
        answered = {message.get("tool_call_id") for message in self.messages if message.get("role") == "tool"}
        for message in list(self.messages):
            for call in message.get("tool_calls") or []:
                if call["id"] not in answered:
                    self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                          "content": "Not run: the user stopped the assistant."})

    def _complete(self) -> dict:
        """One streamed chat completion; appends the assistant message to the history."""
        if self._drop_reasoning:
            self._drop_reasoning = False
            for message in self.messages:
                message.pop("reasoning_content", None)
        body = {
            "model": self.config.ai_model,
            "messages": self.messages,
            "tools": self.current_tools(),
            "tool_choice": "auto",
            "stream": True,
        }
        body.update(self.extra_body)
        if self._stream_usage:
            # The token counts arrive in a last chunk without choices.
            body["stream_options"] = {"include_usage": True}
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        thinking = False
        usage = None
        started = time.monotonic()
        try:
            response = _request(self.config, "/chat/completions", body)
        except AgentError as exc:
            if not self._stream_usage or "stream_options" not in str(exc):
                raise
            self._stream_usage = False  # an older server: no token counts
            body.pop("stream_options")
            response = _request(self.config, "/chat/completions", body)
        self._response = response
        try:
            if "text/event-stream" not in (response.headers.get("Content-Type") or ""):
                # A server that ignores "stream": one JSON answer.
                chunks = [json.loads(response.read().decode("utf-8"))]
                chunks[0]["choices"] = [{**choice, "delta": choice.get("message") or {}}
                                        for choice in chunks[0].get("choices") or []]
            else:
                chunks = self._events(response)
            for chunk in chunks:
                if chunk.get("error"):
                    error = chunk["error"]
                    raise AgentError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                        self.on_event("text", delta["content"])
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(thought, str) and thought:
                        reasoning.append(thought)
                        if not thinking:
                            thinking = True
                            self.on_event("thinking", None)
                    for call in delta.get("tool_calls") or []:
                        self._merge_call(calls, call)
        except (OSError, ValueError, AttributeError) as exc:
            if self.cancelled.is_set():
                raise AgentError("stopped") from None
            raise AgentError(f"The AI connection broke: {exc}") from None
        finally:
            self._response = None
            response.close()
        self.usage.append(usage_entry(self.config.ai_model, time.monotonic() - started, usage))

        message: dict = {"role": "assistant", "content": "".join(content) or None}
        tool_calls = []
        for index in sorted(calls):
            call = calls[index]
            if not call["function"]["name"]:
                continue
            if not call.get("id"):
                self._call_counter += 1
                call["id"] = f"call_{self._call_counter}"
            tool_calls.append(call)
        if tool_calls:
            message["tool_calls"] = tool_calls
        if reasoning:
            # Some reasoning models (DeepSeek) want it back with tool calls.
            message["reasoning_content"] = "".join(reasoning)
        if message["content"] or tool_calls:
            self.messages.append(message)
        return message

    def _events(self, response):
        for raw in response:
            if self.cancelled.is_set():
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except ValueError:
                continue

    @staticmethod
    def _merge_call(calls: dict[int, dict], delta: dict) -> None:
        index = delta.get("index")
        if index is None:
            # Some servers send whole calls without an index.
            known = [i for i, call in calls.items() if delta.get("id") and call.get("id") == delta["id"]]
            index = known[0] if known else (len(calls) if delta.get("id") or not calls else max(calls))
        call = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if delta.get("id"):
            call["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            call["function"]["name"] += function["name"]
        if function.get("arguments"):
            arguments = function["arguments"]
            call["function"]["arguments"] += arguments if isinstance(arguments, str) else json.dumps(arguments)

    def _run_tool(self, call: dict) -> None:
        name = call["function"]["name"]
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            result = f"Invalid JSON arguments ({exc}); call {name} again with a JSON object."
        else:
            result = self._dispatch(name, arguments)
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

    def _dispatch(self, name: str, arguments: dict) -> str:
        try:
            targets = {"execute_python": "chemotion"}
            if self.jupyter:
                targets.update(execute_jupyter="jupyter", start_jupyter_job="job")
            if name in targets:
                code = str(arguments.get("code") or "")
                if not code.strip():
                    return "No code given."
                answer = self.execute(str(arguments.get("purpose") or ""), code, self.cancelled, targets[name])
                if FAILED_TEXT in answer:
                    self.failed_run = True
                elif RAN_TEXT in answer and self.failed_run:
                    self.failed_run = False  # one reminder per fixed failure
                    if not self.headless:
                        answer += "\n\n" + LESSON_REMINDER
                return answer
            if self.mcp is not None and name.startswith(MCP_TOOL_PREFIX) and self.mcp.has(name):
                return self.execute(tool_summary(name, arguments),
                                    json.dumps(arguments, indent=2, ensure_ascii=False), self.cancelled, "mcp", name)
            if self.mcp is not None and self.mcp_admin and name in MCP_ADMIN_NAMES:
                if name == "list_mcp_servers":
                    self.on_event("tool", tool_summary(name, arguments))
                    return self.mcp.describe()
                if name == "add_mcp_server":
                    return self.execute(tool_summary(name, arguments), mcp_server_entry(arguments),
                                        self.cancelled, "mcp-add")
                return self.execute(tool_summary(name, arguments),
                                    json.dumps({"name": str(arguments.get("name") or "")}), self.cancelled,
                                    "mcp-remove")
            if name == "jupyter_jobs" and self.jupyter and self.jobs is not None:
                text = self.jobs()
                self.on_event("tool", tool_summary(name, arguments))
                return text
            if name == "save_lesson" and not self.headless:
                key = str(arguments.get("key") or "")
                if arguments.get("remove"):
                    text = lessons.remove_lesson(key)
                else:
                    text = lessons.save_lesson(key, str(arguments.get("topic") or ""),
                                               str(arguments.get("text") or ""))
                if text.startswith("Lesson "):  # saved or removed; refusals stay out of the chat
                    self.on_event("tool", tool_summary(name, arguments))
                return text
            if name == "report_api_bug" and self.config.developer_mode:
                self.on_event("tool", tool_summary(name, arguments))
                return issues.report(self.config, issues.model_label(self.config), str(arguments.get("title") or ""),
                                     str(arguments.get("description") or ""))
            if name == "read_file":
                text = read_file(str(arguments.get("path") or ""), self.roots, arguments.get("offset") or 1,
                                 arguments.get("limit") or MAX_READ_LINES)
                self.on_event("tool", tool_summary(name, arguments))
                return text
            if name == "grep":
                text = grep(str(arguments.get("pattern") or ""), self.roots, arguments.get("path"),
                            arguments.get("glob"))
                self.on_event("tool", tool_summary(name, arguments))
                return text
            if name == "list_files":
                text = list_files(self.roots, arguments.get("path"))
                self.on_event("tool", tool_summary(name, arguments))
                return text
        except AgentError as exc:
            return f"Error: {exc}"
        except (OSError, TypeError, ValueError) as exc:
            return f"Error: {exc}"
        names = ", ".join(tool["function"]["name"] for tool in self.current_tools())
        hint = (" MCP servers can only be added or removed in a turn whose message starts with /mcp; tell the "
                "user to write one." if name in MCP_ADMIN_NAMES else "")
        return f"There is no tool {name}. Use one of: {names}.{hint}"
