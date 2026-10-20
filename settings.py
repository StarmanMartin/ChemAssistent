from __future__ import annotations

import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")

from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

import app_control  # noqa: E402
from config import CONFIG_FILE, EXAMPLE, config_problem, read_raw_config, save_raw_config  # noqa: E402
from opencode_models import list_models  # noqa: E402


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

        # The entry keeps the saved model usable when opencode is unavailable.
        self.ai_model = Gtk.ComboBoxText.new_with_entry()
        self.ai_model.set_hexpand(True)
        ai_entry = self.ai_model.get_child()
        ai_entry.set_placeholder_text("opencode's default model")
        ai_entry.set_text(str(self.data.get("ai_model") or ""))
        row = self._add_row(grid, row, "AI model", self.ai_model)

        self.ai_note = Gtk.Label(
            label="Loading models from opencode...", xalign=0, wrap=True, max_width_chars=60
        )
        self.ai_note.get_style_context().add_class("dim-label")
        grid.attach(self.ai_note, 1, row, 1, 1)
        row += 1
        threading.Thread(target=self._models_worker, daemon=True).start()

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
            "ai_model": self.ai_model.get_child().get_text().strip(),
        }
        for key, spin in self.spins.items():
            values[key] = spin.get_value_as_int()
        return values

    # --- AI models ----------------------------------------------------------
    def _models_worker(self) -> None:
        # Runs in a thread: no GTK calls here.
        try:
            models, error = list_models(), None
        except RuntimeError as exc:
            models, error = [], str(exc)
        GLib.idle_add(self._models_done, models, error)

    def _models_done(self, models: list[str], error: str | None) -> bool:
        for model in models:
            self.ai_model.append_text(model)
        if error:
            self.ai_note.set_text(f"{error}. You can still type a provider/model id.")
        elif not models:
            self.ai_note.set_text("opencode lists no models. You can still type a provider/model id.")
        else:
            self.ai_note.set_text(f"{len(models)} models from opencode. Leave empty for opencode's default.")
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
        # Update the dict that was read, so keys unknown to this form are kept.
        self.data.update(values)
        try:
            save_raw_config(self.data)
        except OSError as exc:
            self._set_status(f"Could not save: {exc}")
            return
        problem = config_problem(values)
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
