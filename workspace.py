"""The opencode workspace the chemotion agent runs in (no GTK).

opencode reads opencode.json from its working directory. write_opencode_config()
regenerates it before every conversation, because the model, the paths and
the socket can change. The agent's only way to reach Chemotion is the
`chemotion_execute_python` tool of mcp_server.py, which waits for the user's
confirmation in the app; every other tool except reading the reference files
is denied. opencode's own agents may run shell commands: "plan" is disabled,
and "build" is replaced by a copy of the chemotion agent, because the TUI
uses "build" (and opencode's default model) for a message sent before it
loaded the config; a disabled "build" silently drops that message. The app
waits for the agent (see AGENT_LABEL), this is the safety net.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from config import STATE_DIR, WORKSPACE_DIR, Config, write_private_json


AGENT = "chemotion"
TOOL = "chemotion_execute_python"
REFERENCE = "CHEMOTION_API.md"
SOURCE_DIR = "chemotionapi"
MCP_SERVER = Path(__file__).resolve().with_name("mcp_server.py")
# Generous: the call waits for the user to read and confirm the code.
TOOL_TIMEOUT_MS = 6 * 60 * 60 * 1000
DISABLED_AGENTS = ("plan",)
FALLBACK_AGENT = "build"
# How the TUI footer names the agent once the workspace config is loaded.
AGENT_LABEL = "Chemotion"
# The TUI ignores the configured model and starts with the model it used last
# (state/opencode/model.json), and reopens the tabs of its last run. opencode
# gets this private state directory, whose only recent model is the configured one.
TUI_STATE_DIR = STATE_DIR / "opencode-state"

SYSTEM_PROMPT = f"""\
You are ChemAssistent, an assistant for the Chemotion electronic lab notebook (ELN).
The user talks to you in a terminal; next to it is a result panel.

How you work with Chemotion:
- The only way to read or change data in Chemotion is the tool `{TOOL}`. It runs
  Python code with the `chemotion_api` package (branch version-3). A logged-in
  `instance` (chemotion_api.Instance) is predefined: never log in, never ask for
  credentials, never use `requests` against the server directly.
- Before you write code for the first time in a conversation, read `{REFERENCE}`
  (the API reference generated from the package docs). If it is not enough, grep or
  read the package source in `{SOURCE_DIR}/chemotion_api/`. Never guess method names.
- The user sees every snippet in the result panel and must confirm it before it
  runs. They may edit it or reject it with a reason; the tool answer tells you
  what happened and which code really ran. Respect a rejection; do not re-send
  the same code.
- Set the variable `result` to what the user should see. A list of flat dicts is
  shown as a table, a dict as a key/value table, anything else as text. Keep
  results small and readable (only useful columns, no huge JSON blobs);
  print() is shown too.
- Prefer small read-only steps to find ids and names before changing anything.
  Say clearly in `purpose` whether the code changes data. Never delete or
  overwrite data unless the user asked for exactly that.
- Elements are loaded through collections, e.g.
  `instance.get_root_collection().get_collection('Name').get_samples()`;
  new elements must be `.save()`d.

Answer in the language of the user. Keep terminal answers short: the data is in
the result panel, so summarize instead of repeating it.
"""


def reference_ready() -> bool:
    return (WORKSPACE_DIR / REFERENCE).is_file()


def console_python() -> str:
    """The interpreter for stdio children: on Windows python.exe next to
    pythonw.exe, which the app runs under and which has no usable stdio."""
    path = Path(sys.executable)
    if path.name.lower() == "pythonw.exe" and path.with_name("python.exe").is_file():
        return str(path.with_name("python.exe"))
    return sys.executable


def build_config(config: Config, server_env: dict[str, str]) -> dict:
    deny_all = [
        {"action": "*", "resource": "*", "effect": "deny"},
        {"action": "read", "resource": "*", "effect": "allow"},
        {"action": "grep", "resource": "*", "effect": "allow"},
        {"action": "glob", "resource": "*", "effect": "allow"},
        {"action": "list", "resource": "*", "effect": "allow"},
        # Multiple-choice questions to the user in the TUI.
        {"action": "question", "resource": "*", "effect": "allow"},
        {"action": TOOL, "resource": "*", "effect": "allow"},
        # Reading stays inside the workspace.
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    agents: dict = {name: {"disabled": True} for name in DISABLED_AGENTS}
    agents[AGENT] = {
        "description": "Chemotion ELN assistant: runs confirmed chemotion_api code.",
        "mode": "primary",
        "color": "#2a9fd8",
        "system": SYSTEM_PROMPT,
        "permissions": deny_all,
    }
    if config.ai_model:
        agents[AGENT]["model"] = config.ai_model
    agents[FALLBACK_AGENT] = dict(agents[AGENT])
    data = {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": AGENT,
        "permissions": deny_all,
        "agents": agents,
        "mcp": {
            "servers": {
                "chemotion": {
                    "type": "local",
                    "command": [console_python(), str(MCP_SERVER)],
                    "environment": server_env,
                    # Offer the tool directly, not wrapped in opencode's
                    # JavaScript "execute" tool.
                    "codemode": False,
                    "timeout": {"execution": TOOL_TIMEOUT_MS},
                }
            }
        },
    }
    if config.ai_model:
        data["model"] = config.ai_model
    return data


def write_opencode_config(config: Config, server_env: dict[str, str]) -> Path:
    """`server_env` tells the MCP server how to reach the app (ipc.Listener.environment())."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKSPACE_DIR / "opencode.json"
    write_private_json(path, build_config(config, server_env))
    return path


def tui_environment(config: Config) -> dict[str, str]:
    """Extra environment for the opencode TUI: its own state directory, so it
    starts with config.ai_model and without the tabs of earlier conversations."""
    state = TUI_STATE_DIR / "opencode"
    shutil.rmtree(state / "latest", ignore_errors=True)  # open tabs of the last run
    model_file = state / "model.json"
    if "/" in config.ai_model:
        provider, model = config.ai_model.split("/", 1)
        model = model.split("#", 1)[0]
        write_private_json(
            model_file,
            {"recent": [{"providerID": provider, "modelID": model}], "favorite": [], "variant": {}},
        )
    else:
        model_file.unlink(missing_ok=True)
    return {"XDG_STATE_HOME": str(TUI_STATE_DIR)}
