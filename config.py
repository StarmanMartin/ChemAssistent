from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "chem-assistent"
WINDOWS = sys.platform == "win32"
if WINDOWS:
    CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    # Runtime state (the opencode workspace config), not configuration.
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
# AF_UNIX paths are limited to ~100 bytes, so the socket lives in the short
# per-user runtime directory.
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR) / f"{APP_NAME}.sock"
# Windows (no AF_UNIX in Python): port and token of the app's localhost server.
ENDPOINT_FILE = STATE_DIR / "endpoint.json"
# The directory opencode works in: CHEMOTION_API.md, the chemotionapi checkout
# and the generated opencode.json. install.sh fills it; from the source tree
# setup_workspace.sh does.
WORKSPACE_DIR = Path(__file__).resolve().with_name("workspace")


@dataclass(frozen=True)
class Config:
    url: str
    user: str
    password: str
    verify_tls: bool = True
    ai_model: str = ""
    execution_timeout: int = 300
    prompt_width: int = 640


EXAMPLE = {
    "url": "https://chemotion.example.com",
    "user": "",
    "password": "",
    "verify_tls": True,
    "ai_model": "",
    "execution_timeout": 300,
    "prompt_width": 640,
}


def ensure_example_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        write_private_json(CONFIG_FILE, EXAMPLE)


def read_raw_config() -> dict:
    """The config file as a dict, without validation (for the settings app)."""
    ensure_example_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {**EXAMPLE, **data}


def write_private_json(path: Path, data: object) -> None:
    """Atomically write JSON readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Create it with mode 0600 from the start: it holds secrets.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def save_raw_config(data: dict) -> None:
    write_private_json(CONFIG_FILE, data)


def config_problem(data: dict) -> str | None:
    """Why `data` cannot be used to talk to Chemotion, or None."""
    url = str(data.get("url", "")).strip()
    if not url or url == EXAMPLE["url"]:
        return "Set the Chemotion URL in the settings."
    if not str(data.get("user", "")).strip():
        return "Set the Chemotion user in the settings."
    if not (os.environ.get("CHEMOTION_PASSWORD") or str(data.get("password", ""))):
        return "Set the Chemotion password in the settings (or CHEMOTION_PASSWORD)."
    return None


def load_config() -> Config:
    """The validated config. Raises ValueError when it is incomplete."""
    data = read_raw_config()
    problem = config_problem(data)
    if problem:
        raise ValueError(f"{problem} ({CONFIG_FILE})")
    try:
        timeout = int(data.get("execution_timeout", 300))
        width = int(data.get("prompt_width", 640))
    except (TypeError, ValueError):
        timeout, width = 300, 640
    return Config(
        url=str(data["url"]).strip().rstrip("/"),
        user=str(data["user"]).strip(),
        password=os.environ.get("CHEMOTION_PASSWORD") or str(data["password"]),
        verify_tls=bool(data.get("verify_tls", True)),
        ai_model=str(data.get("ai_model") or "").strip(),
        execution_timeout=max(10, timeout),
        prompt_width=max(360, width),
    )
