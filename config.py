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
    # Runtime state (Windows: the log and the PID file), not configuration.
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
# AF_UNIX paths are limited to ~100 bytes, so the socket lives in the short
# per-user runtime directory.
SOCKET_PATH = Path(os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR) / f"{APP_NAME}.sock"
# Windows (no AF_UNIX in Python): port and token of the app's localhost server.
ENDPOINT_FILE = STATE_DIR / "endpoint.json"
# What the agent may read: CHEMOTION_API.md and the chemotionapi checkout.
# install.sh fills it; from the source tree setup_workspace.sh does.
WORKSPACE_DIR = Path(__file__).resolve().with_name("workspace")

# The KIT KI-Toolbox (Open WebUI, OpenAI-compatible) is the default AI endpoint;
# any other OpenAI-compatible URL works too.
KI_TOOLBOX_URL = "https://ki-toolbox.scc.kit.edu/api/v1"
DEFAULT_AI_MODEL = "alias.complex-local"
# Pressed twice, opens the prompt from anywhere (GTK accelerator syntax; "" = off).
DEFAULT_HOTKEY = "<Control>space"
# Used when ai_token is empty (older installs put the key there).
AI_TOKEN_ENV = "KI_TOOLBOX_API_KEY"
# ai_model ids of the opencode era ("<provider>/<model>") -> KI-Toolbox ids.
LEGACY_MODELS = {
    "complex-local": "alias.complex-local",
    "medium-local": "alias.medium-local",
    "simple-local": "alias.simple-local",
    "complex-gpt": "alias.complex-gpt",
    "medium-gpt": "alias.medium-gpt",
    "simple-gpt": "alias.simple-gpt",
    "claude-opus-5": "google.claude-opus-5",
    "claude-sonnet-5": "google.claude-sonnet-5",
    "gemini-3.8-flash": "google.gemini-3.8-flash",
}


@dataclass(frozen=True)
class Config:
    url: str
    user: str
    password: str
    verify_tls: bool = True
    ai_url: str = KI_TOOLBOX_URL
    ai_token: str = ""
    ai_model: str = DEFAULT_AI_MODEL
    execution_timeout: int = 300
    prompt_width: int = 640
    hotkey: str = DEFAULT_HOTKEY


EXAMPLE = {
    "url": "https://chemotion.example.com",
    "user": "",
    "password": "",
    "verify_tls": True,
    "ai_url": KI_TOOLBOX_URL,
    "ai_token": "",
    "ai_model": DEFAULT_AI_MODEL,
    "execution_timeout": 300,
    "prompt_width": 640,
    "hotkey": DEFAULT_HOTKEY,
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
    data = {**EXAMPLE, **data}
    model = str(data.get("ai_model") or "")
    if model.startswith("ki-toolbox/"):
        model = model.split("/", 1)[1]
        data["ai_model"] = LEGACY_MODELS.get(model, model)
    return data


def ai_token(data: dict) -> str:
    return str(data.get("ai_token") or "").strip() or os.environ.get(AI_TOKEN_ENV, "").strip()


def ai_problem(data: dict) -> str | None:
    """Why `data` cannot be used to talk to the AI model, or None."""
    if not str(data.get("ai_url", "")).strip():
        return "Set the AI URL in the settings."
    if not str(data.get("ai_model", "")).strip():
        return "Set the AI model in the settings."
    if not ai_token(data) and str(data.get("ai_url", "")).strip().rstrip("/") == KI_TOOLBOX_URL:
        return f"Set the KI-Toolbox token in the settings (or {AI_TOKEN_ENV})."
    return None


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
        ai_url=str(data.get("ai_url") or "").strip().rstrip("/"),
        ai_token=ai_token(data),
        ai_model=str(data.get("ai_model") or "").strip(),
        execution_timeout=max(10, timeout),
        prompt_width=max(360, width),
        hotkey=str(data.get("hotkey") or "").strip(),
    )
