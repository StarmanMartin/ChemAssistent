"""Start, stop and check the app (no GTK).

Linux: the systemd user service. Windows: there is no service manager, so
app.py records its process id in PID_FILE and this module starts it with
pythonw and terminates it by that id.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config import STATE_DIR, WINDOWS


SERVICE = "chem-assistent.service"
PID_FILE = STATE_DIR / "app.pid"
APP_SCRIPT = Path(__file__).with_name("app.py")


class ControlError(RuntimeError):
    pass


# --- Linux: systemd --------------------------------------------------------
def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args, SERVICE],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControlError(str(exc)) from exc


def _systemctl_checked(*args: str) -> None:
    result = _systemctl(*args)
    if result.returncode != 0:
        raise ControlError(result.stderr.strip() or f"exit code {result.returncode}")


# --- Windows: PID file -----------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """True if `pid` is a running Python process (process ids get reused)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        size = wintypes.DWORD(1024)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return True
        return "python" in Path(name.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _recorded_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return pid if pid != os.getpid() and _pid_alive(pid) else None


def pythonw() -> str:
    # pythonw has no console window; fall back to the current interpreter.
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else sys.executable


def write_pid() -> None:
    """Called by app.py on Windows when it starts."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")


def remove_pid() -> None:
    try:
        if PID_FILE.read_text(encoding="ascii").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


# --- Both ------------------------------------------------------------------
def is_running() -> bool:
    if WINDOWS:
        return _recorded_pid() is not None
    try:
        return _systemctl("is-active", "--quiet").returncode == 0
    except ControlError:
        return False


def start() -> None:
    if not WINDOWS:
        _systemctl_checked("start")
        return
    if is_running():
        return
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [pythonw(), str(APP_SCRIPT)],
            cwd=str(APP_SCRIPT.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise ControlError(str(exc)) from exc


def stop() -> None:
    if not WINDOWS:
        _systemctl_checked("stop")
        return
    pid = _recorded_pid()
    if pid is not None:
        try:
            # TerminateProcess on Windows. An open opencode console stays open.
            os.kill(pid, 15)
        except OSError as exc:
            raise ControlError(str(exc)) from exc
    try:
        PID_FILE.unlink()
    except OSError:
        pass
