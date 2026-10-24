"""The JupyterLab side of a conversation (no GTK; requests + websocket-client).

Every conversation gets the folder <jupyter_folder>/<conversation id> on the
Jupyter server with the notebook `analysis.ipynb`. Its kernel runs the
agent's `execute_jupyter` code (started as the notebook's session, so it
works in that folder and the user can open the notebook in JupyterLab and
continue by hand); every snippet is appended there as a cell with its
outputs.

The folder is also the Jupyter end of the conversation's exchange folder
(history.exchange_dir() on this computer, `exchange` in the snippets of both
sides): JupyterBridge.run() uploads what is new there before the code runs
and downloads what is new in Jupyter afterwards. Results go back to
Chemotion through the agent's execute_python, which reads the local copy;
Chemotion credentials never reach the Jupyter server.

Background jobs (start_jupyter_job, jobs.py) run in a kernel of their own,
started for the notebook jobs/<job id>.ipynb in that folder: JOB_RUNNER
runs the code inside the kernel and writes the job's state to
jobs/<job id>.json and its printed output to jobs/<job id>.log, so nobody
has to listen to the kernel while it works (hours or days); jobs.py polls
the state file.

Only the standard Jupyter Server REST API (contents, sessions, kernels) and
the kernel websocket (messaging protocol 5) are used, so JupyterLab,
Notebook 7 and JupyterHub user servers (/user/<name>/) all work.
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import requests

from config import Config, write_private_json


NOTEBOOK = "analysis.ipynb"
# Background jobs: notebook, state and log of each job; not synced.
JOBS_FOLDER = "jobs"
# How much of a job's printed output its state file and job_log() keep.
MAX_JOB_OUTPUT_CHARS = 20000
SYNC_FILE = ".sync.json"  # in the local exchange folder: what was transferred
MAX_TRANSFER_BYTES = 50 * 1024 * 1024
REQUEST_TIMEOUT_S = 60
# After an interrupt, how long to wait for the kernel to report idle.
INTERRUPT_GRACE_S = 15
KERNEL_NAME = "python3"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# Runs silently before every snippet: `exchange` is the folder the kernel
# started in (the conversation's folder), even if a snippet changed the cwd;
# _ca_fresh says whether the kernel is new (variables of earlier runs are gone).
PREAMBLE = """\
if "_ca_exchange" not in globals():
    import pathlib as _ca_pathlib
    _ca_exchange = _ca_pathlib.Path.cwd()
    _ca_fresh = True
else:
    _ca_fresh = False
exchange = _ca_exchange
"""
# The first cells of a new notebook: someone running it by hand needs `exchange` too.
INTRO_CELLS = [
    {"cell_type": "markdown", "metadata": {}, "source":
        "# ChemAssistent analysis\n\nThe assistant appends every snippet it ran here. Files in this "
        "folder (`exchange`) are shared with ChemAssistent: what the assistant fetched from "
        "Chemotion arrives here, and what you save here can go back to Chemotion."},
    {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source":
        "from pathlib import Path\n\nexchange = Path.cwd()  # files shared with ChemAssistent"},
]


# Runs a background job inside its kernel (cwd = <conversation folder>/jobs):
# `exchange` is the conversation folder, prints go to <job id>.log, figures
# into `exchange` as <job id>_figure_<n>.png, the outcome into <job id>.json
# (state running → done/failed; written atomically).
JOB_RUNNER = """\
def _ca_job(code, job_id, max_output):
    import base64, contextlib, json, os, pathlib, sys, time, traceback
    from IPython import get_ipython
    from IPython.utils.capture import capture_output

    def formatted(exc):
        # From the job's own code on: IPython's frames above it say nothing.
        if isinstance(exc, SyntaxError):
            return "".join(traceback.format_exception_only(type(exc), exc))
        tb = exc.__traceback__
        while tb is not None and "ipykernel_" not in tb.tb_frame.f_code.co_filename \\
                and not tb.tb_frame.f_code.co_filename.startswith("<"):
            tb = tb.tb_next
        return "".join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))

    folder = pathlib.Path.cwd()
    exchange = folder.parent
    status = {"state": "running", "started": time.time()}

    def save(**changes):
        status.update(changes, updated=time.time())
        temporary = folder / f"{job_id}.json.tmp"
        temporary.write_text(json.dumps(status, default=str), encoding="utf-8")
        os.replace(temporary, folder / f"{job_id}.json")

    save()
    shell = get_ipython()
    shell.user_ns["exchange"] = exchange
    error, result, figures = "", None, []
    with open(folder / f"{job_id}.log", "w", buffering=1, encoding="utf-8") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log), \\
                capture_output(stdout=False, stderr=False, display=True) as captured:
            # capture_output() captures the last expression too; the shell's
            # own displayhook puts it into outcome.result.
            sys.displayhook = shell.displayhook
            try:
                outcome = shell.run_cell(code, store_history=False)
                failure = outcome.error_before_exec or outcome.error_in_exec
                if failure is not None:
                    error = formatted(failure)
                elif outcome.result is not None:
                    result = repr(outcome.result)[:max_output]
            except BaseException as exc:
                error = formatted(exc)
        for output in captured.outputs:
            png = output.data.get("image/png")
            if png is None:
                continue
            name = f"{job_id}_figure_{len(figures) + 1}.png"
            try:
                (exchange / name).write_bytes(png if isinstance(png, bytes) else base64.b64decode(png))
                figures.append(name)
            except (OSError, ValueError) as exc:
                print(f"Could not save a figure: {exc}", file=log)
    text = (folder / f"{job_id}.log").read_text(encoding="utf-8", errors="replace")
    save(state="failed" if error else "done", finished=time.time(), error=error[-max_output:], result=result,
         figures=figures, output=text[-max_output:], output_chars=len(text))
"""


class JupyterError(Exception):
    pass


def split_url(url: str) -> tuple[str, str]:
    """The server's base URL and a token from a URL as Jupyter prints it
    (http://host:8888/lab?token=abc → http://host:8888, abc)."""
    parts = urlsplit(url.strip())
    token = (parse_qs(parts.query).get("token") or [""])[0]
    path = parts.path
    for suffix in ("/lab", "/tree", "/notebooks", "/login"):
        index = path.find(suffix)
        if index >= 0 and (len(path) == index + len(suffix) or path[index + len(suffix)] == "/"):
            path = path[:index]
            break
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", "")), token


def _quote_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


class JupyterClient:
    """REST and websocket access to one Jupyter server."""

    def __init__(self, config: Config) -> None:
        self.base, url_token = split_url(config.jupyter_url)
        self.token = config.jupyter_token or url_token
        self.password = config.jupyter_password
        self.verify = config.jupyter_verify_tls
        self.http = requests.Session()
        self.http.verify = self.verify
        if self.token:
            self.http.headers["Authorization"] = f"token {self.token}"
        self.logged_in = False

    # --- HTTP -------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def login(self) -> None:
        """Password login (the token needs none): the login form sets the
        session cookie; later requests send the _xsrf cookie back as header."""
        if self.logged_in or self.token or not self.password:
            self.logged_in = True
            return
        try:
            self.http.get(self._url("login"), timeout=REQUEST_TIMEOUT_S)
            xsrf = self.http.cookies.get("_xsrf", "")
            response = self.http.post(self._url("login"), data={"password": self.password, "_xsrf": xsrf},
                                      timeout=REQUEST_TIMEOUT_S, allow_redirects=False)
        except requests.RequestException as exc:
            raise JupyterError(f"Cannot reach JupyterLab at {self.base}: {exc}") from None
        if response.status_code >= 400 or "login" in response.headers.get("Location", "/login"):
            raise JupyterError(f"Login to JupyterLab at {self.base} failed: check the password in the settings.")
        if xsrf := self.http.cookies.get("_xsrf", ""):
            self.http.headers["X-XSRFToken"] = xsrf
        self.logged_in = True

    def request(self, method: str, path: str, body=None, params: dict | None = None, ok404: bool = False):
        self.login()
        try:
            response = self.http.request(method, self._url(path), json=body, params=params,
                                         timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            raise JupyterError(f"Cannot reach JupyterLab at {self.base}: {exc}") from None
        if response.status_code == 404 and ok404:
            return None
        if response.status_code in (401, 403):
            raise JupyterError(f"JupyterLab at {self.base} refused the request (HTTP {response.status_code}): "
                               "check the token or password in the settings.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise JupyterError(f"{method} {path}: HTTP {response.status_code}: {str(detail)[:500]}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise JupyterError(f"{self._url(path)} did not return JSON; is this a Jupyter server?") from None

    def test(self) -> str:
        """A short description of the server, or JupyterError (the settings' test)."""
        status = self.request("GET", "api/status") or {}
        specs = self.request("GET", "api/kernelspecs") or {}
        names = sorted((specs.get("kernelspecs") or {}).keys())
        if KERNEL_NAME not in names:
            raise JupyterError(f"Connected, but the server has no {KERNEL_NAME} kernel (only: {', '.join(names)}).")
        version = (self.request("GET", "api") or {}).get("version", "?")
        return (f"Connected to Jupyter Server {version}; kernels: {', '.join(names)}; "
                f"{status.get('kernels', 0)} running.")

    # --- contents -----------------------------------------------------------------
    def get(self, path: str, content: bool = True, fmt: str | None = None) -> dict | None:
        params = {"content": 1 if content else 0}
        if fmt:
            params.update(format=fmt, type="file")
        return self.request("GET", f"api/contents/{_quote_path(path)}", params=params, ok404=True)

    def ensure_dir(self, path: str) -> None:
        current = ""
        for part in path.strip("/").split("/"):
            current = f"{current}/{part}" if current else part
            if self.get(current, content=False) is None:
                self.request("PUT", f"api/contents/{_quote_path(current)}", {"type": "directory"})

    def files(self, path: str) -> dict[str, dict]:
        """Every file below `path`: relative posix path → contents model (no content)."""
        result: dict[str, dict] = {}
        pending = [path.strip("/")]
        while pending:
            folder = pending.pop()
            model = self.get(folder)
            if model is None or model.get("type") != "directory":
                continue
            for item in model.get("content") or []:
                name = str(item.get("name") or "")
                if not name or name.startswith(".") or name == ".ipynb_checkpoints":
                    continue
                if item.get("type") == "directory":
                    pending.append(item["path"])
                else:
                    relative = str(item["path"])[len(path.strip("/")) + 1:]
                    result[relative] = item
        return result

    def upload(self, local: Path, remote: str) -> dict:
        content = base64.b64encode(local.read_bytes()).decode("ascii")
        return self.request("PUT", f"api/contents/{_quote_path(remote)}",
                            {"type": "file", "format": "base64", "content": content}) or {}

    def download(self, remote: str) -> bytes:
        model = self.get(remote, fmt="base64")
        if model is None:
            raise JupyterError(f"{remote} is gone from JupyterLab.")
        content = model.get("content") or ""
        if model.get("format") == "base64":
            return base64.b64decode(content)
        return str(content).encode("utf-8")

    def read_notebook(self, path: str) -> dict | None:
        model = self.get(path)
        return model.get("content") if model else None

    def write_notebook(self, path: str, notebook: dict) -> None:
        self.request("PUT", f"api/contents/{_quote_path(path)}",
                     {"type": "notebook", "format": "json", "content": notebook})

    # --- kernels ------------------------------------------------------------------
    def session(self, path: str) -> str:
        """The kernel id of the notebook's session; Jupyter returns the existing
        session when the notebook is already open (the same live kernel)."""
        model = self.request("POST", "api/sessions", {
            "path": path, "name": Path(path).name, "type": "notebook", "kernel": {"name": KERNEL_NAME},
        }) or {}
        kernel = (model.get("kernel") or {}).get("id")
        if not kernel:
            raise JupyterError("JupyterLab did not start a kernel.")
        return str(kernel)

    def interrupt(self, kernel_id: str) -> None:
        try:
            self.request("POST", f"api/kernels/{kernel_id}/interrupt")
        except JupyterError:
            pass

    def kernel_state(self, kernel_id: str) -> str | None:
        """The kernel's execution_state (idle, busy, ...), None if it is gone."""
        model = self.request("GET", f"api/kernels/{kernel_id}", ok404=True)
        return str(model.get("execution_state") or "unknown") if model is not None else None

    def close_session(self, path: str) -> None:
        """Shut down the session (and kernel) of the notebook at `path`, if any."""
        for model in self.request("GET", "api/sessions") or []:
            if str(model.get("path") or "").strip("/") == path.strip("/"):
                self.request("DELETE", f"api/sessions/{model['id']}", ok404=True)

    def _channels(self, kernel_id: str, code: str, silent: bool = False,
                  user_expressions: dict | None = None):
        """A websocket to the kernel with an execute_request for `code` sent;
        returns (websocket, msg_id)."""
        import websocket  # websocket-client; only needed here

        self.login()
        parts = urlsplit(self.base)
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = urlunsplit((scheme, parts.netloc, f"{parts.path}/api/kernels/{kernel_id}/channels", "", ""))
        headers = [f"{key}: {value}" for key, value in self.http.headers.items()
                   if key in ("Authorization", "X-XSRFToken")]
        cookie = "; ".join(f"{c.name}={c.value}" for c in self.http.cookies)
        session_id = uuid.uuid4().hex
        msg_id = uuid.uuid4().hex
        message = {
            "header": {"msg_id": msg_id, "username": "chemassistent", "session": session_id,
                       "msg_type": "execute_request", "version": "5.3",
                       "date": datetime.now(timezone.utc).isoformat()},
            "parent_header": {}, "metadata": {}, "channel": "shell", "buffers": [],
            "content": {"code": code, "silent": silent, "store_history": not silent,
                        "user_expressions": user_expressions or {}, "allow_stdin": False,
                        "stop_on_error": True},
        }
        try:
            ws = websocket.create_connection(
                f"{url}?session_id={session_id}", header=headers, cookie=cookie or None, timeout=REQUEST_TIMEOUT_S,
                sslopt=None if self.verify else {"cert_reqs": 0},
            )
        except Exception as exc:
            raise JupyterError(f"Cannot connect to the kernel at {self.base}: {exc}") from None
        try:
            ws.send(json.dumps(message))
        except Exception as exc:
            ws.close()
            raise JupyterError(f"Cannot send code to the kernel at {self.base}: {exc}") from None
        return ws, msg_id

    def start(self, kernel_id: str, code: str) -> None:
        """Send `code` to the kernel and return once it started running; the
        kernel keeps going after the websocket closes (background jobs)."""
        import websocket

        ws, msg_id = self._channels(kernel_id, code)
        deadline = time.monotonic() + REQUEST_TIMEOUT_S
        try:
            ws.settimeout(0.5)
            while time.monotonic() < deadline:
                try:
                    reply = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except (TypeError, ValueError):
                    continue
                if (reply.get("parent_header") or {}).get("msg_id") != msg_id:
                    continue
                kind = (reply.get("header") or {}).get("msg_type") or reply.get("msg_type")
                content = reply.get("content") or {}
                if kind == "status" and content.get("execution_state") == "busy":
                    return
                if kind == "execute_reply" or kind == "error":
                    return  # already over: the state file says how it went
            raise JupyterError("The job's kernel did not start running the code.")
        except (OSError, websocket.WebSocketException) as exc:
            raise JupyterError(f"The connection to the kernel broke: {exc}") from None
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def execute(self, kernel_id: str, code: str, timeout: float, cancelled: threading.Event | None = None,
                silent: bool = False, user_expressions: dict | None = None) -> dict:
        """Run `code` in the kernel and collect what it outputs.
        Returns {ok, stdout, result, images, error, outputs, execution_count,
        user_expressions}; `outputs` are nbformat outputs for the notebook."""
        import websocket  # websocket-client; only needed here

        ws, msg_id = self._channels(kernel_id, code, silent, user_expressions)
        outcome = {"ok": True, "stdout": "", "result": None, "images": [], "error": "", "outputs": [],
                   "execution_count": None, "user_expressions": {}}
        streams: list[str] = []
        deadline = time.monotonic() + timeout
        interrupted = ""
        idle = replied = False
        try:
            ws.settimeout(0.5)
            while not (idle and replied):
                if not interrupted and (time.monotonic() > deadline or (cancelled and cancelled.is_set())):
                    interrupted = ("Cancelled by the user." if cancelled and cancelled.is_set()
                                   else f"Stopped after {timeout:.0f} s (execution_timeout).")
                    self.interrupt(kernel_id)
                    deadline = time.monotonic() + INTERRUPT_GRACE_S
                elif interrupted and time.monotonic() > deadline:
                    break
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(raw, str):
                    continue  # binary frames: not requested
                reply = json.loads(raw)
                if (reply.get("parent_header") or {}).get("msg_id") != msg_id:
                    continue
                kind = (reply.get("header") or {}).get("msg_type") or reply.get("msg_type")
                content = reply.get("content") or {}
                if kind == "status" and content.get("execution_state") == "idle":
                    idle = True
                elif kind == "execute_reply":
                    replied = True
                    outcome["execution_count"] = content.get("execution_count")
                    outcome["user_expressions"] = content.get("user_expressions") or {}
                    if content.get("status") != "ok" and not outcome["error"]:
                        outcome["ok"] = False
                        outcome["error"] = f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
                elif kind == "stream":
                    text = str(content.get("text") or "")
                    streams.append(text)
                    outcome["outputs"].append({"output_type": "stream", "name": content.get("name", "stdout"),
                                               "text": text})
                elif kind in ("execute_result", "display_data"):
                    data = content.get("data") or {}
                    output = {"output_type": kind, "data": data, "metadata": content.get("metadata") or {}}
                    if kind == "execute_result":
                        output["execution_count"] = content.get("execution_count")
                        outcome["result"] = data.get("text/plain")
                    elif data.get("text/plain") and not data.get("image/png"):
                        streams.append(str(data["text/plain"]) + "\n")
                    if data.get("image/png"):
                        outcome["images"].append(str(data["image/png"]).replace("\n", ""))
                    outcome["outputs"].append(output)
                elif kind == "error":
                    outcome["ok"] = False
                    traceback = "\n".join(content.get("traceback") or [])
                    outcome["error"] = ANSI.sub("", traceback) or f"{content.get('ename')}: {content.get('evalue')}"
                    outcome["outputs"].append({"output_type": "error", "ename": content.get("ename", ""),
                                               "evalue": content.get("evalue", ""),
                                               "traceback": content.get("traceback") or []})
                elif kind == "clear_output":
                    outcome["outputs"].clear()
                    streams.clear()
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            outcome["ok"] = False
            outcome["error"] = f"The connection to the kernel broke: {exc}"
        finally:
            try:
                ws.close()
            except Exception:
                pass
        outcome["stdout"] = "".join(streams)
        if interrupted:
            outcome["ok"] = False
            outcome["error"] = interrupted + (f"\n{outcome['error']}" if outcome["error"] else "")
        return outcome


# One lock per exchange folder: the window's bridge and the job watcher's
# (jobs.py) must not sync the same folder at the same time.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def folder_lock(local_dir: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(local_dir.resolve(), threading.Lock())


class JupyterBridge:
    """One conversation's notebook, kernel and exchange folder."""

    def __init__(self, config: Config, conversation_id: str, local_dir: Path) -> None:
        self.config = config
        self.client = JupyterClient(config)
        self.folder = f"{config.jupyter_folder}/{conversation_id}"
        self.notebook = f"{self.folder}/{NOTEBOOK}"
        self.local_dir = local_dir
        self.lock = folder_lock(local_dir)  # one run or sync at a time
        self.ran_before = False  # the notebook has cells of earlier runs (also before a resume)

    @property
    def notebook_url(self) -> str:
        return f"{self.client.base}/lab/tree/{_quote_path(self.notebook)}"

    # --- the exchange folder --------------------------------------------------------
    def _manifest(self) -> dict:
        try:
            data = json.loads((self.local_dir / SYNC_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("local", {})
        data.setdefault("remote", {})
        return data

    def _save_manifest(self, manifest: dict) -> None:
        write_private_json(self.local_dir / SYNC_FILE, manifest)

    def _local_files(self) -> dict[str, Path]:
        result = {}
        for path in sorted(self.local_dir.rglob("*")):
            relative = path.relative_to(self.local_dir)
            if path.is_file() and not any(part.startswith(".") for part in relative.parts):
                result[relative.as_posix()] = path
        return result

    @staticmethod
    def _signature(path: Path) -> list:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]

    def mark_local(self, paths: list[Path]) -> None:
        """Files the app wrote itself (figures) that need not go to Jupyter."""
        manifest = self._manifest()
        for path in paths:
            manifest["local"][path.relative_to(self.local_dir).as_posix()] = self._signature(path)
        self._save_manifest(manifest)

    def sync_up(self, notes: list[str]) -> list[str]:
        manifest = self._manifest()
        uploaded = []
        for relative, path in self._local_files().items():
            signature = self._signature(path)
            if manifest["local"].get(relative) == signature:
                continue
            if signature[0] > MAX_TRANSFER_BYTES:
                notes.append(f"{relative} was not sent to JupyterLab: larger than {MAX_TRANSFER_BYTES >> 20} MB.")
                manifest["local"][relative] = signature
                continue
            if "/" in relative:
                self.client.ensure_dir(f"{self.folder}/{relative.rsplit('/', 1)[0]}")
            model = self.client.upload(path, f"{self.folder}/{relative}")
            manifest["local"][relative] = signature
            manifest["remote"][relative] = model.get("last_modified")
            uploaded.append(relative)
        self._save_manifest(manifest)
        return uploaded

    def sync_down(self, notes: list[str]) -> list[str]:
        manifest = self._manifest()
        downloaded = []
        for relative, model in self.client.files(self.folder).items():
            if (relative == NOTEBOOK or relative.startswith(f"{JOBS_FOLDER}/")
                    or manifest["remote"].get(relative) == model.get("last_modified")):
                continue
            manifest["remote"][relative] = model.get("last_modified")
            if (model.get("size") or 0) > MAX_TRANSFER_BYTES:
                notes.append(f"{relative} was not copied from JupyterLab: larger than {MAX_TRANSFER_BYTES >> 20} MB.")
                continue
            target = self.local_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.client.download(f"{self.folder}/{relative}"))
            manifest["local"][relative] = self._signature(target)
            downloaded.append(relative)
        self._save_manifest(manifest)
        return downloaded

    # --- running code ---------------------------------------------------------------
    def _append_cell(self, code: str, purpose: str, outcome: dict) -> None:
        notebook = self.client.read_notebook(self.notebook)
        if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
            return
        notebook["cells"].append({
            "cell_type": "code",
            "execution_count": outcome.get("execution_count"),
            "metadata": {"chemassistent": {"purpose": purpose}} if purpose else {},
            "outputs": outcome.get("outputs") or [],
            "source": code,
        })
        self.client.write_notebook(self.notebook, notebook)

    def _ensure_notebook(self) -> None:
        self.client.ensure_dir(self.folder)
        if self.client.get(self.notebook, content=False) is not None:
            self.ran_before = True
        else:
            self.client.write_notebook(self.notebook, {
                "cells": INTRO_CELLS,
                "metadata": {"kernelspec": {"name": KERNEL_NAME, "display_name": "Python 3", "language": "python"},
                             "language_info": {"name": "python"}},
                "nbformat": 4, "nbformat_minor": 5,
            })

    def job_notebook(self, job_id: str) -> str:
        return f"{self.folder}/{JOBS_FOLDER}/{job_id}.ipynb"

    def job_url(self, job_id: str) -> str:
        return f"{self.client.base}/lab/tree/{_quote_path(self.job_notebook(job_id))}"

    def start_job(self, job_id: str, code: str, purpose: str) -> dict:
        """Sync up and start `code` in a new kernel of its own (the notebook
        jobs/<job_id>.ipynb shows the code); returns at once with
        {kernel, notebook_url, uploaded, notes}."""
        with self.lock:
            self.local_dir.mkdir(parents=True, exist_ok=True)
            notes: list[str] = []
            self._ensure_notebook()
            uploaded = self.sync_up(notes)
            self.client.ensure_dir(f"{self.folder}/{JOBS_FOLDER}")
            notebook = self.job_notebook(job_id)
            self.client.write_notebook(notebook, {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source":
                        f"# Background job {job_id}\n\n{purpose}\n\nChemAssistent runs the code below in this "
                        f"notebook's kernel. Its state is in `{job_id}.json`, its printed output in "
                        f"`{job_id}.log` (both in this folder); files go to the conversation folder "
                        "(`exchange`)."},
                    {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": code},
                ],
                "metadata": {"kernelspec": {"name": KERNEL_NAME, "display_name": "Python 3", "language": "python"},
                             "language_info": {"name": "python"}},
                "nbformat": 4, "nbformat_minor": 5,
            })
            kernel = self.client.session(notebook)
            self.client.start(kernel, f"{JOB_RUNNER}\n_ca_job({code!r}, {job_id!r}, {MAX_JOB_OUTPUT_CHARS})\n")
            return {"kernel": kernel, "notebook_url": self.job_url(job_id), "uploaded": uploaded, "notes": notes}

    def job_status(self, job_id: str, kernel_id: str) -> dict:
        """The job's state file ({"state": "running"|"done"|"failed", ...}).
        A job whose kernel is gone before it finished is "lost"."""
        status: dict = {"state": "running"}
        model = self.client.get(f"{self.folder}/{JOBS_FOLDER}/{job_id}.json", fmt="text")
        if model is not None:
            try:
                status = json.loads(model.get("content") or "{}")
            except ValueError:
                pass  # being written; next time
        if status.get("state") == "running" and self.client.kernel_state(kernel_id) in (None, "dead"):
            status = {**status, "state": "lost",
                      "error": "The job's kernel is gone (JupyterLab restarted, the kernel was shut down or "
                               "culled, or it ran out of memory)."}
        return status

    def job_log(self, job_id: str, chars: int = 2000) -> str:
        """The end of the job's printed output so far."""
        model = self.client.get(f"{self.folder}/{JOBS_FOLDER}/{job_id}.log", fmt="text")
        return str((model or {}).get("content") or "")[-chars:]

    def finish_job(self, job_id: str) -> dict:
        """Shut the job's kernel down and copy new files back:
        {downloaded, notes}. Raises TimeoutError while a run holds the folder."""
        self.client.close_session(self.job_notebook(job_id))
        if not self.lock.acquire(timeout=5):
            raise TimeoutError("the exchange folder is busy")
        try:
            notes: list[str] = []
            downloaded = self.sync_down(notes) if self.local_dir.is_dir() else []
            return {"downloaded": downloaded, "notes": notes}
        finally:
            self.lock.release()

    def cancel_job(self, job_id: str) -> None:
        self.client.close_session(self.job_notebook(job_id))

    def run(self, code: str, purpose: str, timeout: float, cancelled: threading.Event | None = None) -> dict:
        """Sync up, run in the notebook's kernel, record the cell, sync down.
        The outcome of JupyterClient.execute() plus uploaded, downloaded,
        notes, new_kernel and notebook_url."""
        with self.lock:
            self.local_dir.mkdir(parents=True, exist_ok=True)
            notes: list[str] = []
            self._ensure_notebook()
            uploaded = self.sync_up(notes)
            kernel = self.client.session(self.notebook)
            preamble = self.client.execute(kernel, PREAMBLE, 30, cancelled, silent=True,
                                           user_expressions={"fresh": "_ca_fresh"})
            if not preamble["ok"]:
                raise JupyterError(f"The kernel did not answer: {preamble['error']}")
            fresh = ((preamble["user_expressions"].get("fresh") or {}).get("data") or {}).get("text/plain")
            outcome = self.client.execute(kernel, code, timeout, cancelled)
            try:
                self._append_cell(code, purpose, outcome)
            except JupyterError as exc:
                notes.append(f"The cell was not added to {NOTEBOOK}: {exc}")
            downloaded = self.sync_down(notes)
            outcome.update(uploaded=uploaded, downloaded=downloaded, notes=notes,
                           new_kernel=fresh == "True" and self.ran_before, notebook_url=self.notebook_url)
            return outcome
