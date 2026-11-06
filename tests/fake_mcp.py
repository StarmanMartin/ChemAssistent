"""A scripted MCP server for the tests (standard library only).

Run as a program it speaks MCP on stdio (`python fake_mcp.py [--fail-start]`);
`FakeHttpMcp` is the same server over Streamable HTTP (JSON answers, or SSE
with `sse=True`). Tools: `echo` (read-only, returns its text and structured
content), `fail` (a tool error), `slow` (sleeps; for cancelling) and
`stamp` (writes $FAKE_MCP_MARK, to see the environment arrive).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "echo", "description": "Echo a text back.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Sleeps.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stamp", "description": "Returns $FAKE_MCP_MARK.", "inputSchema": {"type": "object"}},
]


def answer(message: dict) -> dict | None:
    """The response to one JSON-RPC message (None for notifications)."""
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        return None
    params = message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-mcp", "version": "1.0"}}
    elif method == "tools/list":
        # Two pages, to test the cursor.
        result = ({"tools": TOOLS[:2], "nextCursor": "page2"} if not params.get("cursor")
                  else {"tools": TOOLS[2:]})
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            result = {"content": [{"type": "text", "text": f"echo: {arguments.get('text')}"}],
                      "structuredContent": {"text": arguments.get("text")}}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "it broke"}], "isError": True}
        elif name == "slow":
            time.sleep(30)
            result = {"content": []}
        elif name == "stamp":
            result = {"content": [{"type": "text", "text": os.environ.get("FAKE_MCP_MARK", "")}]}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"no tool {name}"}}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    if "--fail-start" in sys.argv:
        print("fake-mcp: missing API key", file=sys.stderr, flush=True)
        sys.exit(3)
    print("fake-mcp starting", file=sys.stderr, flush=True)
    lock = threading.Lock()

    def handle(message: dict) -> None:
        reply = answer(message)
        if reply is not None:
            with lock:
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        # "slow" must not block the others (cancelling, closing).
        threading.Thread(target=handle, args=(message,), daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        server: FakeHttpMcp = self.server
        message = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        server.requests.append({"message": message, "headers": {k.lower(): v for k, v in self.headers.items()}})
        reply = answer(message)
        if reply is None:
            self.send_response(202)
            self.end_headers()
            return
        body = json.dumps(reply)
        self.send_response(200)
        if message.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", "session-1")
        if server.sse:
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            # A notification first: it must be skipped.
            self.wfile.write(b'data: {"jsonrpc": "2.0", "method": "notifications/message", "params": {}}\n\n')
            self.wfile.write(f"event: message\ndata: {body}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

    def do_DELETE(self) -> None:  # noqa: N802
        self.server.deleted = self.headers.get("Mcp-Session-Id")
        self.send_response(200)
        self.end_headers()


class FakeHttpMcp(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sse: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.sse = sse
        self.requests: list[dict] = []
        self.deleted = None
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/mcp"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


if __name__ == "__main__":
    main()
