from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

import api_versions  # noqa: E402
import app_control  # noqa: E402
import history  # noqa: E402
import hotkey  # noqa: E402
import issues  # noqa: E402
import lessons  # noqa: E402
from agent import AgentError, list_models  # noqa: E402
from cli_agent import check_command, cli_models  # noqa: E402
from config import (  # noqa: E402
    AGENT_KEYS, AGENT_LABELS, AGENTS, AI_TOKEN_ENV, AUTO_RUN_LABELS, AUTO_RUN_MODES, CLI_AGENTS,
    CONFIG_FILE, DEFAULT_JUPYTER_FOLDER, EXAMPLE, KI_TOOLBOX_URL, OLLAMA_URL, Config, agent_of, ai_problem,
    ai_token, config_problem, jupyter_folder, jupyter_problem, WINDOWS, read_raw_config, save_raw_config,
)
from jupyter import JupyterClient, JupyterError, split_url  # noqa: E402


PRGNAME = "chem-assistent-settings"
ICON_FILE = Path(__file__).with_name("chem-assistent.svg")
SETUP_WORKSPACE = Path(__file__).with_name("setup_workspace.sh")
# Fetch, pip install and the API reference of another chemotion_api version.
SETUP_TIMEOUT_S = 900

# What the settings say below the agent's fields.
AGENT_NOTES = {
    "api": f"Any OpenAI-compatible API. KIT KI-Toolbox: create an API key at {KI_TOOLBOX_URL.split('/api')[0]}.",
    "ollama": "A local Ollama (ollama serve); “Load models” lists the models you pulled. "
              "Pick one that supports tool calls.",
    "claude": "Uses your Claude Code login (run claude once in a terminal to sign in). Its own tools "
              "are switched off: like the built-in agent it reads the workspace and the attachments, "
              "and its code runs only through the result panel. Empty model: Claude Code's default.",
    "codex": "Uses your Codex login (codex login). Its shell and web tools are switched off: like the "
             "built-in agent it reads the workspace and the attachments, and its code runs only "
             "through the result panel. Empty model: Codex's default.",
}

# key, label, lower bound, upper bound (the same lower bounds load_config() clamps to)
NUMBER_FIELDS = (
    ("execution_timeout", "Max. run time per snippet (seconds)", 10, 86400),
    ("prompt_width", "Prompt width", 360, 4000),
)


class SettingsWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="ChemAssistent Settings")
        self.set_border_width(18)
        self.set_default_size(560, -1)
        try:
            self.set_icon_from_file(str(ICON_FILE))
        except GLib.Error:
            pass
        self.connect("destroy", Gtk.main_quit)

        self.data = read_raw_config()
        self.testing = False

        # One tab per topic: the whole form does not fit on small screens.
        self.notebook = Gtk.Notebook()
        grid, row = self._page("Chemotion"), 0

        self.url = Gtk.Entry(hexpand=True)
        self.url.set_placeholder_text(EXAMPLE["url"])
        self.url.set_text(str(self.data.get("url", "")))
        row = self._add_row(grid, row, "Chemotion URL", self.url)

        self.user = Gtk.Entry(hexpand=True)
        self.user.set_placeholder_text("Abbreviation or e-mail")
        self.user.set_text(str(self.data.get("user", "")))
        row = self._add_row(grid, row, "User", self.user)

        self.password = self._secret_entry(str(self.data.get("password", "")))
        row = self._add_row(grid, row, "Password", self.password)

        if os.environ.get("CHEMOTION_PASSWORD"):
            note = Gtk.Label(
                label="CHEMOTION_PASSWORD is set in the environment and overrides this password.",
                xalign=0,
                wrap=True,
            )
            note.get_style_context().add_class("dim-label")
            grid.attach(note, 1, row, 1, 1)
            row += 1

        self.verify_tls = Gtk.Switch(halign=Gtk.Align.START)
        self.verify_tls.set_active(bool(self.data.get("verify_tls", True)))
        row = self._add_row(grid, row, "Verify TLS certificate", self.verify_tls)

        self.test_button = Gtk.Button(label="Test connection", halign=Gtk.Align.START)
        self.test_button.set_tooltip_text("Log in to Chemotion with these settings")
        self.test_button.connect("clicked", self._on_test)
        grid.attach(self.test_button, 1, row, 1, 1)
        row += 1

        # The chemotion_api release in the workspace (setup_workspace.sh, a
        # bash script); the Windows bundle comes with its own.
        installed = api_versions.installed_label()
        if WINDOWS:
            version_label = Gtk.Label(label=installed or "unknown", xalign=0, selectable=True)
            row = self._add_row(grid, row, "chemotion_api version", version_label)
        else:
            self.api_version = Gtk.ComboBoxText(hexpand=True)
            self.api_version.connect("changed", lambda *_: self._update_api_button())
            self.api_install_button = Gtk.Button(label="Install", sensitive=False)
            self.api_install_button.set_tooltip_text(
                "Fetch this version (or, in developer mode, the head of this branch) of chemotion_api, "
                "install it and rebuild the API reference for the assistant. Applies to the next snippet "
                "and the next conversation.")
            self.api_install_button.connect("clicked", self._on_install_api)
            version_box = Gtk.Box(spacing=8)
            version_box.pack_start(self.api_version, True, True, 0)
            version_box.pack_start(self.api_install_button, False, False, 0)
            row = self._add_row(grid, row, "chemotion_api version", version_box)
            self.api_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
            self.api_note.get_style_context().add_class("dim-label")
            grid.attach(self.api_note, 1, row, 1, 1)
            row += 1
            self.api_busy, self.api_tags, self.api_branches = True, [], []
            self.api_note.set_text(f"Installed: {installed or 'unknown'}. Loading the released versions...")
            threading.Thread(target=self._api_tags_worker, daemon=True).start()

        grid, row = self._page("AI agent", "The built-in agent against an OpenAI-compatible API or Ollama, "
                                           "or a CLI agent."), 1

        # Every agent keeps its own url/token/model/command (config.AGENT_KEYS);
        # the fields below show the chosen one's.
        self.agent_values = {key: str(self.data.get(key) or "") for keys in AGENT_KEYS.values()
                             for key in keys.values()}
        self.current_agent = agent_of(self.data)
        self.agent = Gtk.ComboBoxText()
        for agent in AGENTS:
            self.agent.append_text(AGENT_LABELS[agent])
        self.agent.set_active(AGENTS.index(self.current_agent))
        self.agent.connect("changed", lambda *_: self._on_agent_changed())
        row = self._add_row(grid, row, "Agent", self.agent)
        self.agent_rows: dict[str, tuple[Gtk.Widget, Gtk.Widget]] = {}

        # The KI-Toolbox (or Ollama) is offered in the list; any other URL can be typed.
        self.ai_url = Gtk.ComboBoxText.new_with_entry()
        self.ai_url.set_hexpand(True)
        row = self._add_agent_row(grid, row, "url", "API URL", self.ai_url)

        self.ai_token = self._secret_entry("")
        self.ai_token.set_placeholder_text(
            f"From {AI_TOKEN_ENV}" if os.environ.get(AI_TOKEN_ENV) else "API key (Bearer token)"
        )
        row = self._add_agent_row(grid, row, "token", "API token", self.ai_token)

        self.command = Gtk.Entry(hexpand=True)
        self.command.set_placeholder_text("Name on the PATH or full path")
        self.check_button = Gtk.Button(label="Check")
        self.check_button.set_tooltip_text("Run the program with --version")
        self.check_button.connect("clicked", lambda *_: self._check_command())
        command_row = Gtk.Box(spacing=6)
        command_row.pack_start(self.command, True, True, 0)
        command_row.pack_start(self.check_button, False, False, 0)
        row = self._add_agent_row(grid, row, "command", "Command", command_row)

        # The entry keeps any model id usable, also before the list is loaded.
        self.ai_model = Gtk.ComboBoxText.new_with_entry()
        self.ai_model.set_hexpand(True)
        self.models_button = Gtk.Button(label="Load models")
        self.models_button.set_tooltip_text("Ask the API for its models (also tests URL and token)")
        self.models_button.connect("clicked", lambda *_: self._load_models())
        self.models_button.set_no_show_all(True)
        model_row = Gtk.Box(spacing=6)
        model_row.pack_start(self.ai_model, True, True, 0)
        model_row.pack_start(self.models_button, False, False, 0)
        row = self._add_row(grid, row, "Model", model_row)

        self.ai_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        self.ai_note.get_style_context().add_class("dim-label")
        grid.attach(self.ai_note, 1, row, 1, 1)
        row += 1
        self.loading_models = False
        self.checking = False
        self._show_agent()

        jupyter_grid = self._page("JupyterLab", "Optional: analyses (RDKit, pandas, plots, ...) in your JupyterLab.")
        run_grid, run_row = self._page("Running code"), 0
        grid, row = self._page("General"), 0

        self.hotkey = Gtk.Entry(hexpand=True)
        self.hotkey.set_placeholder_text("Off (e.g. <Control>space)")
        self.hotkey.set_text(str(self.data.get("hotkey") or ""))
        row = self._add_row(grid, row, "Global hotkey", self.hotkey)
        note = Gtk.Label(
            label="Press it twice quickly to open the prompt from anywhere. While it is set, "
                  "a single press no longer reaches other programs (e.g. completion in editors). "
                  "Empty turns it off." + (" Applies after restarting the app." if WINDOWS else ""),
            xalign=0, wrap=True, max_width_chars=60,
        )
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 1, row, 1, 1)
        row += 1

        self.spins: dict[str, Gtk.SpinButton] = {}
        for key, label, lower, upper in NUMBER_FIELDS:
            spin = Gtk.SpinButton.new_with_range(lower, upper, 1)
            spin.set_halign(Gtk.Align.START)
            spin.set_value(self._int_value(key, lower))
            self.spins[key] = spin
            if key == "execution_timeout":
                timeout_row = (label, spin)  # on "Running code", below the mode
            else:
                row = self._add_row(grid, row, label, spin)

        general_grid, general_row = grid, row
        grid, row = jupyter_grid, 1

        self.jupyter_url = Gtk.Entry(hexpand=True)
        self.jupyter_url.set_placeholder_text("Off (e.g. http://localhost:8888/lab?token=…)")
        self.jupyter_url.set_text(str(self.data.get("jupyter_url") or ""))
        row = self._add_row(grid, row, "JupyterLab URL", self.jupyter_url)
        self.jupyter_token = self._secret_entry(str(self.data.get("jupyter_token") or ""))
        self.jupyter_token.set_placeholder_text("Token (or a password below)")
        row = self._add_row(grid, row, "Token", self.jupyter_token)
        self.jupyter_password = self._secret_entry(str(self.data.get("jupyter_password") or ""))
        self.jupyter_password.set_placeholder_text("Password, if the server uses one instead of a token")
        row = self._add_row(grid, row, "Password", self.jupyter_password)
        self.jupyter_folder = Gtk.Entry(hexpand=True)
        self.jupyter_folder.set_placeholder_text(DEFAULT_JUPYTER_FOLDER)
        self.jupyter_folder.set_text(str(self.data.get("jupyter_folder") or ""))
        row = self._add_row(grid, row, "Folder in Jupyter", self.jupyter_folder)
        self.jupyter_verify_tls = Gtk.Switch(halign=Gtk.Align.START)
        self.jupyter_verify_tls.set_active(bool(self.data.get("jupyter_verify_tls", True)))
        row = self._add_row(grid, row, "Verify TLS certificate", self.jupyter_verify_tls)
        self.jupyter_test_button = Gtk.Button(label="Test JupyterLab")
        self.jupyter_test_button.connect("clicked", lambda *_: self._test_jupyter())
        self.jupyter_test_button.set_halign(Gtk.Align.START)
        grid.attach(self.jupyter_test_button, 1, row, 1, 1)
        row += 1
        self.jupyter_note = Gtk.Label(
            label="Each conversation gets a folder with a notebook there; files the assistant "
                  "exchanges between Chemotion and Jupyter go through it.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        self.jupyter_note.get_style_context().add_class("dim-label")
        grid.attach(self.jupyter_note, 0, row, 2, 1)
        row += 1
        self.testing_jupyter = False

        grid, row = run_grid, run_row
        self.auto_run = Gtk.ComboBoxText()
        for mode in AUTO_RUN_MODES:
            self.auto_run.append_text(AUTO_RUN_LABELS[mode])
        current = str(self.data.get("auto_run") or "off")
        self.auto_run.set_active(AUTO_RUN_MODES.index(current) if current in AUTO_RUN_MODES else 0)
        row = self._add_row(grid, row, "Run the assistant's code", self.auto_run)
        note = Gtk.Label(
            label="Auto-run starts a snippet without your confirmation; you can still stop it while "
                  "it runs. “Read-only code” still asks for anything that creates, changes or deletes "
                  "data, or writes files. New conversations start with this mode; the menu in a "
                  "conversation's header can change it while it is open.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 0, row, 2, 1)
        row += 1
        row = self._add_row(grid, row, *timeout_row)

        grid, row = self._page("Developer", "For developers of chemotion_api."), 1
        self.developer_mode = Gtk.Switch(halign=Gtk.Align.START)
        self.developer_mode.set_active(bool(self.data.get("developer_mode", False)))
        if not WINDOWS:
            self.developer_mode.connect("notify::active", lambda *_: self._fill_api_versions())
        row = self._add_row(grid, row, "Developer mode", self.developer_mode)
        note = Gtk.Label(
            label="The assistant reports every bug it finds in chemotion_api as an issue of "
                  f"{issues.REPO} on GitHub, with the date, the AI model, the Chemotion instance "
                  "(URL, version) and the chemotion_api branch. "
                  + ("" if WINDOWS else "The Chemotion tab also offers the branches of chemotion_api. ")
                  + "Applies to the next conversation.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 1, row, 1, 1)
        row += 1
        self.github_token = self._secret_entry(str(self.data.get("github_token") or ""))
        self.github_token.set_placeholder_text("Optional: creates the issues directly")
        row = self._add_row(grid, row, "GitHub token", self.github_token)
        note = Gtk.Label(
            label="A fine-grained token with “Issues: read and write” on the repository (or a classic one "
                  f"with public_repo). Without one, every report is saved in {issues.ISSUES_DIR} and the "
                  "assistant gives you a link to GitHub's new-issue form, filled in, to submit it yourself.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        note.get_style_context().add_class("dim-label")
        grid.attach(note, 1, row, 1, 1)
        row += 1

        grid, row = general_grid, general_row
        self.clear_history_button = Gtk.Button(label="Clear history")
        self.clear_history_button.set_tooltip_text(f"Delete the saved conversations in {history.HISTORY_DIR}")
        self.clear_history_button.connect("clicked", self._on_clear_history)
        self.history_note = Gtk.Label(xalign=0)
        self.history_note.get_style_context().add_class("dim-label")
        history_row = Gtk.Box(spacing=10)
        history_row.pack_start(self.clear_history_button, False, False, 0)
        history_row.pack_start(self.history_note, False, False, 0)
        row = self._add_row(grid, row, "Conversation history", history_row)
        self._update_history_note()

        grid = self._page("Lessons", "What the assistant learned about its tools (chemotion_api, packages, "
                                     "JupyterLab): every conversation gets these notes. The assistant adds "
                                     "them after it fixed a failing snippet; edit or delete what is wrong.")
        self.lessons_text = lessons.read_text()
        self.lessons_view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                         left_margin=6, right_margin=6, top_margin=6, bottom_margin=6)
        self.lessons_view.get_buffer().set_text(self.lessons_text)
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, min_content_height=260)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.lessons_view)
        grid.attach(scroller, 0, 1, 2, 1)
        self.clear_lessons_button = Gtk.Button(label="Clear lessons", halign=Gtk.Align.START)
        self.clear_lessons_button.set_tooltip_text(f"Delete all lessons in {lessons.LESSONS_FILE}")
        self.clear_lessons_button.connect("clicked", self._on_clear_lessons)
        grid.attach(self.clear_lessons_button, 0, 2, 1, 1)

        # max_width_chars keeps a long message from widening the window.
        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=60)
        self.status.set_text(str(CONFIG_FILE))
        self.status.get_style_context().add_class("dim-label")

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: self.destroy())
        save_button = Gtk.Button(label="Save")
        save_button.get_style_context().add_class("suggested-action")
        save_button.connect("clicked", self._on_save)

        self.service_button = Gtk.Button()
        self.service_button.set_tooltip_text(
            "Stops or starts the installed top-bar app. "
            "A copy started with python app.py is not affected."
        )
        self.service_button.connect("clicked", self._on_service_toggle)

        buttons = Gtk.Box(spacing=8)
        buttons.pack_start(self.service_button, False, False, 0)
        buttons.pack_end(save_button, False, False, 0)
        buttons.pack_end(close_button, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.pack_start(self.notebook, True, True, 0)
        box.pack_start(self.status, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self._update_service_button()
        if self.current_agent in CLI_AGENTS:
            self._check_command()
        elif self.current_agent == "ollama" or ai_token(self.data):
            self._load_models()

    def _page(self, title: str, intro: str = "") -> Gtk.Grid:
        """A new tab with a form grid; `intro` goes into its first row."""
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=14)
        if intro:
            note = Gtk.Label(label=intro, xalign=0, wrap=True, max_width_chars=60)
            note.get_style_context().add_class("dim-label")
            grid.attach(note, 0, 0, 2, 1)
        self.notebook.append_page(grid, Gtk.Label(label=title))
        return grid

    @staticmethod
    def _add_row(grid: Gtk.Grid, row: int, text: str, widget: Gtk.Widget) -> int:
        label = Gtk.Label(label=text, xalign=0)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        return row + 1

    def _add_agent_row(self, grid: Gtk.Grid, row: int, field: str, text: str, widget: Gtk.Widget) -> int:
        """A row only some agents have (`field` of config.AGENT_KEYS); _show_agent() shows it."""
        row = self._add_row(grid, row, text, widget)
        label = grid.get_child_at(0, row - 1)
        for item in (label, widget):
            item.show_all()  # its children; the row itself only through _show_agent()
            item.set_no_show_all(True)  # the window's show_all() must not show it
        self.agent_rows[field] = (label, widget)
        return row

    # --- agent ----------------------------------------------------------------
    def _agent_fields(self) -> dict[str, str]:
        """The visible fields' values, by AGENT_KEYS field."""
        return {
            "url": self.ai_url.get_child().get_text().strip().rstrip("/"),
            "token": self.ai_token.get_text().strip(),
            "command": self.command.get_text().strip(),
            "model": self.ai_model.get_child().get_text().strip(),
        }

    def _store_agent_fields(self) -> None:
        fields = self._agent_fields()
        for field, key in AGENT_KEYS[self.current_agent].items():
            self.agent_values[key] = fields[field]

    def _on_agent_changed(self) -> None:
        active = self.agent.get_active()
        if active not in range(len(AGENTS)) or AGENTS[active] == self.current_agent:
            return
        self._store_agent_fields()
        self.current_agent = AGENTS[active]
        self._show_agent()
        self.resize(self.get_size()[0], 1)  # shrink to the new fields
        if self.current_agent in CLI_AGENTS:
            self._check_command()

    def _show_agent(self) -> None:
        """Fill and show the fields of the chosen agent."""
        agent = self.current_agent
        keys = AGENT_KEYS[agent]
        for field, (label, widget) in self.agent_rows.items():
            for item in (label, widget):
                item.set_visible(field in keys)
        self.ai_url.remove_all()
        self.ai_url.append_text(OLLAMA_URL if agent == "ollama" else KI_TOOLBOX_URL)
        self.ai_url.get_child().set_placeholder_text(OLLAMA_URL if agent == "ollama" else KI_TOOLBOX_URL)
        self.ai_url.get_child().set_text(self.agent_values.get(keys.get("url", ""), ""))
        self.ai_token.set_text(self.agent_values.get(keys.get("token", ""), ""))
        self.command.set_text(self.agent_values.get(keys.get("command", ""), ""))
        self.ai_model.remove_all()
        for model in cli_models(agent) if agent in CLI_AGENTS else []:
            if model:
                self.ai_model.append_text(model)
        self.ai_model.get_child().set_text(self.agent_values.get(keys["model"], ""))
        self.ai_model.get_child().set_placeholder_text("Default model of the CLI" if agent in CLI_AGENTS else "")
        self.models_button.set_visible(agent not in CLI_AGENTS)
        self.ai_note.set_text(AGENT_NOTES[agent])

    def _check_command(self) -> None:
        if self.checking:
            return
        agent, command = self.current_agent, self.command.get_text().strip()
        if not command:
            self.ai_note.set_text(f"Set the command of {AGENT_LABELS[agent]}.")
            return
        self.checking = True
        self.check_button.set_sensitive(False)
        self.ai_note.set_text(f"Checking {command}...")
        threading.Thread(target=self._check_worker, args=(agent, command), daemon=True).start()

    def _check_worker(self, agent: str, command: str) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            text = f"Found {check_command(agent, command)}. {AGENT_NOTES[agent]}"
        except AgentError as exc:
            text = str(exc)
        GLib.idle_add(self._check_done, agent, text)

    def _check_done(self, agent: str, text: str) -> bool:
        self.checking = False
        self.check_button.set_sensitive(True)
        if agent == self.current_agent:
            self.ai_note.set_text(text)
        return False

    def _int_value(self, key: str, lower: int) -> int:
        try:
            return max(lower, int(self.data.get(key, EXAMPLE[key])))
        except (TypeError, ValueError):
            return max(lower, EXAMPLE[key])

    @staticmethod
    def _secret_entry(text: str) -> Gtk.Entry:
        entry = Gtk.Entry(hexpand=True)
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic"
        )
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show or hide")
        entry.connect(
            "icon-press", lambda e, *_args: e.set_visibility(not e.get_visibility())
        )
        entry.set_text(text)
        return entry

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _form_values(self) -> dict:
        values = {
            "url": self.url.get_text().strip().rstrip("/"),
            "user": self.user.get_text().strip(),
            "password": self.password.get_text(),
            "verify_tls": self.verify_tls.get_active(),
            "hotkey": self.hotkey.get_text().strip(),
            "jupyter_url": self.jupyter_url.get_text().strip(),
            "jupyter_token": self.jupyter_token.get_text().strip(),
            "jupyter_password": self.jupyter_password.get_text(),
            "jupyter_folder": self.jupyter_folder.get_text().strip(),
            "jupyter_verify_tls": self.jupyter_verify_tls.get_active(),
            "developer_mode": self.developer_mode.get_active(),
            "github_token": self.github_token.get_text().strip(),
        }
        self._store_agent_fields()
        values.update(self.agent_values)
        values["agent"] = self.current_agent
        if values["jupyter_url"]:
            # A URL as Jupyter prints it carries the token: keep them apart.
            values["jupyter_url"], url_token = split_url(values["jupyter_url"])
            values["jupyter_token"] = values["jupyter_token"] or url_token
        values["jupyter_folder"] = jupyter_folder(values)
        for key, spin in self.spins.items():
            values[key] = spin.get_value_as_int()
        active = self.auto_run.get_active()
        values["auto_run"] = AUTO_RUN_MODES[active] if active in range(len(AUTO_RUN_MODES)) else "off"
        return values

    # --- AI models ----------------------------------------------------------
    def _load_models(self) -> None:
        if self.loading_models:
            return
        agent, fields = self.current_agent, self._agent_fields()
        if agent in CLI_AGENTS:
            return
        if not fields["url"]:
            self.ai_note.set_text("Set the API URL first.")
            return
        token = ai_token({"ai_token": fields["token"]}) if agent == "api" else ""
        config = dataclasses.replace(Config("", "", ""), ai_url=fields["url"], ai_token=token)
        self.loading_models = True
        self.models_button.set_sensitive(False)
        self.ai_note.set_text("Loading models...")
        threading.Thread(target=self._models_worker, args=(agent, config), daemon=True).start()

    def _models_worker(self, agent: str, config: Config) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            models, error = list_models(config), None
        except AgentError as exc:
            models, error = [], str(exc)
        GLib.idle_add(self._models_done, agent, models, error)

    def _models_done(self, agent: str, models: list[str], error: str | None) -> bool:
        self.loading_models = False
        self.models_button.set_sensitive(True)
        if agent != self.current_agent:
            return False  # switched to another agent meanwhile
        self.ai_model.remove_all()
        for model in models:
            self.ai_model.append_text(model)
        if error:
            self.ai_note.set_text(f"{error}\nYou can still type a model id.")
        elif not models:
            self.ai_note.set_text("The API lists no models. You can still type a model id.")
        else:
            current = self.ai_model.get_child().get_text().strip()
            known = "" if not current or current in models else f" “{current}” is not among them."
            self.ai_note.set_text(f"Connection OK: {len(models)} models.{known}")
        return False

    # --- JupyterLab -----------------------------------------------------------
    def _test_jupyter(self) -> None:
        if self.testing_jupyter:
            return
        values = self._form_values()
        if not values["jupyter_url"]:
            self.jupyter_note.set_text("Set the JupyterLab URL first.")
            return
        problem = jupyter_problem(values)
        if problem:
            self.jupyter_note.set_text(problem)
            return
        config = dataclasses.replace(
            Config("", "", ""), jupyter_url=values["jupyter_url"], jupyter_token=values["jupyter_token"],
            jupyter_password=values["jupyter_password"], jupyter_verify_tls=values["jupyter_verify_tls"],
        )
        self.testing_jupyter = True
        self.jupyter_test_button.set_sensitive(False)
        self.jupyter_note.set_text("Connecting...")
        threading.Thread(target=self._jupyter_worker, args=(config,), daemon=True).start()

    def _jupyter_worker(self, config: Config) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            text = JupyterClient(config).test()
        except JupyterError as exc:
            text = str(exc)
        except Exception as exc:
            text = f"Connection failed: {exc}"
        GLib.idle_add(self._jupyter_done, text)

    def _jupyter_done(self, text: str) -> bool:
        self.testing_jupyter = False
        self.jupyter_test_button.set_sensitive(True)
        self.jupyter_note.set_text(text)
        return False

    # --- chemotion_api version ----------------------------------------------
    def _api_tags_worker(self) -> None:
        try:
            (tags, branches), error = api_versions.list_refs(), ""
        except api_versions.VersionError as exc:
            tags, branches, error = [], [], str(exc)
        GLib.idle_add(self._api_tags_done, tags, branches, error)

    def _api_tags_done(self, tags: list[str], branches: list[str], error: str) -> bool:
        self.api_busy = False
        self.api_tags, self.api_branches = tags, branches
        self._fill_api_versions(error)
        return False

    def _fill_api_versions(self, error: str = "") -> None:
        """The released tags, in developer mode also the branches (id "branch:<name>")."""
        tags, branches = self.api_tags, self.api_branches if self.developer_mode.get_active() else []
        tag, branch = api_versions.installed_tag(), api_versions.installed_branch()
        installed = api_versions.installed_label() or "unknown"
        self.api_version.remove_all()
        for name in tags:
            self.api_version.append(name, f"{name} (installed)" if name == tag else name)
        for name in branches:
            self.api_version.append(f"branch:{name}", f"branch {name}" + (" (installed)" if name == branch else ""))
        active = f"branch:{branch}" if branch in branches else tag if tag in tags else (tags[0] if tags else None)
        self.api_version.set_active_id(active)
        if error:
            self.api_note.set_text(f"Installed: {installed}. Cannot list the released versions: {error}")
        elif not tags:
            self.api_note.set_text(f"Installed: {installed}. No released version found.")
        else:
            more = f" and {len(branches)} branch{'' if len(branches) == 1 else 'es'}" if branches else ""
            self.api_note.set_text(f"Installed: {installed}. "
                                   f"{len(tags)} released version{'' if len(tags) == 1 else 's'}{more}.")
        self._update_api_button()

    def _update_api_button(self) -> None:
        self.api_install_button.set_sensitive(not self.api_busy and bool(self.api_version.get_active_id()))

    def _on_install_api(self, _button: Gtk.Button) -> None:
        ref = self.api_version.get_active_id()
        if self.api_busy or not ref:
            return
        self.api_busy = True
        self._update_api_button()
        self.api_version.set_sensitive(False)
        self.developer_mode.set_sensitive(False)  # it refills the list
        label = f"branch {ref.removeprefix('branch:')}" if ref.startswith("branch:") else ref
        self.api_note.set_text(f"Installing chemotion_api {label}, this takes a minute...")
        threading.Thread(target=self._install_api_worker, args=(ref, label), daemon=True).start()

    def _install_api_worker(self, ref: str, label: str) -> None:
        # Runs in a thread: no GTK calls here. sys.executable is the app's venv.
        command = [str(SETUP_WORKSPACE), str(SETUP_WORKSPACE.parent), sys.executable]
        command += ["--branch", ref.removeprefix("branch:")] if ref.startswith("branch:") else [ref]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=SETUP_TIMEOUT_S)
            if done.returncode == 0:
                text = done.stdout.strip().splitlines()[-1]  # "chemotionapi <label> (<commit>) installed, ..."
            else:
                lines = (done.stderr or done.stdout).strip().splitlines()
                text = f"Installing {label} failed: {lines[-1] if lines else f'exit code {done.returncode}'}"
        except subprocess.TimeoutExpired:
            text = f"Installing {label} took longer than {SETUP_TIMEOUT_S} s and was stopped."
        except OSError as exc:
            text = f"Installing {label} failed: {exc}"
        GLib.idle_add(self._install_api_done, text)

    def _install_api_done(self, text: str) -> bool:
        self.api_version.set_sensitive(True)
        self.developer_mode.set_sensitive(True)
        self._api_tags_done(self.api_tags, self.api_branches, "")  # moves "(installed)"
        self.api_note.set_text(text)
        return False

    # --- Test connection ----------------------------------------------------
    def _on_test(self, _button: Gtk.Button) -> None:
        values = self._form_values()
        if self.testing:
            return
        problem = config_problem(values)
        if problem:
            self._set_status(problem)
            return
        password = os.environ.get("CHEMOTION_PASSWORD") or values["password"]
        self.testing = True
        self.test_button.set_sensitive(False)
        self._set_status("Connecting...")
        threading.Thread(
            target=self._test_worker,
            args=(values["url"], values["user"], password, values["verify_tls"]),
            daemon=True,
        ).start()

    def _test_worker(self, url: str, user: str, password: str, verify_tls: bool) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            from chemotion_api import Instance

            instance = Instance(url, verify_ssl=verify_tls).test_connection().login(user, password)
            count = len(instance.get_root_collection().children)
            text = f"Connection OK: logged in as {user}, {count} top-level collection{'' if count == 1 else 's'}."
        except PermissionError:
            text = "Connection OK, but the login failed: check user and password."
        except Exception as exc:
            text = f"Connection failed: {exc}"
        GLib.idle_add(self._test_done, text)

    def _test_done(self, text: str) -> bool:
        self.testing = False
        self.test_button.set_sensitive(True)
        self._set_status(text)
        return False

    def _on_save(self, _button: Gtk.Button) -> None:
        values = self._form_values()
        if values["hotkey"] and not Gtk.accelerator_parse(values["hotkey"])[0]:
            self._set_status(f"“{values['hotkey']}” is not a key: write it like <Control>space or <Super>c.")
            return
        # Update the dict that was read, so keys unknown to this form are kept.
        self.data.update(values)
        self.jupyter_url.set_text(values["jupyter_url"])
        self.jupyter_token.set_text(values["jupyter_token"])
        try:
            save_raw_config(self.data)
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")
            return
        problem = (self._save_lessons() or config_problem(values) or ai_problem(values)
                   or jupyter_problem(values))
        if not WINDOWS:
            # The GNOME shortcut is the hotkey; the app needs no restart.
            try:
                problem = hotkey.register_gnome(values["hotkey"]) or problem
            except Exception as exc:
                problem = f"Could not set the GNOME shortcut: {exc}"
        # The app reads the config at the start of every conversation.
        self._set_status(f"Saved. {problem or 'Applies to the next conversation.'}")

    # --- History ------------------------------------------------------------
    def _update_history_note(self) -> None:
        count = history.count()
        self.history_note.set_text(f"{count} saved conversation{'' if count == 1 else 's'}")
        self.clear_history_button.set_sensitive(count > 0)

    def _on_clear_history(self, _button: Gtk.Button) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text="Delete all saved conversations?",
        )
        dialog.format_secondary_text("They can no longer be continued from the prompt. "
                                     "A conversation that is open right now is saved again when it goes on.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Delete", Gtk.ResponseType.ACCEPT).get_style_context().add_class("destructive-action")
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.ACCEPT:
            return
        try:
            deleted = history.clear()
        except OSError as exc:
            self._set_status(f"Could not clear the history: {exc}")
        else:
            self._set_status(f"Deleted {deleted} conversation{'' if deleted == 1 else 's'}.")
        self._update_history_note()

    def _save_lessons(self) -> str | None:
        """Write the edited lessons; a problem, or None."""
        buffer = self.lessons_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        if text == self.lessons_text:
            return None
        if lessons.read_text() != self.lessons_text:
            return ("The lessons were not saved: the assistant changed them meanwhile. "
                    "Close and open the settings again to edit them.")
        try:
            lessons.write_text(text)
        except OSError as exc:
            return f"Could not save the lessons: {exc}"
        self.lessons_text = lessons.read_text()
        buffer.set_text(self.lessons_text)
        return None

    def _on_clear_lessons(self, _button: Gtk.Button) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE, text="Delete all lessons?",
        )
        dialog.format_secondary_text("New conversations start without them.")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Delete", Gtk.ResponseType.ACCEPT).get_style_context().add_class("destructive-action")
        answer = dialog.run()
        dialog.destroy()
        if answer != Gtk.ResponseType.ACCEPT:
            return
        try:
            lessons.write_text("")
        except OSError as exc:
            self._set_status(f"Could not clear the lessons: {exc}")
            return
        self.lessons_text = ""
        self.lessons_view.get_buffer().set_text("")
        self._set_status("Deleted the lessons.")

    # --- Service ------------------------------------------------------------
    def _update_service_button(self) -> None:
        label = "Stop app" if app_control.is_running() else "Start app"
        self.service_button.set_label(label)

    def _on_service_toggle(self, _button: Gtk.Button) -> None:
        stop = app_control.is_running()
        try:
            app_control.stop() if stop else app_control.start()
        except app_control.ControlError as exc:
            self._set_status(f"Could not {'stop' if stop else 'start'} the app: {exc}")
        else:
            self._set_status("App stopped (it starts again at next login)." if stop else "App started.")
        self._update_service_button()


def main() -> None:
    # Matches the .desktop file name so the desktop shows its name and icon.
    GLib.set_prgname(PRGNAME)
    window = SettingsWindow()
    window.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
