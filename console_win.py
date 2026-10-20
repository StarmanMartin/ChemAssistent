"""Windows: opencode in its own console window (no GTK).

There is no VTE on Windows, so the opencode TUI cannot be embedded; it runs
in a new console next to the app's result window instead.
"""
from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

from opencode_models import NO_WINDOW


def start(argv: list[str], env: dict[str, str], cwd: str) -> subprocess.Popen:
    return subprocess.Popen(argv, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)


def console_window(pid: int) -> int:
    """The console window of process `pid`, or 0 while it has none yet.

    For a classic console host it is the real window. Under Windows Terminal
    it is a hidden stand-in that cannot be moved, which place() then cannot
    change.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    # Only a process without a console of its own (pythonw) may attach.
    if kernel32.GetConsoleWindow():
        return 0
    if not kernel32.AttachConsole(pid):
        return 0
    try:
        return kernel32.GetConsoleWindow() or 0
    finally:
        kernel32.FreeConsole()


def place(hwnd: int, x: int, y: int, width: int, height: int) -> bool:
    """Move the window to physical screen pixels; False if that failed."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.BOOL]
    if not user32.IsWindowVisible(hwnd):
        return False
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    return bool(user32.MoveWindow(hwnd, x, y, width, height, True))


def kill_tree(pid: int) -> None:
    """End opencode with its server and the MCP server it started."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=NO_WINDOW, check=False)
