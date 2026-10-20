"""ChemAssistent: a Chemotion icon in the top bar (the tray on Windows) that
opens an opencode chat with the Chemotion ELN. Code the agent wants to run is
shown on the right and runs only after the user confirmed it."""
from __future__ import annotations

import importlib
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

# Mutter ignores client window positioning on Wayland; the prompt must be
# centered and then grow to the work area, so run through XWayland (as
# vikunja-popups does). Must be set before Gtk is imported.
if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"

from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

try:
    gi.require_version("Vte", "2.91")
    from gi.repository import Vte  # type: ignore  # noqa: E402
except (ValueError, ImportError):
    Vte = None  # Windows: opencode runs in its own console (console_win.py)

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

import app_control  # noqa: E402
import ipc  # noqa: E402
import workspace  # noqa: E402
from config import CONFIG_FILE, STATE_DIR, WINDOWS, WORKSPACE_DIR, load_config  # noqa: E402
from opencode_models import NO_WINDOW, find_opencode  # noqa: E402

if WINDOWS:
    import console_win  # noqa: E402


LOG = logging.getLogger("chem-assistent")

APP_ID = "chem-assistent"
HERE = Path(__file__).resolve().parent
ICON_FILE = HERE / "chem-assistent.svg"
PANEL_ICON = "chem-assistent-panel"  # chem-assistent-panel.svg next to this file
RUNNER = HERE / "chemotion_runner.py"
SETTINGS = HERE / "settings.py"

# The prompt is typed into the TUI once its footer shows our agent (sending it
# earlier lands in opencode's fallback agent and model); give up waiting after
# READY_TIMEOUT_S and send it anyway.
READY_POLL_MS = 200
READY_TIMEOUT_S = 30
GROW_STEPS = 14
GROW_STEP_MS = 16
# Windows: the share of the work area the result window takes on the right;
# the opencode console goes to the left of it.
RESULTS_SHARE = 0.45
CONSOLE_PLACE_TIMEOUT_S = 10
# What the agent gets back from one snippet; the panel shows everything.
MAX_ANSWER_CHARS = 20000
MAX_TABLE_COLUMNS = 12
MAX_CELL_CHARS = 120
MAX_REQUEST_BYTES = 10 * 1024 * 1024


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
    subprocess.Popen([sys.executable, str(SETTINGS)], start_new_session=True, creationflags=NO_WINDOW)


# --- requests from the agent -------------------------------------------------
class PendingRequest:
    """One execute_python call, waiting in a server thread for the user."""

    def __init__(self, purpose: str, code: str) -> None:
        self.purpose = purpose
        self.code = code
        self.reply: dict | None = None
        self.done = threading.Event()
        # Set by the card; called on the main loop when the agent stopped waiting.
        self.on_abandoned = None

    def answer(self, ok: bool, text: str) -> None:
        if not self.done.is_set():
            self.reply = {"ok": ok, "text": text}
            self.done.set()

    def abandoned(self) -> bool:
        self.done.set()
        if self.on_abandoned is not None:
            self.on_abandoned()
        return False


def peer_hung_up(conn: socket.socket) -> bool:
    # The MCP server sends nothing after its request, so a readable socket
    # means EOF or an error: it closed the connection because opencode
    # cancelled the tool call or ended.
    readable, _, _ = select.select([conn], [], [], 0)
    if not readable:
        return False
    try:
        return conn.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class RequestServer:
    """The app's end of ipc.py, for mcp_server.py (execute requests) and for
    a second start of the app (which asks this one to show the prompt)."""

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
            data = b""
            while len(data) < MAX_REQUEST_BYTES:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break
            try:
                request = json.loads(data.decode("utf-8"))
            except ValueError:
                return
            if not isinstance(request, dict) or not ipc.token_ok(request, self.listener.token):
                return
            if request.get("type") == "show":
                GLib.idle_add(self.app.open_prompt)
                return
            if request.get("type") != "execute":
                return
            pending = PendingRequest(str(request.get("purpose") or ""), str(request.get("code") or ""))
            GLib.idle_add(self.app.handle_request, pending)
            while not pending.done.wait(0.5):
                if peer_hung_up(conn):
                    GLib.idle_add(pending.abandoned)
                    return
            if pending.reply is not None:
                try:
                    conn.sendall(json.dumps(pending.reply).encode("utf-8"))
                except OSError:
                    pass


# --- running confirmed code ---------------------------------------------------
class Execution:
    """Runs one snippet in chemotion_runner.py; cancel() kills it."""

    def __init__(self, code: str) -> None:
        self.code = code
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
        }
        try:
            self.process = subprocess.Popen(
                [workspace.console_python(), str(RUNNER)],
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
            return json.loads(stdout)
        except ValueError:
            detail = (stderr or stdout or "").strip()[-3000:]
            return {"ok": False, "stdout": "", "result": None,
                    "error": f"The runner ended unexpectedly (exit code {self.process.returncode}).\n{detail}"}

    def cancel(self) -> None:
        self.cancelled = True
        if self.process is not None and self.process.poll() is None:
            self.process.kill()


# --- result widgets ----------------------------------------------------------
def table_widget(columns: list[str], rows: list[list[str]]) -> Gtk.Widget:
    store = Gtk.ListStore(*([str] * len(columns)))
    for row in rows:
        store.append(row)
    view = Gtk.TreeView(model=store)
    view.set_enable_search(False)
    view.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
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
    """A table for lists of dicts and dicts, text for everything else."""
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        columns: list[str] = []
        for item in value:
            for key in item:
                if key not in columns and len(columns) < MAX_TABLE_COLUMNS:
                    columns.append(key)
        rows = [[cell_text(item.get(key)) for key in columns] for item in value]
        return table_widget(columns, rows)
    if isinstance(value, dict) and value:
        return table_widget(["Key", "Value"], [[str(key), cell_text(item)] for key, item in value.items()])
    if isinstance(value, list) and value and not any(isinstance(item, (dict, list)) for item in value):
        return table_widget(["Value"], [[cell_text(item)] for item in value])
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text_block(text)


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
    """One snippet: editable until the user runs or rejects it, then its output."""

    def __init__(self, number: int, pending: PendingRequest, on_state_change) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pending = pending
        self.on_state_change = on_state_change
        self.execution: Execution | None = None
        self.state = "waiting"
        pending.on_abandoned = self._on_abandoned
        self.get_style_context().add_class("request-card")
        self.get_style_context().add_class("waiting")

        head = Gtk.Box(spacing=8)
        head.pack_start(label(f"#{number}", "dim", wrap=False), False, False, 0)
        purpose = label(pending.purpose or "Run code against Chemotion", "request-purpose")
        head.pack_start(purpose, True, True, 0)
        head.pack_end(label(time.strftime("%H:%M:%S"), "dim", wrap=False), False, False, 0)
        self.pack_start(head, False, False, 0)

        editor, self.view = code_editor(pending.code)
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
        self.run_button.set_tooltip_text("Run this code against Chemotion (Ctrl+Enter in the code)")
        self.run_button.connect("clicked", lambda *_: self.run())
        self.actions.pack_start(self.run_button, False, False, 0)
        self.pack_start(self.actions, False, False, 0)

        status_row = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        status_row.pack_start(self.spinner, False, False, 0)
        self.status = label("Waiting for your confirmation. Nothing runs before you click Run.", "request-status")
        status_row.pack_start(self.status, True, True, 0)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.connect("clicked", lambda *_: self.execution and self.execution.cancel())
        status_row.pack_end(self.cancel_button, False, False, 0)
        self.pack_start(status_row, False, False, 0)

        self.output = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.pack_start(self.output, False, False, 0)

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
        self.execution = Execution(code)
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

        if stdout.strip():
            self.output.pack_start(label("Output", "dim"), False, False, 0)
            self.output.pack_start(text_block(stdout.rstrip()), False, False, 0)
        if error:
            self.output.pack_start(text_block(error.rstrip(), "request-error"), False, False, 0)
            self.output.get_children()[-1].get_style_context().add_class("request-output")
        if ok and result is not None:
            row = Gtk.Box(spacing=8)
            row.pack_start(label("Result", "dim"), True, True, 0)
            copy = Gtk.Button(label="Copy JSON")
            copy.set_relief(Gtk.ReliefStyle.NONE)
            copy.connect("clicked", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(
                json.dumps(result, ensure_ascii=False, indent=2), -1))
            row.pack_end(copy, False, False, 0)
            self.output.pack_start(row, False, False, 0)
            self.output.pack_start(result_widget(result), False, False, 0)
        self.output.show_all()

        abandoned = self.pending.done.is_set() and self.pending.reply is None
        note = " The assistant had stopped waiting, so it did not get this output." if abandoned else ""
        if ok:
            self._set_state("done", f"Ran in {elapsed:.1f} s.{note}")
        else:
            self._set_state("failed", f"Failed after {elapsed:.1f} s.{note}")
        self.pending.answer(ok, self._answer_text(code, ok, stdout, error, result))
        return False

    def _answer_text(self, code: str, ok: bool, stdout: str, error: str, result) -> str:
        parts = []
        if code != self.pending.code:
            parts.append(f"The user edited the code before running it. This code ran:\n```python\n{code}\n```")
        parts.append("The code ran successfully." if ok else f"The code failed:\n{error}")
        if stdout.strip():
            parts.append(f"Printed output:\n{stdout}")
        if ok:
            if result is None:
                parts.append("`result` was not set.")
            else:
                parts.append("result = " + json.dumps(result, ensure_ascii=False, indent=1, default=str))
        parts.append("The user sees this output in the result panel.")
        return clip("\n\n".join(parts))

    def _on_abandoned(self) -> None:
        if self.state == "waiting":
            self._lock()
            self._set_state("failed", "Cancelled by the assistant before it ran.")

    def close(self) -> None:
        """The conversation ends: answer the agent if it still waits."""
        if self.state == "waiting":
            self.reject("The user closed the conversation.")
        elif self.execution is not None:
            self.execution.cancel()


class ResultsPanel(Gtk.Box):
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
        self.pack_start(self.scroller, True, True, 0)

        # Stay at the bottom while new cards and output arrive, unless the
        # user scrolled up. "changed" fires once the new content is allocated.
        self.follow = True
        adjustment = self.scroller.get_vadjustment()
        adjustment.connect("changed", self._on_content_changed)
        adjustment.connect("value-changed", self._on_scrolled)

    def add_request(self, pending: PendingRequest) -> RequestCard:
        self.empty.hide()
        card = RequestCard(len(self.cards) + 1, pending, self._update_counter)
        self.cards.append(card)
        self.list.pack_start(card, False, False, 0)
        card.show_all()
        self._update_counter()
        self.follow = True
        card.focus_code()
        return card

    def _on_content_changed(self, adjustment: Gtk.Adjustment) -> None:
        if self.follow:
            adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())

    def _on_scrolled(self, adjustment: Gtk.Adjustment) -> None:
        self.follow = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 20

    def waiting(self) -> int:
        return sum(card.state == "waiting" for card in self.cards)

    def _update_counter(self) -> None:
        count = self.waiting()
        self.counter.set_text(f"· {count} waiting for confirmation" if count else "")
        window = self.get_toplevel()
        if isinstance(window, Gtk.Window):
            window.set_urgency_hint(bool(count) and not window.is_active())

    def close(self) -> None:
        for card in self.cards:
            card.close()


# --- the window ----------------------------------------------------------------
class AssistantWindow(Gtk.Window):
    """Starts as a centered prompt; grows to the work area with the opencode
    terminal on the left and the results on the right. Without VTE (Windows)
    opencode runs in its own console on the left and this window takes only
    the right part of the work area, with the results."""

    def __init__(self, app: ChemAssistentApp) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL, title="ChemAssistent")
        self.app = app
        self.terminal = None  # Vte.Terminal
        self.paned: Gtk.Paned | None = None
        self.console: subprocess.Popen | None = None  # Windows
        self.results: ResultsPanel | None = None
        self.child_pid: int | None = None
        self.expanded = False

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

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON_FILE), 28, 28)
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

        outer = Gtk.Box()
        outer.pack_start(box, True, True, 0)
        return outer

    def open_prompt(self) -> None:
        try:
            config = load_config()
            model = config.ai_model or "opencode's default model"
            self.hint.set_text(f"Enter starts the assistant · Esc closes · Model: {model}")
            self.set_default_size(config.prompt_width, -1)
            self.entry.set_width_chars(max(30, config.prompt_width // 11))
        except ValueError:
            self.hint.set_text("Enter starts the assistant · Esc closes")
        self.error.hide()
        self.show_all()
        width, height = self.get_size()
        area = work_area()
        self.move(area.x + (area.width - width) // 2, area.y + (area.height - height) // 3)
        self.present_now()
        self.entry.grab_focus()

    def present_now(self) -> None:
        self.show()
        self.deiconify()
        self.present_with_time(server_time(self))

    def show_error(self, text: str) -> None:
        self.error.set_text(text)
        self.error.show()
        self.entry.grab_focus()

    def _submit(self) -> None:
        text = self.entry.get_text().strip()
        if not text:
            return
        try:
            config = load_config()
        except ValueError as exc:
            self.show_error(f"{exc}\nOpen the settings with the gear button.")
            return
        opencode = find_opencode()
        if opencode is None:
            self.show_error("opencode was not found (looked in PATH and ~/.opencode/bin).")
            return
        if WINDOWS and opencode.lower().endswith((".cmd", ".bat")):
            # cmd.exe would interpret the prompt on the command line.
            self.show_error(f"{opencode} is a script, not opencode.exe. Run install.ps1 to install opencode.exe.")
            return
        if not workspace.reference_ready():
            self.show_error(f"The Chemotion API reference is missing in {WORKSPACE_DIR}. Run install.sh "
                            "(or setup_workspace.sh in the source tree).")
            return
        try:
            workspace.write_opencode_config(config, self.app.server.listener.environment())
        except OSError as exc:
            self.show_error(f"Could not write the opencode config: {exc}")
            return
        try:
            extra_env = workspace.tui_environment(config)
        except OSError as exc:
            self.show_error(f"Could not prepare the opencode state: {exc}")
            return
        self._start_conversation(opencode, text, config.ai_model, extra_env)

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

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON_FILE), 22, 22)
            header.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
        except GLib.Error:
            pass
        header.pack_start(label("ChemAssistent", "assistant-title", wrap=False), False, False, 0)
        header.pack_start(label(f"· {model or 'default model'}", "dim", wrap=False), False, False, 0)
        header.pack_end(icon_button("window-close-symbolic", "End the conversation and close", self.close_conversation),
                        False, False, 0)
        header.pack_end(icon_button("window-minimize-symbolic", "Minimize (the Chemotion icon brings it back)",
                                    self.iconify), False, False, 0)
        header.pack_end(icon_button("emblem-system-symbolic", "Settings", open_settings), False, False, 0)
        header.pack_end(icon_button("document-new-symbolic", "New conversation", self.app.new_conversation),
                        False, False, 0)
        main.pack_start(handle, False, False, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.ended_bar = Gtk.InfoBar()
        self.ended_bar.set_message_type(Gtk.MessageType.INFO)
        self.ended_label = label("opencode has ended.")
        self.ended_bar.get_content_area().add(self.ended_label)
        self.ended_bar.add_button("New conversation", 1)
        self.ended_bar.add_button("Close", 2)
        self.ended_bar.connect("response", self._on_ended_response)
        self.ended_bar.set_no_show_all(True)
        left.pack_start(self.ended_bar, False, False, 0)
        self.results = ResultsPanel()

        if Vte is None:
            left.pack_start(self.results, True, True, 0)
            main.pack_start(left, True, True, 0)
            return main

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        self.terminal = Vte.Terminal()
        self.terminal.set_size(20, 5)  # small request: the window animates from the prompt size
        self.terminal.set_scrollback_lines(10000)
        self.terminal.set_font(Pango.FontDescription("Monospace 11"))
        background = Gdk.RGBA()
        background.parse("#1e1f22")
        foreground = Gdk.RGBA()
        foreground.parse("#e6e6e6")
        self.terminal.set_colors(foreground, background, None)
        self.terminal.connect("key-press-event", self._on_terminal_key)
        self.terminal.connect("child-exited", self._on_child_exited)
        left.pack_start(self.terminal, True, True, 0)
        paned.pack1(left, True, False)
        paned.pack2(self.results, True, False)
        self.paned = paned
        main.pack_start(paned, True, True, 0)
        return main

    def _start_conversation(self, opencode: str, text: str, model: str, extra_env: dict[str, str]) -> None:
        self.stack.add_named(self._build_conversation(model), "conversation")
        self.stack.show_all()
        self.stack.set_visible_child_name("conversation")
        self.expanded = True
        self.set_keep_above(False)
        self.set_skip_taskbar_hint(False)
        self.set_skip_pager_hint(False)
        self.set_title(f"ChemAssistent – {text[:60]}")
        self.pending_prompt = text
        self._grow()

        env = {key: value for key, value in os.environ.items() if key != "GDK_BACKEND"}
        # opencode finds its project (and our agent) through $PWD.
        env["PWD"] = str(WORKSPACE_DIR)
        env.update(extra_env)
        argv = [opencode, "--standalone", str(WORKSPACE_DIR)]
        if self.terminal is None:
            self._start_console(argv, env, text)
            return
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        self.terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            str(WORKSPACE_DIR),
            argv,
            [f"{key}={value}" for key, value in env.items()],
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            self._on_spawned,
        )
        self.terminal.grab_focus()

    def _start_console(self, argv: list[str], env: dict[str, str], text: str) -> None:
        """Windows: opencode in its own console, moved left of this window.

        There is no terminal to type into, so the prompt goes on the command
        line. opencode may take it before it loaded the workspace config; then
        it lands in the "build" agent, which workspace.py makes a copy of ours,
        with the model from the private TUI state.
        """
        try:
            self.console = console_win.start([*argv, "--prompt", text], env, str(WORKSPACE_DIR))
        except OSError as exc:
            self._show_ended(f"Could not start opencode: {exc}")
            return
        self.child_pid = self.console.pid
        deadline = time.monotonic() + CONSOLE_PLACE_TIMEOUT_S

        def place() -> bool:
            if self.console is None:
                return False
            hwnd = console_win.console_window(self.console.pid)
            if hwnd:
                x, y, width, height = self._console_area()
                scale = self.get_scale_factor()  # GDK works in logical pixels
                if console_win.place(hwnd, x * scale, y * scale, width * scale, height * scale):
                    return False
            return time.monotonic() < deadline

        def watch() -> bool:
            if self.console is None:
                return False
            if self.console.poll() is None:
                return True
            self.console = None
            self._on_child_exited(None, 0)
            return False

        GLib.timeout_add(250, place)
        GLib.timeout_add_seconds(1, watch)

    def _target_area(self) -> tuple[int, int, int, int]:
        area = work_area()
        if self.terminal is not None:
            return area.x, area.y, area.width, area.height
        width = int(area.width * RESULTS_SHARE)
        return area.x + area.width - width, area.y, width, area.height

    def _console_area(self) -> tuple[int, int, int, int]:
        area = work_area()
        return area.x, area.y, area.width - int(area.width * RESULTS_SHARE), area.height

    def _on_header_press(self, _widget, event) -> bool:
        if event.button != 1:
            return False
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.unmaximize() if self.is_maximized() else self.maximize()
        else:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return True

    def _grow(self) -> None:
        """Animate from the prompt to the work area (its right part on
        Windows), then maximize (not on Windows)."""
        target_x, target_y, target_width, target_height = self._target_area()
        x0, y0 = self.get_position()
        w0, h0 = self.get_size()
        self.set_resizable(True)
        step = 0

        def tick() -> bool:
            nonlocal step
            step += 1
            t = 1 - (1 - step / GROW_STEPS) ** 3  # ease out
            self.move(round(x0 + (target_x - x0) * t), round(y0 + (target_y - y0) * t))
            self.resize(max(1, round(w0 + (target_width - w0) * t)), max(1, round(h0 + (target_height - h0) * t)))
            if step < GROW_STEPS:
                return True
            if self.terminal is not None:
                self.maximize()
                self.paned.set_position(int(target_width * 0.55))
                self.present_now()
                self.terminal.grab_focus()
            return False

        GLib.timeout_add(GROW_STEP_MS, tick)

    def _on_spawned(self, _terminal, pid: int, error, *_args) -> None:
        if error is not None:
            self._show_ended(f"Could not start opencode: {error.message}")
            return
        self.child_pid = pid
        self._send_when_ready(self.pending_prompt)

    def _send_when_ready(self, text: str) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S

        def poll() -> bool:
            if self.child_pid is None or self.terminal is None:
                return False
            screen = self.terminal.get_text_format(Vte.Format.TEXT) or ""
            if f"{workspace.AGENT_LABEL} ·" not in screen and time.monotonic() < deadline:
                return True
            self.terminal.paste_text(text)
            # Enter only after the TUI took the (bracketed) paste.
            GLib.timeout_add(150, lambda: self.terminal is not None and self.terminal.feed_child(b"\r") and False)
            return False

        GLib.timeout_add(READY_POLL_MS, poll)

    def _on_child_exited(self, _terminal, status: int) -> None:
        self.child_pid = None
        self._show_ended("opencode has ended.")
        if self.results is not None:
            self.results.close()

    def _show_ended(self, text: str) -> None:
        self.ended_label.set_text(text)
        # show_all() skips widgets with no_show_all, which keeps the bar
        # hidden until now.
        self.ended_bar.set_no_show_all(False)
        self.ended_bar.show_all()

    def _on_ended_response(self, _bar, response: int) -> None:
        if response == 1:
            self.app.new_conversation()
        else:
            self.close_conversation()

    def _on_terminal_key(self, _widget, event) -> bool:
        mask = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if event.state & mask == mask:
            key = Gdk.keyval_to_lower(event.keyval)
            if key == Gdk.KEY_c:
                self.terminal.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if key == Gdk.KEY_v:
                self.terminal.paste_clipboard()
                return True
        return False

    def _on_key(self, _widget, event) -> bool:
        # Escape belongs to opencode once the conversation runs.
        if event.keyval == Gdk.KEY_Escape and not self.expanded:
            self.close_conversation()
            return True
        return False

    def close_conversation(self) -> None:
        if self.results is not None:
            self.results.close()
        if self.child_pid is not None:
            if WINDOWS:
                self.console = None
                console_win.kill_tree(self.child_pid)
            else:
                try:
                    # The child leads its own session on the pty; opencode's
                    # server and the MCP server are in its process group.
                    os.killpg(self.child_pid, signal.SIGHUP)
                except OSError:
                    pass
            self.child_pid = None
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

    def new_conversation(self) -> None:
        if self.window is not None:
            self.window.close_conversation()
        self.open_prompt()

    def window_closed(self, window: AssistantWindow) -> None:
        if self.window is window:
            self.window = None

    def handle_request(self, pending: PendingRequest) -> bool:
        window = self.window
        if window is None or window.results is None:
            pending.answer(False, "ChemAssistent has no open conversation; the code did not run.")
            return False
        window.results.add_request(pending)
        return False

    def quit(self) -> bool:
        if self.window is not None:
            self.window.close_conversation()
        self.server.close()
        Gtk.main_quit()
        return False

    def run(self) -> None:
        self.server.start()
        self._build_indicator()
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
        # MSYS2's DLLs (libmagic for chemotion_api in the runner) are found
        # through PATH, which a Start-menu shortcut does not extend.
        dirs = [str(Path(sys.executable).parent), str(Path(sys.base_prefix) / "bin")]
        os.environ["PATH"] = os.pathsep.join([*dirs, os.environ.get("PATH", "")])
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
