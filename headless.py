"""Synchronous, GTK-free interface to the built-in assistant."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from .agent import Agent, AgentError, FAILED_TEXT, RAN_TEXT, clip
from .config import WINDOWS, Config, load_config
from . import workspace


# Started by path, not with -m: -m puts the caller's cwd first on sys.path,
# where a chemotion_api.py or json.py would be imported instead (with the
# password on stdin).
RUNNER = Path(__file__).resolve().with_name("chemotion_runner.py")
# How much of the runner's stderr a crash report keeps.
MAX_CRASH_CHARS = 3000


def _kill_tree(process: subprocess.Popen) -> None:
    """Kills the runner and whatever the snippet started."""
    if process.poll() is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(process.pid, signal.SIGKILL)  # start_new_session: its own group
    except OSError:
        pass
    process.kill()
    process.wait()


class ChemAssistent:
    """A headless assistant. An empty config uses the desktop settings.

    ``approve`` receives (purpose, code) and must return True to run code.
    Generated code is rejected by default; approval is not a sandbox.
    """

    def __init__(self, config: Config | str = "", *,
                 approve: Callable[[str, str], bool] | None = None) -> None:
        if config == "":
            config = load_config()
        if not isinstance(config, Config):
            raise TypeError('config must be a Config or "" to use saved settings')
        if config.agent not in ("api", "ollama"):
            raise ValueError("The headless interface supports OpenAI-compatible API and Ollama agents only")
        # Reporting public issues is not appropriate without an interactive
        # approval, and a script must not read the desktop's saved conversations.
        self.config = dataclasses.replace(config, developer_mode=False, history_access=False)
        self.approve = approve
        self.conversations = Conversations(self)


class Conversations:
    def __init__(self, assistant: ChemAssistent) -> None:
        self._assistant = assistant

    def new_conversation(self) -> Conversation:
        return Conversation(self._assistant)


class Conversation:
    """An independent in-memory conversation; prompt() returns the final answer."""

    def __init__(self, assistant: ChemAssistent) -> None:
        self._assistant = assistant
        self._lock = threading.Lock()
        self._error = ""
        # No save_lesson: a prompt (maybe from an untrusted document) must not
        # write into the lessons every desktop conversation reads.
        prompt = workspace.system_prompt(save_lessons=False) + (
            "\nThis is a headless Python conversation, not a graphical window. "
            "The caller receives only your final text; include the requested results and element URLs "
            "in that text. Code execution requires explicit caller approval and may be rejected.\n"
        )
        self._agent = Agent(assistant.config, prompt, self._execute, self._event, save_lessons=False)

    def _event(self, kind: str, data) -> None:
        if kind == "error":
            self._error = str(data)
        elif kind == "stopped":
            self._error = "The conversation was stopped."

    def _execute(self, purpose: str, code: str, cancelled: threading.Event, target: str) -> str:
        approve = self._assistant.approve
        if cancelled.is_set() or approve is None or approve(purpose, code) is not True:
            return "Not run: code execution was not approved by the caller."
        config = self._assistant.config
        request = {"url": config.url, "user": config.user, "password": config.password,
                   "verify_tls": config.verify_tls, "code": code}
        try:
            process = subprocess.Popen([sys.executable, str(RUNNER)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", start_new_session=not WINDOWS,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
        except OSError as exc:
            return f"{FAILED_TEXT}\nCould not start the runner: {exc}"
        try:
            stdout, stderr = process.communicate(json.dumps(request), timeout=config.execution_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            return f"{FAILED_TEXT}\nStopped after {config.execution_timeout} s (execution_timeout)."
        except BaseException:  # Ctrl+C: no runner left behind
            _kill_tree(process)
            raise
        try:
            outcome = json.loads(stdout)
            if not isinstance(outcome, dict):
                raise ValueError("not an object")
        except ValueError:
            detail = (stderr or stdout or "").strip()[-MAX_CRASH_CHARS:]
            return f"{FAILED_TEXT}\nThe runner ended unexpectedly (exit code {process.returncode}).\n{detail}"
        parts = [RAN_TEXT if outcome.get("ok") else f"{FAILED_TEXT}\n{outcome.get('error', '')}"]
        if outcome.get("stdout"):
            parts.append("Printed output:\n" + str(outcome["stdout"]))
        if outcome.get("ok"):
            parts.append("result = " + json.dumps(outcome.get("result"), ensure_ascii=False))
        return clip("\n\n".join(parts))

    def prompt(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("prompt text must be a string")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("This conversation is already processing a prompt")
        try:
            self._error = ""
            start = len(self._agent.messages)
            try:
                self._agent.send(text)
            except BaseException:
                # Agent catches only Exception: after Ctrl+C every tool call
                # still needs an answer, or the next request is refused.
                self._agent.cancel()
                self._agent._close_open_calls()
                self._agent.busy = False
                raise
            if self._error:
                raise AgentError(self._error)
            for message in reversed(self._agent.messages[start:]):
                if message.get("role") == "assistant" and not message.get("tool_calls"):
                    return message.get("content") or ""
            return ""
        finally:
            self._lock.release()
