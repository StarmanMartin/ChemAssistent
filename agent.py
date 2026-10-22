"""The ChemAssistent agent: a tool-calling loop over an OpenAI-compatible
chat completions API (no GTK, standard library only).

Only standard endpoints are used: GET <ai_url>/models and POST
<ai_url>/chat/completions with `stream`, `tools` and `tool_choice`, so the
KIT KI-Toolbox, OpenAI, Ollama, vLLM, LiteLLM, ... all work.

The model gets exactly four tools: `execute_python` (runs only after the
user confirmed it in the result panel; the app provides it as a callback)
and read-only `read_file`, `grep` and `list_files`, which cannot leave the
workspace (the API reference and the chemotionapi checkout) and the files
and folders the user attached.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

from config import WORKSPACE_DIR, Config


REQUEST_TIMEOUT_S = 300
# Tool rounds per user message before the agent stops and asks the user.
MAX_ROUNDS = 40
MAX_READ_LINES = 400
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

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Run Python code against the user's Chemotion ELN with the chemotion_api package. "
                "A logged-in `instance` (chemotion_api.Instance) is predefined; never log in. "
                "The user sees the code in the result panel and must confirm it before it runs; "
                "they may edit it first or reject it with a reason. Assign what the user should "
                "see to `result`. The answer contains the printed output, `result` as JSON, the "
                "error traceback, or the user's reason for rejecting the code."
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
    },
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
def _allowed(target: Path, roots: list[Path]) -> bool:
    return any(target == root or root in target.parents for root in roots)


def _resolve(path: str | None, roots: list[Path]) -> Path:
    """An absolute path inside one of `roots`, or a path relative to the workspace."""
    workspace = WORKSPACE_DIR.resolve()
    text = (path or ".").strip()
    absolute = Path(text).expanduser().is_absolute()
    if absolute and _allowed(Path(text).expanduser().resolve(), roots):
        target = Path(text).expanduser().resolve()
    else:
        # Relative paths, and "/CHEMOTION_API.md"-style ones, are workspace paths.
        target = (workspace / text.lstrip("/\\")).resolve()
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
    return f"Listed {arguments.get('path') or 'the workspace'}"


def describe_attachment(path: Path) -> str:
    if path.is_dir():
        count = sum(1 for _ in _files(path, None))
        return f"- {path}{os.sep} (folder, {count}{'+' if count >= MAX_GREP_FILES else ''} files)"
    size = path.stat().st_size
    kind = "binary file" if _is_binary(path) else "text file"
    return f"- {path} ({kind}, {size / 1024:.1f} kB)"


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
    `execute(purpose, code, cancelled)` asks the user and returns the text
    for the model; it must return when the `cancelled` event is set.
    """

    def __init__(self, config: Config, system_prompt: str, execute, on_event) -> None:
        self.config = config
        self.execute = execute
        self.on_event = on_event
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        # What the read-only tools may read: the workspace and every attachment.
        self.roots: list[Path] = [WORKSPACE_DIR.resolve()]
        self.cancelled = threading.Event()
        self.busy = False
        self._response = None
        self._call_counter = 0

    def restore(self, messages: list[dict], roots: list[str]) -> None:
        """Continue a saved conversation (history.py): its messages after the
        current system prompt, and the attachments that still exist."""
        self.messages = self.messages[:1] + [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        for item in roots:
            path = Path(item)
            if path.exists() and path not in self.roots:
                self.roots.append(path)
        self._close_open_calls()

    def cancel(self) -> None:
        self.cancelled.set()
        response = self._response
        if response is not None:
            try:
                response.close()  # ends a blocking read of the stream
            except Exception:
                pass

    def send(self, text: str, attachments: list[str] | tuple[str, ...] = ()) -> None:
        """`attachments`: files and folders the user attached to this message."""
        self.busy = True
        self.cancelled.clear()
        lines = []
        for item in attachments:
            path = Path(item).resolve()
            if path.exists():
                if path not in self.roots:
                    self.roots.append(path)
                lines.append(describe_attachment(path))
        if lines:
            text += ATTACHMENT_NOTE + "\n".join(lines)
        self.messages.append({"role": "user", "content": text})
        try:
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
        body = {
            "model": self.config.ai_model,
            "messages": self.messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "stream": True,
        }
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        thinking = False
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
            if name == "execute_python":
                code = str(arguments.get("code") or "")
                if not code.strip():
                    return "No code given."
                return self.execute(str(arguments.get("purpose") or ""), code, self.cancelled)
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
        return f"There is no tool {name}. Use execute_python, read_file, grep or list_files."
