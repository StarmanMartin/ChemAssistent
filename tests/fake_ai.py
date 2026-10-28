"""A scripted stand-in for the AI endpoint (no network, no model).

The agent only speaks the two standard endpoints, so a small HTTP server is
enough to drive it through any conversation: `FakeAI(script)` answers one
scripted turn per POST /chat/completions, in order, and remembers every
request body in `requests` so a test can check what the agent sent.

A turn is a dict:
    {"content": "text"}                      a streamed answer
    {"content": ..., "reasoning": "..."}     with reasoning_content (DeepSeek)
    {"tool_calls": [call, ...]}              call: {"name", "arguments", "id"}
                                             ("arguments" may be a dict or a
                                             string, e.g. broken JSON; optional
                                             "extra_content" like Gemini's
                                             thought signature)
    {"content": ..., "plain": True}          one JSON answer, not SSE
    {"status": 500, "message": "..."}        an HTTP error
    {"error": {"message": "..."}}            an error inside the stream
Everything is sent in several SSE chunks (content in thirds, tool-call names
and arguments in pieces), which is what real servers do and what the agent's
delta merging must survive.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _pieces(text: str, count: int) -> list[str]:
    """`text` in `count` roughly equal pieces (at least one)."""
    if not text:
        return [""]
    size = max(1, -(-len(text) // count))
    return [text[start:start + size] for start in range(0, len(text), size)]


def _chunk(delta: dict) -> dict:
    return {"id": "chatcmpl-test", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def turn_chunks(turn: dict) -> list[dict]:
    """The SSE chunks of one scripted turn."""
    chunks: list[dict] = []
    if turn.get("error"):
        return [{"error": turn["error"]}]
    for piece in _pieces(str(turn.get("reasoning") or ""), 2):
        if piece:
            chunks.append(_chunk({"reasoning_content": piece}))
    for piece in _pieces(str(turn.get("content") or ""), 3):
        if piece:
            chunks.append(_chunk({"content": piece}))
    for index, call in enumerate(turn.get("tool_calls") or []):
        arguments = call.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        name_start, name_rest = call["name"][:3], call["name"][3:]
        # id and the start of the name first, then the arguments in pieces.
        first = {"index": index, "id": call.get("id") or f"call_{index + 1}", "type": "function",
                 "function": {"name": name_start, "arguments": ""}}
        if call.get("extra_content"):
            first["extra_content"] = call["extra_content"]
        chunks.append(_chunk({"tool_calls": [first]}))
        chunks.append(_chunk({"tool_calls": [{"index": index,
                                              "function": {"name": name_rest, "arguments": ""}}]}))
        for piece in _pieces(arguments, 3):
            chunks.append(_chunk({"tool_calls": [{"index": index, "function": {"name": "", "arguments": piece}}]}))
    return chunks


def turn_message(turn: dict) -> dict:
    """One scripted turn as a whole message (the non-streaming answer)."""
    message: dict = {"role": "assistant", "content": turn.get("content") or None}
    calls = []
    for index, call in enumerate(turn.get("tool_calls") or []):
        arguments = call.get("arguments", {})
        calls.append({"id": call.get("id") or f"call_{index + 1}", "type": "function",
                      "function": {"name": call["name"],
                                   "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}})
    if calls:
        message["tool_calls"] = calls
    return message


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    # The agent only ever sends these two.
    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler's name)
        if not self.path.endswith("/models"):
            self._json(404, {"error": "no such endpoint"})
            return
        self._json(200, {"data": self.server.models})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "no such endpoint"})
            return
        self.server.requests.append(body)
        self.server.headers_seen.append(dict(self.headers))
        turn = self.server.next_turn()
        if turn.get("status"):
            self._json(turn["status"], {"error": {"message": turn.get("message") or "scripted failure"}})
            return
        if turn.get("plain"):
            self._json(200, {"id": "chatcmpl-test", "object": "chat.completion",
                             "choices": [{"index": 0, "message": turn_message(turn), "finish_reason": "stop"}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in turn_chunks(turn):
            self._sse(json.dumps(chunk))
            if turn.get("delay"):
                self.server.sending.set()
                turn["delay"].wait(10)
        self._sse("[DONE]")
        self.wfile.write(b"0\r\n\r\n")

    def _sse(self, payload: str) -> None:
        data = f"data: {payload}\n\n".encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeAI(ThreadingHTTPServer):
    """A scripted OpenAI-compatible endpoint; `url` goes into Config.ai_url."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # The agent closes the connection to cancel a turn; that is not news.
        pass

    def __init__(self, script: list[dict] | None = None, models: list[dict] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script: list[dict] = list(script or [])
        self.models = models if models is not None else [{"id": "test-model"}]
        self.requests: list[dict] = []
        self.headers_seen: list[dict] = []
        # Set while a turn with "delay" is streaming (for cancel() tests).
        self.sending = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def script_next(self, *turns: dict) -> None:
        """Add turns, e.g. before retry()."""
        with self._lock:
            self.script.extend(turns)

    def next_turn(self) -> dict:
        with self._lock:
            if not self.script:
                # Not a crash: the agent would loop, so say so in the answer.
                return {"content": "[fake AI: the script is empty]"}
            return self.script.pop(0)

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)
