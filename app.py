"""ChemAssistent: a Chemotion icon in the top bar (the tray on Windows) that
opens a chat with an AI agent (agent.py) about the Chemotion ELN. Code the
agent wants to run is shown on the right and runs only after the user
confirmed it — unless an auto-run mode is on (config key `auto_run`, and the
menu in the conversation header): then read-only or all code starts by
itself, and the card shows what ran. The header also switches the model of
the conversation, e.g. when the configured one fails; an error note offers
Retry, which repeats the request without a new message. The settings choose
the agent: the built-in one (agent.py, an OpenAI-compatible API or Ollama) or
a CLI agent (cli_agent.py: Claude Code, Codex), whose execute_python calls
arrive from mcp_bridge.py over ipc.py and become the same cards. With
JupyterLab configured, the built-in agent's execute_jupyter code runs there on
the same cards (jupyter.py), and both sides share the conversation's exchange
folder. Long calculations run there as background jobs (jobs.py): their
reports come back into the conversation as a new turn, a notification and a
menu entry tell the user about it when the conversation is not open."""
from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

# Mutter ignores client window positioning on Wayland; the prompt must be
# centered and then grow to the work area, so run through XWayland (as
# vikunja-popups does). Must be set before Gtk is imported.
# Not under WSLg: once an X11 client sets a blank cursor, which GTK does while
# the user types, its Weston shows no mouse pointer over any X11 window until
# WSL restarts. GTK's Wayland backend hides it properly; Weston places the
# window itself (_grow() then just maximizes).
WSLG = bool(os.environ.get("WSL_DISTRO_NAME") and os.environ.get("WAYLAND_DISPLAY"))
if sys.platform.startswith("linux") and os.environ.get("DISPLAY") and not WSLG:
    os.environ["GDK_BACKEND"] = "x11"

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # type: ignore  # noqa: E402

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    GdkX11 = None

try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # type: ignore  # noqa: E402

    signal_add = GLibUnix.signal_add
except (ValueError, ImportError, AttributeError):  # GLib < 2.80, or Windows (None)
    signal_add = getattr(GLib, "unix_signal_add", None)

try:
    gi.require_version("GtkSource", "4")
    from gi.repository import GtkSource  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    GtkSource = None

try:  # desktop notifications (finished background jobs); not on Windows
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    Notify = None

import app_control  # noqa: E402
import history  # noqa: E402
import hotkey  # noqa: E402
import ipc  # noqa: E402
import workspace  # noqa: E402
from agent import (  # noqa: E402
    ATTACHMENT_NOTE, STOPPED_NOTE, Agent, AgentError, list_models, looks_read_only, tool_summary,
)
from cli_agent import CliAgent, cli_models, command_problem  # noqa: E402
from jobs import ACTIVE, FINISHED, REPORT_HEADER, USER_TEXT, Jobs, duration  # noqa: E402
from jupyter import JupyterBridge, JupyterError  # noqa: E402
from config import (  # noqa: E402
    AGENT_SHORT, AUTO_RUN_LABELS, AUTO_RUN_MODES, CLI_AGENTS, CONFIG_FILE, STATE_DIR, WINDOWS,
    WORKSPACE_DIR, Config, ai_problem, jupyter_problem, load_config, model_name, read_raw_config,
)


LOG = logging.getLogger("chem-assistent")

APP_ID = "chem-assistent"
HERE = Path(__file__).resolve().parent
ICON_FILE = HERE / "chem-assistent.svg"
PANEL_ICON = "chem-assistent-panel"  # chem-assistent-panel.svg next to this file
# The light variant of the mark, for the app's own dark prompt and header.
DARK_UI_ICON_FILE = HERE / f"{PANEL_ICON}.svg"

# Keeps the runner (a console program) from opening a console window on
# Windows. 0 elsewhere (creationflags must be 0 there).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GROW_STEPS = 14
GROW_STEP_MS = 16
# The chat's share of the window width; the results get the rest.
CHAT_SHARE = 0.45
# Header icon for each auto-run mode (config.AUTO_RUN_MODES).
RUN_MODE_ICONS = {"off": "dialog-question-symbolic", "readonly": "emblem-readonly-symbolic",
                  "always": "system-run-symbolic"}
# How much of a failed GET /models the model menu shows.
MAX_MODEL_ERROR_CHARS = 120
# Two presses of the global hotkey within this time open the prompt.
HOTKEY_DOUBLE_S = 0.6
# What the agent gets back from one snippet; the panel shows everything.
MAX_ANSWER_CHARS = 20000
MAX_TABLE_COLUMNS = 12
# Figures from JupyterLab are scaled down to this width in the result tab.
MAX_IMAGE_WIDTH = 720
MAX_CELL_CHARS = 120
# Result tabs kept in the panel; older ones are dropped.
MAX_RESULT_TABS = 10
# An ipc request; a CLI agent's execute_python request carries its code.
MAX_REQUEST_BYTES = 2_000_000
# Old conversations the prompt offers to continue.
RECENT_CONVERSATIONS = 6
# Between job reports in one message (jobs.REPORT_HEADER).
REPORT_SEPARATOR = "\n\n---\n\n"
# How often the header's job menu updates the run times.
JOB_MENU_REFRESH_S = 60


CSS = b"""
/* The window is transparent around the rounded prompt box. */
.popup-window {
    background-color: transparent;
}
.prompt-box {
    background: rgba(35, 35, 35, 0.98);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 12px;
    padding: 12px;
}
.prompt-box entry {
    font-size: 15px;
    min-height: 36px;
}
.prompt-heading, .assistant-title {
    color: #ffffff;
    font-weight: bold;
    font-size: 15px;
}
.prompt-hint, .dim {
    color: #a8adb3;
    font-size: 11px;
}
.prompt-error, .request-error {
    color: #ffb4b4;
}
.assistant-main {
    background: #1e1f22;
}
.assistant-header {
    background: #26282c;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding: 4px 8px;
}
.results-heading {
    color: #ffffff;
    font-weight: bold;
    font-size: 14px;
}
.request-card {
    background: #2a2c30;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 10px;
    padding: 10px;
}
.request-card.waiting {
    border-color: #d6da1e;
}
.request-card.failed {
    border-color: #c0505a;
}
.request-card.done {
    border-color: #2a9fd8;
}
.request-target {
    color: #f0b35a;
    font-size: 11px;
    font-weight: bold;
}
.request-purpose {
    color: #ffffff;
    font-weight: bold;
}
.request-code, .request-output {
    font-family: monospace;
    font-size: 11px;
}
.request-status {
    color: #d0d4d8;
    font-size: 11px;
}
.chat-user {
    background: #2f4f63;
    color: #ffffff;
    border-radius: 10px;
    padding: 8px 12px;
}
.chat-assistant {
    color: #e6e6e6;
}
.attachment-chip {
    background: #2a2c30;
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 12px;
    padding: 0 0 0 8px;
}
.recent-list, .recent-list row {
    background: transparent;
}
.recent-list row {
    border-radius: 6px;
    padding: 2px 6px;
}
.recent-list row:hover, .recent-list row:selected {
    background: rgba(255, 255, 255, 0.08);
}
.recent-title {
    color: #e6e6e6;
}
.chat-input text {
    background: #2a2c30;
    color: #ffffff;
}
"""


# --- helpers ---------------------------------------------------------------
def work_area() -> Gdk.Rectangle:
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    return monitor.get_workarea()  # excludes the GNOME top bar


def server_time(window: Gtk.Window) -> int:
    """A timestamp the window manager accepts for focusing `window`.

    Menu activations from the top bar arrive over D-Bus without a usable
    event time, so ask the X server (XWayland) for its current time.
    """
    gdk_window = window.get_window()
    if GdkX11 is not None and gdk_window is not None and isinstance(gdk_window, GdkX11.X11Window):
        return GdkX11.x11_get_server_time(gdk_window)
    return Gtk.get_current_event_time()


def clip(text: str, limit: int = MAX_ANSWER_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters not shown)"


def cell_text(value) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def label(text: str = "", css: str | None = None, wrap: bool = True, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if css:
        widget.get_style_context().add_class(css)
    return widget


def icon_button(icon: str, tooltip: str, callback) -> Gtk.Button:
    button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_tooltip_text(tooltip)
    button.connect("clicked", lambda *_: callback())
    return button


def open_settings() -> None:
    subprocess.Popen(workspace.command("settings.py"), start_new_session=True, creationflags=NO_WINDOW)


# --- requests from the agent -------------------------------------------------
class PendingRequest:
    """One execute_python/execute_jupyter call, waiting in the agent's thread for the user."""

    def __init__(self, purpose: str, code: str, target: str = "chemotion") -> None:
        self.purpose = purpose
        self.code = code
        self.target = target  # "chemotion", "jupyter" or "job" (start_jupyter_job)
        # Set by AssistantWindow._show_request(): code -> Execution/JupyterExecution/JobExecution.
        self.new_execution = Execution
        self.reply: dict | None = None
        self.done = threading.Event()
        # Set by the card; called on the main loop when the agent stopped waiting.
        self.on_abandoned = None
        # Auto-run, decided by AssistantWindow._show_request() on the main loop:
        # "readonly"/"always" = run without confirmation; None = wait for Run/Reject.
        # auto_reason says why auto-run was skipped (shown on the card).
        self.auto: str | None = None
        self.auto_reason = ""

    def answer(self, ok: bool, text: str) -> None:
        if not self.done.is_set():
            self.reply = {"ok": ok, "text": text}
            self.done.set()

    def abandoned(self) -> bool:
        self.done.set()
        if self.on_abandoned is not None:
            self.on_abandoned()
        return False


class RequestServer:
    """The app's end of ipc.py: a second start of the app asks this one to
    show the prompt, hotkey.py reports a press, mcp_bridge.py brings a CLI
    agent's execute_python call and waits for the answer."""

    def __init__(self, app: ChemAssistentApp) -> None:
        self.app = app
        self.listener = ipc.Listener()

    @staticmethod
    def signal_running_instance() -> bool:
        """True if another instance runs; it was asked to show the prompt."""
        address, token = ipc.running_endpoint()
        if not address:
            return False
        try:
            with ipc.connect(address, timeout=2) as conn:
                conn.sendall(json.dumps({"type": "show", "token": token}).encode("utf-8") + b"\n")
        except OSError:
            return False
        return True

    def start(self) -> None:
        self.sock = self.listener.listen()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        self.listener.close()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return  # closed on quit
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        # Runs in a thread: GTK only through GLib.idle_add.
        with conn:
            conn.settimeout(5)
            data = b""
            try:
                while len(data) < MAX_REQUEST_BYTES and not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode("utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(request, dict) or not ipc.token_ok(request, self.listener.token):
                return
            if request.get("type") == "show":
                GLib.idle_add(self.app.open_prompt)
            elif request.get("type") == "hotkey":  # hotkey.py, run by the GNOME shortcut
                GLib.idle_add(self.app.hotkey_pressed)
            elif request.get("type") == "execute":  # mcp_bridge.py
                conn.settimeout(None)  # the user may take their time
                text = self.app.execute_for_bridge(str(request.get("session") or ""),
                                                   str(request.get("purpose") or ""), str(request.get("code") or ""))
                try:
                    conn.sendall(json.dumps({"text": text}).encode("utf-8") + b"\n")
                except OSError:
                    pass  # the CLI was stopped meanwhile


# --- running confirmed code ---------------------------------------------------
class Execution:
    """Runs one snippet in chemotion_runner.py; cancel() kills it.
    `exchange`: the conversation's exchange folder (JupyterLab on), predefined
    in the snippet; a `result` is saved there too."""

    def __init__(self, code: str, exchange: Path | None = None) -> None:
        self.code = code
        self.exchange = exchange
        self.process: subprocess.Popen | None = None
        self.cancelled = False

    def run(self) -> dict:
        # Runs in a thread: no GTK calls here.
        try:
            config = load_config()
        except ValueError as exc:
            return {"ok": False, "stdout": "", "result": None, "error": str(exc)}
        request = {
            "url": config.url,
            "user": config.user,
            "password": config.password,
            "verify_tls": config.verify_tls,
            "code": self.code,
            "exchange": str(self.exchange) if self.exchange else None,
        }
        try:
            self.process = subprocess.Popen(
                workspace.command("chemotion_runner.py", console=True),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=str(WORKSPACE_DIR if WORKSPACE_DIR.is_dir() else HERE),
                start_new_session=True,
                creationflags=NO_WINDOW,
            )
            stdout, stderr = self.process.communicate(json.dumps(request), timeout=config.execution_timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            return {"ok": False, "stdout": "", "result": None,
                    "error": f"Stopped after {config.execution_timeout} s (execution_timeout)."}
        except OSError as exc:
            return {"ok": False, "stdout": "", "result": None, "error": f"Could not start the runner: {exc}"}
        if self.cancelled:
            return {"ok": False, "stdout": "", "result": None, "error": "Cancelled by the user."}
        try:
            outcome = json.loads(stdout)
        except ValueError:
            detail = (stderr or stdout or "").strip()[-3000:]
            return {"ok": False, "stdout": "", "result": None,
                    "error": f"The runner ended unexpectedly (exit code {self.process.returncode}).\n{detail}"}
        if self.exchange is not None and outcome.get("ok") and outcome.get("result") is not None:
            path = free_name(self.exchange, "chemotion_result_{}.json")
            try:
                path.write_text(json.dumps(outcome["result"], ensure_ascii=False, indent=1), encoding="utf-8")
                outcome["saved"] = [path.name]
                outcome["exchange"] = str(self.exchange)
            except OSError as exc:
                LOG.warning("Could not save the result in %s: %s", self.exchange, exc)
        return outcome

    def cancel(self) -> None:
        self.cancelled = True
        if self.process is not None and self.process.poll() is None:
            self.process.kill()


def free_name(folder: Path, pattern: str) -> Path:
    """The first folder/pattern.format(n), n = 1, 2, ..., that does not exist."""
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (folder / pattern.format(number)).exists():
        number += 1
    return folder / pattern.format(number)


class JupyterExecution:
    """Runs one snippet in the conversation's JupyterLab kernel (jupyter.py);
    the same interface as Execution. Figures are saved in the exchange folder."""

    def __init__(self, code: str, purpose: str, bridge: JupyterBridge) -> None:
        self.code = code
        self.purpose = purpose
        self.bridge = bridge
        self.cancel_event = threading.Event()

    def run(self) -> dict:
        # Runs in a thread: no GTK calls here.
        try:
            timeout = load_config().execution_timeout
        except ValueError:
            timeout = 300
        try:
            outcome = self.bridge.run(self.code, self.purpose, timeout, self.cancel_event)
        except JupyterError as exc:
            return {"ok": False, "stdout": "", "result": None, "error": str(exc)}
        except Exception as exc:  # a bug must not leave the card running forever
            LOG.exception("JupyterLab run failed")
            return {"ok": False, "stdout": "", "result": None, "error": f"{type(exc).__name__}: {exc}"}
        figures = []
        for image in outcome.get("images") or []:
            path = free_name(self.bridge.local_dir, "figure_{}.png")
            try:
                path.write_bytes(base64.b64decode(image))
                figures.append(path)
            except (OSError, ValueError) as exc:
                LOG.warning("Could not save a figure: %s", exc)
        if figures:
            self.bridge.mark_local(figures)
        outcome["figures"] = [path.name for path in figures]
        outcome["exchange"] = str(self.bridge.local_dir)
        return outcome

    def cancel(self) -> None:
        self.cancel_event.set()


class JobExecution:
    """Starts a background job in JupyterLab (jobs.py); the same interface as
    Execution. Only the start runs here; the report arrives later."""

    def __init__(self, code: str, purpose: str, bridge: JupyterBridge, jobs: Jobs, conversation: str) -> None:
        self.code = code
        self.purpose = purpose
        self.bridge = bridge
        self.jobs = jobs
        self.conversation = conversation

    def run(self) -> dict:
        # Runs in a thread: no GTK calls here.
        try:
            job = self.jobs.start_job(self.bridge, self.conversation, self.code, self.purpose)
        except JupyterError as exc:
            return {"ok": False, "stdout": "", "result": None, "error": str(exc)}
        except Exception as exc:
            LOG.exception("Starting a JupyterLab job failed")
            return {"ok": False, "stdout": "", "result": None, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "stdout": "", "result": None, "error": "", "job": job["id"],
                "notebook_url": job["notebook_url"], "uploaded": job["uploaded"], "notes": job["notes"],
                "exchange": str(self.bridge.local_dir)}

    def cancel(self) -> None:
        pass  # starting takes seconds; the job menu cancels the job itself


# --- result widgets ----------------------------------------------------------
def open_url(url: str) -> None:
    if not webbrowser.open_new_tab(url):
        LOG.warning("No browser to open %s", url)


def table_widget(columns: list[str], rows: list[list[str]], urls: list[str] | None = None) -> Gtk.Widget:
    """A sortable table; rows with a URL open it in the browser when clicked."""
    urls = urls or [""] * len(rows)
    url_column = len(columns)
    store = Gtk.ListStore(*([str] * (len(columns) + 2)))  # + URL, + link icon name
    for row, url in zip(rows, urls):
        store.append(row + [url, "web-browser-symbolic" if url else ""])
    view = Gtk.TreeView(model=store)
    view.set_enable_search(False)
    view.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
    if any(urls):
        icon = Gtk.TreeViewColumn("", Gtk.CellRendererPixbuf(), icon_name=url_column + 1)
        view.append_column(icon)
        view.set_activate_on_single_click(True)
        view.set_tooltip_text("Click a row to open it in Chemotion")
        view.connect("row-activated", lambda tree, path, _column: (
            store[path][url_column] and open_url(store[path][url_column])))
    for index, name in enumerate(columns):
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn(name.replace("_", "__"), renderer, text=index)
        column.set_resizable(True)
        column.set_sort_column_id(index)
        column.set_min_width(60)
        view.append_column(column)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_shadow_type(Gtk.ShadowType.IN)
    scroller.set_min_content_height(min(len(rows) + 1, 14) * 26 + 6)
    scroller.add(view)
    return scroller


def result_widget(value) -> Gtk.Widget:
    """A table for lists of dicts and dicts, text for everything else. Dicts with
    a "url" (chemotion_runner gives one to every element) are clickable rows."""
    if isinstance(value, dict) and isinstance(value.get("url"), str):
        value = [value]  # a single element: one clickable row
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        columns: list[str] = []
        for item in value:
            for key in item:
                if key != "url" and key not in columns and len(columns) < MAX_TABLE_COLUMNS:
                    columns.append(key)
        rows = [[cell_text(item.get(key)) for key in columns] for item in value]
        urls = [item["url"] if isinstance(item.get("url"), str) else "" for item in value]
        return table_widget(columns, rows, urls)
    if isinstance(value, dict) and any(isinstance(item, (dict, list)) and item for item in value.values()):
        # e.g. {"samples": [...], "reactions": [...]}: one section per entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scalars = {key: item for key, item in value.items() if not (isinstance(item, (dict, list)) and item)}
        if scalars:
            box.pack_start(result_widget(scalars), False, False, 0)
        for key, item in value.items():
            if key not in scalars:
                box.pack_start(label(str(key), "dim"), False, False, 0)
                box.pack_start(result_widget(item), False, False, 0)
        return box
    if isinstance(value, dict) and value:
        return table_widget(["Key", "Value"], [[str(key), cell_text(item)] for key, item in value.items()])
    if isinstance(value, list) and value and not any(isinstance(item, (dict, list)) for item in value):
        return table_widget(["Value"], [[cell_text(item)] for item in value])
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text_block(text)


def image_widget(data: str) -> Gtk.Widget | None:
    """A base64 PNG (a JupyterLab figure), at most MAX_IMAGE_WIDTH wide."""
    from gi.repository import GdkPixbuf  # type: ignore

    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(base64.b64decode(data))
        loader.close()
        pixbuf = loader.get_pixbuf()
    except (GLib.Error, ValueError):
        return None
    if pixbuf is None:
        return None
    if pixbuf.get_width() > MAX_IMAGE_WIDTH:
        height = round(pixbuf.get_height() * MAX_IMAGE_WIDTH / pixbuf.get_width())
        pixbuf = pixbuf.scale_simple(MAX_IMAGE_WIDTH, height, GdkPixbuf.InterpType.BILINEAR)
    image = Gtk.Image.new_from_pixbuf(pixbuf)
    image.set_halign(Gtk.Align.START)
    return image


def link_row(text: str, tooltip: str, target: str) -> Gtk.Widget:
    """A flat button that opens `target` (a URL or file URI) on click."""
    button = Gtk.Button(label=text)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_halign(Gtk.Align.START)
    button.set_tooltip_text(tooltip)
    button.connect("clicked", lambda *_: open_url(target))
    return button


def exchange_section(box: Gtk.Box, outcome: dict) -> None:
    """Figures, exchanged files and the notebook link of one run (JupyterLab on)."""
    for data in outcome.get("images") or []:
        image = image_widget(data)
        if image is not None:
            box.pack_start(image, False, False, 0)
    folder = outcome.get("exchange")
    names = []
    for key in ("figures", "saved", "uploaded", "downloaded"):
        names += [name for name in outcome.get(key) or [] if name not in names]
    if names and folder:
        box.pack_start(label("Files in the exchange folder", "dim"), False, False, 0)
        for name in names:
            path = Path(folder) / name
            box.pack_start(link_row(name, str(path), path.as_uri()), False, False, 0)
    for note in outcome.get("notes") or []:
        box.pack_start(label(note, "dim", selectable=True), False, False, 0)
    if outcome.get("notebook_url"):
        box.pack_start(link_row("Open the notebook in JupyterLab", outcome["notebook_url"],
                                outcome["notebook_url"]), False, False, 0)


def result_page(stdout: str, error: str, result, outcome: dict | None = None) -> Gtk.Widget:
    """The content of one result tab: printed output, error, `result`, and
    with JupyterLab figures and exchanged files (`outcome`)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, border_width=10)
    if (outcome or {}).get("job"):
        box.pack_start(label(f"Background job {outcome['job']} started. It runs on in JupyterLab (the job "
                             "menu in the header shows it); the assistant continues when it ends.", "dim"),
                       False, False, 0)
    if stdout.strip():
        box.pack_start(label("Output", "dim"), False, False, 0)
        box.pack_start(text_block(stdout.rstrip()), False, False, 0)
    if error:
        block = text_block(error.rstrip(), "request-error")
        block.get_style_context().add_class("request-output")
        box.pack_start(block, False, False, 0)
    if result is not None:
        row = Gtk.Box(spacing=8)
        row.pack_start(label("Result", "dim"), True, True, 0)
        copy = Gtk.Button(label="Copy JSON")
        copy.set_relief(Gtk.ReliefStyle.NONE)
        copy.connect("clicked", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
            json.dumps(result, ensure_ascii=False, indent=2), -1))
        row.pack_end(copy, False, False, 0)
        box.pack_start(row, False, False, 0)
        box.pack_start(result_widget(result), False, False, 0)
    exchange_section(box, outcome or {})
    if not box.get_children():
        box.pack_start(label("Nothing was printed and `result` was not set.", "dim"), False, False, 0)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.add(box)
    return scroller


def text_block(text: str, css: str = "request-output") -> Gtk.Widget:
    widget = label(clip(text, 100000), css, wrap=True, selectable=True)
    widget.set_valign(Gtk.Align.START)
    return widget


def code_editor(code: str) -> tuple[Gtk.Widget, Gtk.TextView]:
    if GtkSource is not None:
        buffer = GtkSource.Buffer()
        language = GtkSource.LanguageManager.get_default().get_language("python3") or \
            GtkSource.LanguageManager.get_default().get_language("python")
        if language is not None:
            buffer.set_language(language)
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme("oblivion")
        if scheme is not None:
            buffer.set_style_scheme(scheme)
        view = GtkSource.View(buffer=buffer)
        view.set_show_line_numbers(True)
        view.set_tab_width(4)
        view.set_insert_spaces_instead_of_tabs(True)
        view.set_auto_indent(True)
    else:
        view = Gtk.TextView()
    view.get_buffer().set_text(code)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    view.set_left_margin(6)
    view.set_top_margin(4)
    view.set_bottom_margin(4)
    view.get_style_context().add_class("request-code")
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_shadow_type(Gtk.ShadowType.IN)
    lines = code.count("\n") + 1
    scroller.set_min_content_height(min(max(lines, 3), 22) * 17 + 10)
    scroller.add(view)
    return scroller, view


class RequestCard(Gtk.Box):
    """One snippet: editable until the user runs or rejects it. Its output goes
    to a result tab through `on_result(card, page)`."""

    def __init__(self, number: int, pending: PendingRequest, on_state_change, on_result) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.number = number
        self.pending = pending
        self.on_state_change = on_state_change
        self.on_result = on_result
        self.execution: Execution | JupyterExecution | None = None
        self.state = "waiting"
        pending.on_abandoned = self._on_abandoned
        self.get_style_context().add_class("request-card")
        self.get_style_context().add_class("waiting")

        head = Gtk.Box(spacing=8)
        head.pack_start(label(f"#{number}", "dim", wrap=False), False, False, 0)
        jupyter = pending.target in ("jupyter", "job")
        job = pending.target == "job"
        purpose = label(pending.purpose or ("Start a background job in JupyterLab" if job else
                                            "Run code in JupyterLab" if jupyter else "Run code against Chemotion"),
                        "request-purpose")
        head.pack_start(purpose, True, True, 0)
        if jupyter:
            head.pack_end(label("JupyterLab job" if job else "JupyterLab", "request-target", wrap=False),
                          False, False, 0)
        if pending.auto:
            # pack_end: goes left of the time below.
            head.pack_end(label("auto · read-only" if pending.auto == "readonly" else "auto-run",
                                "dim", wrap=False), False, False, 0)
        head.pack_end(label(time.strftime("%H:%M:%S"), "dim", wrap=False), False, False, 0)
        self.pack_start(head, False, False, 0)

        editor, self.view = code_editor(pending.code)
        if pending.auto:
            self.view.set_editable(False)  # the code runs as it arrived
        self.view.connect("key-press-event", self._on_code_key)
        self.pack_start(editor, False, False, 0)

        self.actions = Gtk.Box(spacing=8)
        self.reason = Gtk.Entry(hexpand=True)
        self.reason.set_placeholder_text("Reason for rejecting (optional, sent to the assistant)")
        self.reason.connect("activate", lambda *_: self.reject())
        self.actions.pack_start(self.reason, True, True, 0)
        reject = Gtk.Button(label="Reject")
        reject.get_style_context().add_class("destructive-action")
        reject.connect("clicked", lambda *_: self.reject())
        self.actions.pack_start(reject, False, False, 0)
        self.run_button = Gtk.Button(label="Run")
        self.run_button.get_style_context().add_class("suggested-action")
        self.run_button.set_tooltip_text(("Start this code as a background job in JupyterLab" if job else
                                          "Run this code in JupyterLab" if jupyter else
                                          "Run this code against Chemotion") + " (Ctrl+Enter in the code)")
        self.run_button.connect("clicked", lambda *_: self.run())
        self.actions.pack_start(self.run_button, False, False, 0)
        self.pack_start(self.actions, False, False, 0)

        status_row = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        status_row.pack_start(self.spinner, False, False, 0)
        if pending.auto:
            waiting = "Auto-run: this code runs without your confirmation."
        elif pending.auto_reason:
            waiting = (f"Waiting for your confirmation (auto-run skipped: {pending.auto_reason}). "
                       "Nothing runs before you click Run.")
        else:
            waiting = "Waiting for your confirmation. Nothing runs before you click Run."
        self.status = label(waiting, "request-status")
        status_row.pack_start(self.status, True, True, 0)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.connect("clicked", lambda *_: self.execution and self.execution.cancel())
        status_row.pack_end(self.cancel_button, False, False, 0)
        self.pack_start(status_row, False, False, 0)

    @property
    def code(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def focus_code(self) -> None:
        self.run_button.grab_focus()

    def _set_state(self, state: str, text: str) -> None:
        context = self.get_style_context()
        context.remove_class(self.state)
        context.add_class(state)
        self.state = state
        self.status.set_text(text)
        self.on_state_change()

    def _lock(self) -> None:
        self.view.set_editable(False)
        self.actions.hide()

    def _on_code_key(self, _widget, event) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.run()
            return True
        return False

    def reject(self, reason: str | None = None) -> None:
        if self.state != "waiting":
            return
        reason = (reason if reason is not None else self.reason.get_text()).strip()
        self._lock()
        self._set_state("failed", f"Rejected{': ' + reason if reason else ''}. The code did not run.")
        self.pending.answer(
            False,
            "The user rejected the code; it did not run. "
            + (f"Reason: {reason}" if reason else "No reason was given; ask the user what to do instead."),
        )

    def run(self) -> None:
        if self.state != "waiting":
            return
        code = self.code
        if not code.strip():
            return
        self._lock()
        self._set_state("running", "Running…")
        self.spinner.show()
        self.spinner.start()
        self.cancel_button.show()
        self.execution = self.pending.new_execution(code)
        started = time.monotonic()

        def worker() -> None:
            outcome = self.execution.run()
            GLib.idle_add(self._finish, code, outcome, time.monotonic() - started)

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, code: str, outcome: dict, elapsed: float) -> bool:
        self.spinner.stop()
        self.spinner.hide()
        self.cancel_button.hide()
        ok = bool(outcome.get("ok"))
        stdout = str(outcome.get("stdout") or "")
        error = str(outcome.get("error") or "")
        result = outcome.get("result")

        self.on_result(self, result_page(stdout, error, result if ok else None, outcome))

        abandoned = self.pending.done.is_set() and self.pending.reply is None
        note = " The assistant had stopped waiting, so it did not get this output." if abandoned else ""
        if ok and outcome.get("job"):
            self._set_state("done", f"Started background job {outcome['job']}; it runs in JupyterLab.{note}")
        elif ok:
            self._set_state("done", f"Ran in {elapsed:.1f} s.{note}")
        else:
            self._set_state("failed", f"Failed after {elapsed:.1f} s.{note}")
        self.pending.answer(ok, self._answer_text(code, ok, stdout, error, result, outcome))
        return False

    def _answer_text(self, code: str, ok: bool, stdout: str, error: str, result, outcome: dict) -> str:
        parts = []
        jupyter = self.pending.target in ("jupyter", "job")
        if code != self.pending.code:
            parts.append(f"The user edited the code before running it. This code ran:\n```python\n{code}\n```")
        if outcome.get("new_kernel"):
            parts.append("The JupyterLab kernel is new (restarted): variables of earlier runs are gone.")
        if ok and outcome.get("job"):
            parts.append(f"Background job {outcome['job']} started in a kernel of its own and runs on. "
                         "Do not wait or poll: end your answer now (tell the user what runs). When it ends, "
                         "a message with its outcome, printed output and new files arrives.")
        else:
            parts.append("The code ran successfully." if ok else f"The code failed:\n{error}")
        if stdout.strip():
            parts.append(f"Printed output:\n{stdout}")
        if ok and not outcome.get("job"):
            if jupyter:
                parts.append("Value of the last expression: " + (str(result) if result is not None else "none"))
            elif result is None:
                parts.append("`result` was not set.")
            else:
                parts.append("result = " + json.dumps(result, ensure_ascii=False, indent=1, default=str))
        exchanged = [
            ("Figures the user saw, saved in `exchange`", outcome.get("figures")),
            ("Saved in `exchange`", outcome.get("saved")),
            ("Sent from `exchange` to JupyterLab", outcome.get("uploaded")),
            ("New or changed in `exchange` from JupyterLab", outcome.get("downloaded")),
        ]
        parts.extend(f"{title}: {', '.join(names)}" for title, names in exchanged if names)
        parts.extend(outcome.get("notes") or [])
        parts.append("The user sees this output in the result panel.")
        return clip("\n\n".join(parts))

    def _on_abandoned(self) -> None:
        if self.state == "waiting":
            self._lock()
            self._set_state("failed", "Not run: the assistant was stopped.")
        elif self.state == "running" and self.pending.auto and self.execution is not None:
            # Auto-run was going while the agent stopped; nothing listens any more.
            self.execution.cancel()

    def close(self) -> None:
        """The conversation ends: answer the agent if it still waits."""
        if self.state == "waiting":
            self.reject("The user closed the conversation.")
        elif self.execution is not None:
            self.execution.cancel()


class BottomFollower:
    """Keeps a scrolled window at the bottom while content arrives, unless the
    user scrolled up. "changed" fires once new content is allocated."""

    def __init__(self, scroller: Gtk.ScrolledWindow) -> None:
        self.active = True
        adjustment = scroller.get_vadjustment()
        adjustment.connect("changed", self._on_changed)
        adjustment.connect("value-changed", self._on_scrolled)

    def _on_changed(self, adjustment: Gtk.Adjustment) -> None:
        if self.active:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        self.active = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 20


class ResultsPanel(Gtk.Box):
    """The newest snippet (and older ones still waiting or running) on top, the
    latest results as tabs below."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.cards: list[RequestCard] = []

        head = Gtk.Box(spacing=8, border_width=10)
        head.pack_start(label("Results", "results-heading", wrap=False), False, False, 0)
        self.counter = label("", "dim", wrap=False)
        head.pack_start(self.counter, False, False, 0)
        self.pack_start(head, False, False, 0)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=10)
        self.empty = label(
            "Code the assistant wants to run against Chemotion appears here.\n"
            "You can read and edit it, then Run or Reject it. Nothing runs without your confirmation.",
            "dim",
        )
        self.empty.set_margin_top(20)
        self.list.pack_start(self.empty, False, False, 0)
        self.scroller.add(self.list)
        self.follower = BottomFollower(self.scroller)

        self.tabs = Gtk.Notebook()
        self.tabs.set_scrollable(True)
        self.tabs.set_no_show_all(True)  # shown with the first result
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.paned.pack1(self.scroller, True, False)
        self.paned.pack2(self.tabs, True, False)
        self.pack_start(self.paned, True, True, 0)


    def add_request(self, pending: PendingRequest) -> RequestCard:
        self.empty.hide()
        card = RequestCard(len(self.cards) + 1, pending, self._update_counter, self._add_result)
        self.cards.append(card)
        self._prune()
        self.list.pack_start(card, False, False, 0)
        card.show_all()
        self._update_counter()
        self.follower.active = True
        if pending.auto:
            GLib.idle_add(card.run)  # auto-run: start as soon as the card is on screen
        else:
            card.focus_code()
        return card

    def _prune(self) -> None:
        """Only the newest code stays visible, plus older code still waiting or running."""
        for card in self.cards[:-1]:
            if card.state not in ("waiting", "running") and card.get_parent() is self.list:
                self.list.remove(card)

    def _add_result(self, card: RequestCard, page: Gtk.Widget) -> None:
        title = card.pending.purpose or "Result"
        tab = Gtk.Box(spacing=4)
        text = label(f"#{card.number} {title}", wrap=False)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.set_width_chars(min(len(text.get_text()), 22))
        text.set_max_width_chars(22)
        tab.pack_start(text, True, True, 0)
        tab.pack_start(icon_button("window-close-symbolic", "Close this result",
                                   lambda: self.tabs.remove_page(self.tabs.page_num(page))), False, False, 0)
        tab.set_tooltip_text(title)
        tab.show_all()
        page.show_all()
        if not self.tabs.get_visible():
            self.tabs.show()
            self.paned.set_position(int(self.paned.get_allocated_height() * 0.4))
        index = self.tabs.append_page(page, tab)
        self.tabs.set_current_page(index)
        while self.tabs.get_n_pages() > MAX_RESULT_TABS:
            self.tabs.remove_page(0)

    def waiting(self) -> int:
        return sum(card.state == "waiting" for card in self.cards)

    def _update_counter(self) -> None:
        self._prune()
        count = self.waiting()
        self.counter.set_text(f"· {count} waiting for confirmation" if count else "")
        window = self.get_toplevel()
        if isinstance(window, Gtk.Window):
            window.set_urgency_hint(bool(count) and not window.is_active())

    def close(self) -> None:
        for card in self.cards:
            card.close()


# --- the chat --------------------------------------------------------------------
def inline_markup(text: str) -> str:
    """`code`, **bold** and [links](https://...) of one Markdown line as Pango markup."""
    parts = []
    for index, part in enumerate(re.split(r"(`[^`]+`)", text)):
        if index % 2:
            parts.append(f"<tt>{GLib.markup_escape_text(part[1:-1])}</tt>")
            continue
        part = GLib.markup_escape_text(part)
        part = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", part)
        part = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', part)
        parts.append(part)
    return "".join(parts)


def table_markup(lines: list[str]) -> str:
    """A Markdown table as aligned monospace text."""
    rows = [[re.sub(r"\*\*|`", "", cell).strip() for cell in line.strip().strip("|").split("|")] for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r":?-{2,}:?", cell) for cell in row)]
    if not rows:
        return ""
    widths = [max(len(row[column]) if column < len(row) else 0 for row in rows)
              for column in range(max(len(row) for row in rows))]
    text = ["  ".join(cell.ljust(widths[column]) for column, cell in enumerate(row)).rstrip() for row in rows]
    return f"<tt><b>{GLib.markup_escape_text(text[0])}</b>\n{GLib.markup_escape_text(chr(10).join(text[1:]))}</tt>"


def markdown_markup(text: str) -> str:
    """The Markdown models write (code blocks, tables, headings, lists, inline
    code, bold, links) as Pango markup. Unclosed blocks (while the answer
    streams in) are closed."""
    out: list[str] = []
    code: list[str] | None = None
    table: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            if table:
                out.append(table_markup(table))
                table = []
            if code is None:
                code = []
            else:
                out.append(f"<tt>{GLib.markup_escape_text(chr(10).join(code))}</tt>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        if table:
            out.append(table_markup(table))
            table = []
        heading = re.match(r"#{1,6}\s+(.*)", line)
        item = re.match(r"(\s*)[-*+]\s+(.*)", line)
        if heading:
            out.append(f"<b>{inline_markup(heading.group(1))}</b>")
        elif item:
            out.append(f"{item.group(1)}• {inline_markup(item.group(2))}")
        else:
            out.append(inline_markup(line))
    if code is not None:
        out.append(f"<tt>{GLib.markup_escape_text(chr(10).join(code))}</tt>")
    if table:
        out.append(table_markup(table))
    return "\n".join(out).strip()


class ChatPanel(Gtk.Box):
    """The conversation with the agent: messages, a status line, the input
    with attached files and folders (button or drag and drop)."""

    def __init__(self, on_send, on_stop) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.on_send = on_send  # on_send(text, attachments)
        self.attachments: list[str] = []
        self.answer: Gtk.Label | None = None  # the assistant message being streamed
        self.answer_text = ""
        self.render_pending = False
        self.actions: list[Gtk.Button] = []  # add_note(actions=...), until the next message

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=14)
        scroller.add(self.list)
        self.follower = BottomFollower(scroller)
        self.pack_start(scroller, True, True, 0)

        status_row = Gtk.Box(spacing=8, border_width=6)
        status_row.set_margin_start(8)
        self.spinner = Gtk.Spinner()
        status_row.pack_start(self.spinner, False, False, 0)
        self.status = label("", "dim", wrap=False)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        status_row.pack_start(self.status, True, True, 0)
        self.pack_start(status_row, False, False, 0)

        self.chips = Gtk.FlowBox()
        self.chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self.chips.set_max_children_per_line(20)
        self.chips.set_margin_start(10)
        self.chips.set_margin_end(10)
        self.pack_start(self.chips, False, False, 0)

        input_row = Gtk.Box(spacing=8, border_width=10)
        input_row.set_margin_top(0)
        attach = Gtk.MenuButton()
        attach.set_image(Gtk.Image.new_from_icon_name("mail-attachment-symbolic", Gtk.IconSize.BUTTON))
        attach.set_tooltip_text("Attach a file or a folder (or drop it here)")
        attach.set_valign(Gtk.Align.END)
        menu = Gtk.Menu()
        for text, folder in (("File…", False), ("Folder…", True)):
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda _item, folder=folder: self._choose(folder))
            menu.append(item)
        menu.show_all()
        attach.set_popup(menu)
        input_row.pack_start(attach, False, False, 0)
        self.input = Gtk.TextView()
        self.input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input.set_left_margin(8)
        self.input.set_right_margin(8)
        self.input.set_top_margin(6)
        self.input.set_bottom_margin(6)
        self.input.get_style_context().add_class("chat-input")
        self.input.connect("key-press-event", self._on_input_key)
        input_scroller = Gtk.ScrolledWindow()
        input_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroller.set_shadow_type(Gtk.ShadowType.IN)
        input_scroller.set_min_content_height(56)
        input_scroller.set_max_content_height(180)
        input_scroller.set_propagate_natural_height(True)
        input_scroller.add(self.input)
        input_row.pack_start(input_scroller, True, True, 0)
        self.send_button = Gtk.Button(label="Send")
        self.send_button.get_style_context().add_class("suggested-action")
        self.send_button.set_tooltip_text("Send (Enter; Shift+Enter for a new line)")
        self.send_button.set_valign(Gtk.Align.END)
        self.send_button.connect("clicked", lambda *_: self._send())
        input_row.pack_start(self.send_button, False, False, 0)
        self.stop_button = Gtk.Button(label="Stop")
        self.stop_button.get_style_context().add_class("destructive-action")
        self.stop_button.set_tooltip_text("Stop the assistant (Esc)")
        self.stop_button.set_valign(Gtk.Align.END)
        self.stop_button.set_no_show_all(True)
        self.stop_button.connect("clicked", lambda *_: on_stop())
        input_row.pack_start(self.stop_button, False, False, 0)
        self.pack_start(input_row, False, False, 0)
        self.busy = False

        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)
        # The text view has its own drop handling (it inserts the URIs as text).
        self.input.drag_dest_unset()

    def focus_input(self) -> None:
        self.input.grab_focus()

    def _on_input_key(self, _widget, event) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not event.state & Gdk.ModifierType.SHIFT_MASK:
            self._send()
            return True
        return False

    def _send(self) -> None:
        buffer = self.input.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
        if not (text or self.attachments) or self.busy:
            return
        buffer.set_text("")
        attachments, self.attachments = self.attachments, []
        self._update_chips()
        self.on_send(text or "Have a look at the attachments.", attachments)

    # --- attachments ----------------------------------------------------------
    def _choose(self, folder: bool) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Attach a folder" if folder else "Attach files",
            self.get_toplevel(),
            Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN,
            "Attach",
            "Cancel",
        )
        dialog.set_select_multiple(not folder)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            self.attach([path for path in (file.get_path() for file in dialog.get_files()) if path])
        dialog.destroy()
        self.focus_input()

    def _on_drop(self, _widget, context, _x, _y, data, _info, time_) -> None:
        paths = [Gio.File.new_for_uri(uri).get_path() for uri in data.get_uris() or []]
        self.attach([path for path in paths if path])
        Gtk.drag_finish(context, True, False, time_)

    def attach(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self.attachments and os.path.exists(path):
                self.attachments.append(path)
        self._update_chips()

    def _update_chips(self) -> None:
        for child in self.chips.get_children():
            self.chips.remove(child)
        for path in self.attachments:
            chip = Gtk.Box(spacing=4)
            chip.get_style_context().add_class("attachment-chip")
            folder = os.path.isdir(path)
            chip.pack_start(Gtk.Image.new_from_icon_name(
                "folder-symbolic" if folder else "text-x-generic-symbolic", Gtk.IconSize.MENU), False, False, 0)
            name = label(os.path.basename(path.rstrip("/\\")) + ("/" if folder else ""), wrap=False)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.set_max_width_chars(30)
            chip.pack_start(name, False, False, 0)
            chip.pack_start(icon_button("window-close-symbolic", "Remove",
                                        lambda path=path: self._detach(path)), False, False, 0)
            chip.set_tooltip_text(path)
            self.chips.add(chip)
        self.chips.show_all()

    def _detach(self, path: str) -> None:
        self.attachments.remove(path)
        self._update_chips()

    def _add(self, widget: Gtk.Widget) -> None:
        self.list.pack_start(widget, False, False, 0)
        widget.show_all()

    def add_user(self, text: str, attachments: list[str] | tuple[str, ...] = ()) -> None:
        self.answer = None
        self.clear_actions()
        bubble = label(text, "chat-user", selectable=True)
        bubble.set_halign(Gtk.Align.END)
        bubble.set_margin_start(60)
        self.follower.active = True
        self._add(bubble)
        if attachments:
            names = ", ".join(os.path.basename(path.rstrip("/\\")) + ("/" if os.path.isdir(path) else "")
                              for path in attachments)
            note = label(f"Attached: {names}", "dim", selectable=True)
            note.set_halign(Gtk.Align.END)
            note.set_tooltip_text("\n".join(attachments))
            self._add(note)

    def append_text(self, delta: str) -> None:
        if self.answer is None:
            self.answer = label("", "chat-assistant", selectable=True)
            self.answer.connect("activate-link", lambda _label, uri: open_url(uri) or True)
            self.answer_text = ""
            self._add(self.answer)
        self.answer_text += delta
        if not self.render_pending:
            self.render_pending = True
            GLib.timeout_add(60, self._render)

    def _render(self) -> bool:
        self.render_pending = False
        if self.answer is None:
            return False
        markup = markdown_markup(self.answer_text)
        try:
            Pango.parse_markup(markup, -1, "\0")
            self.answer.set_markup(markup)
        except GLib.Error:
            self.answer.set_text(self.answer_text.strip())
        return False

    def replay(self, messages: list[dict]) -> None:
        """Show the messages of a saved conversation (Agent.messages)."""
        for message in messages:
            role, content = message.get("role"), message.get("content")
            if role == "user" and isinstance(content, str):
                if content == STOPPED_NOTE:
                    self.add_note("Stopped after many tool calls.", "dialog-error-symbolic", "request-error")
                    continue
                if content.startswith(REPORT_HEADER):
                    reports, _, content = content[len(REPORT_HEADER):].partition(USER_TEXT)
                    for item in reports.split(REPORT_SEPARATOR):
                        self.add_note(item.split("\n", 1)[0], "preferences-system-time-symbolic")
                    if not content:
                        continue
                text, _, listed = content.partition(ATTACHMENT_NOTE)
                # "- /path (text file, 1.2 kB)" lines; the paths contain no " (".
                paths = [line[2:].split(" (", 1)[0].rstrip("/\\") for line in listed.splitlines()
                         if line.startswith("- ")]
                self.add_user(text, paths)
            elif role == "assistant":
                if isinstance(content, str) and content.strip():
                    self.append_text(content)
                    self._render()
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    if function.get("name") in ("execute_python", "execute_jupyter", "start_jupyter_job"):
                        self.add_note(arguments.get("purpose") or "Code", "system-run-symbolic")
                    else:
                        self.add_note(tool_summary(str(function.get("name")), arguments))
        self.answer = None
        self.add_note("Continued from the history. Earlier results are not shown on the right.",
                      "document-open-recent-symbolic")

    def add_note(self, text: str, icon: str = "document-open-symbolic", css: str = "dim",
                 actions: tuple[tuple[str, object], ...] = ()) -> None:
        """`actions`: (label, callback) buttons below the note; they work until
        the next message or the next note with actions (clear_actions())."""
        if self.answer is not None:
            self._render()
        self.answer = None
        if actions:
            self.clear_actions()
        row = Gtk.Box(spacing=6)
        row.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU), False, False, 0)
        if actions:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            column.pack_start(label(text, css, selectable=True), False, False, 0)
            buttons = Gtk.Box(spacing=6)
            for text_, callback in actions:
                button = Gtk.Button(label=text_)
                button.connect("clicked", lambda _button, callback=callback: callback())
                buttons.pack_start(button, False, False, 0)
                self.actions.append(button)
            column.pack_start(buttons, False, False, 0)
            row.pack_start(column, True, True, 0)
        else:
            row.pack_start(label(text, css, selectable=True), True, True, 0)
        self._add(row)

    def clear_actions(self) -> None:
        """Disable the buttons of the last note: they belong to that moment."""
        for button in self.actions:
            button.set_sensitive(False)
        self.actions = []

    def set_busy(self, busy: bool, status: str = "") -> None:
        self.busy = busy
        self.spinner.start() if busy else self.spinner.stop()
        self.status.set_text(status)
        self.send_button.set_visible(not busy)
        self.stop_button.set_visible(busy)


# --- the window ----------------------------------------------------------------
class AssistantWindow(Gtk.Window):
    """Starts as a centered prompt; grows to the work area with the chat on
    the left and the results on the right."""

    def __init__(self, app: ChemAssistentApp) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title="ChemAssistent")
        self.app = app
        self.paned: Gtk.Paned | None = None
        self.chat: ChatPanel | None = None
        self.results: ResultsPanel | None = None
        self.agent: Agent | None = None
        self.conversation_id: str | None = None
        self.conversation_title = ""
        self.saved_messages = 0  # how many messages history has; saves only on change
        self.expanded = False
        self.closed = False
        self.auto_run = "off"  # the conversation's auto-run mode; set in _start_conversation
        self.run_mode_button: Gtk.MenuButton | None = None
        self.model_button: Gtk.MenuButton | None = None
        self.model_label: Gtk.Label | None = None
        # JupyterLab (config.jupyter_url): the conversation's notebook/kernel and exchange folder.
        self.jupyter: JupyterBridge | None = None
        self.exchange: Path | None = None
        self.jobs_button: Gtk.MenuButton | None = None
        self.jobs_label: Gtk.Label | None = None

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        try:
            self.set_icon_from_file(str(ICON_FILE))
        except GLib.Error:
            pass
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        self.get_style_context().add_class("popup-window")

        self.stack = Gtk.Stack()
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.add_named(self._build_prompt(), "prompt")
        self.add(self.stack)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", lambda *_: self.close_conversation() or True)
        self.connect("notify::is-active", lambda *_: self.results and self.results._update_counter())

    # --- prompt -------------------------------------------------------------
    def _build_prompt(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.get_style_context().add_class("prompt-box")
        box.set_border_width(4)

        head = Gtk.Box(spacing=10)
        try:
            from gi.repository import GdkPixbuf  # type: ignore

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(DARK_UI_ICON_FILE), 28, 28)
            head.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        except GLib.Error:
            pass
        head.pack_start(label("Ask Chemotion", "prompt-heading", wrap=False), False, False, 0)
        head.pack_end(icon_button("emblem-system-symbolic", "Settings", open_settings), False, False, 0)
        box.pack_start(head, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_width_chars(56)
        self.entry.set_placeholder_text("e.g. “Which samples are in my collection Main Test?”")
        self.entry.connect("activate", lambda *_: self._submit())
        box.pack_start(self.entry, False, False, 0)

        self.hint = label("", "prompt-hint")
        box.pack_start(self.hint, False, False, 0)
        self.error = label("", "prompt-error")
        self.error.set_no_show_all(True)
        box.pack_start(self.error, False, False, 0)

        self.recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.recent.pack_start(label("Recent conversations", "prompt-hint", wrap=False), False, False, 0)
        self.recent_list = Gtk.ListBox()
        self.recent_list.get_style_context().add_class("recent-list")
        self.recent_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.recent_list.connect("row-activated", lambda _list, row: self.resume(row.conversation_id))
        self.recent.pack_start(self.recent_list, False, False, 0)
        box.pack_start(self.recent, False, False, 0)

        outer = Gtk.Box()
        outer.pack_start(box, True, True, 0)
        return outer

    def open_prompt(self) -> None:
        try:
            config = load_config()
            self.hint.set_text(f"Enter starts the assistant · Esc closes · {AGENT_SHORT[config.agent]}: "
                               f"{model_name(config.ai_model)}"
                               + (" · JupyterLab" if config.jupyter and config.agent not in CLI_AGENTS else ""))
            self.set_default_size(config.prompt_width, -1)
            self.entry.set_width_chars(max(30, config.prompt_width // 11))
        except ValueError:
            self.hint.set_text("Enter starts the assistant · Esc closes")
        self.show_all()
        self.error.hide()
        self._fill_recent()
        width, height = self.get_size()
        area = work_area()
        self.move(area.x + (area.width - width) // 2, area.y + (area.height - height) // 3)
        self.present_now()
        self.entry.grab_focus()

    def _fill_recent(self) -> None:
        for row in self.recent_list.get_children():
            self.recent_list.remove(row)
        entries = history.entries(RECENT_CONVERSATIONS)
        for entry in entries:
            row = Gtk.ListBoxRow()
            row.conversation_id = entry.id
            line = Gtk.Box(spacing=8)
            line.pack_start(Gtk.Image.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.MENU),
                            False, False, 0)
            title = label(entry.title, "recent-title", wrap=False)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            line.pack_start(title, True, True, 0)
            if any(job.get("wake", True) for job in self.app.jobs.undelivered(entry.id)):
                line.pack_start(label("job finished", "request-target", wrap=False), False, False, 0)
            elif any(job["state"] in ACTIVE for job in self.app.jobs.list(entry.id)):
                line.pack_start(label("job running", "dim", wrap=False), False, False, 0)
            line.pack_start(label(history.age(entry.updated), "dim", wrap=False), False, False, 0)
            line.pack_start(icon_button("user-trash-symbolic", "Delete from the history",
                                        lambda entry_id=entry.id: self._forget(entry_id)), False, False, 0)
            row.add(line)
            row.set_tooltip_text(f"Continue this conversation ({entry.model})" if entry.model
                                 else "Continue this conversation")
            self.recent_list.add(row)
        self.recent.show_all() if entries else self.recent.hide()

    def _forget(self, conversation_id: str) -> None:
        try:
            history.delete(conversation_id)
        except OSError as exc:
            self.show_error(f"Could not delete it: {exc}")
        self._fill_recent()
        self.entry.grab_focus()

    def present_now(self) -> None:
        self.show()
        self.deiconify()
        self.present_with_time(server_time(self))

    def show_error(self, text: str) -> None:
        self.error.set_text(text)
        self.error.show()
        self.entry.grab_focus()

    def _ready_config(self) -> Config | None:
        """The config, or None after showing why no conversation can start."""
        try:
            config = load_config()
        except ValueError as exc:
            self.show_error(f"{exc}\nOpen the settings with the gear button.")
            return None
        problem = ai_problem(read_raw_config()) or command_problem(config)
        if problem:
            self.show_error(f"{problem}\nOpen the settings with the gear button.")
            return None
        if not workspace.reference_ready():
            self.show_error(f"The Chemotion API reference is missing in {WORKSPACE_DIR}. Run install.sh "
                            "(or setup_workspace.sh in the source tree).")
            return None
        return config

    def _submit(self) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        config = self._ready_config()
        if config is not None:
            self._start_conversation(config, text)

    def resume(self, conversation_id: str) -> None:
        data = history.load(conversation_id)
        if data is None:
            self.show_error("This conversation cannot be read any more.")
            self._fill_recent()
            return
        config = self._ready_config()
        if config is None:
            return
        self._start_conversation(config, None, conversation_id, data)

    # --- conversation ---------------------------------------------------------
    def _build_conversation(self, model: str) -> Gtk.Widget:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.get_style_context().add_class("assistant-main")

        header = Gtk.Box(spacing=8)
        header.get_style_context().add_class("assistant-header")
        # The window has no title bar: drag it by the header.
        handle = Gtk.EventBox()
        handle.add(header)
        handle.connect("button-press-event", self._on_header_press)
        try:
            from gi.repository import GdkPixbuf  # type: ignore

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(DARK_UI_ICON_FILE), 22, 22)
            header.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        except GLib.Error:
            pass
        header.pack_start(label("ChemAssistent", "assistant-title", wrap=False), False, False, 0)
        header.pack_start(self._build_model_button(model), False, False, 0)
        header.pack_end(icon_button("window-close-symbolic", "End the conversation and close", self.close_conversation),
                        False, False, 0)
        header.pack_end(icon_button("window-minimize-symbolic", "Minimize (the Chemotion icon brings it back)",
                                    self.iconify), False, False, 0)
        header.pack_end(icon_button("emblem-system-symbolic", "Settings", open_settings), False, False, 0)
        header.pack_end(icon_button("document-new-symbolic", "New conversation", self.app.new_conversation),
                        False, False, 0)
        header.pack_end(self._build_run_mode_button(), False, False, 0)
        header.pack_end(self._build_jobs_button(), False, False, 0)
        main.pack_start(handle, False, False, 0)

        self.chat = ChatPanel(self._send, self._stop)
        self.results = ResultsPanel()
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(True)
        self.paned.pack1(self.chat, True, False)
        self.paned.pack2(self.results, True, False)
        main.pack_start(self.paned, True, True, 0)
        return main

    # --- auto-run mode ----------------------------------------------------------
    def _build_run_mode_button(self) -> Gtk.MenuButton:
        """The header menu that switches this conversation's auto-run mode."""
        button = Gtk.MenuButton()
        button.set_relief(Gtk.ReliefStyle.NONE)
        menu = Gtk.Menu()
        first: Gtk.RadioMenuItem | None = None
        for mode in AUTO_RUN_MODES:
            item = Gtk.RadioMenuItem(label=AUTO_RUN_LABELS[mode], group=first)
            first = first or item
            if mode == self.auto_run:
                item.set_active(True)  # before connecting "toggled": no spurious handler call
            item.connect("toggled", self._on_run_mode_toggled, mode)
            menu.append(item)
        menu.show_all()
        button.set_popup(menu)
        self.run_mode_button = button
        self._update_run_mode_icon()
        return button

    def _on_run_mode_toggled(self, item: Gtk.RadioMenuItem, mode: str) -> bool:
        if item.get_active():
            self._set_auto_run(mode)
        return False

    def _set_auto_run(self, mode: str) -> None:
        """Switch the mode for this conversation (and the agent's tool
        descriptions); the config default for new conversations is unchanged."""
        if mode not in AUTO_RUN_MODES or mode == self.auto_run:
            return
        self.auto_run = mode
        if self.agent is not None:
            self.agent.set_auto_run(mode)
        self._update_run_mode_icon()

    def _update_run_mode_icon(self) -> None:
        if self.run_mode_button is None:
            return
        mode = self.auto_run if self.auto_run in RUN_MODE_ICONS else "off"
        self.run_mode_button.set_image(Gtk.Image.new_from_icon_name(
            RUN_MODE_ICONS[mode], Gtk.IconSize.BUTTON))
        self.run_mode_button.set_tooltip_text(f"Code execution: {AUTO_RUN_LABELS[mode]}")
        self.run_mode_button.show_all()

    # --- background jobs ---------------------------------------------------------------
    def _build_jobs_button(self) -> Gtk.MenuButton:
        """The header menu with this conversation's background jobs (hidden without any)."""
        button = Gtk.MenuButton()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text("Background jobs in JupyterLab")
        box = Gtk.Box(spacing=4)
        box.pack_start(Gtk.Image.new_from_icon_name("preferences-system-time-symbolic", Gtk.IconSize.BUTTON),
                       False, False, 0)
        self.jobs_label = label("", "dim", wrap=False)
        box.pack_start(self.jobs_label, False, False, 0)
        button.add(box)
        box.show_all()
        button.set_no_show_all(True)  # shown with the first job
        self.jobs_button = button
        self._fill_jobs_menu()
        GLib.timeout_add_seconds(JOB_MENU_REFRESH_S, self._refresh_jobs_menu)
        return button

    def _refresh_jobs_menu(self) -> bool:
        """Keeps the run times current; not while the menu is open."""
        if self.closed:
            return False
        if self.jobs_button is not None and not self.jobs_button.get_active():
            self._fill_jobs_menu()
        return True

    def _fill_jobs_menu(self) -> None:
        if self.jobs_button is None or self.conversation_id is None:
            return
        jobs = self.app.jobs.list(self.conversation_id)
        self.jobs_button.set_visible(bool(jobs))
        running = sum(job["state"] in ACTIVE for job in jobs)
        self.jobs_label.set_text(f"{running} running" if running else f"{len(jobs)} job{'s' * (len(jobs) != 1)}")
        menu = Gtk.Menu()
        for job in reversed(jobs):
            purpose = job.get("purpose") or job["id"]
            if len(purpose) > 60:
                purpose = purpose[:59] + "…"
            since = duration((job.get("finished") or time.time()) - job["started"])
            item = Gtk.MenuItem(label=f"{purpose} · {job['state']} · {since}")
            submenu = Gtk.Menu()
            if job.get("problem"):
                problem = Gtk.MenuItem(label=f"Cannot check it: {job['problem'][:MAX_MODEL_ERROR_CHARS]}")
                problem.set_sensitive(False)
                submenu.append(problem)
            notebook = Gtk.MenuItem(label="Open its notebook in JupyterLab")
            notebook.connect("activate", lambda _item, url=job["notebook_url"]: open_url(url))
            submenu.append(notebook)
            if job["state"] == "running":
                cancel = Gtk.MenuItem(label="Cancel the job")
                cancel.connect("activate", lambda _item, job=job: self._cancel_job(job))
                submenu.append(cancel)
            item.set_submenu(submenu)
            menu.append(item)
        menu.show_all()
        self.jobs_button.set_popup(menu)

    def _cancel_job(self, job: dict) -> None:
        self.app.jobs.cancel(job["id"])
        if self.chat is not None:
            self.chat.add_note(f"Cancelling background job {job['id']} ({job.get('purpose') or 'no purpose'}); "
                               "the assistant learns about it with your next message.",
                               "media-playback-stop-symbolic")

    def jobs_changed(self) -> None:
        self._fill_jobs_menu()
        self._deliver_jobs()

    def _reports(self) -> list[dict]:
        """Jobs of this conversation whose report the agent has not had yet."""
        if self.conversation_id is None:
            return []
        return [job for job in self.app.jobs.undelivered(self.conversation_id) if job.get("report")]

    def _take_reports(self, reports: list[dict]) -> str:
        """Show the reports as notes, mark them delivered; the message text."""
        for job in reports:
            self.chat.add_note(job["report"].split("\n", 1)[0], "preferences-system-time-symbolic")
        self.app.jobs.mark_delivered(reports)
        return REPORT_HEADER + REPORT_SEPARATOR.join(clip(job["report"]) for job in reports)

    def _deliver_jobs(self) -> bool:
        """Jobs ended: their reports go to the agent as a new turn, once it is idle."""
        if self.closed or self.agent is None or self.agent.busy or self.chat is None:
            return False
        reports = self._reports()
        if not any(job.get("wake", True) for job in reports):
            return False  # only cancelled jobs: they go with the next message
        self.chat.clear_actions()
        text = self._take_reports(reports)
        self.chat.set_busy(True, "Working…")
        self.agent.busy = True
        threading.Thread(target=self.agent.send, args=(text,), daemon=True).start()
        return False

    # --- model ---------------------------------------------------------------------
    def _build_model_button(self, model: str) -> Gtk.MenuButton:
        """The header menu that switches this conversation's model."""
        button = Gtk.MenuButton()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text("Model for this conversation")
        box = Gtk.Box(spacing=4)
        self.model_label = label(self._model_text(model), "dim", wrap=False)
        box.pack_start(self.model_label, False, False, 0)
        box.pack_start(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.MENU), False, False, 0)
        button.add(box)
        self.model_button = button
        self.models_changed()
        return button

    def _model_text(self, model: str) -> str:
        agent = self.agent.config.agent if self.agent is not None else "api"
        return f"· {model_name(model)}" if agent == "api" else f"· {AGENT_SHORT[agent]} · {model_name(model)}"

    def models_changed(self) -> None:
        """(Re)fill the model menu: the app's model list, or why it is missing."""
        if self.model_button is None or self.agent is None:
            return
        current = self.agent.config.ai_model
        models, error = self.app.models, self.app.models_error
        menu = Gtk.Menu()
        first: Gtk.RadioMenuItem | None = None
        for model in sorted(set(models or ()) | {current}):
            item = Gtk.RadioMenuItem(label=model_name(model), group=first)
            first = first or item
            if model == current:
                item.set_active(True)  # before connecting "toggled": no spurious handler call
            item.connect("toggled", self._on_model_toggled, model)
            menu.append(item)
        if models is None:
            menu.append(Gtk.SeparatorMenuItem())
            if error:
                if len(error) > MAX_MODEL_ERROR_CHARS:
                    error = error[: MAX_MODEL_ERROR_CHARS - 1] + "…"
                failed = Gtk.MenuItem(label=f"Could not load the models: {error}")
                failed.set_sensitive(False)
                menu.append(failed)
                again = Gtk.MenuItem(label="Load again")
                again.connect("activate", lambda *_: self.agent and self.app.load_models(self.agent.config, True))
                menu.append(again)
            else:
                loading = Gtk.MenuItem(label="Loading models…")
                loading.set_sensitive(False)
                menu.append(loading)
        menu.show_all()
        self.model_button.set_popup(menu)

    def _on_model_toggled(self, item: Gtk.RadioMenuItem, model: str) -> bool:
        if item.get_active():
            self._set_model(model)
        return False

    def _set_model(self, model: str) -> None:
        """Switch the model for this conversation from the next request on;
        the config default for new conversations is unchanged."""
        if self.agent is None or model == self.agent.config.ai_model:
            return
        self.agent.set_model(model)
        if self.model_label is not None:
            self.model_label.set_text(self._model_text(model))
        if self.chat is not None:
            self.chat.add_note(f"Model: {model_name(model)}", "emblem-synchronizing-symbolic")
        GLib.idle_add(self.models_changed)  # not inside the menu's own signal

    def _choose_model(self) -> None:
        if self.model_button is not None:
            self.model_button.set_active(True)

    def _start_conversation(self, config: Config, text: str | None, conversation_id: str | None = None,
                            saved: dict | None = None) -> None:
        """A new conversation starting with `text`, or a `saved` one from the history."""
        self.conversation_id = conversation_id = conversation_id or history.new_id()
        on_event = lambda kind, data: GLib.idle_add(self._on_agent_event, kind, data)  # noqa: E731
        if config.agent in CLI_AGENTS:
            # A CLI agent reaches only execute_python (mcp_bridge.py), not JupyterLab.
            listener = self.app.server.listener
            self.agent = CliAgent(config, workspace.system_prompt(), on_event, auto_run=config.auto_run,
                                  endpoint=(listener.address, listener.token))
        else:
            if config.jupyter:
                self.exchange = history.exchange_dir(self.conversation_id)
                self.jupyter = JupyterBridge(config, self.conversation_id, self.exchange)
            self.agent = Agent(
                config,
                workspace.system_prompt(self.exchange),
                self._execute,
                on_event,
                auto_run=config.auto_run,
                exchange=self.exchange,
                jobs=lambda: self.app.jobs.info(conversation_id),
            )
        self.conversation_title = str(saved.get("title") or "") if saved else text
        self.auto_run = config.auto_run  # the header menu can change it for this conversation
        self.stack.add_named(self._build_conversation(config.ai_model), "conversation")
        self.app.load_models(config)
        self.stack.show_all()
        self.stack.set_visible_child_name("conversation")
        self.expanded = True
        self.set_keep_above(False)
        self.set_skip_taskbar_hint(False)
        self.set_skip_pager_hint(False)
        self.set_title(f"ChemAssistent – {self.conversation_title[:60]}")
        if saved:
            roots = [str(root) for root in saved.get("roots") or []]
            if isinstance(self.agent, CliAgent):
                # The CLI's session only continues in the same CLI; otherwise it gets a transcript.
                session = str(saved.get("session") or "") if saved.get("agent") == config.agent else ""
                self.agent.restore(saved["messages"], roots, session)
            else:
                self.agent.restore(saved["messages"], roots)
            self.chat.replay(self.agent.messages[1:])
            self.saved_messages = len(self.agent.messages) - 1
        # Set but unusable: say so, or the agent quietly works without it.
        problem = jupyter_problem(read_raw_config())
        if problem:
            self.chat.add_note(f"JupyterLab is off for this conversation: {problem}", "dialog-warning-symbolic",
                               "request-error")
        self._grow()
        if text is not None:
            self._send(text)
        else:
            GLib.idle_add(self._deliver_jobs)  # jobs that ended while it was closed

    def _save_history(self) -> None:
        """Store the conversation (after every answer and on close)."""
        if self.agent is None or self.conversation_id is None:
            return
        messages = list(self.agent.messages[1:])  # without the system prompt
        if len(messages) == self.saved_messages or not any(message.get("role") == "user" for message in messages):
            return
        own = {WORKSPACE_DIR.resolve(), self.exchange.resolve() if self.exchange else None}
        roots = [str(root) for root in self.agent.roots if root not in own]
        try:
            history.save(self.conversation_id, self.conversation_title, model_name(self.agent.config.ai_model),
                         messages, roots, self.agent.config.agent, getattr(self.agent, "session", ""))
        except OSError as exc:
            LOG.warning("Could not save the conversation: %s", exc)
        else:
            self.saved_messages = len(messages)

    def _send(self, text: str, attachments: list[str] | tuple[str, ...] = ()) -> None:
        if self.agent is None or self.agent.busy:
            return
        # Reports the agent has not had yet (e.g. cancelled jobs) go first.
        reports = self._reports()
        message = self._take_reports(reports) + USER_TEXT + text if reports else text
        self.chat.add_user(text, attachments)
        self.chat.set_busy(True, "Working…")
        self.agent.busy = True  # before the thread starts: no second send meanwhile
        threading.Thread(target=self.agent.send, args=(message, attachments), daemon=True).start()

    def _retry(self) -> None:
        """Repeat the failed request (maybe with another model)."""
        if self.agent is None or self.agent.busy:
            return
        self.chat.clear_actions()
        self.chat.set_busy(True, "Working…")
        self.agent.busy = True
        threading.Thread(target=self.agent.retry, daemon=True).start()

    def _stop(self) -> None:
        if self.agent is not None and self.agent.busy:
            self.chat.set_busy(True, "Stopping…")
            self.agent.cancel()

    def _on_agent_event(self, kind: str, data) -> bool:
        if self.closed or self.chat is None:
            return False
        if kind == "text":
            self.chat.append_text(data)
            self.chat.set_busy(True, "Writing…")
        elif kind == "thinking":
            self.chat.set_busy(True, "Thinking…")
        elif kind == "tool":
            self.chat.add_note(data)
            self.chat.set_busy(True, "Working…")
        elif kind == "error":
            self.chat.add_note(data, "dialog-error-symbolic", "request-error",
                               (("Retry", self._retry), ("Change model…", self._choose_model)))
            self.chat.set_busy(False)
        elif kind == "stopped":
            self.chat.add_note("Stopped.", "media-playback-stop-symbolic")
            self.chat.set_busy(False)
        elif kind == "done":
            self.chat.set_busy(False)
        if kind in ("done", "error", "stopped"):
            self._save_history()
        if kind == "done":
            GLib.idle_add(self._deliver_jobs)  # a job may have ended during the turn
        if not self.chat.busy:
            self.chat.focus_input()
        return False

    def _execute(self, purpose: str, code: str, cancelled: threading.Event, target: str = "chemotion") -> str:
        """The agent's execute_python/execute_jupyter: a card in the result
        panel; returns the text for the model once the user ran or rejected it."""
        # Runs in the agent's thread: GTK only through GLib.idle_add.
        pending = PendingRequest(purpose, code, target)
        GLib.idle_add(self._show_request, pending)
        while not pending.done.wait(0.2):
            if cancelled.is_set():
                GLib.idle_add(pending.abandoned)
                return "Not run: the user stopped the assistant."
        GLib.idle_add(lambda: self.chat is not None and self.chat.busy and self.chat.set_busy(True, "Working…"))
        if pending.reply is None:
            return "Not run: the user stopped the assistant."
        return pending.reply["text"]

    def _show_request(self, pending: PendingRequest) -> bool:
        if self.closed or self.results is None:
            pending.answer(False, "ChemAssistent has no open conversation; the code did not run.")
            return False
        if pending.target in ("jupyter", "job"):
            if self.jupyter is None:
                pending.answer(False, "JupyterLab is not configured; the code did not run.")
                return False
            bridge = self.jupyter
            if pending.target == "job":
                jobs, conversation = self.app.jobs, self.conversation_id
                pending.new_execution = lambda code: JobExecution(code, pending.purpose, bridge, jobs, conversation)
            else:
                pending.new_execution = lambda code: JupyterExecution(code, pending.purpose, bridge)
        else:
            exchange = self.exchange
            pending.new_execution = lambda code: Execution(code, exchange)
        # Decide about auto-run here on the main loop, with the current mode.
        if self.auto_run == "always":
            pending.auto = "always"
        elif self.auto_run == "readonly" and pending.target != "chemotion":
            pending.auto_reason = "JupyterLab code is never checked for read-only"
        elif self.auto_run == "readonly":
            ok, reason = looks_read_only(pending.code)
            if ok:
                pending.auto = "readonly"
            else:
                pending.auto_reason = reason
        card = self.results.add_request(pending)
        self.chat.add_note(f"#{card.number} {pending.purpose or 'Code'}"
                           + {"jupyter": " · JupyterLab", "job": " · JupyterLab job"}.get(pending.target, "")
                           + (" · auto-run" if pending.auto else ""), "system-run-symbolic")
        self.chat.set_busy(True, "Running code automatically…" if pending.auto else
                           "Waiting for you to Run or Reject the code on the right…")
        return False

    def _on_header_press(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.unmaximize() if self.is_maximized() else self.maximize()
        else:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return True

    def _grow(self) -> None:
        """Animate from the prompt to the work area, then maximize (on
        Wayland, which ignores client positioning, just maximize)."""
        area = work_area()
        x0, y0 = self.get_position()
        w0, h0 = self.get_size()
        self.set_resizable(True)
        animate = "Wayland" not in type(self.get_display()).__name__  # GdkWaylandDisplay
        step = 0 if animate else GROW_STEPS - 1

        def tick() -> bool:
            nonlocal step
            step += 1
            if animate:
                t = 1 - (1 - step / GROW_STEPS) ** 3  # ease out
                self.move(round(x0 + (area.x - x0) * t), round(y0 + (area.y - y0) * t))
                self.resize(max(1, round(w0 + (area.width - w0) * t)), max(1, round(h0 + (area.height - h0) * t)))
            if step < GROW_STEPS:
                return True
            self.maximize()
            self.paned.set_position(int(area.width * CHAT_SHARE))
            self.present_now()
            self.chat.focus_input()
            return False

        GLib.timeout_add(GROW_STEP_MS, tick)

    def _on_key(self, _widget, event) -> bool:
        if event.keyval != Gdk.KEY_Escape:
            return False
        if not self.expanded:
            self.close_conversation()
            return True
        if self.agent is not None and self.agent.busy:
            self._stop()
            return True
        return False

    def close_conversation(self) -> None:
        self.closed = True
        if self.agent is not None:
            # A turn still running is saved as far as it got; restore() answers its open calls.
            self._save_history()
            self.agent.cancel()
        if self.results is not None:
            self.results.close()
        self.app.window_closed(self)
        self.destroy()


# --- the app -------------------------------------------------------------------
def load_indicator_module():
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            return importlib.import_module(f"gi.repository.{name}")
        except (ValueError, ImportError):
            continue
    return None


class ChemAssistentApp:
    def __init__(self, show_prompt: bool) -> None:
        self.window: AssistantWindow | None = None
        self.server = RequestServer(self)
        self.show_prompt = show_prompt
        self.indicator = None
        self.last_hotkey = 0.0
        # GET /models for the conversations' model menu: None while loading or
        # after an error; loaded once per AI endpoint (ai_url, ai_token).
        self.models: list[str] | None = None
        self.models_error = ""
        self.models_key: tuple[str, str] | None = None
        # Background jobs (jobs.py); the watcher reports through jobs_changed().
        self.jobs = Jobs(lambda: GLib.idle_add(self.jobs_changed))
        self.menu: Gtk.Menu | None = None
        self.job_items: list[Gtk.MenuItem] = []  # the menu's entries for jobs
        self.notifications: list = []  # shown notifications (their actions need them alive)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)

    def _build_menu(self) -> tuple[Gtk.Menu, Gtk.MenuItem]:
        menu = Gtk.Menu()
        ask = Gtk.MenuItem(label="Ask Chemotion…")
        ask.connect("activate", lambda *_: self.open_prompt())
        menu.append(ask)
        new = Gtk.MenuItem(label="New conversation")
        new.connect("activate", lambda *_: self.new_conversation())
        menu.append(new)
        menu.append(Gtk.SeparatorMenuItem())
        settings = Gtk.MenuItem(label="Settings…")
        settings.connect("activate", lambda *_: open_settings())
        menu.append(settings)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self.menu = menu
        self._update_job_items()
        return menu, ask

    def _build_indicator(self) -> None:
        if WINDOWS:
            self._build_status_icon()
            return
        module = load_indicator_module()
        if module is None:
            LOG.warning("No AppIndicator library (gir1.2-ayatanaappindicator3-0.1); no top-bar icon")
            self.show_prompt = True
            return
        indicator = module.Indicator.new(APP_ID, PANEL_ICON, module.IndicatorCategory.APPLICATION_STATUS)
        # Finds chem-assistent-panel.svg next to this file, installed or not.
        indicator.set_icon_theme_path(str(HERE))
        indicator.set_icon_full(PANEL_ICON, "Chemotion")
        indicator.set_title("ChemAssistent")
        indicator.set_status(module.IndicatorStatus.ACTIVE)
        menu, ask = self._build_menu()
        indicator.set_menu(menu)
        # A middle click on the icon opens the prompt directly.
        indicator.set_secondary_activate_target(ask)
        self.indicator = indicator

    def _build_status_icon(self) -> None:
        """Windows: a tray icon; a left click opens the prompt, a right click the menu."""
        icon = Gtk.StatusIcon()
        try:
            from gi.repository import GdkPixbuf  # type: ignore

            icon.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON_FILE), 32, 32))
        except GLib.Error:
            icon.set_from_icon_name("dialog-question")
        icon.set_tooltip_text("ChemAssistent: ask Chemotion")
        icon.set_title("ChemAssistent")
        menu, _ask = self._build_menu()
        icon.connect("activate", lambda *_: self.open_prompt())
        icon.connect("popup-menu", lambda _icon, button, when: menu.popup(None, None, None, None, button, when))
        icon.set_visible(True)
        self.menu = menu  # keep a reference
        self.indicator = icon

    def open_prompt(self) -> bool:
        """Show the running conversation, or a new prompt."""
        if self.window is not None:
            if self.window.expanded:
                self.window.present_now()
            else:
                self.window.open_prompt()
            return False
        self.window = AssistantWindow(self)
        self.window.open_prompt()
        return False

    def load_models(self, config: Config, again: bool = False) -> None:
        """Load the model list in a thread (the KI-Toolbox needs ~15 s for it).
        CLI agents have a local list (cli_agent.cli_models())."""
        if config.agent in CLI_AGENTS:
            self.models_key, self.models, self.models_error = (config.agent, ""), cli_models(config.agent), ""
            if self.window is not None:
                self.window.models_changed()
            return
        key = (config.ai_url, config.ai_token)
        if key == self.models_key and not (again and self.models_error):
            return  # loaded, or loading
        self.models_key, self.models, self.models_error = key, None, ""
        if self.window is not None:
            self.window.models_changed()
        threading.Thread(target=self._models_worker, args=(config, key), daemon=True).start()

    def _models_worker(self, config: Config, key: tuple[str, str]) -> None:
        try:
            models, error = list_models(config), ""
        except AgentError as exc:
            models, error = None, str(exc)
        except Exception as exc:
            models, error = None, f"{type(exc).__name__}: {exc}"
        GLib.idle_add(self._models_loaded, key, models, error)

    def _models_loaded(self, key: tuple[str, str], models: list[str] | None, error: str) -> bool:
        if key == self.models_key:
            self.models, self.models_error = models, error
            if self.window is not None:
                self.window.models_changed()
        return False

    # --- background jobs -------------------------------------------------------------
    def jobs_changed(self) -> bool:
        """A job started, ended or cannot be checked (jobs.Jobs.on_change)."""
        window = self.window if self.window is not None and not self.window.closed else None
        open_id = window.conversation_id if window is not None else None
        for job in self.jobs.undelivered():
            if job["conversation"] != open_id and history.load(job["conversation"]) is None:
                self.jobs.mark_delivered([job])  # its conversation was deleted
        notify = [job for job in self.jobs.undelivered()
                  if job.get("report") and job.get("wake", True) and not job.get("notified")]
        for job in notify:
            self.jobs.update(job["id"], notified=True)
        if window is not None:
            window.jobs_changed()  # delivers this conversation's reports
        for job in notify:
            if job["conversation"] != open_id or not window.is_active():
                self._notify_job(job)
        self._update_job_items()
        return False

    def _notify_job(self, job: dict) -> None:
        if Notify is None:
            return  # the menu entry is there anyway
        verb = {"done": "finished", "failed": "failed", "lost": "stopped"}.get(job["state"], job["state"])
        conversation = job["conversation"]
        try:
            if not Notify.is_initted():
                Notify.init(APP_ID)
            note = Notify.Notification.new(f"Background job {verb}", job.get("purpose") or job["id"], str(ICON_FILE))
            note.add_action("default", "Continue", lambda *_: self.open_conversation(conversation))
            note.add_action("continue", "Continue the conversation", lambda *_: self.open_conversation(conversation))
            note.connect("closed", lambda closed: closed in self.notifications and self.notifications.remove(closed))
            self.notifications.append(note)
            note.show()
        except GLib.Error as exc:
            LOG.warning("Could not show a notification: %s", exc)

    def _update_job_items(self) -> None:
        """Menu entries: finished jobs to continue, and how many still run."""
        if self.menu is None:
            return
        for item in self.job_items:
            self.menu.remove(item)
        items: list[Gtk.MenuItem] = []
        jobs = self.jobs.list()
        for job in jobs:
            if job["state"] in FINISHED and not job.get("delivered") and job.get("wake", True) and job.get("report"):
                purpose = job.get("purpose") or job["id"]
                purpose = purpose if len(purpose) <= 50 else purpose[:49] + "…"
                item = Gtk.MenuItem(label=f"Job {job['state']}: {purpose} – continue…")
                item.connect("activate", lambda _item, conversation=job["conversation"]:
                             self.open_conversation(conversation))
                items.append(item)
        running = sum(job["state"] in ACTIVE for job in jobs)
        if running:
            item = Gtk.MenuItem(label=f"{running} background job{'s' * (running != 1)} running")
            item.set_sensitive(False)
            items.append(item)
        if items:
            items.insert(0, Gtk.SeparatorMenuItem())
        for index, item in enumerate(items):
            self.menu.insert(item, 2 + index)  # after "New conversation"
            item.show()
        self.job_items = items

    def open_conversation(self, conversation_id: str) -> bool:
        """Continue a conversation from the history (a job of it ended)."""
        window = self.window if self.window is not None and not self.window.closed else None
        if window is not None and window.conversation_id == conversation_id:
            window.present_now()
            return False
        if window is not None and window.agent is not None and (window.agent.busy or window.results.waiting()):
            # Do not end a conversation that is working or waiting for the user.
            window.present_now()
            window.chat.add_note("A background job of another conversation ended.", "preferences-system-time-symbolic",
                                 actions=(("Switch to it", lambda: self._switch_to(conversation_id)),))
            return False
        self._switch_to(conversation_id)
        return False

    def _switch_to(self, conversation_id: str) -> None:
        if self.window is not None:
            self.window.close_conversation()
        self.window = AssistantWindow(self)
        self.window.open_prompt()
        self.window.resume(conversation_id)

    def execute_for_bridge(self, session: str, purpose: str, code: str) -> str:
        """A CLI agent's execute_python (mcp_bridge.py; in a server thread):
        the same card as the built-in agent's, if `session` belongs to the
        open conversation."""
        window = self.window
        agent = window.agent if window is not None else None
        if (not isinstance(agent, CliAgent) or window.closed
                or not secrets.compare_digest(agent.session_token, session)):
            return "Not run: this ChemAssistent conversation is closed."
        if not code.strip():
            return "No code given."
        return window._execute(purpose, code, agent.cancelled)

    def new_conversation(self) -> None:
        if self.window is not None:
            self.window.close_conversation()
        self.open_prompt()

    def window_closed(self, window: AssistantWindow) -> None:
        if self.window is window:
            self.window = None

    def hotkey_pressed(self) -> bool:
        now = time.monotonic()
        if now - self.last_hotkey <= HOTKEY_DOUBLE_S:
            self.last_hotkey = 0.0
            self.open_prompt()
        else:
            self.last_hotkey = now
        return False

    def _register_hotkey(self) -> None:
        """GNOME: (re)write the custom shortcut, so it runs this copy's hotkey.py.
        Windows: RegisterHotKey (a change in the settings needs a restart)."""
        accelerator = str(read_raw_config().get("hotkey") or "").strip()
        if not WINDOWS:
            try:
                problem = hotkey.register_gnome(accelerator)
            except Exception as exc:  # dconf/D-Bus trouble must not stop the app
                problem = f"Could not set the GNOME shortcut: {exc}"
            if problem:
                LOG.warning(problem)
            return
        if not accelerator:
            return
        keyval, modifiers = Gtk.accelerator_parse(accelerator)
        key = hotkey.virtual_key(Gdk.keyval_name(keyval) or "") if keyval else None
        if key is None:
            LOG.warning("Hotkey %r is not supported on Windows (use space, a-z, 0-9 or F1-F12)", accelerator)
            return
        flags = 0
        for mask, flag in ((Gdk.ModifierType.CONTROL_MASK, hotkey.MOD_CONTROL),
                           (Gdk.ModifierType.SHIFT_MASK, hotkey.MOD_SHIFT),
                           (Gdk.ModifierType.MOD1_MASK, hotkey.MOD_ALT),
                           (Gdk.ModifierType.SUPER_MASK, hotkey.MOD_WIN)):
            if modifiers & mask:
                flags |= flag
        hotkey.listen_windows(flags, key, lambda: GLib.idle_add(self.hotkey_pressed))

    def quit(self) -> bool:
        if self.window is not None:
            self.window.close_conversation()
        self.server.close()
        Gtk.main_quit()
        return False

    def run(self) -> None:
        self.server.start()
        self.jobs.start()
        self._build_indicator()
        self._register_hotkey()
        if signal_add is not None:
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal_add(GLib.PRIORITY_DEFAULT, signum, self.quit)
        if self.show_prompt:
            GLib.idle_add(self.open_prompt)
        LOG.info("ChemAssistent running; config %s, workspace %s", CONFIG_FILE, WORKSPACE_DIR)
        Gtk.main()


def main() -> None:
    log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if WINDOWS:
        # pythonw has no console and there is no journal: log to a file.
        from logging.handlers import RotatingFileHandler

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(STATE_DIR / "app.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format=log_format, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, format=log_format)
    show_prompt = "--show" in sys.argv[1:]
    if RequestServer.signal_running_instance():
        print("ChemAssistent is already running; it opens the prompt.")
        return
    GLib.set_prgname(APP_ID)
    if WINDOWS:
        app_control.write_pid()
    try:
        ChemAssistentApp(show_prompt).run()
    except Exception:
        LOG.exception("ChemAssistent failed")  # on Windows nobody sees the output
        raise
    finally:
        if WINDOWS:
            app_control.remove_pid()


if __name__ == "__main__":
    main()
