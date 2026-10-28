from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import keystore


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
# The settings that are secrets. With a system keyring (keystore.py) they live
# there, and config.json has them empty plus "secret_store": "keyring";
# without one, or with "use_keyring": false, they stay in config.json (0600).
# CHEMOTION_PASSWORD and KI_TOOLBOX_API_KEY still override both.
SECRET_KEYS = {
    "password": "Chemotion password",
    "ai_token": "AI API token",
    "jupyter_token": "JupyterLab token",
    "jupyter_password": "JupyterLab password",
    "github_token": "GitHub token",
}
SECRET_STORE_KEY = "secret_store"
KEYRING = "keyring"
# Read the keyring again after this long even if config.json did not change
# (secrets edited in Seahorse or the Credential Manager) ...
SECRET_TTL_S = 300
# ... and after it failed to answer, at most this often.
SECRET_RETRY_S = 30
# How long the main loop of the app and the settings waits for the keyring
# (it may ask to be unlocked); limit_main_thread_wait().
MAIN_THREAD_KEYRING_WAIT_S = 3
# How long save_raw_config() waits for another program's save (they take
# milliseconds unless the keyring asks to be unlocked); then OSError.
SAVE_LOCK_WAIT_S = 10
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
    # Developer mode: chemotion_api branches in the settings, and the agent
    # reports chemotion_api bugs as GitHub issues (issues.py).
    developer_mode: bool = False
    github_token: str = ""

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
    "developer_mode": False,
    "github_token": "",
    "use_keyring": True,
}


def ensure_example_config() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        write_private_json(CONFIG_FILE, EXAMPLE)


def _read_file() -> dict:
    ensure_example_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


class RawConfig(dict):
    """What read_raw_config() returns: the config, and in `keyring_problem`
    why its secrets from the keyring are incomplete ("" when they are, or
    when none come from there): the keyring failed, or has not answered yet.
    load_config() says so then (not "set the password"), and a save would
    delete the secrets."""

    keyring_problem = ""


def read_raw_config(secrets: bool = True, retry: bool = False) -> RawConfig:
    """The config file as a dict with the secrets from the keyring (unless
    not `secrets`; `retry`: read again after a failure just before), without
    validation (for the settings app)."""
    data = RawConfig({**EXAMPLE, **_read_file()})
    if secrets and data.get(SECRET_STORE_KEY) == KEYRING:
        values, data.keyring_problem = _keyring_secrets(retry)
        for key, value in values.items():
            # A value typed into config.json wins; migrate_secrets() moves it.
            if not str(data.get(key) or ""):
                data[key] = value
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


_secrets_lock = threading.Lock()
# (config.json's stamp, monotonic time of the read, its error or "", the secrets)
_secrets_cache: tuple[object, float, str, dict[str, str]] | None = None
# The thread that reads the keyring for _keyring_secrets().
_reader: _Reader | None = None
# Counts the saves that set or dropped the cache: a read that began before
# one must not put its older secrets back.
_saves = 0
# How long _keyring_secrets() in the main thread waits for the keyring (None:
# until it answers). GUI processes set it (limit_main_thread_wait()): their
# main loop must not hang while the keyring asks to be unlocked.
_main_thread_wait: float | None = None
NOT_ANSWERED = "the system keyring has not answered yet (is it locked?)"


def limit_main_thread_wait(seconds: float) -> None:
    global _main_thread_wait
    _main_thread_wait = seconds


def _file_stamp() -> tuple | None:
    try:
        stat = CONFIG_FILE.stat()
    except OSError:
        return None
    return str(CONFIG_FILE), stat.st_ino, stat.st_mtime_ns, stat.st_size


def _cache_valid(stamp: tuple | None) -> bool:
    if _secrets_cache is None or _secrets_cache[0] != stamp:
        return False
    age = time.monotonic() - _secrets_cache[1]
    return age < (SECRET_RETRY_S if _secrets_cache[2] else SECRET_TTL_S)


class _Reader(threading.Thread):
    """One read of the keyring; its result goes into _secrets_cache unless a
    save changed the cache meanwhile. Started under _secrets_lock."""

    def __init__(self, stamp: tuple | None) -> None:
        super().__init__(name="keyring", daemon=True)
        self.stamp, self.saves = stamp, _saves
        self.error, self.secrets = "", dict.fromkeys(SECRET_KEYS, "")

    def run(self) -> None:
        global _secrets_cache
        try:
            self.secrets.update(keystore.read_all(SECRET_KEYS))
        except Exception as exc:
            # Also what is no KeystoreError (a bug): unrecorded, it would read
            # as "no secrets", and a save then deletes them.
            self.error = str(exc) or type(exc).__name__
            if isinstance(exc, keystore.KeystoreError):
                keystore.LOG.warning("Could not read the secrets from the keyring: %s", exc)
            else:
                keystore.LOG.exception("Reading the keyring failed")
        with _secrets_lock:
            if _saves == self.saves:
                _secrets_cache = (self.stamp, time.monotonic(), self.error, self.secrets)


def _keyring_secrets(retry: bool = False) -> tuple[dict[str, str], str]:
    """The secrets in the keyring ("" for those it has none of) and why they
    are incomplete ("" when they are not): the read failed, or has not
    answered yet.

    load_config() runs for every snippet and in the job watcher, so they are
    read again only when config.json changed (save_raw_config() rewrites it,
    also in other processes), after SECRET_TTL_S, or after a failure every
    SECRET_RETRY_S (at once with `retry`). One thread reads; the main thread
    of a GUI process waits for it at most limit_main_thread_wait() seconds,
    and not at all for a read it did not start. Until the read is done the
    last secrets serve on while config.json is unchanged (they are only
    getting old); after a change, or a failure, none do.
    """
    global _reader
    with _secrets_lock:
        stamp = _file_stamp()
        cache = _secrets_cache
        if _cache_valid(stamp) and not (retry and cache[2]):
            return dict(cache[3]), cache[2]
        started = _reader is None or not _reader.is_alive()
        if started:
            _reader = _Reader(stamp)
            _reader.start()
        reader = _reader
        stale = cache[3] if cache is not None and cache[0] == stamp and not cache[2] else None
    timeout = None
    if _main_thread_wait is not None and threading.current_thread() is threading.main_thread():
        timeout = _main_thread_wait if started else 0
    reader.join(timeout)
    if reader.is_alive():
        return (dict(stale), "") if stale is not None else (dict.fromkeys(SECRET_KEYS, ""), NOT_ANSWERED)
    with _secrets_lock:
        # A save meanwhile cached what it wrote (newer than this read), or
        # dropped the cache (its file write failed).
        if _secrets_cache is not None:
            return dict(_secrets_cache[3]), _secrets_cache[2]
    return dict(reader.secrets), reader.error


def uses_keyring(data: dict) -> bool:
    """Whether the user lets save_raw_config() use the keyring (config key
    use_keyring; the settings write a bool, by hand it may be 0 or "false")."""
    value = data.get("use_keyring", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return bool(value)


def storage_location(use_keyring: bool | None = None) -> str:
    """Where save_raw_config() puts the secrets (`use_keyring`: uses_keyring();
    None: as config.json says), for the settings and the installers. (No
    "secret" in the name: CodeQL counts what such a function returns as one.)"""
    if use_keyring is None:
        use_keyring = uses_keyring(_read_file())
    if not use_keyring:
        return f"{CONFIG_FILE} (the system keyring is switched off)"
    if keystore.available():
        return "Windows Credential Manager" if WINDOWS else "system keyring"
    return f"{CONFIG_FILE} (no system keyring: {keystore.problem()})"


def _lock_file(fd: int, lock: bool = True) -> None:
    """Lock (or unlock) the open file `fd`; OSError while another open file
    holds the lock (also one of this process)."""
    if WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, (fcntl.LOCK_EX | fcntl.LOCK_NB) if lock else fcntl.LOCK_UN)


@contextlib.contextmanager
def _save_lock():
    """Held by every save_raw_config(), across processes (the app's migration,
    the settings, setup_ai.py), so that one save writes the keyring and the
    file before another starts. The system drops it when a process dies; the
    lock file stays. OSError after SAVE_LOCK_WAIT_S."""
    path = CONFIG_FILE.with_name(CONFIG_FILE.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + SAVE_LOCK_WAIT_S
        while True:
            try:
                _lock_file(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError(f"another program is saving the settings ({path} is locked)") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            _lock_file(fd, lock=False)
    finally:
        os.close(fd)


def save_raw_config(data: dict, if_stamp: tuple | None | bool = False) -> bool:
    """Write config.json; with a system keyring the secrets go there instead
    (if storing one fails, all of them stay in the file). With use_keyring
    off they go into the file and leave the keyring. With `if_stamp` (a
    _file_stamp()) it writes nothing, and returns False, if config.json changed
    meanwhile. Raises OSError, also when another save holds _save_lock() too
    long."""
    global _secrets_cache, _saves
    with _save_lock():
        if if_stamp is not False and _file_stamp() != if_stamp:
            return False
        data = dict(data)
        data.pop(SECRET_STORE_KEY, None)
        secrets = {key: str(data.get(key) or "") for key in SECRET_KEYS}
        in_keyring = False
        if not uses_keyring(data):
            # Every time, not only with the marker in config.json: a save that
            # failed halfway left some there without it.
            if keystore.available():
                _forget_keyring_secrets()
        elif keystore.available():
            with _secrets_lock:
                # What the keyring holds, as far as known: an unchanged secret
                # needs no write (a D-Bus call each, and an unlock prompt
                # while the keyring is locked).
                known = (_secrets_cache[3] if _secrets_cache is not None and _secrets_cache[0] == _file_stamp()
                         and not _secrets_cache[2] else {})
            try:
                for key, value in secrets.items():
                    if known.get(key) != value:
                        keystore.write(key, value, f"ChemAssistent: {SECRET_KEYS[key]}")
            except keystore.KeystoreError as exc:
                keystore.LOG.warning("Could not store the secrets in the keyring, keeping them in %s: %s",
                                     CONFIG_FILE, exc)
            else:
                data.update(dict.fromkeys(SECRET_KEYS, ""))
                data[SECRET_STORE_KEY] = KEYRING
                in_keyring = True
        with _secrets_lock:
            _saves += 1  # a read that began before now must not set the cache
            try:
                write_private_json(CONFIG_FILE, data)
            except OSError:
                _secrets_cache = None  # the keyring may have changed already
                raise
            _secrets_cache = (_file_stamp(), time.monotonic(), "", secrets) if in_keyring else None
    return True


def _forget_keyring_secrets() -> None:
    """Delete the secrets from the keyring (the user switched it off)."""
    try:
        for key in SECRET_KEYS:
            keystore.delete(key)
    except keystore.KeystoreError as exc:
        keystore.LOG.warning("Could not delete the secrets from the keyring: %s", exc)


def migrate_secrets() -> bool:
    """Move secrets that config.json holds in plain text into the keyring;
    True when it did."""
    ensure_example_config()
    # Reading the keyring may wait for an unlock: a save in the settings
    # meanwhile must not be overwritten with this older state.
    stamp = _file_stamp()
    raw = _read_file()
    if (not any(str(raw.get(key) or "") for key in SECRET_KEYS) or not uses_keyring(raw)
            or not keystore.available()):
        return False
    data = read_raw_config()
    if data.keyring_problem:  # the secrets already in the keyring are unknown
        return False
    return save_raw_config(data, if_stamp=stamp) and _read_file().get(SECRET_STORE_KEY) == KEYRING


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
    if data.keyring_problem:
        # Not "set the password": it is stored, only not readable now.
        raise ValueError(f"Could not read the passwords and tokens from the system keyring: {data.keyring_problem}.")
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
        developer_mode=bool(data.get("developer_mode", False)),
        github_token=str(data.get("github_token") or "").strip(),
    )
