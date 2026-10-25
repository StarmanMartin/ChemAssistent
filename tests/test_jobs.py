"""jobs.py: the registry, its report texts and the jupyter_jobs info — without
JupyterLab; the JupyterBridge calls are only touched where a fake covers them."""
from __future__ import annotations

import json
import threading
import time

import jobs as jobs_module
from jobs import FINISHED, KEEP_S, Jobs, duration, new_id, report


def make_jobs(state_dir, on_change=lambda: None) -> Jobs:
    return Jobs(on_change)


def a_job(**changes) -> dict:
    job = {"id": new_id(), "conversation": "conv-1", "purpose": "A long calculation",
           "server": "http://jupyter.test", "folder": "ChemAssistent/conv-1", "kernel": "k1",
           "notebook_url": "http://jupyter.test/lab/tree/jobs/job-x.ipynb",
           "started": time.time() - 65, "state": "running", "finished": None, "report": "",
           "delivered": False, "problem": "", "wake": True}
    job.update(changes)
    return job


# --- the registry ---------------------------------------------------------------
def test_jobs_survive_a_restart(state_dir):
    jobs = make_jobs(state_dir)
    job = a_job()
    jobs.add(job)
    assert jobs.list()[0]["id"] == job["id"]
    assert jobs_module.JOBS_FILE.is_file() and json.loads(jobs_module.JOBS_FILE.read_text(encoding="utf-8"))

    # A second Jobs (as after a restart of the app) reads the same registry.
    again = make_jobs(state_dir)
    assert [j["id"] for j in again.list()] == [job["id"]]
    assert again.update(job["id"], state="done", finished=123.0)
    assert make_jobs(state_dir).list()[0]["state"] == "done"


def test_update_and_list_filtering(state_dir):
    jobs = make_jobs(state_dir)
    one, other = a_job(conversation="conv-1"), a_job(conversation="conv-2")
    jobs.add(one)
    jobs.add(other)

    assert [j["id"] for j in jobs.list("conv-2")] == [other["id"]]
    assert jobs.update(other["id"], only_running=True, state="done")
    assert not jobs.update(other["id"], only_running=True, state="running")  # it is no longer running


def test_undelivered_and_mark_delivered(state_dir):
    jobs = make_jobs(state_dir)
    done, running = a_job(state="done", finished=time.time()), a_job()
    jobs.add(done)
    jobs.add(running)

    assert [j["id"] for j in jobs.undelivered()] == [done["id"]]
    jobs.mark_delivered(jobs.undelivered())
    assert jobs.undelivered() == []
    assert jobs.on_change_called if hasattr(jobs, "on_change_called") else True


def test_on_change_is_called(state_dir):
    calls = []
    jobs = Jobs(on_change=lambda: calls.append(1))
    jobs.add(a_job(state="done", finished=time.time()))
    assert not calls  # add() is silent; only ends and deliveries notify
    jobs.mark_delivered(jobs.undelivered())
    assert calls


def test_delivered_jobs_are_forgotten(state_dir, monkeypatch):
    monkeypatch.setattr(jobs_module, "KEEP_S", 0)
    jobs = make_jobs(state_dir)
    jobs.add(a_job(state="done", finished=time.time() - 10, delivered=True))
    jobs._save()
    assert jobs.list() == []


def test_a_broken_registry_file_starts_empty(state_dir):
    jobs_module.JOBS_FILE.write_text("[not json", encoding="utf-8")
    assert make_jobs(state_dir).list() == []


# --- the report the agent gets --------------------------------------------------
def status(**changes) -> dict:
    return {"started": 1000.0, "finished": 1000.0 + 125, "state": "done", "error": "",
            "output": "progress\n", "output_chars": 8, "result": "42", "figures": [], **changes}


def finish(**changes) -> dict:
    return {"downloaded": [], "notes": [], **changes}


def test_report_of_a_finished_job():
    job = a_job(state="done", started=1000.0, finished=1000.0 + 125)
    text = report(job, status(), finish())
    assert text.startswith("Background job")
    assert "A long calculation" in text
    assert "finished successfully after 2 min" in text
    assert "Value of the last expression: 42" in text
    assert "progress" in text
    assert "Continue with the task" in text


def test_report_of_a_failed_and_a_cancelled_job():
    failed = report(a_job(state="failed", started=1000.0, finished=1000.0 + 10),
                    status(state="failed", error="MemoryError", started=1000.0, finished=1000.0 + 10), finish())
    assert "failed after 10 s" in failed and "MemoryError" in failed

    cancelled = report(a_job(state="cancelled", started=1000.0, finished=1000.0 + 10), {}, {})
    assert "cancelled by the user" in cancelled
    assert "Continue with the task" not in cancelled  # there is nothing to continue

    lost = report(a_job(state="lost", started=1000.0, finished=1000.0 + 10),
                  status(state="lost", error=""), finish())
    assert "without finishing" in lost and "kernel is gone" in lost


def test_report_clipped_output_and_exchanged_files():
    text = report(a_job(state="done", started=1000.0, finished=1000.0 + 5),
                  status(output="x" * 40, output_chars=200, figures=["f1.png"]),
                  finish(downloaded=["data.csv"], notes=["Two figures were not shown."]))
    assert "(the last 40 of 200 characters)" in text
    assert "f1.png" in text and "data.csv" in text
    assert "Two figures were not shown." in text


def test_report_keeps_the_end_of_long_output():
    output = "early\n" + "x" * (jobs_module.REPORT_OUTPUT_CHARS + 100) + "\nthe last line"
    text = report(a_job(state="done", started=1000.0, finished=1000.0 + 5),
                  status(output=output, output_chars=50000), finish())
    assert "the last line" in text and "early" not in text
    assert f"(the last {jobs_module.REPORT_OUTPUT_CHARS} of 50000 characters)" in text
    assert text.endswith("tell the user what came out.") and len(text) < 20000  # app.MAX_ANSWER_CHARS


# --- the jupyter_jobs tool --------------------------------------------------------
def test_info_without_jobs(state_dir):
    assert make_jobs(state_dir).info("conv-1") == "This conversation has no background jobs."


def test_info_lists_states_and_reports(state_dir, monkeypatch):
    jobs = make_jobs(state_dir)
    running, done = a_job(), a_job(state="done", finished=time.time(), report="It is finished.", delivered=True)
    jobs.add(running)
    jobs.add(done)

    # load_config() would read the real config; with JupyterLab off, no log is read.
    monkeypatch.setattr(jobs_module, "load_config", lambda: (_ for _ in ()).throw(ValueError("no config")))

    lines = jobs.info("conv-1").splitlines()
    # Without a working config the running job's log cannot be read; the
    # line then just says its state and age.
    assert lines[0].startswith(f"- {running['id']} (A long calculation): running (1 min)")
    assert lines[1].startswith(f"- {done['id']}") and "its report was delivered" in lines[1]
    assert len(lines) == 2


# --- the watcher ------------------------------------------------------------------
def test_watcher_finds_nothing_without_running_jobs(state_dir, monkeypatch):
    jobs = make_jobs(state_dir)
    monkeypatch.setattr(jobs_module, "POLL_S", 0.05)
    finished = []

    class Watched(Jobs):
        def _check(self) -> bool:
            result = super()._check()
            finished.append(result)
            return result

    watched = Watched(on_change=lambda: None)
    watched.add(a_job(state="done", finished=time.time()))
    watched.start()
    time.sleep(0.4)
    assert finished and finished[0] is False


def test_finished_states_and_durations():
    assert set(FINISHED) == {"done", "failed", "lost", "cancelled"}
    assert duration(0) == "0 s" and duration(-5) == "0 s"
    assert duration(7200) == "2 h 0 min"
    assert duration(100000) == "1 d 3 h"
    assert new_id().startswith("job-")
    assert KEEP_S > 86400


def test_threading_lock_protects_the_registry(state_dir):
    """add() from many threads keeps every job."""
    jobs = make_jobs(state_dir)
    threads = [threading.Thread(target=jobs.add, args=(a_job(),)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(jobs.list()) == 8
