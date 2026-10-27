"""Developer mode: chemotion_api bug reports (issues.py), the report_api_bug
tool and the branches in api_versions.py. A local HTTP server plays the
Chemotion /about page and GitHub's issues API."""
from __future__ import annotations

import dataclasses
import json
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import api_versions
import issues
import workspace as workspace_module
from agent import Agent, build_tools
from mcp_bridge import Bridge


ABOUT = """<h1>Chemotion ELN - About</h1>
<h4>Version: v3.1.2</h4>
<h4>Base revision: b343980cc</h4>
<h4>Current revision: 16d52d961f98576a4e5827bc28ed420bfbf76e1f</h4>"""


class Server:
    """GET /about, GET /issues (the open ones), POST /issues (records the body)."""

    def __init__(self) -> None:
        self.open_issues = [{"number": 7, "title": "Sample.save() drops the solvent",
                             "html_url": "https://github.com/x/7"},
                            {"number": 8, "title": "A pull request", "pull_request": {}}]
        self.created: list[dict] = []
        self.headers: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _send(self, status: int, body: str, kind: str = "application/json") -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                server.headers.append(dict(self.headers))
                if self.path == "/about":
                    self._send(200, ABOUT, "text/html")
                elif self.path.startswith("/issues"):
                    self._send(200, json.dumps(server.open_issues))
                else:
                    self._send(404, "{}")

            def do_POST(self) -> None:
                server.headers.append(dict(self.headers))
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.created.append(data)
                self._send(201, json.dumps({"number": 9, "html_url": "https://github.com/x/9"}))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(tmp_path, monkeypatch):
    server = Server()
    monkeypatch.setattr(issues, "API_URL", f"{server.url}/issues")
    monkeypatch.setattr(issues, "ISSUES_DIR", tmp_path / "issues")
    monkeypatch.setattr(issues.api_versions, "installed_label", lambda: "branch version-3")
    monkeypatch.setattr(issues.lessons, "api_commit", lambda: "abc1234")
    yield server
    server.close()


@pytest.fixture
def dev_config(server, ai_config):
    return dataclasses.replace(ai_config, url=server.url, developer_mode=True)


# --- issues.py ----------------------------------------------------------------
def test_environment(server, dev_config):
    rows = dict(issues.environment(dev_config, "test-model (Built-in agent)"))
    assert rows["AI model"] == "test-model (Built-in agent)"
    assert rows["Chemotion instance"] == f"{server.url} (version v3.1.2, revision 16d52d961)"
    assert rows["chemotion_api"].startswith("branch version-3 (commit abc1234")
    assert rows["Date"][:4].isdigit()


def test_eln_version_unreachable(dev_config):
    config = dataclasses.replace(dev_config, url="http://127.0.0.1:9")
    assert issues.eln_version(config).startswith("unknown")


def test_model_label(ai_config):
    assert issues.model_label(ai_config) == f"test-model (Built-in agent · OpenAI-compatible API, {ai_config.ai_url})"
    claude = dataclasses.replace(ai_config, agent="claude", ai_model="", ai_url="")
    assert issues.model_label(claude) == "default model (Claude Code CLI)"


def test_report_without_token_gives_a_link(server, dev_config, tmp_path):
    text = issues.report(dev_config, "m", "Collection.get_samples()  fails\non empty", "It raises KeyError.")
    assert "No GitHub token" in text
    link = text.rsplit(": ", 1)[1]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
    assert query["title"] == ["Collection.get_samples() fails on empty"]
    assert query["body"][0].startswith("It raises KeyError.")
    assert "| chemotion_api | branch version-3" in query["body"][0]
    drafts = list((tmp_path / "issues").glob("*.md"))
    assert len(drafts) == 1 and str(drafts[0]) in text
    assert server.created == []


def test_report_with_token_creates_the_issue(server, dev_config):
    config = dataclasses.replace(dev_config, github_token="ghp_test")
    text = issues.report(config, "m", "Reaction.save() loses the yield", "Steps ...")
    assert text.startswith("Issue #9 created: https://github.com/x/9")
    assert server.created[0]["title"] == "Reaction.save() loses the yield"
    assert "| Chemotion instance |" in server.created[0]["body"]
    assert server.headers[-1]["Authorization"] == "Bearer ghp_test"


def test_report_with_token_finds_an_open_duplicate(server, dev_config):
    config = dataclasses.replace(dev_config, github_token="ghp_test")
    text = issues.report(config, "m", "sample.save() drops the solvent!", "Again.")
    assert text.startswith("An open issue with this title exists already: #7")
    assert server.created == []


def test_report_needs_title_and_description(server, dev_config):
    assert issues.report(dev_config, "m", " ", "x").startswith("Give a title")


def test_long_bodies_are_clipped_in_the_link():
    link, clipped = issues.new_issue_link("t", "x" * (issues.MAX_URL_BODY_CHARS + 10))
    assert clipped and len(link) < 3 * issues.MAX_URL_BODY_CHARS


# --- the tool -----------------------------------------------------------------
def test_tool_only_in_developer_mode(server, dev_config, fake_ai, workspace):
    assert "report_api_bug" not in [tool["function"]["name"] for tool in build_tools("off")]
    assert build_tools("off", developer=True)[-1]["function"]["name"] == "report_api_bug"
    events = []
    agent = Agent(dev_config, "system", lambda *_: "", lambda kind, data: events.append((kind, data)))
    fake_ai.script_next(
        {"tool_calls": [{"name": "report_api_bug", "arguments": {"title": "Bug", "description": "Broken."}}]},
        {"content": "Reported."},
    )
    agent.send("go")
    result = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "No GitHub token" in result
    assert ("tool", "Reported a chemotion_api bug: Bug") in events

    plain = Agent(dataclasses.replace(dev_config, developer_mode=False), "system", lambda *_: "", lambda *_: None)
    assert "There is no tool report_api_bug" in plain._dispatch("report_api_bug", {"title": "t", "description": "d"})


def test_bridge_offers_it_in_developer_mode():
    names = lambda bridge: [tool["name"] for tool in bridge.tools]  # noqa: E731
    assert "report_api_bug" not in names(Bridge({}))
    assert "report_api_bug" in names(Bridge({"developer": True, "model": "opus (Claude Code CLI)"}))


def test_developer_prompt(workspace, monkeypatch):
    monkeypatch.setattr(workspace_module.api_versions, "installed_branch", lambda: "version-3")
    monkeypatch.setattr(workspace_module.api_versions, "installed_label", lambda: "branch version-3")
    assert "Developer mode" not in workspace_module.system_prompt()
    prompt = workspace_module.system_prompt(developer=True)
    assert "chemotion_api` package (branch version-3)" in prompt
    assert "installed: branch version-3" in prompt and "report_api_bug" in prompt


# --- branches -----------------------------------------------------------------
def test_list_refs_with_branches(monkeypatch):
    out = ("1111111111111111111111111111111111111111\trefs/tags/v312.0.1\n"
           "2222222222222222222222222222222222222222\trefs/heads/version-3\n"
           "3333333333333333333333333333333333333333\trefs/heads/main\n"
           "4444444444444444444444444444444444444444\trefs/tags/0.3.32\n")
    monkeypatch.setattr(api_versions.subprocess, "run",
                        lambda command, **_: subprocess.CompletedProcess(command, 0, out, ""))
    assert api_versions.list_refs() == (["v312.0.1"], ["main", "version-3"])
    assert api_versions.list_tags() == ["v312.0.1"]


def test_installed_branch(tmp_path):
    assert api_versions.installed_label(tmp_path) == ""
    (tmp_path / api_versions.TAG_NAME).write_text("v312.0.1\n")
    assert api_versions.installed_label(tmp_path) == "v312.0.1"
    (tmp_path / api_versions.TAG_NAME).unlink()
    (tmp_path / api_versions.BRANCH_NAME).write_text("feature/x\n")
    assert api_versions.installed_branch(tmp_path) == "feature/x"
    assert api_versions.installed_label(tmp_path) == "branch feature/x"
