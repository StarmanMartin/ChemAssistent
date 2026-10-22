"""Build the Windows bundle of ChemAssistent with PyInstaller.

    py -3.14 windows\\build.py

Result: dist\\ChemAssistent-windows-x64.zip, one folder with
ChemAssistent.exe, chem-assistent-runner.exe, Python, GTK 3, chemotion_api,
the agent's workspace and License.md (windows/licenses.py: the licenses of
everything in it), which install.ps1 unpacks. No MSYS2: GTK 3,
GtkSourceView and PyGObject come from gvsbuild's release
(https://github.com/wingtk/gvsbuild), whose PyGObject wheel is built for one
Python version (3.14 for GTK_RELEASE), so run this with that Python.
Everything else goes to build\\windows\\ (downloads, build venv, PyInstaller's
work files) and dist\\; nothing is installed system-wide.
.github/workflows/windows.yml runs it. Standard library only.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "windows"
DOWNLOADS = BUILD / "download"
WORKSPACE = BUILD / "workspace"
DIST = ROOT / "dist"
PACKAGE = DIST / "ChemAssistent-windows-x64.zip"

GTK_RELEASE = "2026.8.0"
GTK_URL = f"https://github.com/wingtk/gvsbuild/releases/download/{GTK_RELEASE}/GTK3_Gvsbuild_{GTK_RELEASE}_x64.zip"
CHEMOTION_REPO = "https://github.com/Chemotion/chemotionapi"
CHEMOTION_REF = "version-3"
# chemotion_api's dependencies the bundle leaves out: windows/magic.py replaces
# python-magic, which needs libmagic.
SKIP_DEPENDENCIES = {"python-magic"}
# Parts of the chemotionapi checkout the agent does not need (docker setup and
# sample files of the test instance, with long file names).
WORKSPACE_SKIP = {"test_config"}
PYINSTALLER = "pyinstaller>=6.16,<7"


def run(command: list, env: dict | None = None) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run([str(part) for part in command], check=True, env=env)


def download(url: str, target: Path) -> Path:
    """Download `url` to `target` unless it is already there."""
    if not target.is_file():
        print(f"Downloading {url} ...", flush=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(url) as response, open(partial, "wb") as handle:
            shutil.copyfileobj(response, handle)
        partial.replace(target)
    return target


def fetch_gtk() -> Path:
    """gvsbuild's GTK 3 release, unpacked into build/windows/gtk-<release>."""
    gtk = BUILD / f"gtk-{GTK_RELEASE}"
    if not (gtk / "bin").is_dir():
        archive = download(GTK_URL, DOWNLOADS / Path(GTK_URL).name)
        for old in BUILD.glob("gtk-*"):
            shutil.rmtree(old)
        print(f"Unpacking {archive.name} ...", flush=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(gtk)
    return gtk


def gtk_wheels(gtk: Path) -> list[Path]:
    """The PyGObject and pycairo wheels of the GTK release, for this Python."""
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    wheels = sorted((gtk / "wheels").glob("*.whl"))
    names = {wheel.name.split("-")[0].lower() for wheel in wheels}
    if not {"pygobject", "pycairo"} <= names:
        sys.exit(f"No PyGObject/pycairo wheels in {gtk / 'wheels'}.")
    if not all(f"-{tag}-" in wheel.name for wheel in wheels):
        sys.exit(f"gvsbuild {GTK_RELEASE} has wheels for another Python ({', '.join(w.name for w in wheels)}); "
                 f"run this script with that Python version, not {sys.version.split()[0]}.")
    return wheels


def fetch_chemotionapi() -> tuple[Path, str]:
    """The chemotionapi branch as build/windows/workspace/chemotionapi and its commit."""
    archive = DOWNLOADS / f"chemotionapi-{CHEMOTION_REF}.zip"
    archive.unlink(missing_ok=True)  # always the newest commit of the branch
    download(f"{CHEMOTION_REPO}/archive/refs/heads/{CHEMOTION_REF}.zip", archive)
    checkout = WORKSPACE / "chemotionapi"
    shutil.rmtree(WORKSPACE, ignore_errors=True)
    checkout.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        # GitHub puts the commit id into the archive's comment.
        commit = bundle.comment.decode("ascii", "replace").strip()
        for member in bundle.infolist():
            # <repo>-<ref>/<path>: drop the top folder and the skipped parts.
            parts = Path(member.filename).parts[1:]
            if not parts or parts[0] in WORKSPACE_SKIP or member.is_dir():
                continue
            target = checkout.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)
    return checkout, commit


def chemotionapi_dependencies(checkout: Path) -> list[str]:
    project = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    keep = []
    for requirement in project.get("dependencies", []):
        name = requirement.split(";")[0]
        for separator in "<>=!~[ ":
            name = name.split(separator)[0]
        if name.strip().lower() not in SKIP_DEPENDENCIES:
            keep.append(requirement)
    return keep


def app_commit() -> str:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> int:
    if sys.platform != "win32":
        sys.exit("The Windows bundle can only be built on Windows (PyInstaller does not cross-compile).")
    gtk = fetch_gtk()
    wheels = gtk_wheels(gtk)
    # GTK's DLLs and tools for PyGObject, PyInstaller's analysis and make_ico.py;
    # windows/ for magic.py wherever chemotion_api is imported.
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([str(gtk / "bin"), env.get("PATH", "")])
    shim_env = {**env, "PYTHONPATH": str(ROOT / "windows")}

    python = BUILD / "venv" / "Scripts" / "python.exe"
    config = BUILD / "venv" / "pyvenv.cfg"
    version = f"version = {sys.version_info.major}.{sys.version_info.minor}."
    if not python.is_file() or version not in config.read_text(encoding="utf-8"):
        print("Creating the build venv ...", flush=True)
        venv.EnvBuilder(with_pip=True, clear=True).create(BUILD / "venv")
    checkout, chemotion_commit = fetch_chemotionapi()
    pip = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    run([*pip, "--force-reinstall", "--no-deps", *wheels], env)
    run([*pip, "-r", ROOT / "requirements.txt", PYINSTALLER, *chemotionapi_dependencies(checkout)], env)
    run([*pip, "--no-deps", "--force-reinstall", checkout], env)

    run([python, ROOT / "gen_api_reference.py", checkout, WORKSPACE / "CHEMOTION_API.md"], shim_env)
    run([python, ROOT / "windows" / "make_ico.py", ROOT / "chem-assistent.svg", BUILD / "chem-assistent.ico"], env)
    run([python, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", BUILD / "dist", "--workpath", BUILD / "pyinstaller",
         ROOT / "windows" / "chem-assistent.spec"], shim_env)

    bundle = BUILD / "dist" / "ChemAssistent"
    shutil.copy2(ROOT / "uninstall.ps1", bundle / "uninstall.ps1")
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE.txt")
    (bundle / "build.json").write_text(json.dumps({
        "app_commit": app_commit(),
        "chemotionapi": f"{CHEMOTION_REF} {chemotion_commit}",
        "gtk": f"gvsbuild {GTK_RELEASE}",
        "python": sys.version.split()[0],
        "built": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }, indent=2) + "\n", encoding="utf-8")
    # License.md with every third-party license; fails when one is missing.
    run([python, ROOT / "windows" / "licenses.py", bundle, gtk])
    # Without GTK on PATH: the bundle must find everything in itself.
    run([bundle / "chem-assistent-runner.exe", "--self-test"])

    DIST.mkdir(exist_ok=True)
    PACKAGE.unlink(missing_ok=True)
    shutil.make_archive(str(PACKAGE.with_suffix("")), "zip", root_dir=bundle)
    print(f"Built {PACKAGE} ({PACKAGE.stat().st_size // 1_000_000} MB, chemotionapi {chemotion_commit[:7]}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
