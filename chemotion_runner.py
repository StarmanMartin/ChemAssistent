"""Run one confirmed code snippet against Chemotion (no GTK, own process).

app.py starts this file with the venv's Python for every snippet the user
confirmed, so a hanging or crashing snippet can be killed without taking
the app down. Input on stdin, one JSON object:
    {"url", "user", "password", "verify_tls", "code", "exchange"}
`exchange` (optional, with JupyterLab on): the conversation's exchange
folder, predefined in the snippet as a pathlib.Path (jupyter.py).
Output on stdout, one JSON object:
    {"ok", "stdout", "result", "error"}
`result` is the snippet's `result` variable made JSON-safe (see to_jsonable).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback
from collections.abc import Mapping
from enum import Enum
from pathlib import Path


MAX_ITEMS = 500
MAX_DEPTH = 6


def describe_object(obj) -> dict | None:
    """Collections, elements and users as small dicts instead of their repr."""
    fields = {}
    for name in ("short_label", "name", "label", "element_type", "id", "email", "first_name", "last_name"):
        try:
            value = getattr(obj, name)
        except Exception:  # lazy properties may hit the server and fail
            continue
        if value is None or callable(value):
            continue
        if isinstance(value, (str, int, float, bool)):
            fields[name] = value
    if not fields:
        return None
    if "element_type" in fields:
        # Elements open in the browser when the user clicks them in the panel.
        try:
            fields["url"] = obj.url()
        except Exception:  # not saved yet, or no url() in this version
            pass
        return fields
    return {"type": type(obj).__name__, **fields}


def to_jsonable(value, depth: int = 0):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value, depth)
    if depth >= MAX_DEPTH:
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item, depth + 1) for key, item in list(value.items())[:MAX_ITEMS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = []
        for index, item in enumerate(value):
            if index >= MAX_ITEMS:
                items.append(f"... (more than {MAX_ITEMS} items)")
                break
            items.append(to_jsonable(item, depth + 1))
        return items
    if hasattr(value, "isoformat"):
        return value.isoformat()
    described = describe_object(value)
    return described if described is not None else str(value)


def run(request: dict) -> dict:
    from chemotion_api import Instance

    stdout = io.StringIO()
    namespace = {"__name__": "__chemotion__"}
    try:
        instance = Instance(request["url"], verify_ssl=bool(request.get("verify_tls", True)))
        instance.test_connection().login(request["user"], request["password"])
    except PermissionError:
        return {"ok": False, "stdout": "", "result": None,
                "error": "Login to Chemotion failed: check user and password in the settings."}
    except Exception as exc:
        return {"ok": False, "stdout": "", "result": None,
                "error": f"Cannot connect to Chemotion at {request.get('url')}: {exc}"}
    namespace["instance"] = instance
    if request.get("exchange"):
        namespace["exchange"] = Path(request["exchange"])
        namespace["exchange"].mkdir(parents=True, exist_ok=True)
    try:
        compiled = compile(request["code"], "<chemotion>", "exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            exec(compiled, namespace)
    except BaseException:
        # Drop the runner's own frame so the traceback starts in the snippet.
        kind, exc, tb = sys.exc_info()
        text = "".join(traceback.format_exception(kind, exc, tb.tb_next if tb else None))
        return {"ok": False, "stdout": stdout.getvalue(), "result": None, "error": text}
    return {"ok": True, "stdout": stdout.getvalue(), "result": to_jsonable(namespace.get("result")), "error": ""}


def main() -> None:
    request = json.loads(sys.stdin.read())
    answer = run(request)
    sys.stdout.write(json.dumps(answer, default=str))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
