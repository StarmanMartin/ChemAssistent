#!/usr/bin/env bash
# Install or update ChemAssistent for the current user.
# Safe to re-run: it updates the code and chemotionapi, keeps the config and
# restarts the service if it is running.
#
# One file is enough: without the source tree next to it the script fetches
# the app from GitHub, e.g.
#   curl -fsSL https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.sh | bash
# CHEM_ASSISTENT_REPO and CHEM_ASSISTENT_REF choose another repository or branch,
# CHEMOTION_API_TAG the chemotionapi version (else the script asks).
set -euo pipefail

APP_NAME="chem-assistent"
APP_DIR="${HOME}/.local/share/${APP_NAME}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/${APP_NAME}"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE="${APP_NAME}.service"
REPO="${CHEM_ASSISTENT_REPO:-https://github.com/StarmanMartin/ChemAssistent.git}"
REF="${CHEM_ASSISTENT_REF:-main}"
# The checkout this script belongs to; empty when it runs from a pipe or alone.
SOURCE_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$script_dir/app.py" && -f "$script_dir/setup_ai.py" ]]; then
    SOURCE_DIR="$script_dir"
  fi
fi

DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILES=("${APP_NAME}.desktop" "${APP_NAME}-settings.desktop")
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

APP_FILES=(agent.py api_versions.py app.py app_control.py chemotion_runner.py cli_agent.py config.py gen_api_reference.py history.py hotkey.py ipc.py
  issues.py jobs.py jupyter.py lessons.py mcp_bridge.py
  settings.py workspace.py setup_workspace.sh uninstall.sh requirements.txt
  chem-assistent.svg chem-assistent-panel.svg)
# Folders copied as a whole (the app's agent skills).
APP_DIRS=(skills)
# Files of older versions (the opencode era) that an update removes.
OLD_FILES=(console_win.py mcp_server.py opencode_models.py workspace/opencode.json)

# The system Python, which has PyGObject from apt (a python3 from pyenv/asdf
# on the PATH does not).
PYTHON=/usr/bin/python3

# A temporary clone of $REPO, removed on exit.
fetch_source() {
  SOURCE_DIR="$(mktemp -d)"
  trap 'rm -rf "$SOURCE_DIR"' EXIT
  echo "Fetching ChemAssistent ($REF) from $REPO ..."
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$SOURCE_DIR"
}

# Runs a command with the terminal as stdin: under "curl ... | bash" stdin is
# the script, and prompts must read from /dev/tty instead.
with_terminal() {
  if [[ -t 0 ]]; then
    "$@"
  elif (exec </dev/tty) 2>/dev/null; then
    "$@" </dev/tty
  else
    "$@" </dev/null
  fi
}

# Everything runs inside main, called on the last line, so that bash has read
# the whole script before it starts (it may come through a pipe).
main() {
if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as your normal user, not with sudo." >&2
  echo "It asks for sudo itself when system packages are missing." >&2
  exit 1
fi

# --- System packages --------------------------------------------------------
# PyGObject, GTK and the AppIndicator come from apt; pip installs
# requests and chemotion_api.
packages=(python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
  gir1.2-gtksource-4 git)

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

# --- Source -------------------------------------------------------------------
# git comes from the packages above.
if [[ -z "$SOURCE_DIR" ]]; then
  fetch_source
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
chmod 0755 "$APP_DIR/setup_workspace.sh" "$APP_DIR/uninstall.sh"
for file in "${OLD_FILES[@]}"; do
  rm -f "$APP_DIR/$file"
done
rm -rf "$APP_DIR/__pycache__"
# Folders are replaced as a whole (files removed upstream disappear too).
for dir in "${APP_DIRS[@]}"; do
  rm -rf "${APP_DIR:?}/$dir"
  cp -r "$SOURCE_DIR/$dir" "$APP_DIR/$dir"
done

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

if ! "$venv_python" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "Error: GTK 3 bindings are not importable from $venv_python" >&2
  exit 1
fi

# chemotionapi with its skills, and the API reference for the agent. The
# version is asked for on a terminal (default: the installed one, else the
# newest release); CHEMOTION_API_TAG chooses it without asking.
chemotion_tag="$(with_terminal "$venv_python" "$APP_DIR/api_versions.py" choose "$APP_DIR/workspace")"
"$APP_DIR/setup_workspace.sh" "$APP_DIR" "$venv_python" "$chemotion_tag"

# --- Configuration ----------------------------------------------------------
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  install -m 0600 "$SOURCE_DIR/config.example.json" "$CONFIG_DIR/config.json"
  created_config=1
else
  chmod 0600 "$CONFIG_DIR/config.json"
  created_config=0
fi

# --- The KIT KI-Toolbox -------------------------------------------------------
# The AI endpoint (default: the KI-Toolbox) and its token live in the app
# config. An older install kept the key in environment.d; take it from there.
KEY_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/environment.d/50-ki-toolbox.conf"
if [[ -z "${KI_TOOLBOX_API_KEY:-}" ]] && grep -qs '^KI_TOOLBOX_API_KEY=.' "$KEY_FILE"; then
  KI_TOOLBOX_API_KEY="$(sed -n 's/^KI_TOOLBOX_API_KEY=//p' "$KEY_FILE" | tail -n 1)"
  export KI_TOOLBOX_API_KEY
fi
with_terminal "$PYTHON" "$SOURCE_DIR/setup_ai.py"


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
echo "Settings:  open \"ChemAssistent Settings\" from the application menu"
echo "Use:       click the Chemotion icon in the top bar -> \"Ask Chemotion…\" (middle click opens it directly)"
echo "Logs:      journalctl --user -u $SERVICE -f"
echo "Remove:    $APP_DIR/uninstall.sh"
}

main "$@"
