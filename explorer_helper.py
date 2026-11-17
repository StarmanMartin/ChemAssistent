"""The Explorer's helper process: runs ExplorerSource (and so chemotion_api)
outside the app, because a Python process keeps the chemotion_api modules it
imported first, also after another release is installed (explorer.py's
ExplorerProxy starts a new helper then).

Line-delimited JSON (UTF-8) on stdin/stdout, one request at a time:
    request  {"id": n, "method": "collections" | "types" | "page" | "ping", "args": [...],
              "config": {"url", "user", "password", "verify_tls", "explorer_types"}}
    answer   {"id": n, "result": ...}  or  {"id": n, "error": "<text>", "kind": "explorer" | "bug"}
"ping" answers with the installed chemotion_api's version (the Windows self-test).
`config` comes with every request (the credentials never go on the command
line). The process ends on EOF of stdin. Nothing but answers goes to stdout:
anything chemotion_api prints is redirected to stderr (file descriptor 1
included), stderr is free text.

Started with the venv's Python (workspace.command("explorer_helper.py",
console=True)) or, in the Windows bundle, `chem-assistent-runner.exe
--explorer-helper`. Standard library only here; chemotion_api is imported by
ExplorerSource on the first real request.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import IO, Any

import explorer
from explorer import ExplorerError, ExplorerSource

METHODS = ("collections", "types", "page")


def login_of(data: dict) -> SimpleNamespace:
    """The config object ExplorerSource reads, from a request's `config`."""
    types = data.get("explorer_types")
    config = SimpleNamespace(url=str(data.get("url") or ""), user=str(data.get("user") or ""),
                             password=str(data.get("password") or ""),
                             verify_tls=bool(data.get("verify_tls", True)),
                             explorer_types=None if types is None else tuple(types))
    if not (config.url and config.user and config.password):
        raise ValueError("URL, user or password missing")
    return config


class Handler:
    """Answers requests; keeps one ExplorerSource (and its login) between them."""

    def __init__(self) -> None:
        self.config: SimpleNamespace | None = None
        self.source = ExplorerSource(self._config, save_setting=lambda key, value: None)

    def _config(self) -> SimpleNamespace:
        if self.config is None:
            raise ValueError("no configuration")
        return self.config

    def handle(self, request: dict) -> dict:
        answer: dict[str, Any] = {"id": request.get("id")}
        try:
            method = request.get("method")
            if method == "ping":
                from importlib import metadata

                answer["result"] = metadata.version("chemotion_api")
                return answer
            if method not in METHODS:
                raise ValueError(f"unknown method {method!r}")
            try:
                self.config = login_of(request.get("config") or {})
            except ValueError as exc:
                self.config = None
                raise ExplorerError(f"{explorer.NOT_CONFIGURED} ({exc})") from None
            answer["result"] = getattr(self.source, method)(*(request.get("args") or []))
        except ExplorerError as exc:
            answer.pop("result", None)
            answer.update(error=str(exc), kind="explorer")
        except Exception as exc:  # a bug in a request must not end the helper
            answer.pop("result", None)
            answer.update(error=f"{type(exc).__name__}: {exc}", kind="bug")
        return answer


def serve(stdin: IO[str], out: IO[str], handler: Handler | None = None) -> None:
    handler = handler or Handler()
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            answer = {"id": None, "error": f"ValueError: bad request ({exc})", "kind": "bug"}
        else:
            answer = handler.handle(request)
        try:
            text = json.dumps(answer, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            text = json.dumps({"id": answer.get("id"), "error": f"{type(exc).__name__}: {exc}", "kind": "bug"})
        out.write(text + "\n")
        out.flush()


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Answers go to the original stdout; everything else printed meanwhile (even
    # by C code writing to fd 1) goes to stderr.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        serve(sys.stdin, out)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
