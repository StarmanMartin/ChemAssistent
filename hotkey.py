"""The global hotkey (default Ctrl+Space, pressed twice opens the prompt).

Apps cannot grab keys globally on GNOME under Wayland, so the hotkey is a
GNOME custom keyboard shortcut whose command runs this file; it tells the
running app over ipc.py ({"type": "hotkey"}), and the app opens the prompt
when two presses arrive within HOTKEY_DOUBLE_S (app.py). On Windows the app
registers the key itself with RegisterHotKey (listen_windows()).

    hotkey.py               one press: notify the running app (GNOME runs this)
    hotkey.py --unregister  remove the GNOME shortcut (uninstall.sh)

Only the standard library at module level: GNOME starts this on every press.
"""
from __future__ import annotations

import json
import logging
import shlex
import sys
import threading
from pathlib import Path

import ipc


LOG = logging.getLogger("chem-assistent")

HERE = Path(__file__).resolve().parent
GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
GNOME_BINDING_SCHEMA = f"{GNOME_SCHEMA}.custom-keybinding"
GNOME_BINDING_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/chem-assistent/"


def notify() -> int:
    address, token = ipc.running_endpoint()
    if not address:
        return 1
    try:
        with ipc.connect(address, timeout=2) as conn:
            conn.sendall(json.dumps({"type": "hotkey", "token": token}).encode("utf-8") + b"\n")
    except OSError:
        return 1  # the app does not run
    return 0


# --- GNOME --------------------------------------------------------------------
def _gnome_settings():
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio  # type: ignore

    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup(GNOME_SCHEMA, True) is None or \
            source.lookup(GNOME_BINDING_SCHEMA, True) is None:
        return None, None
    return Gio.Settings.new(GNOME_SCHEMA), Gio.Settings.new_with_path(GNOME_BINDING_SCHEMA, GNOME_BINDING_PATH)


def register_gnome(accelerator: str, python: str = sys.executable) -> str | None:
    """Make `accelerator` (GTK syntax, e.g. "<Control>space") a GNOME shortcut
    that runs this file; an empty one removes it. Returns a problem or None."""
    media_keys, binding = _gnome_settings()
    if media_keys is None:
        return "No GNOME keyboard shortcuts on this desktop; the hotkey is off."
    paths = list(media_keys.get_strv("custom-keybindings"))
    if not accelerator:
        if GNOME_BINDING_PATH in paths:
            paths.remove(GNOME_BINDING_PATH)
            media_keys.set_strv("custom-keybindings", paths)
        for key in ("name", "command", "binding"):
            binding.reset(key)
    else:
        binding.set_string("name", "ChemAssistent (press twice)")
        binding.set_string("command", shlex.join([python, str(HERE / "hotkey.py")]))
        binding.set_string("binding", accelerator)
        if GNOME_BINDING_PATH not in paths:
            media_keys.set_strv("custom-keybindings", [*paths, GNOME_BINDING_PATH])
    from gi.repository import Gio  # type: ignore

    Gio.Settings.sync()
    return None


# --- Windows ------------------------------------------------------------------
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312


def virtual_key(name: str) -> int | None:
    """The Windows virtual-key code of a GTK key name (space, a-z, 0-9, F1-F12)."""
    name = name.lower()
    if name == "space":
        return 0x20
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit() and 1 <= int(name[1:]) <= 12:
        return 0x6F + int(name[1:])
    return None


def listen_windows(modifiers: int, key: int, callback) -> None:
    """Calls `callback` (in a thread) on every press of the hotkey."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    def loop() -> None:
        # The hotkey belongs to this thread; its message loop gets WM_HOTKEY.
        if not user32.RegisterHotKey(None, 1, modifiers | MOD_NOREPEAT, key):
            LOG.warning("Could not register the hotkey (is it used by another program?)")
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_HOTKEY:
                callback()

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    if sys.argv[1:] == ["--unregister"]:
        problem = register_gnome("")
        if problem:
            print(problem)
        sys.exit(0)
    sys.exit(notify())
