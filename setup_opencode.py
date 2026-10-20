"""Set up opencode for the KIT KI-Toolbox (used by install.sh and install.ps1).

Usage: setup_opencode.py <opencode.json> <app config.json>

Adds the `ki-toolbox` provider (OpenAI-compatible, key from the environment
variable KI_TOOLBOX_API_KEY) to the user's opencode config unless it is
already there; everything else in the file is kept. An empty `ai_model` in
the app config becomes DEFAULT_MODEL.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROVIDER = "ki-toolbox"
DEFAULT_MODEL = f"{PROVIDER}/complex-local"
MODELS = {
    "complex-local": ("alias.complex-local", "Complex Model (Local) - GLM-5.3"),
    "medium-local": ("alias.medium-local", "Medium Model (Local) - DeepSeek V4 Flash"),
    "simple-local": ("alias.simple-local", "Simple Model (Local) - Mistral 4 Small"),
    "complex-gpt": ("alias.complex-gpt", "Complex Model (GPT) - GPT-5.6 Sol"),
    "medium-gpt": ("alias.medium-gpt", "Medium Model (GPT) - GPT-5.6 Terra"),
    "simple-gpt": ("alias.simple-gpt", "Simple Model (GPT) - GPT-5.6 Luna"),
    "claude-opus-5": ("google.claude-opus-5", "Claude Opus 5"),
    "claude-sonnet-5": ("google.claude-sonnet-5", "Claude Sonnet 5"),
    "gemini-3.8-flash": ("google.gemini-3.8-flash", "Gemini 3.8 Flash"),
}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # Private from the start (the app config holds the password), then the
    # mode of the file it replaces.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    if path.exists():
        tmp.chmod(path.stat().st_mode & 0o777)
    os.replace(tmp, path)


def add_provider(path: Path) -> None:
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        print(f"Warning: {path} is not plain JSON ({exc}); add the KI-Toolbox provider yourself.")
        return
    providers = config.setdefault("providers", {})
    if PROVIDER in providers:
        print(f"KI-Toolbox provider already configured in {path}.")
        return
    config.setdefault("$schema", "https://opencode.ai/config.json")
    config.setdefault("model", DEFAULT_MODEL)
    providers[PROVIDER] = {
        "name": "KI-Toolbox (KIT)",
        "env": ["KI_TOOLBOX_API_KEY"],
        "package": "@opencode/ai/providers/openai-compatible",
        "settings": {"baseURL": "https://ki-toolbox.scc.kit.edu/api/v1"},
        "models": {key: {"modelID": model_id, "name": name} for key, (model_id, name) in MODELS.items()},
    }
    write_json(path, config)
    print(f"Added the KI-Toolbox provider to {path}.")


def default_model(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not config.get("ai_model"):
        config["ai_model"] = DEFAULT_MODEL
        write_json(path, config)
        print(f"Set ai_model to {DEFAULT_MODEL}.")


def main() -> None:
    opencode_config, app_config = map(Path, sys.argv[1:3])
    add_provider(opencode_config)
    default_model(app_config)


if __name__ == "__main__":
    main()
