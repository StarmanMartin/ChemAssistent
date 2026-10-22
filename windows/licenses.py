"""Write License.md, the third-party licenses of the Windows bundle.

    python licenses.py <bundle dir> <gvsbuild GTK dir>

build.py runs it with the build venv's Python (so importlib.metadata sees the
bundled packages) after PyInstaller. It lists what is really in the bundle:
every DLL must belong to a component in COMPONENTS, every Python package
must ship a license file. Otherwise it fails and names what is missing, so a
bundle with incomplete license information is never built. License texts
come from gvsbuild's share/doc and share/licenses, the packages' dist-info,
Python's LICENSE.txt and windows/licenses/ (texts gvsbuild does not ship).
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


HERE = Path(__file__).resolve().parent
VENDORED = HERE / "licenses"
OUTPUT = "License.md"
APP_LICENSE = "LICENSE.txt"
# Who sends the source code of the (L)GPL components on request (the written
# offer in License.md). Until it is filled in, the build warns.
CONTACT_PLACEHOLDER = "[CONTACT: name, e-mail and postal address of the distributor]"
SOURCE_CONTACT = "[CONTACT: David Herrmann, david.herrmann@kit.edu, Hermann-von-Helmholtz-Platz 1, 76344 Eggenstein-Leopoldshafen, Germany]"
# The gvsbuild texts used by several components.
LGPL_2_1 = "share/doc/glib/LGPL-2.1-or-later.txt"


@dataclass(frozen=True)
class Component:
    name: str
    license: str
    url: str
    # Paths in the gvsbuild GTK dir; "vendored:" = windows/licenses/,
    # "python:" = relative to the base Python's prefix.
    texts: tuple[str, ...]
    # File name patterns (lower case) of its DLLs.
    dlls: tuple[str, ...] = ()
    # A path in the bundle's _internal\ that means it is included (data only).
    data: str = ""
    # The pkg-config file in the GTK dir that holds its version.
    pc: str = ""
    note: str = ""


COMPONENTS = (
    # --- Python and the runtime -----------------------------------------------
    Component("Python", "PSF-2.0 (and the licenses of its bundled libraries)", "https://www.python.org",
              ("python:LICENSE.txt",),
              dlls=("python3.dll", "python3*.dll", "libcrypto-3.dll", "libssl-3.dll", "libffi-8.dll",
                    "sqlite3.dll"),
              note="Python's LICENSE.txt also contains the licenses of the libraries of the Windows build of "
                   "Python (OpenSSL, libffi, SQLite, zlib, expat, mpdecimal, bzip2, xz, HACL*, ...)."),
    Component("Microsoft Visual C++ Runtime", "Microsoft Visual Studio license terms (redistributable code)",
              "https://visualstudio.microsoft.com/license-terms/", (),
              dlls=("vcruntime140*.dll", "msvcp140*.dll", "concrt140.dll", "vcomp140.dll"),
              note="Redistributable files of Microsoft Visual C++, distributed unmodified as permitted by the "
                   "Visual Studio license terms."),
    Component("Microsoft Universal C Runtime (UCRT)", "Microsoft Windows SDK license terms (redistributable code)",
              "https://learn.microsoft.com/cpp/windows/universal-crt-deployment", (),
              dlls=("ucrtbase.dll", "api-ms-win-*.dll"),
              note="Redistributable files of the Universal CRT from the Windows SDK, distributed unmodified for "
                   "app-local deployment as permitted by the Windows SDK license terms. Windows 10 and later "
                   "already contain them."),
    # --- GTK 3 and its dependencies (gvsbuild) ---------------------------------
    Component("GLib", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/glib", (LGPL_2_1,),
              dlls=("glib-2.0-0.dll", "gobject-2.0-0.dll", "gio-2.0-0.dll", "gmodule-2.0-0.dll",
                    "gthread-2.0-0.dll", "girepository-2.0-0.dll"), pc="glib-2.0"),
    Component("GObject Introspection", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/gobject-introspection",
              (LGPL_2_1,), dlls=("girepository-1.0-1.dll",), pc="gobject-introspection-1.0"),
    Component("GTK 3", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/gtk", ("share/doc/gtk3/COPYING",),
              dlls=("gtk-3-*.dll", "gdk-3-*.dll", "gailutil-3-*.dll"), pc="gtk+-3.0"),
    Component("GtkSourceView 4", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/gtksourceview",
              ("share/doc/gtksourceview4/COPYING",), dlls=("gtksourceview-4-*.dll",), pc="gtksourceview-4"),
    Component("ATK", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/atk", ("share/doc/atk/COPYING",),
              dlls=("atk-1.0-0.dll",), pc="atk"),
    Component("Pango", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/pango", ("share/doc/pango/COPYING",),
              dlls=("pango-1.0-0.dll", "pangocairo-1.0-0.dll", "pangoft2-1.0-0.dll", "pangowin32-1.0-0.dll"),
              pc="pango"),
    Component("gdk-pixbuf", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/gdk-pixbuf",
              ("share/doc/gdk-pixbuf/COPYING",), dlls=("gdk_pixbuf-2.0-0.dll",), pc="gdk-pixbuf-2.0"),
    Component("librsvg", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/librsvg",
              ("share/doc/librsvg/COPYING.LIB",), dlls=("rsvg-2-2.dll", "pixbufloader_svg.dll"), pc="librsvg-2.0",
              note="librsvg is written in Rust and contains statically linked Rust crates under permissive "
                   "licenses (mostly MIT and/or Apache-2.0); they are listed in Cargo.lock of its source."),
    Component("cairo", "LGPL-2.1-only OR MPL-1.1", "https://www.cairographics.org",
              ("share/doc/cairo/COPYING", "share/doc/pycairo/COPYING-LGPL-2.1", "share/doc/pycairo/COPYING-MPL-1.1"),
              dlls=("cairo-2.dll", "cairo-gobject-2.dll", "cairo-script-interpreter-2.dll"), pc="cairo"),
    Component("pixman", "MIT", "https://gitlab.freedesktop.org/pixman/pixman", ("share/doc/pixman/COPYING",),
              dlls=("pixman-1-0.dll",), pc="pixman-1"),
    Component("HarfBuzz", "MIT", "https://github.com/harfbuzz/harfbuzz", ("share/doc/harfbuzz/COPYING",),
              dlls=("harfbuzz*.dll",), pc="harfbuzz"),
    Component("FreeType", "FTL (chosen from FTL OR GPL-2.0-or-later)", "https://freetype.org",
              ("vendored:FreeType-FTL.txt",), dlls=("freetype-6.dll",),
              note="Portions of this software are copyright (C) The FreeType Project (www.freetype.org). "
                   "All rights reserved."),
    Component("Fontconfig", "MIT-style (HPND)", "https://gitlab.freedesktop.org/fontconfig/fontconfig",
              ("share/doc/fontconfig/COPYING",), dlls=("fontconfig-1.dll",), pc="fontconfig"),
    Component("Expat", "MIT", "https://libexpat.github.io", ("share/doc/expat/COPYING",),
              dlls=("libexpat.dll",), pc="expat"),
    Component("FriBidi", "LGPL-2.1-or-later", "https://github.com/fribidi/fribidi", ("share/doc/fribidi/COPYING",),
              dlls=("fribidi-0.dll",), pc="fribidi"),
    Component("libepoxy", "MIT", "https://github.com/anholt/libepoxy", ("share/doc/libepoxy/COPYING",),
              dlls=("epoxy-0.dll",), pc="epoxy"),
    Component("libffi", "MIT", "https://github.com/libffi/libffi", ("share/doc/libffi/LICENSE",),
              dlls=("ffi-8.dll",), pc="libffi"),
    Component("gettext runtime (libintl)", "LGPL-2.1-or-later", "https://www.gnu.org/software/gettext/",
              (LGPL_2_1,), dlls=("intl.dll", "asprintf.dll")),
    Component("gettext libraries", "GPL-3.0-or-later", "https://www.gnu.org/software/gettext/",
              ("share/doc/gettext/COPYING",), dlls=("gettextlib-*.dll", "gettextsrc-*.dll", "gettextpo.dll",
                                                    "textstyle.dll")),
    Component("win-iconv", "Public domain", "https://github.com/win-iconv/win-iconv",
              ("share/doc/win-iconv/COPYING",), dlls=("iconv.dll",)),
    Component("libjpeg-turbo", "IJG AND BSD-3-Clause AND Zlib", "https://libjpeg-turbo.org",
              ("share/doc/libjpeg-turbo/LICENSE.md", "share/doc/libjpeg-turbo/README.ijg"),
              dlls=("jpeg62.dll", "turbojpeg.dll"), pc="libjpeg",
              note="This software is based in part on the work of the Independent JPEG Group."),
    Component("libpng", "libpng-2.0", "http://www.libpng.org/pub/png/libpng.html", ("share/doc/libpng/LICENSE",),
              dlls=("libpng16.dll",), pc="libpng"),
    Component("LibTIFF", "libtiff", "https://libtiff.gitlab.io/libtiff/",
              ("share/doc/tiff/manual/html/_sources/project/license.rst.txt",), dlls=("tiff.dll",), pc="libtiff-4"),
    Component("PCRE2", "BSD-3-Clause WITH PCRE2-exception", "https://github.com/PCRE2Project/pcre2",
              ("share/doc/pcre2/COPYING",), dlls=("pcre2-*.dll",), pc="libpcre2-8"),
    Component("libxml2", "MIT", "https://gitlab.gnome.org/GNOME/libxml2", ("vendored:libxml2-Copyright.txt",),
              dlls=("xml2-*.dll", "libxml2*.dll"), pc="libxml-2.0"),
    Component("zlib", "Zlib", "https://zlib.net", ("share/doc/zlib/README",), dlls=("zlib1.dll",), pc="zlib"),
    Component("OpenSSL", "Apache-2.0", "https://www.openssl.org", ("vendored:OpenSSL-LICENSE.txt",),
              dlls=("libcrypto-3-x64.dll", "libssl-3-x64.dll"), pc="openssl"),
    # --- Data files collected by PyInstaller's GTK hooks -----------------------
    Component("Adwaita icon theme", "CC-BY-SA-3.0 OR LGPL-3.0-only", "https://gitlab.gnome.org/GNOME/adwaita-icon-theme",
              ("share/licenses/adwaita-icon-theme/COPYING", "share/licenses/adwaita-icon-theme/COPYING_CCBYSA3",
               "share/licenses/adwaita-icon-theme/COPYING_LGPL"), data="share/icons/Adwaita"),
    Component("hicolor icon theme", "GPL-2.0-only", "https://gitlab.freedesktop.org/xdg/default-icon-theme",
              ("share/doc/glib/GPL-2.0-or-later.txt",), data="share/icons/hicolor",
              note="Only the theme's index file. The license text is the GNU GPL version 2."),
    Component("GSettings desktop schemas", "LGPL-2.1-or-later",
              "https://gitlab.gnome.org/GNOME/gsettings-desktop-schemas",
              ("share/doc/gsettings-desktop-schemas/COPYING",), data="share/glib-2.0/schemas"),
)

# gvsbuild's Python bindings: their wheels may come without license files.
GTK_BINDINGS = {
    "pygobject": Component("PyGObject", "LGPL-2.1-or-later", "https://gitlab.gnome.org/GNOME/pygobject",
                           ("share/doc/pygobject/COPYING",)),
    "pycairo": Component("pycairo", "LGPL-2.1-only OR MPL-1.1", "https://github.com/pygobject/pycairo",
                         ("share/doc/pycairo/COPYING", "share/doc/pycairo/COPYING-LGPL-2.1",
                          "share/doc/pycairo/COPYING-MPL-1.1")),
}
# Installed in the build venv, but not part of the bundle (PyInstaller itself
# is listed separately: its bootloader is in the exes).
BUILD_ONLY = {"pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
              "pywin32-ctypes", "packaging"}
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)", re.IGNORECASE)


@dataclass
class Entry:
    name: str
    version: str
    license: str
    url: str
    included: str
    texts: list[tuple[str, str]]  # (file name, text)
    note: str = ""


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").strip("\n")


def resolve_text(spec: str, gtk: Path) -> Path:
    if spec.startswith("vendored:"):
        return VENDORED / spec.split(":", 1)[1]
    if spec.startswith("python:"):
        return Path(sys.base_prefix) / spec.split(":", 1)[1]
    return gtk / spec


def pc_version(gtk: Path, pc: str) -> str:
    try:
        for line in (gtk / "lib" / "pkgconfig" / f"{pc}.pc").read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def component_entry(component: Component, gtk: Path, included: str, version: str,
                    missing: list[str]) -> Entry:
    texts = []
    for spec in component.texts:
        path = resolve_text(spec, gtk)
        if path.is_file():
            texts.append((path.name, read_text(path)))
        else:
            missing.append(f"{component.name}: license text {path} not found")
    return Entry(component.name, version, component.license, component.url, included, texts, component.note)


def native_entries(bundle: Path, gtk: Path, missing: list[str]) -> list[Entry]:
    internal = bundle / "_internal"
    dlls: dict[Component, list[str]] = {}
    for dll in sorted(internal.rglob("*.dll")):
        name = dll.name.lower()
        owner = next((c for c in COMPONENTS if any(fnmatch.fnmatch(name, p) for p in c.dlls)), None)
        if owner is None:
            missing.append(f"{dll.relative_to(bundle)}: no component in COMPONENTS of windows/licenses.py")
        else:
            dlls.setdefault(owner, []).append(dll.name)
    entries = []
    for component in COMPONENTS:
        if component in dlls:
            included = ", ".join(f"`{name}`" for name in dlls[component])
        elif component.data and (internal / component.data).exists():
            included = f"`{component.data}`"
        else:
            continue
        if component.name == "Python":
            version = sys.version.split()[0]
        else:
            version = pc_version(gtk, component.pc) if component.pc else ""
        entries.append(component_entry(component, gtk, included, version, missing))
    return entries


def dist_license(dist: importlib.metadata.Distribution) -> str:
    meta = dist.metadata
    if meta.get("License-Expression"):
        return meta["License-Expression"]
    license_field = (meta.get("License") or "").strip()
    if license_field and len(license_field) <= 80 and "\n" not in license_field:
        return license_field
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return ", ".join(classifiers) or "see the license text"


def dist_url(dist: importlib.metadata.Distribution) -> str:
    meta = dist.metadata
    urls = [value.split(",", 1)[-1].strip() for value in meta.get_all("Project-URL") or []]
    source = [u for u in urls if "github" in u or "gitlab" in u]
    return meta.get("Home-page") or (source or urls or [f"https://pypi.org/project/{meta['Name']}/"])[0]


def python_entries(gtk: Path, missing: list[str]) -> tuple[list[Entry], Entry | None]:
    entries, pyinstaller = [], None
    seen = set()
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: normalize(d.metadata["Name"])):
        key = normalize(dist.metadata["Name"])
        if key in seen or (key in BUILD_ONLY and key != "pyinstaller"):
            continue
        seen.add(key)
        texts = []
        for file in dist.files or []:
            if ".dist-info" in str(file) and LICENSE_FILE.match(file.name):
                texts.append((file.name, read_text(Path(file.locate()))))
        name, version = dist.metadata["Name"], dist.version
        if key in GTK_BINDINGS:
            binding = GTK_BINDINGS[key]
            entry = component_entry(binding, gtk, "Python package", version, missing) if not texts else \
                Entry(binding.name, version, binding.license, binding.url, "Python package", texts)
            entries.append(entry)
            continue
        if not texts:
            missing.append(f"Python package {name} {version}: no license file in its dist-info")
        entry = Entry(name, version, dist_license(dist), dist_url(dist), "Python package", texts)
        if key == "pyinstaller":
            entry.included = "the bootloader of `ChemAssistent.exe` and `chem-assistent-runner.exe`"
            entry.license = "GPL-2.0-or-later WITH Bootloader-exception"
            entry.note = ("The bootloader exception allows distributing the bootloader together with programs "
                          "under any license.")
            pyinstaller = entry
        else:
            if key == "rpds-py":
                entry.note = "Contains statically linked Rust crates (MIT and/or Apache-2.0)."
            entries.append(entry)
    if pyinstaller is None:
        missing.append("PyInstaller is not installed in the build venv")
    return entries, pyinstaller


def fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=2)
    marker = "`" * (longest + 1)
    return f"{marker}text\n{text}\n{marker}"


def render(entries: list[Entry], build: dict) -> str:
    commit = build.get("app_commit", "")
    lines = [
        "# Third-party licenses",
        "",
        "ChemAssistent for Windows is free software under the GNU Affero General Public License v3.0, version 3 "
        f"only (`{APP_LICENSE}` in this folder). Its source code: https://github.com/StarmanMartin/ChemAssistent"
        + (f" (this build: commit `{commit}`)." if commit else "."),
        "",
        "This folder also contains the third-party software listed below, each under its own license. "
        "This file is generated by `windows/licenses.py` when the build is made "
        + (f"({build['built']}) " if build.get("built") else "") + "and lists what this build contains.",
        "",
        "## Notes",
        "",
        "- The libraries under the GNU LGPL (GTK, GLib, Pango, gdk-pixbuf, librsvg, ATK, FriBidi, GtkSourceView, "
        "cairo, libintl, PyGObject, pycairo) are not modified and are dynamically linked: they are separate "
        "DLLs and Python modules in `_internal\\`, and you may replace them with your own builds of compatible "
        "versions.",
        "- FreeType: portions of this software are copyright (C) The FreeType Project (www.freetype.org). All "
        "rights reserved.",
        "- This software is based in part on the work of the Independent JPEG Group.",
        "",
        "## Source code",
        "",
        "The components under the GNU LGPL or GPL in this build (see the table below) are unmodified upstream "
        "releases. Their source code is available from the projects listed below, in the versions given. They "
        "were built with gvsbuild (https://github.com/wingtk/gvsbuild, release "
        f"{build.get('gtk', 'see build.json').replace('gvsbuild ', '')}), whose build recipes are part of that "
        "release.",
        "",
        "**Written offer:** for at least three years after this build was last distributed, anyone who received "
        f"it can obtain from {SOURCE_CONTACT} a complete machine-readable copy of the corresponding source code "
        "of these components, for a charge no more than the cost of performing this distribution (a download is "
        "free of charge). Please name the build (`build.json` in this folder) in the request.",
        "",
        "ChemAssistent's own source code (AGPL-3.0) is at https://github.com/StarmanMartin/ChemAssistent; "
        "`build.json` names the commit of this build and the version of chemotion_api "
        "(https://github.com/Chemotion/chemotionapi, AGPL-3.0-or-later) it contains.",
        "",
        "## Components",
        "",
        "| Component | Version | License | Included as | Source |",
        "|---|---|---|---|---|",
    ]
    for entry in entries:
        cells = [entry.name, entry.version or "-", entry.license, entry.included, entry.url]
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    lines += ["", "## License texts", ""]
    printed: dict[str, str] = {}
    for entry in entries:
        lines += [f"### {entry.name}" + (f" {entry.version}" if entry.version else ""), ""]
        lines += [f"License: {entry.license}. Source: {entry.url}", ""]
        if entry.note:
            lines += [entry.note, ""]
        if not entry.texts:
            lines += ["There is no license text to include for this component.", ""]
        for file_name, text in entry.texts:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in printed:
                lines += [f"`{file_name}`: the same text as under {printed[digest]}.", ""]
                continue
            printed[digest] = f"“{entry.name}”"
            lines += [f"`{file_name}`:", "", fence(text), ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    bundle, gtk = Path(argv[1]), Path(argv[2])
    try:
        build = json.loads((bundle / "build.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        build = {}
    missing: list[str] = []
    if not (bundle / APP_LICENSE).is_file():
        missing.append(f"{APP_LICENSE} (ChemAssistent's own license) is missing in {bundle}")
    python_packages, pyinstaller = python_entries(gtk, missing)
    entries = native_entries(bundle, gtk, missing)
    if pyinstaller is not None:
        entries.insert(1, pyinstaller)
    entries += python_packages
    if missing:
        print("License information is incomplete:", file=sys.stderr)
        for problem in missing:
            print(f"  {problem}", file=sys.stderr)
        return 1
    (bundle / OUTPUT).write_text(render(entries, build), encoding="utf-8")
    print(f"Wrote {bundle / OUTPUT} ({len(entries)} components).")
    if SOURCE_CONTACT == CONTACT_PLACEHOLDER:
        print(f"Warning: the written offer in {OUTPUT} still names {CONTACT_PLACEHOLDER}; set SOURCE_CONTACT "
              "in windows/licenses.py before distributing this build.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
