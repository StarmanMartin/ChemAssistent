"""Background jobs in JupyterLab (no GTK): the registry and its watcher.

The agent's start_jupyter_job starts code in a kernel of its own
(JupyterBridge.start_job) and returns at once; the job is recorded in
JOBS_FILE. The watcher thread polls the state file of every running job
every POLL_S seconds (also after a restart of the app: everything is in
files, the job itself runs on the Jupyter server). When a job ends it shuts
its kernel down, copies new files into the conversation's exchange folder
and stores a report for the agent. The app delivers the report into its
conversation as a new turn (when the conversation is open and idle, or as
soon as it is continued from the history) and notifies the user otherwise.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time

import history
from config import STATE_DIR, Config, load_config, write_private_json
from jupyter import NOTEBOOK, JupyterBridge, JupyterError


LOG = logging.getLogger("chem-assistent")

JOBS_FILE = STATE_DIR / "jobs.json"
POLL_S = 30
# Delivered jobs are forgotten after this long.
KEEP_S = 30 * 86400
# Printed output of a running job that jupyter_jobs shows the agent.
PROGRESS_CHARS = 1500
FINISHED = ("done", "failed", "lost", "cancelled")
# The user message that carries job reports to the agent; ChatPanel.replay()
# shows it as notes. A message the user typed meanwhile follows USER_TEXT.
REPORT_HEADER = "[Background job report]\n"
USER_TEXT = "\n\n[Message from the user]\n"


def new_id() -> str:
    return "job-" + secrets.token_hex(3)


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def report(job: dict, status: dict, finish: dict) -> str:
    """What the agent learns about a job that ended."""
    if status.get("started") and status.get("finished"):
        took = duration(status["finished"] - status["started"])  # both from the server's clock
    else:
        took = duration((job.get("finished") or time.time()) - job["started"])
    head = f"Background job {job['id']} ({job.get('purpose') or 'no purpose given'})"
    state = job["state"]
    if state == "done":
        parts = [f"{head} finished successfully after {took}."]
    elif state == "cancelled":
        parts = [f"{head} was cancelled by the user after {took}."]
    elif state == "lost":
        parts = [f"{head} stopped after {took} without finishing: {status.get('error') or 'its kernel is gone'}"]
    else:
        parts = [f"{head} failed after {took}:\n{status.get('error') or 'no error message'}"]
    output = str(status.get("output") or "")
    if output.strip():
        total = int(status.get("output_chars") or len(output))
        shown = f" (the last {len(output)} of {total} characters)" if total > len(output) else ""
        output = output.rstrip()
        parts.append(f"Printed output{shown}:\n{output}")
    if state == "done":
        parts.append("Value of the last expression: " + (str(status["result"]) if status.get("result") else "none"))
    for title, names in (("Figures it showed, saved in `exchange`", status.get("figures")),
                         ("New or changed in `exchange` from JupyterLab", finish.get("downloaded"))):
        if names:
            parts.append(f"{title}: {', '.join(names)}")
    parts.extend(finish.get("notes") or [])
    if state != "cancelled":
        parts.append("Continue with the task this job was for and tell the user what came out.")
    return "\n\n".join(parts)


class Jobs:
    """The registry (JOBS_FILE) and the watcher thread. `on_change()` is
    called from the watcher thread when a job ended or its problem changed."""

    def __init__(self, on_change) -> None:
        self.on_change = on_change
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.bridges: dict[tuple, JupyterBridge] = {}
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            self.jobs: list[dict] = [job for job in data if isinstance(job, dict) and job.get("id")]
        except (OSError, ValueError, TypeError):
            self.jobs = []

    # --- the registry -----------------------------------------------------------
    def _save(self) -> None:
        now = time.time()
        self.jobs = [job for job in self.jobs
                     if not (job.get("delivered") and now - (job.get("finished") or now) > KEEP_S)]
        try:
            write_private_json(JOBS_FILE, self.jobs)
        except OSError as exc:
            LOG.warning("Could not save the job list: %s", exc)

    def add(self, job: dict) -> None:
        with self.lock:
            self.jobs.append(job)
            self._save()

    def update(self, job_id: str, only_running: bool = False, **changes) -> bool:
        with self.lock:
            for job in self.jobs:
                if job["id"] == job_id and not (only_running and job["state"] != "running"):
                    job.update(changes)
                    self._save()
                    return True
        return False

    def list(self, conversation: str | None = None) -> list[dict]:
        with self.lock:
            return [dict(job) for job in self.jobs if conversation is None or job["conversation"] == conversation]

    def undelivered(self, conversation: str | None = None) -> list[dict]:
        return [job for job in self.list(conversation) if job["state"] in FINISHED and not job.get("delivered")]

    def mark_delivered(self, jobs: list[dict]) -> None:
        if jobs:
            for job in jobs:
                self.update(job["id"], delivered=True)
            self.on_change()

    # --- JupyterLab -----------------------------------------------------------------
    def _bridge(self, job: dict, config: Config) -> JupyterBridge:
        if not config.jupyter:
            raise JupyterError("JupyterLab is off in the settings.")
        key = (job["conversation"], config)
        bridge = self.bridges.get(key)
        if bridge is None:
            bridge = JupyterBridge(config, job["conversation"], history.exchange_dir(job["conversation"]))
            self.bridges[key] = bridge
        if bridge.client.base != job["server"]:
            raise JupyterError(f"The job runs on {job['server']}, but the settings name another JupyterLab.")
        bridge.folder = job["folder"]  # even if jupyter_folder changed since
        bridge.notebook = f"{job['folder']}/{NOTEBOOK}"
        return bridge

    def start_job(self, bridge: JupyterBridge, conversation: str, code: str, purpose: str) -> dict:
        """Start `code` (JupyterBridge.start_job) and record the job; runs in a
        worker thread. Returns the job record."""
        job_id = new_id()
        started = bridge.start_job(job_id, code, purpose)
        job = {
            "id": job_id, "conversation": conversation, "purpose": purpose, "server": bridge.client.base,
            "folder": bridge.folder, "kernel": started["kernel"], "notebook_url": started["notebook_url"],
            "started": time.time(), "state": "running", "finished": None, "report": "", "delivered": False,
            "problem": "", "wake": True,
        }
        self.add(job)
        self.on_change()
        return {**job, "uploaded": started["uploaded"], "notes": started["notes"]}

    def cancel(self, job_id: str) -> None:
        """Shut the job's kernel down (in a thread); its report goes to the
        agent with the next message, without starting a turn by itself."""
        def work() -> None:
            job = next((job for job in self.list() if job["id"] == job_id), None)
            if job is None or job["state"] != "running":
                return
            finished = time.time()
            if not self.update(job_id, only_running=True, state="cancelled", finished=finished, wake=False):
                return
            job.update(state="cancelled", finished=finished)
            finish: dict = {}
            try:
                bridge = self._bridge(job, load_config())
                bridge.cancel_job(job_id)
                finish = bridge.finish_job(job_id)
            except (JupyterError, ValueError, TimeoutError) as exc:
                finish = {"notes": [f"Shutting the job down: {exc}"]}
            except Exception as exc:
                LOG.exception("Cancelling job %s failed", job_id)
                finish = {"notes": [f"Shutting the job down: {type(exc).__name__}: {exc}"]}
            self.update(job_id, report=report(job, {}, finish))
            self.on_change()

        threading.Thread(target=work, daemon=True).start()

    def info(self, conversation: str) -> str:
        """The jupyter_jobs tool: this conversation's jobs, with the latest
        output of running ones (runs in the agent's thread)."""
        jobs = self.list(conversation)
        if not jobs:
            return "This conversation has no background jobs."
        try:
            config = load_config()
        except ValueError:
            config = None
        lines = []
        for job in jobs:
            since = duration((job.get("finished") or time.time()) - job["started"])
            line = f"- {job['id']} ({job.get('purpose') or 'no purpose'}): {job['state']} ({since})"
            if job.get("problem"):
                line += f"; cannot check it: {job['problem']}"
            if job["state"] == "running" and config is not None:
                try:
                    tail = self._bridge(job, config).job_log(job["id"], PROGRESS_CHARS)
                    line += f"\n  Latest printed output:\n{tail}" if tail.strip() else "\n  Nothing printed yet."
                except JupyterError as exc:
                    line += f"\n  Cannot read its output: {exc}"
            elif job["state"] in FINISHED:
                line += "; its report was delivered" if job.get("delivered") else "; its report is on its way"
            lines.append(line)
        return "\n".join(lines)

    # --- the watcher ------------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True).start()

    def wake(self) -> None:
        self.wakeup.set()

    def _watch(self) -> None:
        while True:
            try:
                if self._check():
                    self.on_change()
            except Exception:  # the watcher must keep running
                LOG.exception("Checking the background jobs failed")
            self.wakeup.wait(POLL_S)
            self.wakeup.clear()

    def _check(self) -> bool:
        """One round over the running jobs; True if something changed."""
        running = [job for job in self.list() if job["state"] == "running"]
        if not running:
            return False
        changed = False
        try:
            config = load_config()
        except ValueError as exc:
            for job in running:
                changed |= job.get("problem") != str(exc) and self.update(job["id"], problem=str(exc))
            return changed
        for job in running:
            problem = ""
            try:
                bridge = self._bridge(job, config)
                status = bridge.job_status(job["id"], job["kernel"])
                if status.get("state") in ("done", "failed", "lost"):
                    try:
                        finish = bridge.finish_job(job["id"])
                    except TimeoutError:
                        continue  # a run holds the exchange folder; next round
                    job.update(state=status["state"], finished=time.time())
                    if self.update(job["id"], only_running=True, state=job["state"], finished=job["finished"],
                                   report=report(job, status, finish), problem=""):
                        LOG.info("Background job %s: %s", job["id"], job["state"])
                        changed = True
                    continue
            except JupyterError as exc:
                problem = str(exc)
            if job.get("problem") != problem:
                changed |= self.update(job["id"], only_running=True, problem=problem)
        return changed
