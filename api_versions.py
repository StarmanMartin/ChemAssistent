"""The chemotionapi versions the workspace can be set up with (stdlib).

A version is a release tag of the chemotionapi repository that starts with
"v" and at least three digits (v312.1.1); other tags (0.3.32, ...) are older
releases the app does not support. A tag vXYZ.a.b belongs to the release line
XYZ, made for the Chemotion ELN X.Y.Z (v312.1.1 -> ELN 3.1.2): users pick the
ELN version (or it is read from the server's /about page) and release_for()
finds the release. setup_workspace.sh records the installed tag in TAG_FILE.
Run as a script it serves the installers:

    python api_versions.py default <workspace dir>
    python api_versions.py choose <workspace dir>   # alias of default

It prints the tag to use on stdout: CHEMOTION_API_TAG if set, else the
installed one if it is still released, else the newest. It never asks.

In developer mode (config `developer_mode`) the settings also offer the
repository's branches; setup_workspace.sh --branch records an installed
branch in BRANCH_FILE instead (the installers only know tags).
"""
from __future__ import annotations

import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

if __package__:
    from .config import WORKSPACE_DIR
else:
    from config import WORKSPACE_DIR


REPO_URL = "https://github.com/Chemotion/chemotionapi.git"
TAG_PATTERN = re.compile(r"v\d{3,}")
TAG_NAME = "chemotionapi.tag"
TAG_FILE = WORKSPACE_DIR / TAG_NAME
TAG_ENV = "CHEMOTION_API_TAG"
BRANCH_NAME = "chemotionapi.branch"
BRANCH_FILE = WORKSPACE_DIR / BRANCH_NAME


class VersionError(Exception):
    pass


def usable(tag: str) -> bool:
    return bool(TAG_PATTERN.match(tag))


def version_key(tag: str) -> tuple:
    """Sorts v312.0.10 after v312.0.9; the text breaks ties (v312.0.1rc1)."""
    return tuple(int(number) for number in re.findall(r"\d+", tag)), tag


def list_refs(repo: str = REPO_URL, timeout: float = 60) -> tuple[list[str], list[str]]:
    """The usable tags of `repo`, newest first, and its branches, sorted by
    name (git ls-remote, no clone)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(["git", "ls-remote", "--tags", "--heads", "--refs", repo],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise VersionError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise VersionError(f"{repo} did not answer within {timeout:.0f} s") from None
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        raise VersionError(lines[-1] if lines else f"git ls-remote failed ({done.returncode})")
    refs = [line.split("\t", 1)[-1] for line in done.stdout.splitlines()]
    tags = {ref.removeprefix("refs/tags/") for ref in refs if ref.startswith("refs/tags/")}
    branches = {ref.removeprefix("refs/heads/") for ref in refs if ref.startswith("refs/heads/")}
    return sorted((tag for tag in tags if usable(tag)), key=version_key, reverse=True), sorted(branches)


def list_tags(repo: str = REPO_URL, timeout: float = 60) -> list[str]:
    """The usable tags of `repo`, newest first."""
    return list_refs(repo, timeout)[0]


def installed_tag(workspace: Path = WORKSPACE_DIR) -> str:
    """The tag setup_workspace.sh installed, or ""."""
    try:
        tag = (workspace / TAG_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return tag if usable(tag) else ""


def installed_branch(workspace: Path = WORKSPACE_DIR) -> str:
    """The branch setup_workspace.sh --branch installed, or "" (a tag is installed)."""
    try:
        return (workspace / BRANCH_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def installed_label(workspace: Path = WORKSPACE_DIR) -> str:
    """What is installed: "v312.0.1", "branch version-3" or ""."""
    branch = installed_branch(workspace)
    return f"branch {branch}" if branch else installed_tag(workspace)


def default_tag(tags: list[str], current: str) -> str:
    """CHEMOTION_API_TAG if set, else the installed tag if it is still
    released, else the newest."""
    wanted = os.environ.get(TAG_ENV, "").strip()
    if wanted:
        if wanted not in tags:
            raise VersionError(f"{TAG_ENV}={wanted} is not a released version ({', '.join(tags)})")
        return wanted
    return current if current in tags else tags[0]


class Release(NamedTuple):
    tag: str      # the release to install
    eln: str      # the ELN version of its line
    exact: bool   # False: the line of a lower ELN version


def line_of(tag: str) -> str:
    """The release line of a tag: "v312.1.1" -> "312"; "" when unusable."""
    match = re.match(r"v(\d{3,})", tag.strip())
    return match.group(1) if match else ""


def eln_of(tag: str) -> str:
    """The ELN version a tag is made for: "v312.1.1" -> "3.1.2"; "" when the
    line is not three digits."""
    line = line_of(tag)
    return ".".join(line) if len(line) == 3 else ""


def eln_key(version: str) -> tuple:
    """"3.1.2" -> (3, 1, 2); ValueError unless X.Y.Z of digits."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version.strip()):
        raise ValueError(f"not an ELN version: {version!r}")
    return tuple(int(part) for part in version.strip().split("."))


def eln_versions(tags: list[str]) -> list[str]:
    """The ELN versions that have a release line, newest first."""
    found = {eln_of(tag) for tag in tags}
    found.discard("")
    return sorted(found, key=eln_key, reverse=True)


def clean_eln_version(version: str) -> str:
    """"v4.0.1 (abc)" -> "4.0.1"."""
    words = version.strip().split()
    return words[0].removeprefix("v") if words else ""


def release_for(eln_version: str, tags: list[str]) -> Release:
    """The highest release of the line of `eln_version`; without such a line
    the highest of the nearest lower line (exact=False)."""
    version = clean_eln_version(eln_version)
    try:
        wanted = eln_key(version)
    except ValueError:
        raise VersionError(f"“{eln_version}” is not an ELN version like 4.0.1") from None
    lines: dict[tuple, str] = {}
    for tag in tags:
        eln = eln_of(tag)
        if eln and (eln_key(eln) not in lines or version_key(tag) > version_key(lines[eln_key(eln)])):
            lines[eln_key(eln)] = tag
    if wanted in lines:
        return Release(lines[wanted], version, True)
    lower = [key for key in lines if key < wanted]
    if not lower:
        raise VersionError(f"No chemotionapi release for ELN {version} or an older ELN version")
    tag = lines[max(lower)]
    return Release(tag, eln_of(tag), False)


def parse_eln_version(page: str) -> str:
    """The ELN version from its /about page ("Version: v4.0.1" -> "4.0.1"),
    or ""."""
    match = re.search(r"Version:\s*v?(\d+(?:\.\d+)*)", page)
    return match.group(1) if match else ""


def server_eln_version(url: str, verify_tls: bool = True, timeout: float = 10) -> str:
    """The ELN version of the server at `url`, read from its public /about
    page. VersionError with a short reason."""
    context = None if verify_tls else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/about", timeout=timeout, context=context) as response:
            page = response.read(500_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise VersionError(f"/about answered HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise VersionError(f"{url} is not reachable ({reason})") from None
    version = parse_eln_version(page)
    if not version:
        raise VersionError("/about names no version")
    return version


def check_installed(eln_version: str, tags: list[str] | None, installed: str) -> str:
    """A warning when the installed release does not fit the server's ELN
    version, else "" (also when it cannot be judged: nothing or a branch
    installed, a version that is no X.Y.Z). `tags` None = the releases are
    not known (offline)."""
    if not installed or not line_of(installed):
        return ""
    version = clean_eln_version(eln_version)
    try:
        wanted = eln_key(version)
    except ValueError:
        return ""
    installed_eln = eln_of(installed)
    described = f"{installed} (for ELN {installed_eln})" if installed_eln else installed
    if tags is not None:
        try:
            expected = release_for(version, tags)
        except VersionError:
            return ""
        if line_of(installed) == line_of(expected.tag):
            return ""
        return (f"Your Chemotion runs ELN {version}, but chemotionapi {described} is installed, "
                f"so the assistant's code may fail. Install {expected.tag} (Settings \u2192 Chemotion).")
    if not installed_eln:
        return ""
    have = eln_key(installed_eln)
    if have == wanted or (have < wanted and have[0] == wanted[0]):
        return ""   # the same line, or possibly the nearest lower one
    return (f"Your Chemotion runs ELN {version}, but chemotionapi {described} is installed, "
            f"so the assistant's code may fail. Install a release for ELN {version} or the "
            f"nearest lower ELN version (Settings \u2192 Chemotion).")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("choose", "default"):
        print("Usage: python api_versions.py default|choose <workspace dir>", file=sys.stderr)
        return 2
    current = installed_tag(Path(argv[2]))
    try:
        tags = list_tags()
        if not tags:
            raise VersionError(f"{REPO_URL} has no tag that starts with v and three digits")
    except VersionError as exc:
        if not current or (os.environ.get(TAG_ENV) or current) != current:
            print(f"Cannot list the chemotionapi versions: {exc}", file=sys.stderr)
            return 1
        print(f"Cannot list the chemotionapi versions ({exc}); keeping {current}.", file=sys.stderr)
        print(current)
        return 0
    try:
        tag = default_tag(tags, current)
    except VersionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
