"""Synchronous, GTK-free interface to the built-in assistant."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from .agent import Agent, AgentError, FAILED_TEXT, RAN_TEXT, clip
from .config import (AI_TOKEN_ENV, WINDOWS, Config, agent_of, ai_token, jupyter_problem, load_config,
                     read_raw_config)
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


def _free_name(folder: Path, pattern: str) -> Path:
    """The first folder/pattern.format(n), n = 1, 2, ..., that does not exist."""
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (folder / pattern.format(number)).exists():
        number += 1
    return folder / pattern.format(number)


def saved_config() -> Config:
    """The desktop app's settings. Without a readable keyring (a venv without
    PyGObject, a locked keyring) CHEMOTION_PASSWORD and KI_TOOLBOX_API_KEY
    may stand in for the secrets kept there."""
    raw = read_raw_config()
    if raw.keyring_problem:
        missing = []
        if not (os.environ.get("CHEMOTION_PASSWORD") or raw.get("password")):
            missing.append("the Chemotion password (or set CHEMOTION_PASSWORD)")
        if agent_of(raw) == "api" and not ai_token(raw):
            missing.append(f"the AI token (or set {AI_TOKEN_ENV})")
        if missing:
            hint = ""
            if "No module named 'gi'" in raw.keyring_problem:
                hint = (" Reading it needs PyGObject: on Linux create the virtual environment with "
                        "--system-site-packages (packages python3-gi and gir1.2-secret-1).")
            raise ValueError(
                f"The saved settings keep {' and '.join(missing)} in the system keyring, which cannot "
                f"be read here: {raw.keyring_problem}.{hint} Alternatively pass a chemassistent.Config.")
    return load_config(keyring_required=False)


class ChemAssistent:
    """A headless assistant. An empty config uses the desktop settings.

    ``approve`` receives (purpose, code) and must return True to run code.
    Generated code is rejected by default; approval is not a sandbox.
    ``report_bugs``: the agent reports chemotion_api bugs it finds with
    report_api_bug (developer mode: a public GitHub issue with the config's
    github_token, else a draft in issues.ISSUES_DIR).
    ``jupyter``: the agent also gets execute_jupyter in the config's
    JupyterLab (needs websocket-client, the extra ``jupyter``); each
    conversation has its own notebook and kernel and an exchange folder
    below ``exchange_dir`` (default: a new temporary folder, kept), see
    ``Conversation.exchange``. There are no background jobs.
    """

    def __init__(self, config: Config | str = "", *,
                 approve: Callable[[str, str], bool] | None = None, report_bugs: bool = False,
                 jupyter: bool = False, exchange_dir: str | os.PathLike | None = None) -> None:
        if config == "":
            config = saved_config()
        if not isinstance(config, Config):
            raise TypeError('config must be a Config or "" to use saved settings')
        if config.agent not in ("api", "ollama"):
            raise ValueError("The headless interface supports OpenAI-compatible API and Ollama agents only")
        # Public issues only when the caller asks (report_bugs: the evals);
        # a script must not read the desktop's saved conversations.
        self.config = dataclasses.replace(config, developer_mode=report_bugs, history_access=False)
        self.approve = approve
        if jupyter:
            problem = jupyter_problem(dataclasses.asdict(config)) if config.jupyter else "jupyter_url is empty"
            if problem:
                raise ValueError(f"jupyter=True needs a JupyterLab in the config: {problem}")
            exchange_dir = Path(exchange_dir) if exchange_dir else Path(tempfile.mkdtemp(prefix="chemassistent-"))
        self.jupyter = jupyter
        self.exchange_dir = Path(exchange_dir).expanduser().resolve() if jupyter else None
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
        # headless: no save_lesson (a prompt, maybe from an untrusted document,
        # must not write into the lessons every desktop conversation reads),
        # and a prompt that knows there is no window or result panel.
        # With JupyterLab: the exchange folder both tools share, and the
        # notebook (kernel) of this conversation.
        self.id = uuid.uuid4().hex
        self.exchange: Path | None = None
        self._bridge = None
        if assistant.jupyter:
            if __package__:
                from .jupyter import JupyterBridge
            else:
                from jupyter import JupyterBridge
            self.exchange = assistant.exchange_dir / self.id
            self._bridge = JupyterBridge(assistant.config, self.id, self.exchange)
        self._agent = Agent(assistant.config,
                            workspace.system_prompt(self.exchange, developer=assistant.config.developer_mode,
                                                    headless=True),
                            self._execute, self._event, exchange=self.exchange, headless=True)

    @property
    def notebook_url(self) -> str:
        """The conversation's notebook in JupyterLab ("" without)."""
        return self._bridge.notebook_url if self._bridge is not None else ""

    def close(self) -> None:
        """Shuts the conversation's JupyterLab kernel down (the notebook and
        the exchange folder stay). Without JupyterLab nothing to do."""
        if self._bridge is not None:
            self._bridge.client.close_session(self._bridge.notebook)

    def _event(self, kind: str, data) -> None:
        if kind == "error":
            self._error = str(data)
        elif kind == "stopped":
            self._error = "The conversation was stopped."

    def _execute(self, purpose: str, code: str, cancelled: threading.Event, target: str) -> str:
        approve = self._assistant.approve
        if cancelled.is_set() or approve is None or approve(purpose, code) is not True:
            return "Not run: code execution was not approved by the caller."
        if target == "jupyter" and self._bridge is not None:
            return self._execute_jupyter(purpose, code, cancelled)
        if target != "chemotion":
            return f"{FAILED_TEXT}\nThe headless interface cannot run {target!r} code."
        config = self._assistant.config
        request = {"url": config.url, "user": config.user, "password": config.password,
                   "verify_tls": config.verify_tls, "code": code,
                   "exchange": str(self.exchange) if self.exchange is not None else None}
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
            if self.exchange is not None and outcome.get("result") is not None:
                # As in the app: the result is a file for execute_jupyter.
                path = _free_name(self.exchange, "chemotion_result_{}.json")
                try:
                    path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
                    parts.append(f"Saved in `exchange`: {path.name}")
                except OSError as exc:
                    parts.append(f"The result could not be saved in `exchange`: {exc}")
        return clip("\n\n".join(parts))

    def _execute_jupyter(self, purpose: str, code: str, cancelled: threading.Event) -> str:
        """execute_jupyter: runs in the conversation's kernel (JupyterBridge);
        figures are saved in the exchange folder as figure_<n>.png."""
        import base64

        if __package__:
            from .jupyter import JupyterError
        else:
            from jupyter import JupyterError
        try:
            outcome = self._bridge.run(code, purpose, self._assistant.config.execution_timeout, cancelled)
        except JupyterError as exc:
            return f"{FAILED_TEXT}\n{exc}"
        figures = []
        for image in outcome.get("images") or []:
            path = _free_name(self.exchange, "figure_{}.png")
            try:
                path.write_bytes(base64.b64decode(image))
                figures.append(path)
            except (OSError, ValueError):
                pass
        if figures:
            self._bridge.mark_local(figures)
        parts = []
        if outcome.get("new_kernel"):
            parts.append("The JupyterLab kernel is new (restarted): variables of earlier runs are gone.")
        parts.append(RAN_TEXT if outcome.get("ok") else f"{FAILED_TEXT}\n{outcome.get('error', '')}")
        if str(outcome.get("stdout") or "").strip():
            parts.append(f"Printed output:\n{outcome['stdout']}")
        if outcome.get("ok"):
            result = outcome.get("result")
            parts.append("Value of the last expression: " + (str(result) if result is not None else "none"))
        exchanged = [("Figures shown, saved in `exchange`", [path.name for path in figures]),
                     ("Sent from `exchange` to JupyterLab", outcome.get("uploaded")),
                     ("New or changed in `exchange` from JupyterLab", outcome.get("downloaded"))]
        parts.extend(f"{title}: {', '.join(names)}" for title, names in exchanged if names)
        parts.extend(outcome.get("notes") or [])
        return clip("\n\n".join(parts))

    def prompt(self, text: str, attachments: list | tuple = ()) -> str:
        """Send `text` and return the final answer. `attachments`: files or
        folders (paths) the agent may read with this and later prompts
        (read_file/grep/list_files, a PDF's text, view_page)."""
        if not isinstance(text, str):
            raise TypeError("prompt text must be a string")
        if isinstance(attachments, (str, os.PathLike)):
            attachments = [attachments]
        paths = []
        for item in attachments:
            if not isinstance(item, (str, os.PathLike)):
                raise TypeError("attachments must be paths")
            path = Path(item).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"attachment not found: {item}")
            paths.append(str(path))
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("This conversation is already processing a prompt")
        try:
            self._error = ""
            start = len(self._agent.messages)
            try:
                self._agent.send(text, paths)
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
