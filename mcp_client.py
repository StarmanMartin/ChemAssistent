"""MCP servers for the agents: a client for the Model Context Protocol (no
GTK, standard library only).

The user adds servers in the settings (tab "MCP servers"), or lets the agent
add them in a turn whose message starts with "/mcp" (agent.MCP_ADMIN_TOOLS).
Every conversation has one McpHub (app.py): it starts the enabled servers
when the agent first needs its tools, names their tools
mcp__<server>__<tool>, and ends them with the conversation. A call goes
through the result panel like code (a card with the JSON arguments), so the
auto-run mode applies; "readonly" runs the tools a server marks readOnlyHint.
CLI agents reach the hub through mcp_bridge.py and the app's ipc.

Transports: stdio (a program: `npx -y <package>`, `uvx <package>`,
`docker run -i --rm <image>`, ...) and Streamable HTTP (a URL). The old
HTTP+SSE transport and OAuth are not supported (a token goes into headers).
"""
from __future__ import annotations

import json
import os
import re
import signal
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from agent import MCP_TOOL_PREFIX
from config import (WINDOWS, mcp_server_problem, mcp_servers, normalize_mcp_server, read_raw_config,
                    save_raw_config)


PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "ChemAssistent", "version": "1"}
# npx/uvx/docker download a server on its first start.
START_TIMEOUT_S = 120
LIST_TIMEOUT_S = 60
# What a tool's description may add to every request.
MAX_DESCRIPTION_CHARS = 1500
STDERR_LINES = 30
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Tool names of the OpenAI API: ^[a-zA-Z0-9_-]{1,64}$
MAX_TOOL_NAME = 64


class McpError(Exception):
    pass


def tool_name(server: str, tool: str) -> str:
    """The agent's name of a server's tool."""
    return f"{MCP_TOOL_PREFIX}{server}__{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}"[:MAX_TOOL_NAME]


def split_tool_name(name: str) -> tuple[str, str]:
    """(server, tool) of tool_name(); server names have no "__"."""
    server, _, tool = name.removeprefix(MCP_TOOL_PREFIX).partition("__")
    return server, tool


# --- configuration --------------------------------------------------------------
def parse_servers(text: str) -> list[dict]:
    """MCP servers from JSON as READMEs give it: {"mcpServers": {name: {...}}}
    (Claude Desktop, Cursor), {"servers": {...}} (VS Code), {name: {...}}, one
    {"name", "command"/"url", ...} or a list of those. Normalized; raises McpError."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise McpError(f"Not valid JSON: {exc}") from None
    if isinstance(data, dict):
        for key in ("mcpServers", "servers", "mcp_servers", "mcp"):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
    if isinstance(data, dict) and any(key in data for key in ("command", "url", "serverUrl", "httpUrl")):
        data = [data]
    elif isinstance(data, dict):
        data = [{**spec, "name": spec.get("name") or name} if isinstance(spec, dict) else spec
                for name, spec in data.items()]
    if not isinstance(data, list) or not data:
        raise McpError("No MCP server found in the JSON.")
    return [server_from_json(spec) for spec in data]


def server_from_json(spec) -> dict:
    """One server in any of the usual JSON shapes, normalized (config.normalize_mcp_server)."""
    if not isinstance(spec, dict):
        raise McpError("An MCP server must be a JSON object.")
    spec = dict(spec)
    name = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_-]+", "-", str(spec.get("name") or "")).strip("-_"))[:32]
    spec["name"] = name
    if not name:
        raise McpError("The MCP server needs a name.")
    kind = str(spec.get("type") or spec.get("transport") or "").lower()
    if kind == "sse":
        raise McpError(f"MCP server {name}: the old SSE transport is not supported; use the server's "
                       "Streamable HTTP URL (often ending in /mcp).")
    spec["url"] = spec.get("url") or spec.get("serverUrl") or spec.get("httpUrl") or ""
    if spec.get("disabled") is True:
        spec["enabled"] = False
    problem = mcp_server_problem(spec)
    if problem:
        raise McpError(problem[0].upper() + problem[1:] + ".")
    return normalize_mcp_server(spec)


def summary(spec: dict) -> str:
    """The command line or URL of a server, for lists (no env/header values)."""
    if spec.get("url"):
        text = spec["url"]
        keys = list(spec.get("headers") or {})
        return text + (f" (headers: {', '.join(keys)})" if keys else "")
    text = " ".join([spec.get("command") or ""] + [_quote(arg) for arg in spec.get("args") or []])
    keys = list(spec.get("env") or {})
    return text + (f" (env: {', '.join(keys)})" if keys else "")


def _quote(arg: str) -> str:
    return json.dumps(arg) if not arg or re.search(r"[\s\"']", arg) else arg


def update_saved(change):
    """Change the mcp_servers of the saved config: `change(list) -> (list,
    answer)`; returns the answer. Raises McpError or OSError."""
    data = read_raw_config(retry=True)
    if data.keyring_problem:
        # Saving now would delete the passwords and tokens (config.RawConfig).
        raise McpError(f"the settings cannot be saved now: the system keyring did not give the "
                       f"passwords and tokens ({data.keyring_problem})")
    entries = data.get("mcp_servers")
    servers, answer = change([dict(spec) for spec in entries if isinstance(spec, dict)]
                             if isinstance(entries, list) else [])
    data["mcp_servers"] = servers
    save_raw_config(data)
    return answer


def save_server(spec: dict) -> bool:
    """Add `spec` to the settings, replacing one of the same name; True if it replaced one."""
    def change(servers):
        replaced = any(item.get("name") == spec["name"] for item in servers)
        return [item for item in servers if item.get("name") != spec["name"]] + [spec], replaced

    return update_saved(change)


def forget_server(name: str) -> bool:
    """Remove a server from the settings; True if it was there."""
    def change(servers):
        kept = [item for item in servers if item.get("name") != name]
        return kept, len(kept) != len(servers)

    return update_saved(change)


# --- connections ----------------------------------------------------------------
class _Connection:
    """JSON-RPC over one transport; responses are matched to requests by id."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.name = spec["name"]
        self.lock = threading.Lock()
        self.next_id = 0
        self.pending: dict[int, dict] = {}
        self.protocol = PROTOCOL_VERSION

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def _send(self, message: dict, timeout: float) -> None:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self._send(message, 30)

    def request(self, method: str, params: dict | None, timeout: float,
                cancelled: threading.Event | None = None) -> dict:
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            slot = self.pending[request_id] = {"event": threading.Event()}
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}, timeout)
            deadline = time.monotonic() + timeout
            while not slot["event"].wait(0.2):
                if cancelled is not None and cancelled.is_set():
                    self._cancel(request_id, "cancelled by the user")
                    raise McpError("Cancelled by the user.")
                if time.monotonic() > deadline:
                    self._cancel(request_id, "timeout")
                    raise McpError(f"{self.name} did not answer {method} within {timeout:.0f} s.")
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        response = slot.get("response")
        if response is None:
            raise McpError(slot.get("error") or f"{self.name} did not answer {method}.")
        if "error" in response:
            error = response["error"]
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise McpError(f"{self.name}: {method} failed: {detail}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _cancel(self, request_id: int, reason: str) -> None:
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": reason})
        except McpError:
            pass

    def _deliver(self, message) -> None:
        """A message from the server: a response, or a request/notification of its own."""
        if not isinstance(message, dict):
            return
        if "method" in message:
            if "id" in message:
                self._answer_server(message)
            return
        with self.lock:
            slot = self.pending.get(message.get("id"))
        if slot is not None:
            slot["response"] = message
            slot["event"].set()

    def _fail(self, request_id: int | None, reason: str) -> None:
        """No answer will come for `request_id` (None: for any request)."""
        with self.lock:
            slots = list(self.pending.values()) if request_id is None else [self.pending.get(request_id)]
        for slot in slots:
            if slot is not None and not slot["event"].is_set():
                slot["error"] = reason
                slot["event"].set()

    def _answer_server(self, message: dict) -> None:
        """Requests of the server: we declare no capabilities, so only ping is expected."""
        method = message.get("method")
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        elif method == "roots/list":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"roots": []}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": f"{method} is not supported by ChemAssistent"}}
        try:
            self._send(reply, 30)
        except McpError:
            pass


def find_program(command: str) -> str:
    """The full path of a server's program (a systemd service has a short
    PATH; npx under nvm, uvx in ~/.local/bin); the command itself if unknown."""
    from cli_agent import find_command  # cli_agent imports agent; not at module level

    return find_command(command) or command


class _Stdio(_Connection):
    """A server program; one JSON-RPC message per line on stdin/stdout."""

    def __init__(self, spec: dict) -> None:
        super().__init__(spec)
        self.process: subprocess.Popen | None = None
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self.write_lock = threading.Lock()

    def open(self) -> None:
        program = find_program(self.spec["command"])
        env = dict(os.environ)
        if os.path.isabs(program):
            # npx starts `node` from its own folder (nvm); as cli_agent.child_env().
            env["PATH"] = os.pathsep.join([os.path.dirname(program), env.get("PATH", "")])
        env.update(self.spec.get("env") or {})
        try:
            self.process = subprocess.Popen(
                [program, *self.spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, cwd=os.path.expanduser("~"), start_new_session=True,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            raise McpError(f"Cannot start {self.spec['command']}: {exc}") from None
        threading.Thread(target=self._read, name=f"mcp-{self.name}", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()

    def _read(self) -> None:
        process = self.process
        for raw in process.stdout:
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue  # a log line on stdout
            for item in message if isinstance(message, list) else [message]:
                self._deliver(item)
        process.wait()
        self._fail(None, self._ended())

    def _read_stderr(self) -> None:
        for raw in self.process.stderr:
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def _ended(self) -> str:
        time.sleep(0.2)  # the last stderr lines
        code = self.process.poll() if self.process is not None else None
        detail = "\n".join(line for line in self.stderr if line.strip())[-2000:]
        return (f"{self.name} ended" + (f" with exit code {code}" if code is not None else "")
                + (f":\n{detail}" if detail else "."))

    def _send(self, message: dict, timeout: float) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            raise McpError(self._ended())
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.write_lock:
                process.stdin.write(data)
                process.stdin.flush()
        except (OSError, ValueError):
            raise McpError(self._ended()) from None

    def close(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.close()  # the spec's way to end a stdio server
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if WINDOWS:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGTERM)  # npx and the node it started
                process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


class _Http(_Connection):
    """Streamable HTTP: every message is a POST; a response comes as JSON or
    as an SSE stream."""

    def __init__(self, spec: dict) -> None:
        super().__init__(spec)
        self.session_id = ""

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.protocol, **(self.spec.get("headers") or {})}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post(self, message: dict, timeout: float):
        request = urllib.request.Request(self.spec["url"], data=json.dumps(message).encode("utf-8"),
                                         headers=self._headers(), method="POST")
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            hint = (" (the server needs authorization: put a token into the server's headers, e.g. "
                    "Authorization: Bearer …; OAuth logins are not supported)" if exc.code in (401, 403) else "")
            raise McpError(f"{self.name}: HTTP {exc.code}{hint}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise McpError(f"Cannot reach {self.spec['url']}: {getattr(exc, 'reason', exc)}") from None

    def _send(self, message: dict, timeout: float) -> None:
        if "id" in message and "method" in message:
            # A request: its answer may stream for long; read it in a thread.
            threading.Thread(target=self._exchange, args=(message, timeout), daemon=True).start()
            return
        with self._post(message, min(timeout, 30)) as response:
            response.read()

    def _exchange(self, message: dict, timeout: float) -> None:
        try:
            with self._post(message, timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                kind = response.headers.get("Content-Type") or ""
                if "text/event-stream" in kind:
                    data: list[str] = []
                    for raw in response:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            self._deliver_text("\n".join(data))
                            data = []
                            if message["id"] not in self.pending:
                                return  # answered (or given up)
                    if data:
                        self._deliver_text("\n".join(data))
                else:
                    body = response.read().decode("utf-8", "replace")
                    if body.strip():
                        self._deliver_text(body)
        except McpError as exc:
            self._fail(message["id"], str(exc))
        except (OSError, ValueError) as exc:
            self._fail(message["id"], f"{self.name}: the connection broke: {exc}")
        self._fail(message["id"], f"{self.name} sent no answer to {message['method']}.")

    def _deliver_text(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        for item in message if isinstance(message, list) else [message]:
            self._deliver(item)

    def close(self) -> None:
        if not self.session_id:
            return
        request = urllib.request.Request(self.spec["url"], headers=self._headers(), method="DELETE")
        try:
            urllib.request.urlopen(request, timeout=5, context=ssl.create_default_context()).close()
        except (urllib.error.URLError, OSError):
            pass


# --- servers ------------------------------------------------------------------------
class McpServer:
    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.name = spec["name"]
        self.connection: _Connection | None = None
        self.tools: list[dict] = []
        self.info = ""

    def start(self, timeout: float = START_TIMEOUT_S, cancelled: threading.Event | None = None) -> None:
        """Connect, initialize and list the tools; raises McpError."""
        connection = _Http(self.spec) if self.spec.get("url") else _Stdio(self.spec)
        connection.open()
        try:
            result = connection.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                       "clientInfo": CLIENT_INFO}, timeout, cancelled)
            connection.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
            self.info = " ".join(str(info.get(key) or "") for key in ("name", "version")).strip()
            connection.notify("notifications/initialized")
            tools, cursor = [], None
            for _ in range(50):
                page = connection.request("tools/list", {"cursor": cursor} if cursor else {}, LIST_TIMEOUT_S,
                                          cancelled)
                tools += [tool for tool in page.get("tools") or [] if isinstance(tool, dict) and tool.get("name")]
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        except BaseException:
            connection.close()
            raise
        self.connection, self.tools = connection, tools

    def call(self, tool: str, arguments: dict, timeout: float, cancelled: threading.Event | None = None) -> dict:
        if self.connection is None:
            raise McpError(f"{self.name} is not running.")
        return self.connection.request("tools/call", {"name": tool, "arguments": arguments}, timeout, cancelled)

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            connection.close()


def result_parts(result: dict) -> tuple[bool, str, object]:
    """(ok, text, structuredContent) of a tools/call result."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind in ("image", "audio"):
            size = len(str(item.get("data") or "")) * 3 // 4
            parts.append(f"[{kind} ({item.get('mimeType') or 'unknown type'}, {size} bytes), not shown]")
        elif kind == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            parts.append(str(resource.get("text") or f"[resource {resource.get('uri')}]"))
        elif kind == "resource_link":
            parts.append(f"[resource {item.get('uri')}: {item.get('name') or ''}]")
    return not result.get("isError"), "\n\n".join(parts), result.get("structuredContent")


def failure(error: str) -> dict:
    return {"ok": False, "stdout": "", "result": None, "error": error}


def test_server(spec: dict, timeout: float = START_TIMEOUT_S) -> str:
    """Start a server, list its tools and end it again (the settings' Test); raises McpError."""
    server = McpServer(spec)
    server.start(timeout)
    try:
        names = ", ".join(tool["name"] for tool in server.tools) or "none"
        return f"{server.info or spec['name']}: {len(server.tools)} tools ({names})."
    finally:
        server.close()


class McpHub:
    """The MCP servers of one conversation. start() starts the enabled ones
    that have not been tried yet (the agent before its first request, the
    bridge for a CLI agent's tools/list); add()/remove() are the agent's
    /mcp tools, after the user confirmed them."""

    def __init__(self, specs) -> None:
        self.specs = [dict(spec) for spec in specs if spec.get("enabled", True)]
        self.servers: dict[str, McpServer] = {}
        self.problems: dict[str, str] = {}
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.closed = False
        self._tools: dict[str, tuple[McpServer, dict]] = {}

    def waiting(self) -> bool:
        """Servers start() would still try."""
        with self.lock:
            return any(spec["name"] not in self.servers and spec["name"] not in self.problems
                       for spec in self.specs)

    def start(self, cancelled: threading.Event | None = None) -> list[str]:
        """Start the servers not tried yet, in parallel; the problems of those
        that failed (each reported once). A cancelled start is tried again later."""
        with self.start_lock:
            with self.lock:
                todo = [spec for spec in self.specs
                        if spec["name"] not in self.servers and spec["name"] not in self.problems]
            if not todo or self.closed:
                return []
            results: dict[str, McpServer | str] = {}

            def run(spec: dict) -> None:
                server = McpServer(spec)
                try:
                    server.start(START_TIMEOUT_S, cancelled)
                    results[spec["name"]] = server
                except McpError as exc:
                    if not (cancelled is not None and cancelled.is_set()):
                        results[spec["name"]] = str(exc)
                except Exception as exc:  # a bug must not stop the others
                    results[spec["name"]] = f"{type(exc).__name__}: {exc}"

            threads = [threading.Thread(target=run, args=(spec,), daemon=True) for spec in todo]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            problems = []
            with self.lock:
                for name, outcome in results.items():
                    if isinstance(outcome, McpServer):
                        if self.closed:
                            outcome.close()
                        else:
                            self.servers[name] = outcome
                    else:
                        self.problems[name] = outcome
                        problems.append(f"MCP server {name} did not start: {outcome}")
            self._index()
            return problems

    def _index(self) -> None:
        tools: dict[str, tuple[McpServer, dict]] = {}
        with self.lock:
            for server in self.servers.values():
                for tool in server.tools:
                    tools.setdefault(tool_name(server.name, str(tool["name"])), (server, tool))
            self._tools = tools

    def tool_entries(self) -> list[tuple[str, McpServer, dict]]:
        with self.lock:
            return [(name, server, tool) for name, (server, tool) in self._tools.items()]

    def openai_tools(self) -> list[dict]:
        """The tools for the built-in agent (OpenAI function format)."""
        return [{"type": "function", "function": {
            "name": name, "description": self.description(server, tool), "parameters": self._schema(tool)}}
            for name, server, tool in self.tool_entries()]

    def mcp_tools(self) -> list[dict]:
        """The tools in MCP format (mcp_bridge.py offers them to a CLI agent)."""
        return [{"name": name, "description": self.description(server, tool), "inputSchema": self._schema(tool),
                 "annotations": {"readOnlyHint": self.read_only(name)}}
                for name, server, tool in self.tool_entries()]

    @staticmethod
    def description(server: McpServer, tool: dict) -> str:
        text = str(tool.get("description") or tool.get("title") or tool["name"]).strip()
        return f"[MCP server {server.name}] {text}"[:MAX_DESCRIPTION_CHARS]

    @staticmethod
    def _schema(tool: dict) -> dict:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return {"type": "object", "properties": {}}
        # Some APIs refuse an object schema without properties.
        return schema if isinstance(schema.get("properties"), dict) else {**schema, "properties": {}}

    def has(self, name: str) -> bool:
        with self.lock:
            return name in self._tools

    def read_only(self, name: str) -> bool:
        """The server marks the tool read-only (auto-run mode "readonly")."""
        with self.lock:
            entry = self._tools.get(name)
        annotations = entry[1].get("annotations") if entry else None
        return isinstance(annotations, dict) and annotations.get("readOnlyHint") is True

    def call(self, name: str, arguments, timeout: float, cancelled: threading.Event | None = None) -> dict:
        """One confirmed tool call: an outcome as app.Execution.run() returns it."""
        with self.lock:
            entry = self._tools.get(name)
        if entry is None:
            return failure(f"There is no MCP tool {name} (any more) in this conversation.")
        server, tool = entry
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError as exc:
            return failure(f"The arguments are not valid JSON: {exc}")
        if not isinstance(arguments, dict):
            return failure("The arguments must be a JSON object.")
        try:
            result = server.call(str(tool["name"]), arguments, timeout, cancelled)
        except McpError as exc:
            return failure(str(exc))
        ok, text, structured = result_parts(result)
        if not ok:
            return failure(text or "The tool reported an error.")
        return {"ok": True, "stdout": text, "result": structured, "error": ""}

    def add(self, text: str, cancelled: threading.Event | None = None) -> dict:
        """The agent's add_mcp_server, confirmed: start the server, and only
        if it works save it in the settings and offer its tools."""
        try:
            specs = parse_servers(text)
        except McpError as exc:
            return failure(str(exc))
        if len(specs) != 1:
            return failure("Give exactly one MCP server.")
        spec = {**specs[0], "enabled": True}
        self.start(cancelled)  # the others first: start() must not start this one again
        server = McpServer(spec)
        try:
            server.start(START_TIMEOUT_S, cancelled)
        except McpError as exc:
            return failure(f"The MCP server {spec['name']} did not start, so it was not saved: {exc}")
        try:
            replaced = save_server(spec)
        except (McpError, OSError) as exc:
            server.close()
            return failure(f"Could not save the MCP server: {exc}")
        with self.lock:
            old = self.servers.pop(spec["name"], None)
            if self.closed:
                server.close()
            else:
                self.servers[spec["name"]] = server
            self.problems.pop(spec["name"], None)
            self.specs = [item for item in self.specs if item["name"] != spec["name"]] + [spec]
        if old is not None:
            old.close()
        self._index()
        tools = [{"name": tool_name(spec["name"], str(tool["name"])),
                  "description": str(tool.get("description") or "").strip().split("\n", 1)[0][:200]}
                 for tool in server.tools]
        return {"ok": True, "error": "", "result": tools,
                "stdout": (f"{'Replaced' if replaced else 'Added'} the MCP server {spec['name']}"
                           + (f" ({server.info})" if server.info else "")
                           + f" and saved it in the settings. It has {len(tools)} tools; they are available "
                             "from now on (a CLI agent sees them from the next message on).")}

    def remove(self, text: str) -> dict:
        """The agent's remove_mcp_server, confirmed."""
        try:
            data = json.loads(text)
            name = str(data.get("name") or "") if isinstance(data, dict) else ""
        except ValueError as exc:
            return failure(f"Not valid JSON: {exc}")
        if not name:
            return failure("Give the name of the MCP server to remove.")
        try:
            saved = forget_server(name)
        except (McpError, OSError) as exc:
            return failure(f"Could not save the settings: {exc}")
        with self.lock:
            server = self.servers.pop(name, None)
            self.problems.pop(name, None)
            self.specs = [item for item in self.specs if item["name"] != name]
        if server is not None:
            server.close()
        self._index()
        if not saved and server is None:
            return failure(f"There is no MCP server named {name}.")
        return {"ok": True, "error": "", "result": None,
                "stdout": f"Removed the MCP server {name} from the settings and from this conversation."}

    def describe(self) -> str:
        """The agent's list_mcp_servers: the saved servers and their state here."""
        saved = mcp_servers(read_raw_config(secrets=False))
        if not saved:
            return "No MCP servers are configured."
        with self.lock:
            servers, problems = dict(self.servers), dict(self.problems)
        lines = []
        for spec in saved:
            name = spec["name"]
            if not spec["enabled"]:
                state = "disabled in the settings"
            elif name in servers:
                tools = [tool_name(name, str(tool["name"])) for tool in servers[name].tools]
                state = f"running, {len(tools)} tools: {', '.join(tools) or 'none'}"
            elif name in problems:
                state = f"failed to start: {problems[name][:500]}"
            else:
                state = "not started in this conversation"
            lines.append(f"- {name}: {summary(spec)} — {state}")
        return "MCP servers in the settings:\n" + "\n".join(lines)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            servers, self.servers = list(self.servers.values()), {}
            self._tools = {}
        for server in servers:
            server.close()
