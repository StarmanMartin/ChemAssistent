"""api_versions: which chemotionapi tags are offered and which one is the default."""
from __future__ import annotations

import io
import subprocess

import pytest

import api_versions


LS_REMOTE = """\
1111111111111111111111111111111111111111\trefs/tags/0.3.32
2222222222222222222222222222222222222222\trefs/tags/v312.0.1
3333333333333333333333333333333333333333\trefs/tags/v312.0.10
4444444444444444444444444444444444444444\trefs/tags/v312.0.9
5555555555555555555555555555555555555555\trefs/tags/v99.1
6666666666666666666666666666666666666666\trefs/tags/version-3
7777777777777777777777777777777777777777\trefs/tags/v1000.0.0
"""


@pytest.fixture
def ls_remote(monkeypatch):
    def run(command, **_kwargs):
        assert command[:2] == ["git", "ls-remote"]
        return subprocess.CompletedProcess(command, 0, LS_REMOTE, "")
    monkeypatch.setattr(api_versions.subprocess, "run", run)


def test_usable_tags():
    assert api_versions.usable("v312.0.1")
    assert api_versions.usable("v1000")
    assert not api_versions.usable("v99.1")
    assert not api_versions.usable("0.3.32")
    assert not api_versions.usable("version-3")


def test_list_tags_newest_first(ls_remote):
    assert api_versions.list_tags() == ["v1000.0.0", "v312.0.10", "v312.0.9", "v312.0.1"]


def test_list_tags_git_error(monkeypatch):
    monkeypatch.setattr(api_versions.subprocess, "run", lambda command, **_: subprocess.CompletedProcess(
        command, 128, "", "fatal: unable to access 'https://github.com/...': Could not resolve host\n"))
    with pytest.raises(api_versions.VersionError, match="Could not resolve host"):
        api_versions.list_tags()


def test_installed_tag(tmp_path):
    assert api_versions.installed_tag(tmp_path) == ""
    (tmp_path / api_versions.TAG_NAME).write_text("v312.0.1\n")
    assert api_versions.installed_tag(tmp_path) == "v312.0.1"
    (tmp_path / api_versions.TAG_NAME).write_text("version-3\n")
    assert api_versions.installed_tag(tmp_path) == ""


def test_default_tag(monkeypatch):
    monkeypatch.delenv(api_versions.TAG_ENV, raising=False)
    tags = ["v313.0.0", "v312.0.1"]
    assert api_versions.default_tag(tags, "") == "v313.0.0"
    assert api_versions.default_tag(tags, "v312.0.1") == "v312.0.1"   # an update keeps the version
    assert api_versions.default_tag(tags, "v311.0.0") == "v313.0.0"   # no longer released
    monkeypatch.setenv(api_versions.TAG_ENV, "v313.0.0")
    assert api_versions.default_tag(tags, "v312.0.1") == "v313.0.0"
    monkeypatch.setenv(api_versions.TAG_ENV, "v400.0.0")
    with pytest.raises(api_versions.VersionError, match="not a released version"):
        api_versions.default_tag(tags, "v312.0.1")


def test_choose():
    tags = ["v313.0.0", "v312.0.1"]
    answers = iter(["", "2", "x", "v313.0.0"])
    ask = lambda _prompt: next(answers)  # noqa: E731
    out = io.StringIO()
    assert api_versions.choose(tags, "v312.0.1", ask, out) == "v312.0.1"
    assert "v312.0.1  (default)" in out.getvalue()
    assert api_versions.choose(tags, "v313.0.0", ask, out) == "v312.0.1"
    assert api_versions.choose(tags, "v312.0.1", ask, out) == "v313.0.0"  # after a wrong answer
    assert "“x” is none of the versions" in out.getvalue()

    def eof(_prompt):
        raise EOFError
    assert api_versions.choose(tags, "v312.0.1", eof, out) == "v312.0.1"


def test_main_keeps_installed_when_offline(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(api_versions.TAG_ENV, raising=False)
    (tmp_path / api_versions.TAG_NAME).write_text("v312.0.1\n")

    def offline(*_args, **_kwargs):
        raise api_versions.VersionError("offline")
    monkeypatch.setattr(api_versions, "list_tags", offline)
    assert api_versions.main(["api_versions.py", "default", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "v312.0.1\n"
    (tmp_path / api_versions.TAG_NAME).unlink()
    assert api_versions.main(["api_versions.py", "default", str(tmp_path)]) == 1
