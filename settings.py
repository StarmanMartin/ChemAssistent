from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

import app_control  # noqa: E402
import hotkey  # noqa: E402
from agent import AgentError, list_models  # noqa: E402
from config import (  # noqa: E402
    AI_TOKEN_ENV, CONFIG_FILE, EXAMPLE, KI_TOOLBOX_URL, Config, ai_problem, ai_token, config_problem,
    WINDOWS, read_raw_config, save_raw_config,
)


PRGNAME = "chem-assistent-settings"
ICON_FILE = Path(__file__).with_name("chem-assistent.svg")

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

        grid = Gtk.Grid(column_spacing=12, row_spacing=10, border_width=14)
        row = 0

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

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>AI model</b> (any OpenAI-compatible API)")
        heading.set_margin_top(8)
        grid.attach(heading, 0, row, 2, 1)
        row += 1

        # The KI-Toolbox is offered in the list; any other URL can be typed.
        self.ai_url = Gtk.ComboBoxText.new_with_entry()
        self.ai_url.set_hexpand(True)
        self.ai_url.append_text(KI_TOOLBOX_URL)
        self.ai_url.get_child().set_placeholder_text(KI_TOOLBOX_URL)
        self.ai_url.get_child().set_text(str(self.data.get("ai_url") or ""))
        row = self._add_row(grid, row, "API URL", self.ai_url)

        self.ai_token = self._secret_entry(str(self.data.get("ai_token") or ""))
        self.ai_token.set_placeholder_text(
            f"From {AI_TOKEN_ENV}" if os.environ.get(AI_TOKEN_ENV) else "API key (Bearer token)"
        )
        row = self._add_row(grid, row, "API token", self.ai_token)

        # The entry keeps any model id usable, also before the list is loaded.
        self.ai_model = Gtk.ComboBoxText.new_with_entry()
        self.ai_model.set_hexpand(True)
        self.ai_model.get_child().set_text(str(self.data.get("ai_model") or ""))
        self.models_button = Gtk.Button(label="Load models")
        self.models_button.set_tooltip_text("Ask the API for its models (also tests URL and token)")
        self.models_button.connect("clicked", lambda *_: self._load_models())
        model_row = Gtk.Box(spacing=6)
        model_row.pack_start(self.ai_model, True, True, 0)
        model_row.pack_start(self.models_button, False, False, 0)
        row = self._add_row(grid, row, "Model", model_row)

        self.ai_note = Gtk.Label(
            label=f"KIT KI-Toolbox: create an API key at {KI_TOOLBOX_URL.split('/api')[0]}.",
            xalign=0, wrap=True, max_width_chars=60,
        )
        self.ai_note.get_style_context().add_class("dim-label")
        grid.attach(self.ai_note, 1, row, 1, 1)
        row += 1
        self.loading_models = False

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
            row = self._add_row(grid, row, label, spin)

        # max_width_chars keeps a long message from widening the window.
        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=60)
        self.status.set_text(str(CONFIG_FILE))
        self.status.get_style_context().add_class("dim-label")

        self.test_button = Gtk.Button(label="Test connection")
        self.test_button.connect("clicked", self._on_test)
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
        buttons.pack_start(self.test_button, False, False, 0)
        buttons.pack_start(self.service_button, False, False, 0)
        buttons.pack_end(save_button, False, False, 0)
        buttons.pack_end(close_button, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.pack_start(grid, True, True, 0)
        box.pack_start(self.status, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)
        self._update_service_button()
        if ai_token(self.data):
            self._load_models()

    @staticmethod
    def _add_row(grid: Gtk.Grid, row: int, text: str, widget: Gtk.Widget) -> int:
        label = Gtk.Label(label=text, xalign=0)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        return row + 1

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
            "ai_url": self.ai_url.get_child().get_text().strip().rstrip("/"),
            "ai_token": self.ai_token.get_text().strip(),
            "ai_model": self.ai_model.get_child().get_text().strip(),
            "hotkey": self.hotkey.get_text().strip(),
        }
        for key, spin in self.spins.items():
            values[key] = spin.get_value_as_int()
        return values

    # --- AI models ----------------------------------------------------------
    def _load_models(self) -> None:
        if self.loading_models:
            return
        values = self._form_values()
        if not values["ai_url"]:
            self.ai_note.set_text("Set the API URL first.")
            return
        config = dataclasses.replace(Config("", "", ""), ai_url=values["ai_url"], ai_token=ai_token(values))
        self.loading_models = True
        self.models_button.set_sensitive(False)
        self.ai_note.set_text("Loading models...")
        threading.Thread(target=self._models_worker, args=(config,), daemon=True).start()

    def _models_worker(self, config: Config) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            models, error = list_models(config), None
        except AgentError as exc:
            models, error = [], str(exc)
        GLib.idle_add(self._models_done, models, error)

    def _models_done(self, models: list[str], error: str | None) -> bool:
        self.loading_models = False
        self.models_button.set_sensitive(True)
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
        try:
            save_raw_config(self.data)
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")
            return
        problem = config_problem(values) or ai_problem(values)
        if not WINDOWS:
            # The GNOME shortcut is the hotkey; the app needs no restart.
            try:
                problem = hotkey.register_gnome(values["hotkey"]) or problem
            except Exception as exc:
                problem = f"Could not set the GNOME shortcut: {exc}"
        # The app reads the config at the start of every conversation.
        self._set_status(f"Saved. {problem or 'Applies to the next conversation.'}")

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
