"""The tools of CLI agents (Claude Code, Codex): a stdio MCP server the CLI
starts (no GTK, standard library only).

It offers the built-in agent's four tools (and in developer mode
`report_api_bug`, issues.py) and nothing else (the CLIs' own
tools are switched off, cli_agent.py): read-only `read_file`, `grep` and
`list_files` (agent.py's, confined to the workspace and the attachments), and
`execute_python`, which goes over ipc.py to the running app as
{"type": "execute", "session", "purpose", "code"}. The app shows it as a card
in the result panel like a call of the built-in agent (auto-run mode
included) and answers with {"text"} once the user ran or rejected the code.
So a CLI agent reaches Chemotion only through the user-confirmed path.

    mcp_bridge.py --session-file <path>

The session file (written by cli_agent.py for every turn, mode 0600) holds
the app's ipc `address` and `token`, the conversation's `session` token,
which the app checks, its `auto_run` mode for the tool description, the
`roots` the read-only tools may read, and `developer` and `model` for
report_api_bug.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import ipc
import issues
from agent import (MAX_READ_LINES, READ_ONLY_TOOLS, REPORT_API_BUG_TOOL, AgentError, execute_python_tool, grep,
                   list_files, read_file)
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
        self.developer = bool(data.get("developer"))
        self.model = str(data.get("model") or "")
        if self.developer:
            self.tools.append(mcp_tool(REPORT_API_BUG_TOOL, False))
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
            result = {"tools": self.tools}
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

    def _call(self, request_id, params: dict) -> None:
        arguments = params.get("arguments") or {}
        try:
            text, error = self.dispatch(str(params.get("name") or ""), arguments), False
        except (AgentError, OSError, TypeError, ValueError) as exc:
            text, error = f"Error: {exc}", True
        self.write({"jsonrpc": "2.0", "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": error}})

    def dispatch(self, name: str, arguments: dict) -> str:
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
        if name == "report_api_bug" and self.developer:
            # Fresh settings (URL, GitHub token); load_config() raises ValueError while incomplete.
            return issues.report(load_config(), self.model, str(arguments.get("title") or ""),
                                 str(arguments.get("description") or ""))
        names = ", ".join(tool["name"] for tool in self.tools)
        raise AgentError(f"There is no tool {name}. Use {names}.")

    def execute(self, purpose: str, code: str) -> str:
        request = {"type": "execute", "token": self.token, "session": self.session, "purpose": purpose, "code": code}
        try:
            with ipc.connect(self.address, timeout=10) as conn:
                conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
                conn.settimeout(None)  # the user may take their time
                data = b""
                while len(data) < MAX_RESPONSE_BYTES and not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            return str(json.loads(data.decode("utf-8"))["text"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentError(f"ChemAssistent did not answer ({exc}); the code did not run.") from None


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
