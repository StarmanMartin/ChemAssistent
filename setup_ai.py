"""Set up the AI endpoint in the app config (used by install.sh and install.ps1).

Fills a missing ai_url/ai_model with the KIT KI-Toolbox defaults (and turns
an opencode-era model id like ki-toolbox/complex-local into alias.complex-local).
If the config has no ai_token yet, it takes KI_TOOLBOX_API_KEY from the
environment (where older installs put it) or asks for the key. Other agents
(Ollama, Claude Code, Codex; config key `agent`) need no token. Saving moves
the passwords and tokens into the system keyring when there is one.
Standard library (and PyGObject's Secret typelib on Linux) only.
"""
from __future__ import annotations

import getpass
import os
import sys

from config import (AGENT_LABELS, AI_TOKEN_ENV, KI_TOOLBOX_URL, agent_of, keyring_problem, read_raw_config,
                    save_raw_config, secret_storage)


def main() -> None:
    data = read_raw_config()  # fills defaults and migrates the model id
    if keyring_problem():
        # Saving the secrets it could not read would delete them.
        print(f"Warning: could not read the system keyring ({keyring_problem()}); left the config as it is.")
        return
    if agent_of(data) != "api":
        save_raw_config(data)
        print(f"Agent: {AGENT_LABELS[agent_of(data)]}; no API token needed.")
        print(f"Passwords and tokens: {secret_storage(data)}")
        return
    if str(data.get("ai_token") or "").strip():
        print("AI token already set.")
    elif os.environ.get(AI_TOKEN_ENV, "").strip():
        data["ai_token"] = os.environ[AI_TOKEN_ENV].strip()
        print(f"Took the AI token from {AI_TOKEN_ENV}.")
    elif sys.stdin.isatty() and str(data.get("ai_url", "")).rstrip("/") == KI_TOOLBOX_URL:
        key = getpass.getpass(
            f"Enter your KI-Toolbox API key ({KI_TOOLBOX_URL.split('/api')[0]}), or leave it empty to skip: "
        ).strip()
        if key:
            data["ai_token"] = key
    save_raw_config(data)
    print(f"Passwords and tokens: {secret_storage(data)}")
    if not str(data.get("ai_token") or "").strip():
        print("Warning: no AI token set. Enter it in ChemAssistent Settings (API token).")


if __name__ == "__main__":
    main()
