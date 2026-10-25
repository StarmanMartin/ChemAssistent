"""jupyter.py and the job watcher against a real Jupyter server: the failure
modes of background jobs (failure, lost kernel, cancel, cancel too late,
restart of the app, other server in the settings) and the exchange sync.

The server is started here (throwaway root folder, token "test") from the
`jupyter` program in JUPYTER_TEST_BIN or on the PATH; it needs jupyter_server
and ipykernel. Without it these tests are skipped (marker "jupyter"). The sync
hardening tests at the end need no server.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest

import config as app_config
import jobs as jobs_module
import jupyter as jupyter_module
from jobs import Jobs
from jupyter import JOBS_FOLDER, NOTEBOOK, JupyterBridge

TOKEN = "test"


@pytest.fixture(scope="session")
def jupyter_url(tmp_path_factory):
    program = os.environ.get("JUPYTER_TEST_BIN") or shutil.which("jupyter")
    if not program:
        pytest.skip("no jupyter program (set JUPYTER_TEST_BIN to one with jupyter_server and ipykernel)")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = tmp_path_factory.mktemp("jupyter-root")
    process = subprocess.Popen(
        [program, "server", "--no-browser", f"--port={port}", "--ip=127.0.0.1",
         f"--ServerApp.root_dir={root}", f"--IdentityProvider.token={TOKEN}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.skip(f"{program} server did not start (is jupyter_server installed?)")
        try:
            urllib.request.urlopen(f"{url}/api/status?token={TOKEN}", timeout=2)
            break
        except OSError:
            time.sleep(0.3)
    else:
        process.kill()
        pytest.skip("the Jupyter server did not come up")
    yield url
    process.terminate()
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture
def jupyter_config(jupyter_url, state_dir, monkeypatch):
    config = app_config.Config(url="http://chemotion.invalid", user="u", password="p",
                               jupyter_url=f"{jupyter_url}/lab?token={TOKEN}", jupyter_folder="Tests")
    monkeypatch.setattr(jobs_module, "load_config", lambda: config)
    return config


@pytest.fixture
def bridge(jupyter_config, state_dir, request):
    import history

    conversation = f"conv-{request.node.name[-40:]}-{time.monotonic_ns()}"
    local = history.exchange_dir(conversation)
    local.mkdir(parents=True)
    return JupyterBridge(jupyter_config, conversation, local)


def wait_for(jobs: Jobs, job_id: str, states=jobs_module.FINISHED, timeout: float = 60) -> dict:
    """Runs the watcher's rounds until the job reaches one of `states`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs._check()
        job = next(job for job in jobs.list() if job["id"] == job_id)
        if job["state"] in states:
            return job
        time.sleep(0.3)
    raise AssertionError(f"{job_id} stayed {job['state']}")


def wait_status(bridge: JupyterBridge, job: dict, state: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while bridge.job_status(job["id"], job["kernel"]).get("state") != state:
        assert time.monotonic() < deadline, f"the state file never said {state}"
        time.sleep(0.3)


needs_server = pytest.mark.jupyter


# --- snippets in the conversation's kernel ------------------------------------------
@needs_server
def test_run_records_the_cell_and_syncs_both_ways(bridge):
    (bridge.local_dir / "input.txt").write_text("21", encoding="utf-8")
    outcome = bridge.run("n = int((exchange / 'input.txt').read_text())\n"
                         "(exchange / 'double.txt').write_text(str(2 * n))\nn", "double it", 60)
    assert outcome["ok"] and outcome["result"] == "21"
    assert outcome["uploaded"] == ["input.txt"] and outcome["downloaded"] == ["double.txt"]
    assert (bridge.local_dir / "double.txt").read_text(encoding="utf-8") == "42"
    cells = bridge.client.read_notebook(bridge.notebook)["cells"]
    assert cells[-1]["source"].startswith("n = int(") and cells[-1]["metadata"]["chemassistent"]["purpose"]
    # Variables persist; nothing is sent again.
    again = bridge.run("n + 1", "", 60)
    assert again["result"] == "22" and again["uploaded"] == [] and not again["new_kernel"]


# --- background jobs ---------------------------------------------------------------
@needs_server
def test_job_reports_value_output_and_files(bridge, state_dir):
    jobs = Jobs(lambda: None)
    (bridge.local_dir / "n.txt").write_text("7", encoding="utf-8")
    job = jobs.start_job(bridge, bridge.local_dir.name,
                         "n = int((exchange / 'n.txt').read_text())\nprint('squaring', n)\n"
                         "(exchange / 'square.txt').write_text(str(n * n))\nn * n", "square it")
    ended = wait_for(jobs, job["id"])
    assert ended["state"] == "done" and ended["wake"] and not ended["delivered"]
    assert "Value of the last expression: 49" in ended["report"]
    assert "squaring 7" in ended["report"] and "square.txt" in ended["report"]
    assert (bridge.local_dir / "square.txt").read_text(encoding="utf-8") == "49"
    assert bridge.client.kernel_state(job["kernel"]) is None  # shut down
    # The job's own files stay in Jupyter, not in the exchange folder.
    assert not (bridge.local_dir / JOBS_FOLDER).exists()


@needs_server
def test_failed_job_traceback_starts_in_its_code(bridge, state_dir):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "def f():\n    return 1 / 0\nf()", "divide")
    ended = wait_for(jobs, job["id"])
    assert ended["state"] == "failed"
    assert "ZeroDivisionError" in ended["report"] and "interactiveshell" not in ended["report"]


@needs_server
def test_a_killed_kernel_makes_the_job_lost(bridge, state_dir, monkeypatch):
    """Out of memory: the kernel process dies, Jupyter restarts it under the
    same id, and the state file still says running. Jupyter may go on calling
    the new kernel "busy"; job_status() then asks the kernel itself."""
    import signal

    monkeypatch.setattr(jupyter_module, "JOB_PROBE_S", 0)
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name,
                         "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(120)", "sleep")
    wait_status(bridge, job, "running")
    deadline = time.monotonic() + 30
    while not bridge.job_log(job["id"]).strip():
        assert time.monotonic() < deadline
        time.sleep(0.3)
    os.kill(int(bridge.job_log(job["id"]).split()[0]), signal.SIGKILL)  # the server runs on this machine
    ended = wait_for(jobs, job["id"])
    assert ended["state"] == "lost" and "kernel" in ended["report"]
    assert "ran out of memory" in ended["report"]


@needs_server
def test_a_busy_job_kernel_does_not_answer_the_probe(bridge, state_dir):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(120)", "sleep")
    wait_status(bridge, job, "running")
    assert not bridge.client.answers(job["kernel"], timeout=3)
    assert bridge.client.answers(bridge.client.session(bridge.notebook), timeout=30)  # an idle kernel does
    jobs.cancel(job["id"])
    assert wait_for(jobs, job["id"])["state"] == "cancelled"


@needs_server
def test_a_kernel_shut_down_ends_the_job(bridge, state_dir):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(120)", "sleep")
    wait_status(bridge, job, "running")
    bridge.client.request("DELETE", f"api/kernels/{job['kernel']}")  # e.g. culled
    ended = wait_for(jobs, job["id"])
    # The runner may still write its outcome (interrupted) or the kernel is just gone.
    assert ended["state"] in ("failed", "lost")


@needs_server
def test_cancel_is_finished_by_the_watcher_even_after_a_restart(bridge, state_dir):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(120)", "sleep")
    wait_status(bridge, job, "running")
    assert jobs.cancel(job["id"])
    assert not jobs.cancel(job["id"])  # only once
    assert jobs.list()[0]["state"] == "cancelling"
    # The app quits before the watcher shut the kernel down; the next start does it.
    restarted = Jobs(lambda: None)
    ended = wait_for(restarted, job["id"])
    assert ended["state"] == "cancelled" and ended["wake"] is False
    assert "cancelled by the user" in ended["report"]
    assert restarted.undelivered() and not restarted.list(None)[0]["delivered"]
    deadline = time.monotonic() + 15
    while bridge.client.kernel_state(job["kernel"]) is not None:
        assert time.monotonic() < deadline, "the kernel still runs"
        time.sleep(0.3)


@needs_server
def test_a_cancel_after_the_job_ended_reports_its_outcome(bridge, state_dir):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "6 * 7", "multiply")
    wait_status(bridge, job, "done")
    assert jobs.cancel(job["id"])  # the watcher has not seen it end yet
    ended = wait_for(jobs, job["id"])
    assert ended["state"] == "done" and "Value of the last expression: 42" in ended["report"]
    assert ended["wake"] is False  # the user asked to stop it: no turn by itself


@needs_server
def test_a_cancel_while_the_exchange_folder_is_busy_stays_cancelled(bridge, state_dir):
    """A long execute_jupyter run holds the folder: the kernel is shut down,
    but the sync waits; next round the kernel is gone, which is not "lost"."""
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(120)", "sleep")
    wait_status(bridge, job, "running")
    assert jobs.cancel(job["id"])
    with bridge.lock:  # the watcher's bridge shares it (folder_lock)
        assert not jobs._check()  # kernel shut down, the sync must wait
        deadline = time.monotonic() + 15
        while bridge.client.kernel_state(job["kernel"]) is not None:
            assert time.monotonic() < deadline, "the kernel still runs"
            time.sleep(0.3)
        assert not jobs._check()  # still waiting; the job is not lost
        assert jobs.list()[0]["state"] == "cancelling"
    ended = wait_for(jobs, job["id"])
    assert ended["state"] == "cancelled" and "Continue with the task" not in ended["report"]


@needs_server
def test_a_job_that_did_not_start_leaves_no_kernel(bridge, state_dir, monkeypatch):
    def broken(kernel, code):
        raise jupyter_module.JupyterError("the websocket broke")

    monkeypatch.setattr(bridge.client, "start", broken)
    with pytest.raises(jupyter_module.JupyterError):
        Jobs(lambda: None).start_job(bridge, bridge.local_dir.name, "1", "one")
    sessions = bridge.client.request("GET", "api/sessions") or []
    assert not [s for s in sessions if f"/{JOBS_FOLDER}/" in str(s.get("path"))]
    assert jobs_module.Jobs(lambda: None).list() == []


@needs_server
def test_a_restarted_app_picks_up_running_jobs(bridge, state_dir):
    first = Jobs(lambda: None)
    job = first.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(2)\n'slept'", "nap")
    del first  # the app quits; the job runs on in Jupyter
    changes = []
    restarted = Jobs(lambda: changes.append(1))
    ended = wait_for(restarted, job["id"])
    assert ended["state"] == "done" and "'slept'" in ended["report"]


@needs_server
def test_another_server_in_the_settings_is_a_problem_not_an_end(bridge, state_dir, jupyter_config, monkeypatch):
    jobs = Jobs(lambda: None)
    job = jobs.start_job(bridge, bridge.local_dir.name, "import time\ntime.sleep(120)", "sleep")
    other = dataclasses.replace(jupyter_config, jupyter_url="http://127.0.0.1:9/lab?token=x")
    monkeypatch.setattr(jobs_module, "load_config", lambda: other)
    assert jobs._check()
    listed = jobs.list()[0]
    assert listed["state"] == "running" and "another JupyterLab" in listed["problem"]
    monkeypatch.setattr(jobs_module, "load_config", lambda: jupyter_config)
    jobs.cancel(job["id"])
    assert wait_for(jobs, job["id"])["state"] == "cancelled"
    assert jobs.list()[0]["problem"] == ""


# --- sync hardening (no server) -------------------------------------------------------
class FakeClient:
    base = "http://jupyter.test"

    def __init__(self, files: dict[str, bytes]) -> None:
        self.remote = files
        self.uploaded: list[str] = []

    def files(self, folder):
        return {name: {"last_modified": "t", "size": len(data)} for name, data in self.remote.items()}

    def download(self, path):
        return self.remote[path.split("/", 2)[2]]

    def upload(self, local, remote):
        self.uploaded.append(remote)
        return {"last_modified": "t"}

    def ensure_dir(self, path):
        pass


def offline_bridge(tmp_path, files=None) -> JupyterBridge:
    config = app_config.Config(url="x", user="u", password="p", jupyter_url="http://jupyter.test")
    bridge = JupyterBridge(config, "conv", tmp_path / "exchange")
    bridge.local_dir.mkdir()
    bridge.client = FakeClient(files or {})
    return bridge


def test_sync_down_never_writes_outside_the_exchange_folder(tmp_path):
    bridge = offline_bridge(tmp_path, {"../escaped.txt": b"x", "ok.txt": b"fine", f"{JOBS_FOLDER}/j.json": b"{}",
                                       NOTEBOOK: b"{}"})
    notes: list[str] = []
    assert bridge.sync_down(notes) == ["ok.txt"]
    assert not (tmp_path / "escaped.txt").exists()
    assert any("leaves the exchange folder" in note for note in notes)


def test_sync_up_skips_symlinks(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    bridge = offline_bridge(tmp_path)
    (bridge.local_dir / "data.csv").write_text("a,b\n", encoding="utf-8")
    (bridge.local_dir / "link.txt").symlink_to(secret)
    assert bridge.sync_up([]) == ["data.csv"]
    assert bridge.client.uploaded == ["ChemAssistent/conv/data.csv"]


def test_a_file_too_large_is_noted_on_every_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(jupyter_module, "MAX_TRANSFER_BYTES", 3)
    bridge = offline_bridge(tmp_path, {"big.bin": b"0123456789", "ok.txt": b"ok"})
    for _ in range(2):
        notes: list[str] = []
        bridge.sync_down(notes)
        assert any("big.bin" in note and "larger than" in note for note in notes)
    assert not (bridge.local_dir / "big.bin").exists()
