"""Entry point of the Windows build (PyInstaller, see chem-assistent.spec).

Both programs of the bundle run this file:

    ChemAssistent.exe [--show]               the app (app.py)
    ChemAssistent.exe --settings             the settings (settings.py)
    chem-assistent-runner.exe                one confirmed snippet (chemotion_runner.py, stdio)
    chem-assistent-runner.exe --setup-ai     install.ps1: AI endpoint and token (setup_ai.py)
    chem-assistent-runner.exe --mcp-bridge --session-file <path>
                                             a CLI agent's tools (mcp_bridge.py, stdio)
    chem-assistent-runner.exe --self-test    build.ps1: checks that the bundle is complete

ChemAssistent.exe has no console; chem-assistent-runner.exe is a console
program, so its stdio works (app.py starts it without a console window).
"""
from __future__ import annotations

import sys
from pathlib import Path


RUNNER_PROGRAM = "chem-assistent-runner"


def self_test() -> int:
    """Import everything the app needs and load the data files GTK finds at
    run time (typelibs, the SVG loader, GtkSourceView's language specs)."""
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("GtkSource", "4")
    from gi.repository import GdkPixbuf, GtkSource  # type: ignore

    import app  # noqa: F401  (GTK, agent, history, ipc, ...)
    import chemotion_api  # noqa: F401
    import mcp_bridge  # noqa: F401
    import settings  # noqa: F401
    import workspace

    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(app.ICON_FILE), 32, 32)
    language = GtkSource.LanguageManager.get_default().get_language("python3")
    problems = []
    if pixbuf is None:
        problems.append("the SVG icon does not load")
    if language is None:
        problems.append("GtkSourceView has no Python language spec")
    if not workspace.reference_ready():
        problems.append(f"{workspace.REFERENCE} is missing in {workspace.WORKSPACE_DIR}")
    if not workspace.skill_lines():
        problems.append("the chemotionapi skills are missing")
    for problem in problems:
        print(f"Self-test: {problem}", file=sys.stderr)
    if not problems:
        print("Self-test passed.")
    return 1 if problems else 0


def main() -> int:
    args = sys.argv[1:]
    if Path(sys.executable).stem.lower() == RUNNER_PROGRAM:
        if args == ["--setup-ai"]:
            import setup_ai

            setup_ai.main()
            return 0
        if args == ["--self-test"]:
            return self_test()
        if args[:1] == ["--mcp-bridge"]:
            import mcp_bridge

            mcp_bridge.main(args[1:])
            return 0
        import chemotion_runner

        chemotion_runner.main()
        return 0
    if args[:1] == ["--settings"]:
        import settings

        settings.main()
        return 0
    import app

    app.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
