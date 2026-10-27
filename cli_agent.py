"""Agents that are command-line programs: Claude Code (`claude`) and Codex
(`codex`) (no GTK, standard library only).

A CliAgent has the interface of agent.Agent, so the app treats both alike.
Every user message runs the CLI once, non-interactively with JSON events on
stdout (`claude -p --output-format stream-json`, `codex exec --json`); later
messages resume the CLI's session. The CLIs' own tools (shell, file edits,
web, ...) are switched off; their only tools come from mcp_bridge.py, an MCP
server the CLI starts: the built-in agent's `execute_python`, which reaches
the app over ipc.py and waits for the user's confirmation in the result panel
(or runs by itself in an auto-run mode), and read-only `read_file`, `grep`
and `list_files`, confined to the workspace and the attachments.

The CLIs log in with their own accounts (`claude` / `codex login`); the app
neither knows nor needs those credentials.
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path

import issues
import workspace
from agent import ATTACHMENT_NOTE, AgentError, describe_attachment, tool_summary
from config import AGENT_LABELS, AUTO_RUN_MODES, CLI_MODELS, STATE_DIR, WINDOWS, WORKSPACE_DIR, Config, write_private_json


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The bridge's MCP server name: Claude calls its tools mcp__chemotion__<tool>.
SERVER = "chemotion"
READ_TOOLS = ("read_file", "grep", "list_files")
# execute_python waits for the user; the CLIs' MCP timeouts must not end it.
TOOL_TIMEOUT_S = 86400
# Codex features that give the model tools of its own (unknown names are
# ignored by older and newer versions; sandbox_mode is the second fence).
CODEX_DISABLED_FEATURES = ("shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external",
                           "computer_use", "in_app_browser", "image_generation", "view_image")
STDERR_LINES = 40
# An earlier conversation the CLI has no session for goes in as a transcript.
MAX_CONTEXT_CHARS = 30000
CONTEXT_NOTE = ("(Continued conversation. What was said before, for context; earlier tool calls and "
                "results are not included:)\n")
# Where else installers put the CLIs; a systemd user service has a short PATH.
SEARCH_DIRS = (
    "~/.local/bin", "~/.claude/local", "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin",
    "~/.nvm/versions/node/*/bin", "/usr/local/bin", "/opt/homebrew/bin",
    "~/AppData/Roaming/npm", "~/AppData/Local/Programs/claude",
)


def find_command(command: str) -> str | None:
    """The full path of a CLI program: `command` itself if it is a path,
    else found on the PATH or in the usual install locations."""
    command = command.strip()
    if not command:
        return None
    path = Path(command).expanduser()
    if path.parent != Path("."):
        return str(path) if path.is_file() else None
    found = shutil.which(command)
    if found:
        return found
    for pattern in SEARCH_DIRS:
        for folder in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True):  # newest node first
            found = shutil.which(command, path=folder)
            if found:
                return found
    return None


def command_problem(config: Config) -> str | None:
    """Why the configured CLI cannot start, or None."""
    if config.agent not in ("claude", "codex"):
        return None
    if find_command(config.agent_command) is None:
        return (f"{AGENT_LABELS[config.agent]}: “{config.agent_command}” was not found. Install it, "
                "or set its full path in the settings.")
    return None


def cli_models(agent: str) -> list[str]:
    """The models to offer for a CLI agent ("" = its default). Codex keeps
    the models of the logged-in account in $CODEX_HOME/models_cache.json."""
    models = list(CLI_MODELS.get(agent, [""]))
    if agent == "codex":
        cache = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "models_cache.json"
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            models += [str(item["slug"]) for item in data.get("models") or []
                       if isinstance(item, dict) and item.get("slug") and item.get("visibility") == "list"]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return models


def child_env(program: str) -> dict:
    """The environment for a CLI: its own folder first on the PATH (an npm
    install under nvm starts `node` from there)."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(Path(program).parent), env.get("PATH", "")])
    return env


def check_command(agent: str, command: str) -> str:
    """`<command> --version` for the settings; raises AgentError."""
    program = find_command(command)
    if program is None:
        raise AgentError(f"“{command}” was not found. Install {AGENT_LABELS[agent]}, or set its full path.")
    try:
        done = subprocess.run([program, "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, env=child_env(program), creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentError(f"Cannot run {program}: {exc}") from None
    output = (done.stdout or done.stderr).strip().splitlines()
    if done.returncode != 0:
        raise AgentError(f"{program} --version failed: {output[-1] if output else done.returncode}")
    return f"{program}: {output[0] if output else 'OK'}"


def _toml(value) -> str:
    """A string or list of strings as TOML (for codex -c): JSON strings are valid TOML."""
    return json.dumps(value)


class CliAgent:
    """One conversation with a CLI agent; see agent.Agent for the events and
    the methods. `endpoint` is the app's ipc address and token, where the
    bridge's execute_python calls go; `session_token` ties them to this
    conversation."""

    def __init__(self, config: Config, system_prompt: str, on_event, auto_run: str = "off",
                 endpoint: tuple[str, str] = ("", "")) -> None:
        self.config = config
        self.system_prompt = system_prompt
        self.on_event = on_event
        self.auto_run = auto_run if auto_run in AUTO_RUN_MODES else "off"
        self.endpoint = endpoint
        self.session_token = secrets.token_urlsafe(24)
        # Like Agent.messages (history.py, ChatPanel.replay()): plain user and assistant texts.
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.roots: list[Path] = [WORKSPACE_DIR.resolve()]
        # The CLI's own session id (claude session_id, codex thread_id); resumed by later turns.
        self.session = ""
        self.cancelled = threading.Event()
        self.busy = False
        self._process: subprocess.Popen | None = None
        self._prompt = ""  # the message of the current turn, for retry()
        self._context = ""  # restore() without a session: the earlier conversation as text

    # --- the Agent interface ----------------------------------------------------
    def set_auto_run(self, mode: str) -> None:
        """The next turn's bridge describes execute_python for `mode`; the app
        decides about every call with its current mode anyway."""
        self.auto_run = mode if mode in AUTO_RUN_MODES else "off"

    def set_model(self, model: str) -> None:
        self.config = dataclasses.replace(self.config, ai_model=model)

    def restore(self, messages: list[dict], roots: list[str], session: str = "") -> None:
        """Continue a saved conversation. `session`: the CLI session it had
        (only when it was this CLI's); without one, the CLI gets the earlier
        messages as text with the next message."""
        self.messages = self.messages[:1] + [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        for item in roots:
            path = Path(item)
            if path.exists() and path not in self.roots:
                self.roots.append(path)
        self.session = session
        if not session:
            lines = []
            for message in self.messages[1:]:
                content = message.get("content")
                if message.get("role") in ("user", "assistant") and isinstance(content, str) and content.strip():
                    lines.append(f"{message['role'].capitalize()}: {content.strip()}")
            self._context = "\n\n".join(lines)[-MAX_CONTEXT_CHARS:]

    def cancel(self) -> None:
        self.cancelled.set()
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if WINDOWS:
                # The CLI may be a .cmd that started node: end the whole tree.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGTERM)  # the CLI and the bridge it started
        except OSError:
            pass

    def send(self, text: str, attachments: list[str] | tuple[str, ...] = ()) -> None:
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
        self._prompt = text
        self._run()

    def retry(self) -> None:
        """Run the last message again (the CLI may already have part of it in its session)."""
        self.busy = True
        self.cancelled.clear()
        self._run()

    # --- one turn -------------------------------------------------------------------
    def _run(self) -> None:
        self._parts: list[str] = []  # the answer's texts, split by tool calls
        self._streamed: set[str] = set()  # claude message ids whose text came as deltas
        self._message = ""  # the claude message being streamed
        self._result: tuple[bool, str] | None = None  # (ok, error) once the CLI reported the end
        self._last_error = ""
        stderr: deque[str] = deque(maxlen=STDERR_LINES)
        try:
            program = find_command(self.config.agent_command)
            if program is None:
                raise AgentError(command_problem(self.config) or "The CLI was not found.")
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="cli-", dir=STATE_DIR) as folder:  # mode 0700
                command = self._command(program, Path(folder))
                prompt = self._prompt
                if self._context:
                    prompt = f"{CONTEXT_NOTE}{self._context}\n\n---\n\n{prompt}"
                if self.config.agent == "codex" and not self.session and self._instructions_in_prompt(program):
                    prompt = f"{self.system_prompt}\n\n---\n\n{prompt}"
                env = child_env(program)
                env["MCP_TOOL_TIMEOUT"] = str(TOOL_TIMEOUT_S * 1000)  # claude, in ms
                try:
                    self._process = subprocess.Popen(
                        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace", cwd=str(WORKSPACE_DIR), env=env,
                        start_new_session=True, creationflags=NO_WINDOW,
                    )
                except OSError as exc:
                    raise AgentError(f"Cannot start {program}: {exc}") from None
                process = self._process
                reader = threading.Thread(target=lambda: stderr.extend(process.stderr), daemon=True)
                reader.start()
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except OSError:
                    pass  # it ended at once; stderr says why
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        self._handle(event)
                process.wait()
                reader.join(timeout=2)
            answer = "\n\n".join(part.strip() for part in self._parts if part.strip())
            if answer:
                self.messages.append({"role": "assistant", "content": answer})
            if self.cancelled.is_set():
                self.on_event("stopped", None)
                return
            ok, error = self._result or (False, "")
            if not ok:
                detail = error or self._last_error or "\n".join(line.rstrip() for line in stderr).strip()[-2000:]
                raise AgentError(f"{AGENT_LABELS[self.config.agent]} ended with exit code {process.returncode}"
                                 + (f": {detail}" if detail else "."))
            self._context = ""  # the CLI has it in its session now
            self.on_event("done", None)
        except AgentError as exc:
            if self.cancelled.is_set():
                self.on_event("stopped", None)
            else:
                self.on_event("error", str(exc))
        except Exception as exc:  # a bug must not leave the panel waiting forever
            self.on_event("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._process = None
            self.busy = False

    @staticmethod
    def _instructions_in_prompt(program: str) -> bool:
        """A .cmd shim (npm on Windows) goes through cmd.exe, whose 8191
        character limit the instructions as an argument could break."""
        return WINDOWS and Path(program).suffix.lower() in (".cmd", ".bat")

    def _bridge(self, folder: Path) -> list[str]:
        """The command line of mcp_bridge.py for this turn."""
        session_file = folder / "session.json"
        write_private_json(session_file, {
            "address": self.endpoint[0], "token": self.endpoint[1], "session": self.session_token,
            "auto_run": self.auto_run, "roots": [str(root) for root in self.roots],
            "developer": self.config.developer_mode, "model": issues.model_label(self.config),
        })
        return workspace.command("mcp_bridge.py", console=True) + ["--session-file", str(session_file)]

    def _command(self, program: str, folder: Path) -> list[str]:
        bridge = self._bridge(folder)
        model = self.config.ai_model
        if self.config.agent == "claude":
            write_private_json(folder / "mcp.json", {"mcpServers": {SERVER: {
                "type": "stdio", "command": bridge[0], "args": bridge[1:]}}})
            prompt_file = folder / "system-prompt.md"
            prompt_file.write_text(self.system_prompt, encoding="utf-8")
            command = [program, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                       "--system-prompt-file", str(prompt_file),
                       "--strict-mcp-config", "--mcp-config", str(folder / "mcp.json"),
                       # No built-in tools; the bridge's tools need no prompt, anything else is denied.
                       "--tools", "", "--allowedTools", f"mcp__{SERVER}", "--permission-mode", "dontAsk"]
            if model:
                command += ["--model", model]
            if self.session:
                command += ["--resume", self.session]
            return command
        # codex
        overrides = {
            "sandbox_mode": _toml("read-only"),
            "approval_policy": _toml("never"),
            "web_search": _toml("disabled"),
            f"mcp_servers.{SERVER}.command": _toml(bridge[0]),
            f"mcp_servers.{SERVER}.args": _toml(bridge[1:]),
            f"mcp_servers.{SERVER}.default_tools_approval_mode": _toml("approve"),
            f"mcp_servers.{SERVER}.tool_timeout_sec": str(TOOL_TIMEOUT_S),
            f"mcp_servers.{SERVER}.startup_timeout_sec": "30",
        }
        for feature in CODEX_DISABLED_FEATURES:
            overrides[f"features.{feature}"] = "false"
        if not self._instructions_in_prompt(program):
            overrides["developer_instructions"] = _toml(self.system_prompt)
        command = [program, "exec"] + (["resume"] if self.session else []) + ["--json", "--skip-git-repo-check"]
        for key, value in overrides.items():
            command += ["-c", f"{key}={value}"]
        if model:
            command += ["-m", model]
        return command + ([self.session] if self.session else []) + ["-"]

    # --- events ---------------------------------------------------------------------
    def _text(self, text: str) -> None:
        if not self._parts:
            self._parts.append("")
        self._parts[-1] += text
        self.on_event("text", text)

    def _tool(self, name: str, arguments) -> None:
        """A tool call: the answer text after it is a new part (a new chat bubble)."""
        self._parts.append("")
        name = name.removeprefix(f"mcp__{SERVER}__")
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else {}
            except ValueError:
                arguments = {}
        if name in READ_TOOLS or name == "report_api_bug":
            self.on_event("tool", tool_summary(name, arguments if isinstance(arguments, dict) else {}))
        elif name != "execute_python":  # that one shows its card
            self.on_event("tool", f"Used {name}")

    def _handle(self, event: dict) -> None:
        if self.config.agent == "claude":
            self._handle_claude(event)
        else:
            self._handle_codex(event)

    def _handle_claude(self, event: dict) -> None:
        kind = event.get("type")
        if event.get("session_id"):
            self.session = str(event["session_id"])
        if kind == "stream_event":
            inner = event.get("event") or {}
            if inner.get("type") == "message_start":
                self._message = str((inner.get("message") or {}).get("id") or "")
            elif inner.get("type") == "content_block_start":
                if (inner.get("content_block") or {}).get("type") == "thinking":
                    self.on_event("thinking", None)
            elif inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self._streamed.add(self._message)
                    self._text(delta["text"])
        elif kind == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text") and message.get("id") not in self._streamed:
                    self._text(block["text"])  # no partial messages from this CLI version
                elif block.get("type") == "tool_use":
                    self._tool(str(block.get("name") or ""), block.get("input"))
        elif kind == "result":
            if not event.get("is_error") and event.get("subtype") == "success":
                self._result = (True, "")
                return
            errors = event.get("errors")
            detail = str(event.get("result") or "") or (", ".join(map(str, errors)) if isinstance(errors, list) else "")
            self._result = (False, detail or str(event.get("subtype") or ""))

    def _handle_codex(self, event: dict) -> None:
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started" and event.get("thread_id"):
            self.session = str(event["thread_id"])
        elif kind == "item.started" and item.get("type") == "reasoning":
            self.on_event("thinking", None)
        elif kind == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                if self._parts and self._parts[-1].strip():
                    self._text("\n\n")
                self._text(str(item["text"]))
            elif item.get("type") == "mcp_tool_call":
                self._tool(str(item.get("tool") or ""), item.get("arguments"))
            elif item.get("type") not in ("reasoning", "error", "todo_list"):  # errors there are warnings
                self._tool(str(item.get("type") or "a tool"), {})
        elif kind == "error" and event.get("message"):
            self._last_error = str(event["message"])  # also "Reconnecting...": only shown if the turn fails
        elif kind == "turn.failed":
            message = str((event.get("error") or {}).get("message") or self._last_error)
            try:  # often the API's JSON error body
                message = str(json.loads(message)["error"]["message"])
            except (ValueError, KeyError, TypeError):
                pass
            self._result = (False, message)
        elif kind == "turn.completed":
            self._result = (True, "")
