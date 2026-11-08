"""The tools of CLI agents (Claude Code, Codex): a stdio MCP server the CLI
starts (no GTK, standard library only).

It offers the built-in agent's four tools (and in developer mode
`report_api_bug`, issues.py) and nothing else (the CLIs' own
tools are switched off, cli_agent.py): read-only `read_file`, `grep` and
`list_files` (agent.py's, confined to the workspace and the attachments),
`view_page` (a PDF page or picture as an MCP image block; off with
`vision: false` in the session file), and
`execute_python`, which goes over ipc.py to the running app as
{"type": "execute", "session", "purpose", "code"}. The app shows it as a card
in the result panel like a call of the built-in agent (auto-run mode
included) and answers with {"text"} once the user ran or rejected the code.
So a CLI agent reaches Chemotion only through the user-confirmed path.

The MCP servers the user added (mcp_client.py) run in the app, one McpHub
per conversation: tools/list asks the app for their tools
({"type": "mcp_tools"}), offered here as <server>__<tool> (the CLI adds its
own mcp__chemotion__ prefix), and a call goes to the app as an "execute"
with target "mcp", i.e. as a card. In a "/mcp" turn (`mcp_admin` in the
session file) the bridge also offers agent.MCP_ADMIN_TOOLS: list_mcp_servers
({"type": "mcp_list"}) and add/remove_mcp_server (targets "mcp-add" and
"mcp-remove", confirmed on a card).

    mcp_bridge.py --session-file <path>

The session file (written by cli_agent.py for every turn, mode 0600) holds
the app's ipc `address` and `token`, the conversation's `session` token,
which the app checks, its `auto_run` mode for the tool description, the
`roots` the read-only tools may read, and `developer` and `model` for
report_api_bug, `mcp_admin` for the MCP admin tools, `vision` (default
true) for view_page.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
from pathlib import Path

import ipc
import issues
from agent import (MAX_READ_LINES, MCP_ADMIN_NAMES, MCP_ADMIN_TOOLS, MCP_TOOL_PREFIX, READ_ONLY_TOOLS,
                   REPORT_API_BUG_TOOL, VIEW_PAGE_TOOL, AgentError, execute_python_tool, grep, list_files,
                   mcp_server_entry, read_file, tool_summary, view_page)
from config import load_config


SERVER_NAME = "chemotion"
PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 4_000_000


def mcp_tool(tool: dict, read_only: bool) -> dict:
    """An OpenAI-style tool (agent.py) as an MCP tool."""
    function = tool["function"]
    return {"name": function["name"], "description": function["description"],
            "inputSchema": function["parameters"], "annotations": {"readOnlyHint": read_only}}


class Bridge:
    def __init__(self, data: dict) -> None:
        self.address = str(data.get("address") or "")
        self.token = str(data.get("token") or "")
        self.session = str(data.get("session") or "")
        self.roots = [Path(root) for root in data.get("roots") or []]
        self.tools = ([mcp_tool(execute_python_tool(str(data.get("auto_run") or "off")), False)]
                      + [mcp_tool(tool, True) for tool in READ_ONLY_TOOLS])
        # view_page answers with an MCP image block, which Claude Code shows its model.
        self.vision = data.get("vision", True) is not False
        if self.vision:
            self.tools.append(mcp_tool(VIEW_PAGE_TOOL, True))
        self.developer = bool(data.get("developer"))
        self.model = str(data.get("model") or "")
        if self.developer:
            self.tools.append(mcp_tool(REPORT_API_BUG_TOOL, False))
        self.mcp_admin = bool(data.get("mcp_admin"))
        if self.mcp_admin:
            self.tools += [mcp_tool(tool, tool["function"]["name"] == "list_mcp_servers") for tool in MCP_ADMIN_TOOLS]
        self.lock = threading.Lock()

    def write(self, message: dict) -> None:
        with self.lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def handle(self, message: dict) -> None:
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            return  # a notification (initialized, cancelled, ...)
        if method == "initialize":
            params = message.get("params") or {}
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": "1"}}
        elif method == "tools/list":
            # The app may first start the MCP servers: in a thread, as tools/call.
            threading.Thread(target=self._list, args=(request_id,), daemon=True).start()
            return
        elif method == "tools/call":
            # execute_python waits for the user: in a thread, so pings are still answered.
            threading.Thread(target=self._call, args=(request_id, message.get("params") or {}),
                             daemon=True).start()
            return
        elif method == "ping":
            result = {}
        else:
            self.write({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": f"Unknown method {method}"}})
            return
        self.write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _list(self, request_id) -> None:
        self.write({"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools + self.mcp_tools()}})

    def mcp_tools(self) -> list[dict]:
        """The app's MCP tools for this conversation, as <server>__<tool>."""
        try:
            tools = self.ask({"type": "mcp_tools"}).get("tools") or []
        except AgentError:
            return []  # the CLI goes on with the built-in tools
        return [{**tool, "name": str(tool["name"]).removeprefix(MCP_TOOL_PREFIX)}
                for tool in tools if isinstance(tool, dict) and str(tool.get("name") or "").startswith(MCP_TOOL_PREFIX)]

    def _call(self, request_id, params: dict) -> None:
        arguments = params.get("arguments") or {}
        try:
            answer, error = self.dispatch(str(params.get("name") or ""), arguments), False
        except (AgentError, OSError, TypeError, ValueError) as exc:
            answer, error = f"Error: {exc}", True
        content = answer if isinstance(answer, list) else [{"type": "text", "text": answer}]
        self.write({"jsonrpc": "2.0", "id": request_id, "result": {"content": content, "isError": error}})

    def dispatch(self, name: str, arguments: dict) -> str | list[dict]:
        """The text answer of a tool, or a list of MCP content blocks (view_page)."""
        if name == "view_page" and self.vision:
            text, image, _ = view_page(str(arguments.get("path") or ""), self.roots, arguments.get("page"),
                                       arguments.get("region"))
            return [{"type": "text", "text": text.replace("follows in the next message", "follows")},
                    {"type": "image", "data": base64.b64encode(image.data).decode("ascii"),
                     "mimeType": image.mime}]
        if name == "execute_python":
            code = str(arguments.get("code") or "")
            if not code.strip():
                raise AgentError("No code given.")
            return self.execute(str(arguments.get("purpose") or ""), code)
        if name == "read_file":
            return read_file(str(arguments.get("path") or ""), self.roots, arguments.get("offset") or 1,
                             arguments.get("limit") or MAX_READ_LINES)
        if name == "grep":
            return grep(str(arguments.get("pattern") or ""), self.roots, arguments.get("path"), arguments.get("glob"))
        if name == "list_files":
            return list_files(self.roots, arguments.get("path"))
        if "__" in name:  # an MCP server's tool; built-in names have no "__"
            return self.execute(tool_summary(MCP_TOOL_PREFIX + name, arguments),
                                json.dumps(arguments, indent=2, ensure_ascii=False), "mcp", MCP_TOOL_PREFIX + name)
        if self.mcp_admin and name == "list_mcp_servers":
            return str(self.ask({"type": "mcp_list"}).get("text") or "")
        if self.mcp_admin and name == "add_mcp_server":
            return self.execute(f"Add the MCP server {arguments.get('name')}", mcp_server_entry(arguments), "mcp-add")
        if self.mcp_admin and name == "remove_mcp_server":
            return self.execute(f"Remove the MCP server {arguments.get('name')}",
                                json.dumps({"name": str(arguments.get("name") or "")}), "mcp-remove")
        if name in MCP_ADMIN_NAMES:
            raise AgentError("MCP servers can only be added or removed in a turn whose message starts with /mcp; "
                             "tell the user to write one.")
        if name == "report_api_bug" and self.developer:
            # Fresh settings (URL, GitHub token); load_config() raises ValueError while incomplete.
            return issues.report(load_config(), self.model, str(arguments.get("title") or ""),
                                 str(arguments.get("description") or ""))
        names = ", ".join(tool["name"] for tool in self.tools)
        raise AgentError(f"There is no tool {name}. Use {names}.")

    def ask(self, request: dict) -> dict:
        """One request to the app (ipc.py) and its JSON answer."""
        request = {**request, "token": self.token, "session": self.session}
        try:
            with ipc.connect(self.address, timeout=10) as conn:
                conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
                conn.settimeout(None)  # the user may take their time; MCP servers may start
                data = b""
                while len(data) < MAX_RESPONSE_BYTES and not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            answer = json.loads(data.decode("utf-8"))
            if not isinstance(answer, dict):
                raise ValueError("not an object")
            return answer
        except (OSError, ValueError, TypeError) as exc:
            raise AgentError(f"ChemAssistent did not answer ({exc}).") from None

    def execute(self, purpose: str, code: str, target: str = "chemotion", tool: str = "") -> str:
        """A card in the app's result panel (code, an MCP call or an MCP server change)."""
        try:
            return str(self.ask({"type": "execute", "purpose": purpose, "code": code, "target": target,
                                 "tool": tool})["text"])
        except (AgentError, KeyError) as exc:
            raise AgentError(f"{exc} Nothing ran.") from None


def main(args: list[str] | None = None) -> None:
    args = sys.argv[1:] if args is None else args
    if len(args) != 2 or args[0] != "--session-file":
        sys.exit("usage: mcp_bridge.py --session-file <path>")
    with open(args[1], encoding="utf-8") as handle:
        bridge = Bridge(json.load(handle))
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict):
            bridge.handle(message)


if __name__ == "__main__":
    main()
