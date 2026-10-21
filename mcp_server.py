"""MCP server (stdio) that opencode starts for the chemotion agent (no GTK).

It offers one tool, `execute_python`. A call does not run anything itself: it
sends the code to the running app (ipc.py: CHEM_ASSISTENT_SOCKET, a Unix
socket, or 127.0.0.1:port with CHEM_ASSISTENT_TOKEN on Windows), which
shows it to the user, runs it only after the user confirmed it, and answers
with the output. The call blocks until then. The connection stays open in
both directions meanwhile: the app takes its closing as the call being
cancelled.

Plain JSON-RPC over newline-delimited stdin/stdout, so it needs no MCP
library. Nothing but protocol messages may go to stdout.
"""
from __future__ import annotations

import json
import os
import sys

import ipc


SERVER_INFO = {"name": "chemotion", "version": "1.0.0"}
PROTOCOL_VERSION = "2025-06-18"

EXECUTE_TOOL = {
    "name": "execute_python",
    "description": (
        "Run Python code against the user's Chemotion ELN using the chemotion_api package "
        "(branch version-3, see CHEMOTION_API.md). A logged-in `instance` "
        "(chemotion_api.Instance) is already defined; do not log in yourself. "
        "The user sees the code in the result panel and must confirm it before it runs; "
        "they may edit it first or reject it with a reason. "
        "Assign what the user should see to the variable `result`: a list of dicts is shown "
        "as a table, a dict as key/value table, anything else as text. Chemotion elements "
        "(or lists of them) in `result` become rows the user can click to open the element "
        "in the browser; so does any dict with a \"url\" key. print() output is "
        "shown as well. The answer contains the output, the result as JSON, or the error "
        "traceback, or the user's reason for rejecting the code. "
        "Read-only queries and changes go through the same confirmation; say in `purpose` "
        "whether the code changes data."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "description": "One sentence for the user: what the code does and whether it changes data.",
            },
            "code": {
                "type": "string",
                "description": "Python source. `instance` is predefined; set `result` to show data.",
            },
        },
        "required": ["purpose", "code"],
    },
}


def ask_app(request: dict) -> dict:
    address = os.environ.get(ipc.ADDRESS_ENV, "")
    if not address:
        return {"ok": False, "text": f"{ipc.ADDRESS_ENV} is not set; start opencode from ChemAssistent."}
    request = {**request, "token": os.environ.get(ipc.TOKEN_ENV, "")}
    try:
        with ipc.connect(address) as conn:
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
    except OSError as exc:
        return {"ok": False, "text": f"ChemAssistent is not reachable ({exc}). Is the app running?"}
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return {"ok": False, "text": "ChemAssistent closed the request without an answer."}


def call_tool(name: str, arguments: dict) -> dict:
    if name != EXECUTE_TOOL["name"]:
        return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}
    code = str(arguments.get("code") or "")
    if not code.strip():
        return {"content": [{"type": "text", "text": "`code` is empty."}], "isError": True}
    answer = ask_app({
        "type": "execute",
        "purpose": str(arguments.get("purpose") or ""),
        "code": code,
    })
    return {
        "content": [{"type": "text", "text": str(answer.get("text") or "")}],
        "isError": not answer.get("ok"),
    }


def handle(message: dict) -> dict | None:
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": [EXECUTE_TOOL]}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    elif "id" not in message:
        return None  # a notification, such as notifications/initialized
    else:
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    if "id" not in message:
        return None
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    # JSON-RPC is UTF-8 with bare \n; Windows would use the ANSI code page and \r\n.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        response = handle(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
