from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "chem-assistent"
WINDOWS = sys.platform == "win32"
# The Windows build is a PyInstaller bundle (windows/): no Python, its
# programs are exes, and this file's folder is the bundle's _internal\.
FROZEN = bool(getattr(sys, "frozen", False))
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
# The app's own agent skills (skills/<name>/SKILL.md); with JupyterLab on,
# the agent may read them (jupyter-calculations).
SKILLS_DIR = Path(__file__).resolve().with_name("skills")

# The KIT KI-Toolbox (Open WebUI, OpenAI-compatible) is the default AI endpoint;
# any other OpenAI-compatible URL works too.
KI_TOOLBOX_URL = "https://ki-toolbox.scc.kit.edu/api/v1"
DEFAULT_AI_MODEL = "alias.complex-local"
# Pressed twice, opens the prompt from anywhere (GTK accelerator syntax; "" = off).
DEFAULT_HOTKEY = "<Control>space"
# How the assistant's execute_python code runs (config key auto_run):
# off: every snippet waits for Run/Reject in the result panel,
# readonly: read-only snippets run automatically, the rest still waits,
# always: every snippet runs automatically.
AUTO_RUN_MODES = ("off", "readonly", "always")
AUTO_RUN_LABELS = {
    "off": "Ask before every run",
    "readonly": "Auto-run read-only code",
    "always": "Auto-run everything",
}
# JupyterLab (optional; empty jupyter_url = off): every conversation gets the
# folder <jupyter_folder>/<conversation id> there with its notebook.
DEFAULT_JUPYTER_FOLDER = "ChemAssistent"
# Files the two sides of a conversation exchange (jupyter.py syncs them);
# one subfolder per conversation.
EXCHANGE_DIR = STATE_DIR / "exchange"
# Which agent answers (config key `agent`): the built-in one (agent.py) against
# an OpenAI-compatible API or a local Ollama, or a CLI agent (cli_agent.py).
AGENTS = ("api", "ollama", "claude", "codex")
AGENT_LABELS = {
    "api": "Built-in agent · OpenAI-compatible API",
    "ollama": "Built-in agent · Ollama (local)",
    "claude": "Claude Code CLI",
    "codex": "Codex CLI",
}
# How the prompt and the conversation header name them.
AGENT_SHORT = {"api": "Model", "ollama": "Ollama", "claude": "Claude Code", "codex": "Codex"}
CLI_AGENTS = ("claude", "codex")
# Ollama's OpenAI-compatible endpoint.
OLLAMA_URL = "http://localhost:11434/v1"
# Per agent: the config keys that hold its url, token, model and command.
# load_config() puts the chosen agent's values into Config.ai_url etc.
AGENT_KEYS = {
    "api": {"url": "ai_url", "token": "ai_token", "model": "ai_model"},
    "ollama": {"url": "ollama_url", "model": "ollama_model"},
    "claude": {"command": "claude_command", "model": "claude_model"},
    "codex": {"command": "codex_command", "model": "codex_model"},
}
# Model names the CLIs accept, offered in the menus ("" = the CLI's default
# model; any other name can be typed in the settings). cli_agent.cli_models()
# adds the models Codex cached for the logged-in account.
CLI_MODELS = {"claude": ["", "opus", "sonnet", "haiku"], "codex": [""]}
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
    auto_run: str = "off"
    jupyter_url: str = ""
    jupyter_token: str = ""
    jupyter_password: str = ""
    jupyter_folder: str = DEFAULT_JUPYTER_FOLDER
    jupyter_verify_tls: bool = True
    agent: str = "api"
    # CLI agents: the program (a name on the PATH or a full path).
    agent_command: str = ""

    @property
    def jupyter(self) -> bool:
        return bool(self.jupyter_url)


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
    "auto_run": "off",
    "jupyter_url": "",
    "jupyter_token": "",
    "jupyter_password": "",
    "jupyter_folder": DEFAULT_JUPYTER_FOLDER,
    "jupyter_verify_tls": True,
    "agent": "api",
    "ollama_url": OLLAMA_URL,
    "ollama_model": "",
    "claude_command": "claude",
    "claude_model": "",
    "codex_command": "codex",
    "codex_model": "",
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


def agent_of(data: dict) -> str:
    agent = str(data.get("agent") or "api").strip()
    return agent if agent in AGENTS else "api"


def model_name(model: str) -> str:
    """How menus show a model; "" is a CLI agent's own default."""
    return model or "default model"


def ai_problem(data: dict) -> str | None:
    """Why `data` cannot be used to talk to the AI model, or None."""
    agent = agent_of(data)
    if agent in CLI_AGENTS:
        if not str(data.get(AGENT_KEYS[agent]["command"]) or "").strip():
            return f"Set the {AGENT_LABELS[agent]} command in the settings."
        return None
    if agent == "ollama":
        if not str(data.get("ollama_url") or "").strip():
            return "Set the Ollama URL in the settings."
        if not str(data.get("ollama_model") or "").strip():
            return "Choose an Ollama model in the settings."
        return None
    if not str(data.get("ai_url", "")).strip():
        return "Set the AI URL in the settings."
    if not str(data.get("ai_model", "")).strip():
        return "Set the AI model in the settings."
    if not ai_token(data) and str(data.get("ai_url", "")).strip().rstrip("/") == KI_TOOLBOX_URL:
        return f"Set the KI-Toolbox token in the settings (or {AI_TOKEN_ENV})."
    return None


def jupyter_problem(data: dict) -> str | None:
    """Why the JupyterLab settings cannot be used, or None (also when it is off)."""
    if not str(data.get("jupyter_url", "")).strip():
        return None
    if not (str(data.get("jupyter_token", "")).strip() or str(data.get("jupyter_password", ""))):
        return "Set the JupyterLab token or password in the settings."
    return None


def jupyter_folder(data: dict) -> str:
    """The folder below the Jupyter root, without leading/trailing slashes."""
    return str(data.get("jupyter_folder") or "").strip().strip("/\\") or DEFAULT_JUPYTER_FOLDER


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
    auto_run = str(data.get("auto_run") or "off").strip()
    if auto_run not in AUTO_RUN_MODES:
        auto_run = "off"
    agent = agent_of(data)
    keys = AGENT_KEYS[agent]
    return Config(
        url=str(data["url"]).strip().rstrip("/"),
        user=str(data["user"]).strip(),
        password=os.environ.get("CHEMOTION_PASSWORD") or str(data["password"]),
        verify_tls=bool(data.get("verify_tls", True)),
        ai_url=str(data.get(keys.get("url", "")) or "").strip().rstrip("/"),
        ai_token=ai_token(data) if agent == "api" else "",
        ai_model=str(data.get(keys["model"]) or "").strip(),
        execution_timeout=max(10, timeout),
        prompt_width=max(360, width),
        hotkey=str(data.get("hotkey") or "").strip(),
        auto_run=auto_run,
        # Incomplete JupyterLab settings switch it off (jupyter_problem() says why).
        jupyter_url="" if jupyter_problem(data) else str(data.get("jupyter_url") or "").strip().rstrip("/"),
        jupyter_token=str(data.get("jupyter_token") or "").strip(),
        jupyter_password=str(data.get("jupyter_password") or ""),
        jupyter_folder=jupyter_folder(data),
        jupyter_verify_tls=bool(data.get("jupyter_verify_tls", True)),
        agent=agent,
        agent_command=str(data.get(keys.get("command", "")) or "").strip(),
    )
