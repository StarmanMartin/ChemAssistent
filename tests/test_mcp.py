"""MCP servers (mcp_client.py): configuration, both transports against the
scripted server (tests/fake_mcp.py), the per-conversation hub, the agent's
MCP tools and "/mcp" turns, and what mcp_bridge.py offers a CLI agent.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

import config as app_config
import mcp_bridge
import mcp_client
from agent import Agent, mcp_command, mcp_server_entry, tool_summary
from fake_mcp import FakeHttpMcp
from mcp_client import McpError, McpHub, McpServer, parse_servers, tool_name

FAKE = str(Path(__file__).with_name("fake_mcp.py"))


def stdio_spec(name: str = "fake", *args: str, **env: str) -> dict:
    return app_config.normalize_mcp_server({"name": name, "command": sys.executable, "args": [FAKE, *args],
                                            "env": env})


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "config" / "config.json"
    monkeypatch.setattr(app_config, "CONFIG_FILE", path)
    monkeypatch.setattr(app_config, "CONFIG_DIR", path.parent)
    return path


@pytest.fixture
def hub():
    hubs = []

    def make(specs) -> McpHub:
        hubs.append(McpHub(specs))
        return hubs[-1]

    yield make
    for item in hubs:
        item.close()


# --- configuration ----------------------------------------------------------------
def test_mcp_servers_in_config_are_validated(config_file):
    data = {"mcp_servers": [
        {"name": "files", "command": "npx", "args": ["-y", "pkg", 3], "env": {"KEY": "v"}},
        {"name": "web", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer x"}, "enabled": False},
        {"name": "bad__name", "command": "x"},
        {"name": "both", "command": "x", "url": "https://x"},
        {"name": "files", "command": "again"},  # a repeated name
        "not a dict",
    ]}
    servers = app_config.mcp_servers(data)
    assert [spec["name"] for spec in servers] == ["files", "web"]
    assert servers[0] == {"name": "files", "command": "npx", "args": ["-y", "pkg", "3"], "env": {"KEY": "v"},
                          "url": "", "headers": {}, "enabled": True}
    assert servers[1]["enabled"] is False and servers[1]["headers"] == {"Authorization": "Bearer x"}
    assert "double underscore" in app_config.mcp_server_problem({"name": "a__b", "command": "x"})


def test_parse_servers_accepts_the_usual_json_shapes():
    claude = parse_servers('{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "srv", "/data"]}}}')
    assert claude[0]["name"] == "filesystem" and claude[0]["args"] == ["-y", "srv", "/data"]
    vscode = parse_servers('{"servers": {"Git Hub": {"type": "http", "url": "https://api.example.com/mcp/"}}}')
    assert vscode[0] == {"name": "Git-Hub", "command": "", "args": [], "env": {}, "url": "https://api.example.com/mcp/",
                         "headers": {}, "enabled": True}
    single = parse_servers('{"name": "x", "command": "uvx", "args": ["mcp-server-time"], "disabled": true}')
    assert single[0]["enabled"] is False
    with pytest.raises(McpError, match="SSE"):
        parse_servers('{"old": {"type": "sse", "url": "https://x/sse"}}')
    with pytest.raises(McpError, match="JSON"):
        parse_servers("{nope")
    with pytest.raises(McpError, match="either a command or a URL"):
        parse_servers('{"empty": {}}')


def test_load_config_has_the_servers(config_file):
    app_config.save_raw_config({"url": "https://eln.example", "user": "u", "password": "p",
                                "mcp_servers": [{"name": "t", "command": "uvx", "args": ["mcp-server-time"]}]})
    assert app_config.load_config().mcp_servers[0]["command"] == "uvx"


def test_mcp_command():
    assert mcp_command("/mcp add a filesystem server")
    assert mcp_command("  /MCP")
    assert not mcp_command("/mcpx")
    assert not mcp_command("what is /mcp")


def test_tool_names():
    assert tool_name("files", "read file.v2") == "mcp__files__read_file_v2"
    assert len(tool_name("s" * 32, "t" * 80)) == 64
    assert mcp_client.split_tool_name("mcp__files__read__deep") == ("files", "read__deep")
    assert tool_summary("mcp__files__read", {}) == "MCP files · read"


# --- transports ----------------------------------------------------------------------
def test_stdio_server():
    server = McpServer(stdio_spec(FAKE_MCP_MARK="from env"))
    server.start(timeout=30)
    try:
        assert server.info == "fake-mcp 1.0"
        assert [tool["name"] for tool in server.tools] == ["echo", "fail", "slow", "stamp"]  # both pages
        assert mcp_client.result_parts(server.call("echo", {"text": "hi"}, 10)) == (True, "echo: hi", {"text": "hi"})
        assert mcp_client.result_parts(server.call("stamp", {}, 10))[1] == "from env"
        assert mcp_client.result_parts(server.call("fail", {}, 10))[:2] == (False, "it broke")
        with pytest.raises(McpError, match="no tool"):
            server.call("nope", {}, 10)
    finally:
        server.close()
    assert server.connection is None


def test_stdio_server_that_fails_reports_its_stderr():
    with pytest.raises(McpError, match=r"(?s)exit code 3.*missing API key"):
        McpServer(stdio_spec("broken", "--fail-start")).start(timeout=30)


def test_missing_program():
    spec = app_config.normalize_mcp_server({"name": "x", "command": "/nonexistent/mcp-server"})
    with pytest.raises(McpError, match="Cannot start"):
        McpServer(spec).start(timeout=5)


def test_cancelling_a_call():
    server = McpServer(stdio_spec())
    server.start(timeout=30)
    cancelled = threading.Event()
    threading.Timer(0.5, cancelled.set).start()
    started = time.monotonic()
    try:
        with pytest.raises(McpError, match="Cancelled"):
            server.call("slow", {}, 60, cancelled)
        assert time.monotonic() - started < 5
    finally:
        server.close()


@pytest.mark.parametrize("sse", [False, True])
def test_http_server(sse):
    fake = FakeHttpMcp(sse=sse)
    spec = app_config.normalize_mcp_server({"name": "web", "url": fake.url, "headers": {"Authorization": "Bearer t"}})
    server = McpServer(spec)
    try:
        server.start(timeout=10)
        assert len(server.tools) == 4
        assert mcp_client.result_parts(server.call("echo", {"text": "over http"}, 10))[1] == "echo: over http"
        later = fake.requests[-1]["headers"]
        assert later["mcp-session-id"] == "session-1"
        assert later["authorization"] == "Bearer t"
        assert later["mcp-protocol-version"] == mcp_client.PROTOCOL_VERSION
        server.close()
        assert fake.deleted == "session-1"
    finally:
        fake.close()


def test_test_server():
    assert mcp_client.test_server(stdio_spec(), timeout=30).startswith("fake-mcp 1.0: 4 tools (echo, fail")


# --- the hub ------------------------------------------------------------------------------
def test_hub_starts_servers_once_and_reports_failures(hub):
    disabled = {**stdio_spec("off"), "enabled": False}
    conversation = hub([stdio_spec(), stdio_spec("broken", "--fail-start"), disabled])
    assert conversation.waiting()
    problems = conversation.start()
    assert len(problems) == 1 and problems[0].startswith("MCP server broken did not start")
    assert conversation.start() == []  # reported once, not tried again
    assert not conversation.waiting()
    names = [tool["function"]["name"] for tool in conversation.openai_tools()]
    assert names == ["mcp__fake__echo", "mcp__fake__fail", "mcp__fake__slow", "mcp__fake__stamp"]
    assert conversation.openai_tools()[0]["function"]["description"] == "[MCP server fake] Echo a text back."
    assert conversation.openai_tools()[3]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert conversation.read_only("mcp__fake__echo") and not conversation.read_only("mcp__fake__fail")
    assert conversation.mcp_tools()[0]["annotations"] == {"readOnlyHint": True}

    outcome = conversation.call("mcp__fake__echo", '{"text": "x"}', 10)
    assert outcome == {"ok": True, "stdout": "echo: x", "result": {"text": "x"}, "error": ""}
    assert conversation.call("mcp__fake__fail", "{}", 10) == {"ok": False, "stdout": "", "result": None,
                                                              "error": "it broke"}
    assert "not valid JSON" in conversation.call("mcp__fake__echo", "{x", 10)["error"]
    assert "no MCP tool" in conversation.call("mcp__fake__gone", "{}", 10)["error"]


def test_hub_add_and_remove_save_the_settings(hub, config_file):
    app_config.save_raw_config({"url": "https://eln.example", "user": "u", "password": "secret"})
    conversation = hub([])
    entry = json.dumps({"name": "fake", "command": sys.executable, "args": [FAKE]})

    outcome = conversation.add(entry)
    assert outcome["ok"], outcome
    assert "Added the MCP server fake (fake-mcp 1.0)" in outcome["stdout"]
    assert [tool["name"] for tool in outcome["result"]][:1] == ["mcp__fake__echo"]
    assert conversation.has("mcp__fake__echo")
    saved = app_config.read_raw_config()
    assert saved["mcp_servers"][0]["name"] == "fake"
    assert saved["password"] == "secret"  # the rest of the settings is kept (via the keyring)
    assert "fake: " in conversation.describe() and "running, 4 tools" in conversation.describe()

    assert "Replaced" in conversation.add(entry)["stdout"]
    assert len(app_config.read_raw_config()["mcp_servers"]) == 1

    broken = json.dumps({"name": "broken", "command": sys.executable, "args": [FAKE, "--fail-start"]})
    outcome = conversation.add(broken)
    assert not outcome["ok"] and "was not saved" in outcome["error"] and "missing API key" in outcome["error"]
    assert [spec["name"] for spec in app_config.read_raw_config()["mcp_servers"]] == ["fake"]

    assert conversation.remove('{"name": "fake"}')["ok"]
    assert not conversation.has("mcp__fake__echo")
    assert app_config.read_raw_config()["mcp_servers"] == []
    assert not conversation.remove('{"name": "fake"}')["ok"]


def test_hub_does_not_save_while_the_keyring_is_unreadable(hub, config_file, keyring):
    app_config.save_raw_config({"url": "https://eln.example", "user": "u", "password": "secret"})
    app_config._secrets_cache = None
    keyring.fail = True
    outcome = hub([]).add(json.dumps({"name": "fake", "command": sys.executable, "args": [FAKE]}))
    assert not outcome["ok"] and "keyring" in outcome["error"]
    keyring.fail = False
    assert keyring.secrets["password"] == "secret"


# --- the built-in agent -------------------------------------------------------------------------
class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.executions: list[dict] = []
        self.answer = "The tool call succeeded."

    def on_event(self, kind: str, data) -> None:
        self.events.append((kind, data))

    def execute(self, purpose: str, code: str, cancelled, target: str, tool: str = "") -> str:
        self.executions.append({"purpose": purpose, "code": code, "target": target, "tool": tool})
        return self.answer


def tool_names(request: dict) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"]]


def test_agent_offers_and_calls_mcp_tools(fake_ai, ai_config, workspace, hub):
    recorder = Recorder()
    conversation = hub([stdio_spec(), stdio_spec("broken", "--fail-start")])
    agent = Agent(ai_config, "system", recorder.execute, recorder.on_event, mcp=conversation)
    fake_ai.script_next({"tool_calls": [{"name": "mcp__fake__echo", "arguments": {"text": "hi"}}]},
                        {"content": "Done."})

    agent.send("Echo hi")

    assert ("status", "Starting the MCP servers…") in recorder.events
    assert any(kind == "warning" and "broken" in data for kind, data in recorder.events)
    names = tool_names(fake_ai.requests[0])
    assert "mcp__fake__echo" in names and "add_mcp_server" not in names
    assert recorder.executions == [{"purpose": "MCP fake · echo", "code": '{\n  "text": "hi"\n}', "target": "mcp",
                                    "tool": "mcp__fake__echo"}]
    assert agent.messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "The tool call succeeded."}


def test_mcp_admin_tools_only_in_mcp_turns(fake_ai, ai_config, workspace, hub, config_file):
    app_config.save_raw_config({"url": "https://eln.example", "user": "u", "password": "p"})
    recorder = Recorder()
    agent = Agent(ai_config, "system", recorder.execute, recorder.on_event, mcp=hub([]))
    arguments = {"name": "time", "command": "uvx", "args": ["mcp-server-time"], "env": {"TZ": "UTC"}}
    fake_ai.script_next({"tool_calls": [{"name": "list_mcp_servers", "arguments": {}}]},
                        {"tool_calls": [{"name": "add_mcp_server", "arguments": arguments}]},
                        {"content": "Added."})

    agent.send("/mcp add a time server", mcp_admin=True)

    assert tool_names(fake_ai.requests[0])[-3:] == ["list_mcp_servers", "add_mcp_server", "remove_mcp_server"]
    assert agent.messages[3]["content"] == "No MCP servers are configured."
    assert recorder.executions == [{"purpose": "Add the MCP server time", "code": mcp_server_entry(arguments),
                                    "target": "mcp-add", "tool": ""}]
    assert json.loads(recorder.executions[0]["code"]) == arguments

    # A normal message: no admin tools, and a call is refused with a hint.
    fake_ai.script_next({"tool_calls": [{"name": "remove_mcp_server", "arguments": {"name": "time"}}]},
                        {"content": "Cannot."})
    agent.send("remove the time server")
    assert "add_mcp_server" not in tool_names(fake_ai.requests[-1])
    refused = [m for m in agent.messages if m.get("role") == "tool"][-1]["content"]
    assert "starts with /mcp" in refused
    assert len(recorder.executions) == 1


# --- the bridge of the CLI agents ----------------------------------------------------------------
def test_bridge_offers_and_forwards_mcp_tools(monkeypatch):
    sent: list[dict] = []

    def ask(self, request: dict) -> dict:
        sent.append(request)
        if request["type"] == "mcp_tools":
            return {"tools": [{"name": "mcp__fake__echo", "description": "d", "inputSchema": {"type": "object"},
                               "annotations": {"readOnlyHint": True}}]}
        if request["type"] == "mcp_list":
            return {"text": "No MCP servers are configured."}
        return {"text": f"answer to {request['target']}"}

    monkeypatch.setattr(mcp_bridge.Bridge, "ask", ask)
    plain = mcp_bridge.Bridge({"session": "s"})
    assert "add_mcp_server" not in [tool["name"] for tool in plain.tools]
    assert [tool["name"] for tool in plain.mcp_tools()] == ["fake__echo"]
    assert plain.dispatch("fake__echo", {"text": "hi"}) == "answer to mcp"
    assert sent[-1]["tool"] == "mcp__fake__echo" and json.loads(sent[-1]["code"]) == {"text": "hi"}
    assert sent[-1]["purpose"] == "MCP fake · echo"
    with pytest.raises(Exception, match="starts with /mcp"):
        plain.dispatch("add_mcp_server", {"name": "x", "command": "npx"})

    admin = mcp_bridge.Bridge({"session": "s", "mcp_admin": True})
    assert [tool["name"] for tool in admin.tools][-3:] == ["list_mcp_servers", "add_mcp_server", "remove_mcp_server"]
    assert admin.dispatch("list_mcp_servers", {}) == "No MCP servers are configured."
    assert admin.dispatch("add_mcp_server", {"name": "x", "command": "npx", "args": ["-y", "p"]}) == "answer to mcp-add"
    assert json.loads(sent[-1]["code"]) == {"name": "x", "command": "npx", "args": ["-y", "p"]}
    assert admin.dispatch("remove_mcp_server", {"name": "x"}) == "answer to mcp-remove"


# --- documents through the bridge (CLI agents) --------------------------------
def bridge_call(bridge, name: str, arguments: dict) -> dict:
    """One tools/call through Bridge._call(); the JSON-RPC result."""
    written = []
    bridge.write = written.append
    bridge._call(1, {"name": name, "arguments": arguments})
    return written[0]["result"]


def test_bridge_reads_and_greps_pdfs(tmp_path):
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Abstract", "Scheme 1: 8a from 7a"]))
    bridge = mcp_bridge.Bridge({"session": "s", "roots": [str(tmp_path)]})

    result = bridge_call(bridge, "read_file", {"path": str(paper)})
    assert not result["isError"]
    assert "--- page 2 of 2 ---" in result["content"][0]["text"]
    assert "Scheme 1: 8a from 7a" in result["content"][0]["text"]
    result = bridge_call(bridge, "grep", {"pattern": "8a", "path": str(tmp_path)})
    assert "(page 2): Scheme 1: 8a from 7a" in result["content"][0]["text"]


def test_bridge_view_page_returns_an_image_block(tmp_path):
    import base64

    import documents
    from pdf_maker import make_pdf

    paper = tmp_path / "paper.pdf"
    paper.write_bytes(make_pdf(["Scheme 1"]))
    bridge = mcp_bridge.Bridge({"session": "s", "roots": [str(tmp_path)]})
    listed = {tool["name"]: tool for tool in bridge.tools}
    assert listed["view_page"]["annotations"] == {"readOnlyHint": True}

    result = bridge_call(bridge, "view_page", {"path": str(paper), "page": 1, "region": "top"})
    assert not result["isError"]
    text, image = result["content"]
    assert text["type"] == "text" and text["text"].startswith("Page 1 of 1 of ") and "the image follows." in text["text"]
    assert image["type"] == "image" and image["mimeType"] == "image/png"
    data = base64.b64decode(image["data"])
    assert documents.image_size(data)[0] == documents.IMAGE_MAX_SIDE

    result = bridge_call(bridge, "view_page", {"path": str(paper), "page": 5})
    assert result["isError"] and "page 5 does not exist" in result["content"][0]["text"]
    result = bridge_call(bridge, "view_page", {"path": "/etc/hostname"})
    assert result["isError"] and "outside the workspace" in result["content"][0]["text"]

    no_vision = mcp_bridge.Bridge({"session": "s", "roots": [str(tmp_path)], "vision": False})
    assert "view_page" not in [tool["name"] for tool in no_vision.tools]
    assert bridge_call(no_vision, "view_page", {"path": str(paper), "page": 1})["isError"]


def test_cli_agent_notes_view_page(ai_config):
    from cli_agent import CliAgent

    events = []
    agent = CliAgent(ai_config, "prompt", lambda kind, data: events.append((kind, data)))
    agent._parts = []  # set by send()
    agent._tool("mcp__chemotion__view_page", '{"path": "/a/paper.pdf", "page": 3}')
    assert events == [("tool", "Looked at page 3 of paper.pdf")]
