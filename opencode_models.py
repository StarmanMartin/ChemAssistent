from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


# Keeps opencode from opening a console window when the app runs under
# pythonw on Windows. 0 elsewhere (creationflags must be 0 there).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_opencode() -> str | None:
    # On Windows, which() also finds opencode.exe / opencode.cmd (npm).
    found = shutil.which("opencode")
    if found:
        return found
    # Apps started from the application menu or systemd do not get the
    # shell's PATH, so also look where the opencode installer puts it.
    candidates = [Path.home() / ".opencode" / "bin" / name for name in ("opencode", "opencode.exe")]
    if os.environ.get("APPDATA"):
        candidates.append(Path(os.environ["APPDATA"]) / "npm" / "opencode.cmd")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def list_models() -> list[str]:
    """Every model opencode offers, as `provider/model` ids."""
    opencode = find_opencode()
    if opencode is None:
        raise RuntimeError("opencode not found (looked in PATH and ~/.opencode/bin)")

    try:
        result = subprocess.run(
            [opencode, "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("opencode models timed out") from None
    except OSError as exc:
        raise RuntimeError(f"Could not run opencode: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise RuntimeError(
            f"opencode models failed: {detail[-1] if detail else result.returncode}"
        )

    models = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "/" in line and " " not in line:
            models.append(line)
    return models
