"""The chemotionapi versions the workspace can be set up with (stdlib).

A version is a release tag of the chemotionapi repository that starts with
"v" and at least three digits (v312.0.1); other tags (0.3.32, ...) are older
releases the app does not support. setup_workspace.sh records the installed
tag in TAG_FILE. Run as a script it serves the installers:

    python api_versions.py choose <workspace dir>   # asks on a terminal
    python api_versions.py default <workspace dir>  # never asks

Both print the tag to use on stdout: the installed one if it is still
released, else the newest (CHEMOTION_API_TAG preselects another); `choose`
lists the released tags on stderr and reads the answer from stdin.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from config import WORKSPACE_DIR


REPO_URL = "https://github.com/Chemotion/chemotionapi.git"
TAG_PATTERN = re.compile(r"v\d{3,}")
TAG_NAME = "chemotionapi.tag"
TAG_FILE = WORKSPACE_DIR / TAG_NAME
TAG_ENV = "CHEMOTION_API_TAG"


class VersionError(Exception):
    pass


def usable(tag: str) -> bool:
    return bool(TAG_PATTERN.match(tag))


def version_key(tag: str) -> tuple:
    """Sorts v312.0.10 after v312.0.9; the text breaks ties (v312.0.1rc1)."""
    return tuple(int(number) for number in re.findall(r"\d+", tag)), tag


def list_tags(repo: str = REPO_URL, timeout: float = 60) -> list[str]:
    """The usable tags of `repo`, newest first (git ls-remote, no clone)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(["git", "ls-remote", "--tags", "--refs", repo],
                              capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise VersionError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise VersionError(f"{repo} did not answer within {timeout:.0f} s") from None
    if done.returncode:
        lines = done.stderr.strip().splitlines()
        raise VersionError(lines[-1] if lines else f"git ls-remote failed ({done.returncode})")
    tags = {line.split("refs/tags/", 1)[1] for line in done.stdout.splitlines() if "refs/tags/" in line}
    return sorted((tag for tag in tags if usable(tag)), key=version_key, reverse=True)


def installed_tag(workspace: Path = WORKSPACE_DIR) -> str:
    """The tag setup_workspace.sh installed, or ""."""
    try:
        tag = (workspace / TAG_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return tag if usable(tag) else ""


def default_tag(tags: list[str], current: str) -> str:
    """CHEMOTION_API_TAG if set, else the installed tag if it is still
    released, else the newest."""
    wanted = os.environ.get(TAG_ENV, "").strip()
    if wanted:
        if wanted not in tags:
            raise VersionError(f"{TAG_ENV}={wanted} is not a released version ({', '.join(tags)})")
        return wanted
    return current if current in tags else tags[0]


def choose(tags: list[str], default: str, ask=input, out=sys.stderr) -> str:
    """Lists `tags` and asks for one (number or tag; empty = `default`)."""
    print("chemotion_api versions:", file=out)
    for number, tag in enumerate(tags, 1):
        print(f"  {number:>2}) {tag}{'  (default)' if tag == default else ''}", file=out)
    while True:
        try:
            answer = ask(f"Version to install [{default}]: ").strip()
        except EOFError:
            return default
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(tags):
            return tags[int(answer) - 1]
        if answer in tags:
            return answer
        print(f"“{answer}” is none of the versions above.", file=out)


def ask_on_stderr(prompt: str) -> str:
    # input() prompts on stdout, which the installer captures.
    print(prompt, end="", file=sys.stderr, flush=True)
    return input()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("choose", "default"):
        print("Usage: python api_versions.py choose|default <workspace dir>", file=sys.stderr)
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
    if argv[1] == "choose" and sys.stdin.isatty() and not os.environ.get(TAG_ENV):
        tag = choose(tags, tag, ask=ask_on_stderr)
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
