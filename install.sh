#!/usr/bin/env bash
# Install or update ChemAssistent for the current user.
# Safe to re-run: it updates the code and chemotionapi, keeps the config and
# restarts the service if it is running.
set -euo pipefail

APP_NAME="chem-assistent"
APP_DIR="${HOME}/.local/share/${APP_NAME}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/${APP_NAME}"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${APP_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILES=("${APP_NAME}.desktop" "${APP_NAME}-settings.desktop")
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

APP_FILES=(app.py app_control.py chemotion_runner.py config.py console_win.py gen_api_reference.py ipc.py mcp_server.py
  opencode_models.py settings.py workspace.py setup_workspace.sh requirements.txt
  chem-assistent.svg chem-assistent-panel.svg)

# The system Python, which has PyGObject from apt (a python3 from pyenv/asdf
# on the PATH does not).
PYTHON=/usr/bin/python3

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as your normal user, not with sudo." >&2
  echo "It asks for sudo itself when system packages are missing." >&2
  exit 1
fi

# --- System packages --------------------------------------------------------
# PyGObject, GTK, VTE and the AppIndicator come from apt; pip installs
# requests and chemotion_api.
packages=(python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-ayatanaappindicator3-0.1
  gir1.2-gtksource-4 git curl)

# venv needs ensurepip, which may come from python3-venv or python3.X-venv.
if ! "$PYTHON" -c 'import ensurepip' 2>/dev/null; then
  packages+=(python3-venv)
fi

# python-magic (a chemotion_api dependency) needs libmagic; the package was
# renamed to libmagic1t64 in Ubuntu 24.04.
if apt-cache show libmagic1t64 >/dev/null 2>&1; then
  packages+=(libmagic1t64)
else
  packages+=(libmagic1)
fi

missing=()
for pkg in "${packages[@]}"; do
  if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
    missing+=("$pkg")
  fi
done

if ((${#missing[@]})); then
  echo "Installing Ubuntu packages: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  echo "Ubuntu packages already installed."
fi

if ! "$PYTHON" -c 'import sys; sys.exit(not (3, 13) <= sys.version_info[:2] < (3, 15))'; then
  echo "Error: chemotion_api needs Python 3.13 or 3.14; $PYTHON is $("$PYTHON" --version)." >&2
  exit 1
fi

# --- Application ------------------------------------------------------------
echo "Installing application to $APP_DIR ..."
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$SERVICE_DIR"
for file in "${APP_FILES[@]}"; do
  install -m 0644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
done
chmod 0755 "$APP_DIR/setup_workspace.sh"
rm -rf "$APP_DIR/__pycache__"

# --system-site-packages makes the apt-installed PyGObject visible in the venv.
# Recreate the venv when the system Python version changed.
venv_python="$APP_DIR/.venv/bin/python"
if [[ -x "$venv_python" ]] && "$venv_python" -c 'import sys' 2>/dev/null \
  && [[ "$("$venv_python" -c 'import sys; print(sys.version_info[:2])')" \
     == "$("$PYTHON" -c 'import sys; print(sys.version_info[:2])')" ]]; then
  echo "Reusing virtual environment."
else
  echo "Creating virtual environment..."
  rm -rf "$APP_DIR/.venv"
  "$PYTHON" -m venv --system-site-packages "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check \
  -r "$APP_DIR/requirements.txt"

if ! "$venv_python" -c 'import gi; gi.require_version("Gtk", "3.0"); gi.require_version("Vte", "2.91"); from gi.repository import Gtk, Vte' 2>/dev/null; then
  echo "Error: GTK 3 / VTE bindings are not importable from $venv_python" >&2
  exit 1
fi

# chemotionapi (branch version-3) and the API reference for the agent.
"$APP_DIR/setup_workspace.sh" "$APP_DIR" "$venv_python"

# --- Configuration ----------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  install -m 0600 "$SOURCE_DIR/config.example.json" "$CONFIG_DIR/config.json"
  created_config=1
else
  chmod 0600 "$CONFIG_DIR/config.json"
  created_config=0
fi

# --- opencode and the KIT KI-Toolbox ----------------------------------------
# The app needs opencode 2 (its config format). The official v2 installer
# puts it into ~/.opencode/bin, where the app looks for it too.
OPENCODE_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
KEY_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/50-ki-toolbox.conf"

opencode_bin="$(command -v opencode || true)"
if [[ -z "$opencode_bin" && -x "$HOME/.opencode/bin/opencode" ]]; then
  opencode_bin="$HOME/.opencode/bin/opencode"
fi
opencode_version="$([[ -n "$opencode_bin" ]] && "$opencode_bin" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9.]+' | head -n 1 || true)"
if [[ "$opencode_version" =~ ^([0-9]+)\. ]] && ((BASH_REMATCH[1] >= 2)); then
  echo "opencode $opencode_version already installed."
else
  echo "Installing opencode 2 ${opencode_version:+(found $opencode_version) }..."
  curl -fsSL https://opencode.ai/v2/install | bash
fi

# Add the KI-Toolbox provider to the user's opencode config (unless it is
# there) and use it in ChemAssistent unless a model is chosen already.
"$PYTHON" "$SOURCE_DIR/setup_opencode.py" "$OPENCODE_CONFIG" "$CONFIG_DIR/config.json"

# The API key goes into environment.d, which systemd reads for the user
# session, so the service (and the opencode it starts) gets it.
if [[ -n "${KI_TOOLBOX_API_KEY:-}" ]] || grep -qs '^KI_TOOLBOX_API_KEY=.' "$KEY_FILE"; then
  echo "KI-Toolbox API key already set."
elif [[ -t 0 ]]; then
  echo "Enter your KI-Toolbox API key (https://ki-toolbox.scc.kit.edu), or leave it empty to skip:"
  read -rs key
  echo
  if [[ -n "$key" ]]; then
    mkdir -p "$(dirname "$KEY_FILE")"
    (umask 077 && printf 'KI_TOOLBOX_API_KEY=%s\n' "$key" > "$KEY_FILE")
    echo "Saved the key to $KEY_FILE (mode 0600)."
  fi
  unset key
fi
# Hand the key to the running systemd user manager, so the service restarted
# below has it without logging out (import-environment keeps it off the
# command line).
if [[ -z "${KI_TOOLBOX_API_KEY:-}" ]] && grep -qs '^KI_TOOLBOX_API_KEY=.' "$KEY_FILE"; then
  KI_TOOLBOX_API_KEY="$(sed -n 's/^KI_TOOLBOX_API_KEY=//p' "$KEY_FILE" | tail -n 1)"
fi
if [[ -n "${KI_TOOLBOX_API_KEY:-}" ]]; then
  export KI_TOOLBOX_API_KEY
  systemctl --user import-environment KI_TOOLBOX_API_KEY
fi


# --- Menu entries and icons -------------------------------------------------
# .desktop files do not expand ~ or variables, so fill in the absolute path.
mkdir -p "$DESKTOP_DIR" "$ICON_DIR"
for desktop in "${DESKTOP_FILES[@]}"; do
  sed "s|@APP_DIR@|$APP_DIR|g" "$SOURCE_DIR/$desktop.in" > "$DESKTOP_DIR/$desktop"
  chmod 0644 "$DESKTOP_DIR/$desktop"
done
install -m 0644 "$SOURCE_DIR/chem-assistent.svg" "$ICON_DIR/chem-assistent.svg"
# GTK trusts an existing icon cache over the directory, so refresh it.
icon_theme_dir="$(dirname "$(dirname "$ICON_DIR")")"
if [[ -f "$icon_theme_dir/icon-theme.cache" ]] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache --quiet --force --ignore-theme-index "$icon_theme_dir" || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# --- systemd user service ---------------------------------------------------
install -m 0644 "$SOURCE_DIR/$SERVICE" "$SERVICE_DIR/$SERVICE"
systemctl --user daemon-reload

if systemctl --user is-enabled --quiet "$SERVICE" 2>/dev/null; then
  # Re-enable so the unit is linked to its current [Install] target.
  systemctl --user reenable "$SERVICE" >/dev/null 2>&1
else
  # First install: the icon belongs in the top bar from now on.
  systemctl --user enable "$SERVICE" >/dev/null 2>&1
fi

if systemctl --user is-active --quiet "$SERVICE"; then
  systemctl --user restart "$SERVICE"
  echo "Restarted $SERVICE."
else
  systemctl --user start "$SERVICE" && echo "Started $SERVICE."
fi

echo
echo "Installed."
if ((created_config)); then
  echo "Created $CONFIG_DIR/config.json (mode 0600): set URL, user and password in the settings."
fi
if ! command -v opencode >/dev/null 2>&1 && [[ ! -x "$HOME/.opencode/bin/opencode" ]]; then
  echo "Warning: opencode was not found. Install it from https://opencode.ai and log in to a provider."
fi
if [[ -z "${KI_TOOLBOX_API_KEY:-}" ]]; then
  echo "Warning: no KI-Toolbox API key set. Put KI_TOOLBOX_API_KEY=<key> into $KEY_FILE and log in again."
fi
echo "Settings:  open \"ChemAssistent Settings\" from the application menu"
echo "Use:       click the Chemotion icon in the top bar -> \"Ask Chemotion…\" (middle click opens it directly)"
echo "Logs:      journalctl --user -u $SERVICE -f"
