# PyInstaller spec of the Windows build; windows/build.py prepares its inputs
# in build/windows/ (gvsbuild's GTK 3 on PATH, the build venv, the agent's
# workspace and the icon) and runs it.
#
# One folder with two programs on the same _internal\ (both run entry.py):
#   ChemAssistent.exe          windowed: the app and the settings
#   chem-assistent-runner.exe  console: runs confirmed snippets over stdio
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (set by PyInstaller)
BUILD = ROOT / "build" / "windows"
ICON = str(BUILD / "chem-assistent.ico")

# Confirmed snippets may import any standard module, not only those the app
# uses: bundle the whole standard library except GUI toolkits, tests and
# installers.
STDLIB_SKIP = {"antigravity", "ensurepip", "idlelib", "lib2to3", "msilib", "test", "this", "tkinter",
               "turtle", "turtledemo", "venv"}
stdlib = []
for name in sorted(sys.stdlib_module_names):
    if name.startswith("_") or name in STDLIB_SKIP:
        continue
    stdlib.append(name)
    spec = importlib.util.find_spec(name)
    if spec is not None and spec.submodule_search_locations:  # a package
        stdlib += collect_submodules(name, filter=lambda module: ".test" not in module)

datas, binaries, hiddenimports = collect_all("chemotion_api")
datas += [
    (str(ROOT / "chem-assistent.svg"), "."),
    (str(ROOT / "chem-assistent-panel.svg"), "."),
    # CHEMOTION_API.md and the chemotionapi checkout (config.WORKSPACE_DIR).
    (str(BUILD / "workspace"), "workspace"),
]
hiddenimports += stdlib + [
    # entry.py imports them inside functions.
    "app", "settings", "chemotion_runner", "setup_ai",
    "gi._gi_cairo", "cairo",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "windows" / "entry.py")],
    # windows/ for magic.py, the stand-in for python-magic.
    pathex=[str(ROOT), str(ROOT / "windows")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "test", "idlelib", "lib2to3"],
    hooksconfig={
        "gi": {
            # gvsbuild's GTK 3 package also has GTK 4 typelibs.
            "module-versions": {"Gtk": "3.0", "Gdk": "3.0", "GtkSource": "4"},
            "icons": ["Adwaita", "hicolor"],
            "languages": ["de"],
        },
    },
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

app_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ChemAssistent",
    console=False,
    icon=ICON,
    upx=False,
)
runner_exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chem-assistent-runner",
    console=True,
    icon=ICON,
    upx=False,
)
COLLECT(  # noqa: F821
    app_exe,
    runner_exe,
    a.binaries,
    a.datas,
    upx=False,
    name="ChemAssistent",
)
