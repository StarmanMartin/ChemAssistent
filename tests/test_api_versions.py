"""api_versions: which chemotionapi tags are offered and which one is the default."""
from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading

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


TAGS = ["v401.1.1", "v401.0.0", "v312.1.1", "v312.1.0", "v312.0.1", "v1234.0.0", "v311.0.2"]
ABOUT = "<h4>Version: v4.0.1</h4>\n<h4>Base revision: 0debdbef54</h4>\n<h4>Current revision: 84f58f4207e0dd09019478f5dfbb24be36916061</h4>"


def test_mapping():
    assert api_versions.line_of("v312.1.1") == "312"
    assert api_versions.line_of("version-3") == ""
    assert api_versions.eln_of("v401.1.1") == "4.0.1"
    assert api_versions.eln_of("v1234.0.0") == ""
    assert api_versions.eln_key("3.1.2") == (3, 1, 2)
    for bad in ("3.1", "a.b.c", ""):
        with pytest.raises(ValueError):
            api_versions.eln_key(bad)


def test_eln_versions_newest_first_without_duplicates():
    assert api_versions.eln_versions(TAGS) == ["4.0.1", "3.1.2", "3.1.1"]
    assert api_versions.eln_versions(["v1234.0.0"]) == []



def test_release_for():
    assert api_versions.release_for("3.1.2", TAGS) == ("v312.1.1", "3.1.2", True)
    assert api_versions.release_for("v4.0.1 (abc)", TAGS) == ("v401.1.1", "4.0.1", True)
    assert api_versions.release_for("3.1.5", TAGS) == ("v312.1.1", "3.1.2", False)
    assert api_versions.release_for("9.9.9", TAGS) == ("v401.1.1", "4.0.1", False)
    assert api_versions.release_for("3.1.2", ["v312.0.9", "v312.0.10"]).tag == "v312.0.10"
    with pytest.raises(api_versions.VersionError, match="older"):
        api_versions.release_for("3.0.0", TAGS)
    with pytest.raises(api_versions.VersionError):
        api_versions.release_for("three", TAGS)


def test_parse_eln_version():
    assert api_versions.parse_eln_version(ABOUT) == "4.0.1"
    assert api_versions.parse_eln_version("<h4>Version: 3.1.2</h4>") == "3.1.2"
    assert api_versions.parse_eln_version("<h4>Base revision: abc</h4>") == ""


@pytest.fixture
def about_server():
    pages = {"/about": (200, ABOUT)}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = pages.get(self.path, (404, "not found"))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", pages
    server.shutdown()
    server.server_close()


def test_server_eln_version(about_server):
    url, pages = about_server
    assert api_versions.server_eln_version(url) == "4.0.1"
    assert api_versions.server_eln_version(url + "/") == "4.0.1"
    pages["/about"] = (200, "<h4>no version here</h4>")
    with pytest.raises(api_versions.VersionError, match="no version"):
        api_versions.server_eln_version(url)
    del pages["/about"]
    with pytest.raises(api_versions.VersionError, match="HTTP 404"):
        api_versions.server_eln_version(url)


def test_server_eln_version_unreachable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(api_versions.VersionError, match="not reachable"):
        api_versions.server_eln_version(f"http://127.0.0.1:{port}", timeout=2)


def test_check_installed_with_tags():
    check = api_versions.check_installed
    assert check("3.1.2", TAGS, "v312.0.1") == ""            # same line
    text = check("4.0.1", TAGS, "v312.1.0")
    assert "ELN 4.0.1" in text and "v312.1.0 (for ELN 3.1.2)" in text and "Install v401.1.1" in text
    assert check("3.1.5", TAGS, "v312.1.1") == ""            # nearest lower line
    assert "v312.1.1" in check("3.1.5", TAGS, "v401.0.0")
    assert check("2.0.0", TAGS, "v312.1.1") == ""            # cannot be judged
    assert check("4.0.1", TAGS, "") == ""
    assert check("garbage", TAGS, "v312.1.1") == ""


def test_check_installed_offline():
    check = api_versions.check_installed
    assert check("3.1.2", None, "v312.0.1") == ""
    assert check("3.1.5", None, "v312.0.1") == ""            # may be the nearest lower line
    text = check("3.1.2", None, "v401.1.1")                  # newer than the server
    assert "ELN 3.1.2" in text and "v401.1.1" in text and "Install v" not in text
    assert check("4.0.1", None, "v312.1.1")                  # other major version
    assert check("4.0.1", None, "") == ""


def test_choose_is_default(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(api_versions.TAG_ENV, raising=False)
    (tmp_path / api_versions.TAG_NAME).write_text("v312.0.1\n")
    monkeypatch.setattr(api_versions, "list_tags", lambda: ["v401.1.1", "v312.0.1"])

    class NoStdin:
        def isatty(self):
            raise AssertionError("stdin must not be looked at")

        def read(self, *_):
            raise AssertionError("stdin must not be read")
        readline = read
    monkeypatch.setattr(sys, "stdin", NoStdin())
    for mode in ("choose", "default"):
        assert api_versions.main(["api_versions.py", mode, str(tmp_path)]) == 0
        assert capsys.readouterr().out == "v312.0.1\n"
    assert not hasattr(api_versions, "ask_on_stderr")
