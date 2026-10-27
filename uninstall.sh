#!/usr/bin/env bash
set -euo pipefail

systemctl --user disable --now chem-assistent.service 2>/dev/null || true
# The global hotkey is a GNOME custom shortcut that runs the app's hotkey.py.
"$HOME/.local/share/chem-assistent/.venv/bin/python" "$HOME/.local/share/chem-assistent/hotkey.py" --unregister \
  2>/dev/null || true
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/chem-assistent.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/chem-assistent"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/chem-assistent.desktop"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/applications/chem-assistent-settings.desktop"
rm -f "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/chem-assistent.svg"

echo "Application removed."
echo "Configuration was kept at:"
echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/chem-assistent"
echo "  and its passwords and tokens in the system keyring (entries \"ChemAssistent: ...\", e.g. in Seahorse)"
echo "State of older versions (may be deleted):"
echo "  ${XDG_STATE_HOME:-$HOME/.local/state}/chem-assistent"
