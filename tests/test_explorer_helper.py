"""The Explorer helper process: explorer_helper.py's protocol, ExplorerProxy
(restarts, timeouts, the version message) and ExplorerWorker's ErrorText.
The proxy runs against tests/fake_explorer_helper.py; one test against the
real helper and the Chemotion test instance."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import explorer
import explorer_helper
from explorer import ErrorText, ExplorerError, ExplorerProxy, ExplorerWorker

FAKE = str(Path(__file__).with_name("fake_explorer_helper.py"))
CONFIG = SimpleNamespace(url="http://eln.test", user="me", password="s3cret-pw", verify_tls=False,
                         explorer_types=None)


def proxy(mode="ok", stamp=None, **kwargs) -> ExplorerProxy:
    kwargs.setdefault("release_stamp", (lambda: stamp["v"]) if stamp is not None else (lambda: "v312.0.1"))
    kwargs.setdefault("version_check", lambda config, installed: ("", ""))
    kwargs.setdefault("load_config", lambda: CONFIG)
    kwargs.setdefault("save_setting", lambda key, value: None)
    return ExplorerProxy(command=[sys.executable, FAKE, mode], **kwargs)


@pytest.fixture
def made():
    proxies = []

    def make(*args, **kwargs):
        p = proxy(*args, **kwargs)
        proxies.append(p)
        return p

    yield make
    for p in proxies:
        helper = p._helper
        p.close()
        if helper:
            helper.stop()


def alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# -- the proxy ----------------------------------------------------------------

def test_answers_and_config_per_request(made):
    p = made()
    result = p.page({"id": 5, "shared": False}, "sample", 2, 7)
    assert result["method"] == "page"
    assert result["args"] == [{"id": 5, "shared": False}, "sample", 2, 7]
    assert result["config"] == {"url": "http://eln.test", "user": "me", "password": "s3cret-pw",
                                "verify_tls": False, "explorer_types": None}
    assert "s3cret-pw" not in " ".join(result["argv"])  # credentials never on the command line
    assert p.collections()["method"] == "collections" and p.types()["method"] == "types"
    assert p.collections()["pid"] == result["pid"]  # one helper serves all requests


def test_config_is_read_for_every_request(made):
    current = {"config": CONFIG}
    p = made(load_config=lambda: current["config"])
    assert p.types()["config"]["user"] == "me"
    current["config"] = SimpleNamespace(url="http://other", user="you", password="x", verify_tls=True,
                                        explorer_types=("sample", "wellplate"))
    answer = p.types()
    assert answer["config"]["user"] == "you" and answer["config"]["explorer_types"] == ["sample", "wellplate"]


def test_not_configured_starts_no_helper(made):
    def broken():
        raise ValueError("no password")

    p = made(load_config=broken)
    with pytest.raises(ExplorerError, match="not set up yet") as info:
        p.collections()
    assert str(info.value).startswith(explorer.NOT_CONFIGURED) and p._helper is None


def test_errors_of_both_kinds(made):
    with pytest.raises(ExplorerError, match="^Boom$"):
        made("explorer").types()
    with pytest.raises(ExplorerError, match="KeyError: 'x'"):
        made("bug").types()


def test_stray_output_is_ignored(made):
    assert made("stray").types()["method"] == "types"


def test_restart_when_the_release_changes(made):
    stamp = {"v": "v312.0.1"}
    p = made(stamp=stamp)
    first = p.types()["pid"]
    old = p._helper.process
    assert p.types()["pid"] == first
    stamp["v"] = "v401.1.1"
    second = p.types()["pid"]
    assert second != first and p._helper.process.pid == second
    assert old.poll() is not None  # the old one was ended
    assert p.types()["pid"] == second


def test_restart_after_a_crash(made):
    p = made("crash")
    with pytest.raises(ExplorerError, match="ended unexpectedly: Traceback: the helper is broken"):
        p.types()
    assert p._helper is None
    p._command = [sys.executable, FAKE, "ok"]
    assert p.types()["method"] == "types"  # the next request starts a fresh helper


def test_a_dead_helper_is_replaced(made):
    p = made()
    pid = p.types()["pid"]
    p._helper.process.kill()
    p._helper.process.wait()
    assert p.types()["pid"] != pid


def test_timeout_kills_the_helper(made):
    p = made("sleep", timeout=0.5)
    with pytest.raises(ExplorerError, match="did not answer within 0.5 s"):
        p.types()
    assert p._helper is None
    p._command = [sys.executable, FAKE, "ok"]
    assert p.types()["method"] == "types"


def test_a_late_answer_of_an_abandoned_request_is_skipped(made):
    p = made("late", timeout=0.3)
    with pytest.raises(ExplorerError):
        p.types()
    # The timeout killed that helper; the new one answers its first request late too,
    # but within a longer timeout, with the right id.
    p.timeout = 5
    assert p.types()["method"] == "types"


def test_close_ends_the_helper(made):
    p = made()
    pid = p.types()["pid"]
    process = p._helper.process
    p.close()
    assert p._helper is None
    process.wait(timeout=10)
    assert not alive(pid)
    assert p.types()["pid"] != pid  # usable again


def test_a_helper_that_cannot_start(made):
    p = ExplorerProxy(command=["/nonexistent/helper"], load_config=lambda: CONFIG,
                      release_stamp=lambda: "x", version_check=lambda c, i: ("", ""))
    with pytest.raises(ExplorerError, match="Could not start the Explorer's helper"):
        p.types()


def test_save_types_in_the_app_process(made):
    saved = []
    p = made(save_setting=lambda key, value: saved.append((key, value)))
    p.types()
    p.save_types(["sample"], ["sample", "reaction"])
    assert saved == [("explorer_types", ["sample"] + [n for n in explorer.DEFAULT_TYPES
                                                       if n not in ("sample", "reaction")])]
    saved.clear()
    p.save_types(["reaction"], ["sample", "reaction"])  # types this server lacks stay saved
    assert saved[0][1][0] == "reaction" and "wellplate" in saved[0][1]


# -- the version message -------------------------------------------------------

def test_version_warning_replaces_the_error(made):
    calls = []

    def check(config, installed):
        calls.append((config.url, installed))
        return "Install v401.1.1.", "v401.1.1"

    p = made("bug", version_check=check)
    with pytest.raises(ExplorerError) as info:
        p.types()
    assert str(info.value) == "Install v401.1.1." and info.value.release == "v401.1.1"
    with pytest.raises(ExplorerError):
        p.types()
    assert calls == [("http://eln.test", "v312.0.1")]  # once per url and release


def test_version_check_again_for_another_release(made):
    stamp = {"v": "v312.0.1"}
    calls = []
    p = made("bug", stamp=stamp, version_check=lambda c, i: (calls.append(i), ("warn", ""))[1])
    for v in ("v312.0.1", "v401.1.1"):
        stamp["v"] = v
        with pytest.raises(ExplorerError) as info:
            p.types()
        assert str(info.value) == "warn" and info.value.release == ""  # offline: no release
    assert calls == ["v312.0.1", "v401.1.1"]


def test_no_warning_keeps_the_error_and_a_success_clears_the_check(made):
    calls = []
    p = made("bug", version_check=lambda c, i: (calls.append(i), ("", ""))[1])
    for _ in range(2):
        with pytest.raises(ExplorerError, match="KeyError"):
            p.types()
    assert len(calls) == 1
    p._command = [sys.executable, FAKE, "ok"]
    p._drop()
    p.types()  # success: the cached check is dropped
    assert p._checks == {}


def test_login_errors_are_not_checked(made):
    p = made("explorer", version_check=lambda c, i: pytest.fail("checked"))
    p._command = [sys.executable, FAKE, "explorer"]
    # an ExplorerError with the login text is passed on as it is
    exc = p._explained(ExplorerError(explorer.LOGIN_FAILED), "v312")
    assert str(exc) == explorer.LOGIN_FAILED


def test_check_versions_with_api_versions(monkeypatch):
    import api_versions

    monkeypatch.setattr(api_versions, "server_eln_version", lambda url, verify: "4.0.1")
    monkeypatch.setattr(api_versions, "list_tags", lambda *a, **k: ["v312.1.1", "v401.1.1", "v401.0.3"])
    monkeypatch.setattr(api_versions, "installed_tag", lambda: "v312.1.1")
    text, release = explorer._check_versions(CONFIG, "v312.1.1")
    assert release == "v401.1.1" and "Install v401.1.1" in text
    monkeypatch.setattr(api_versions, "installed_tag", lambda: "v401.0.3")
    assert explorer._check_versions(CONFIG, "v401.0.3") == ("", "")
    # offline: tags unknown -> a warning without a release
    monkeypatch.setattr(api_versions, "installed_tag", lambda: "v312.1.1")

    def offline(*a, **k):
        raise api_versions.VersionError("offline")

    monkeypatch.setattr(api_versions, "list_tags", offline)
    text, release = explorer._check_versions(CONFIG, "v312.1.1")
    assert text and release == ""
    monkeypatch.setattr(api_versions, "server_eln_version", lambda url, verify: offline())
    assert explorer._check_versions(CONFIG, "v312.1.1") == ("", "")  # server unknown: no change


# -- the worker ------------------------------------------------------------------

def test_worker_passes_error_text_with_release(made):
    p = made("bug", version_check=lambda c, i: ("Install v401.1.1.", "v401.1.1"))
    got, done = [], threading.Event()
    worker = ExplorerWorker(p, lambda fn: fn())
    worker.request("types", (), lambda r: None, lambda text: (got.append(text), done.set()))
    assert done.wait(20)
    assert got[0] == "Install v401.1.1." and isinstance(got[0], ErrorText) and got[0].release == "v401.1.1"
    worker.close()
    assert p._helper is None


def test_error_text_defaults():
    assert ErrorText("x").release == "" and ErrorText("x", "v1").release == "v1" and ErrorText("x") == "x"
    assert ExplorerError("x").release == ""


def test_worker_close_without_a_close_method():
    worker = ExplorerWorker(SimpleNamespace(), lambda fn: fn())
    worker.close()


# -- explorer_helper itself ----------------------------------------------------

def run_helper(requests: list[dict], *extra_lines: str) -> tuple[list[dict], str]:
    text = "".join(json.dumps(r) + "\n" for r in requests) + "".join(extra_lines)
    done = subprocess.run([sys.executable, str(Path(explorer_helper.__file__))], input=text,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    return [json.loads(line) for line in done.stdout.splitlines()], done.stderr


def test_helper_protocol_errors():
    answers, _ = run_helper([
        {"id": 1, "method": "collections", "args": [], "config": {}},
        {"id": 2, "method": "nope", "args": [], "config": {"url": "u", "user": "x", "password": "y"}},
        {"id": 3, "method": "ping", "args": []},
    ], "not json\n", "\n")
    assert answers[0]["id"] == 1 and answers[0]["kind"] == "explorer"
    assert answers[0]["error"].startswith(explorer.NOT_CONFIGURED)
    assert answers[1]["kind"] == "bug" and "unknown method" in answers[1]["error"]
    assert answers[2].get("result") or answers[2]["kind"] == "bug"  # the version, or no chemotion_api here
    assert answers[3]["id"] is None and answers[3]["kind"] == "bug"
    assert len(answers) == 4


def test_details_goes_to_the_helper_but_a_stub_does_not(made):
    p = made()
    result = p.details("sample", 7, 3)
    assert result["method"] == "details" and result["args"] == ["sample", 7, 3]
    assert p.details("sample", 7)["args"] == ["sample", 7, None]
    pid = result["pid"]
    assert p.details("reaction", 4) == {"type": "reaction", "id": 4, "unsupported": True}
    assert p.details("sample", 8)["pid"] == pid  # the stub started no new helper


def test_helper_answers_details_of_an_unsupported_type_without_a_login():
    login = {"url": "http://127.0.0.1:9", "user": "u", "password": "p"}  # nothing listens there
    answers, _ = run_helper([{"id": 1, "method": "details", "args": ["wellplate", 2, None], "config": login}])
    assert answers[0]["result"] == {"type": "wellplate", "id": 2, "unsupported": True}
    answers, _ = run_helper([{"id": 2, "method": "details", "args": ["sample", 2, None], "config": login}])
    assert answers[0]["kind"] == "explorer" and "Cannot connect" in answers[0]["error"]


def test_helper_stdout_stays_clean_with_stray_prints(monkeypatch):
    class Source:
        def types(self):
            print("noise on stdout")
            return [1]

    # in-process: serve() writes only answers to `out`
    import io

    handler = explorer_helper.Handler()
    handler.source = Source()
    out = io.StringIO()
    explorer_helper.serve(io.StringIO(json.dumps({"id": 1, "method": "types", "args": [],
                                                  "config": {"url": "u", "user": "x", "password": "y"}}) + "\n"),
                          out, handler)
    assert json.loads(out.getvalue()) == {"id": 1, "result": [1]}


def test_helper_redirects_prints_to_stderr(tmp_path):
    script = tmp_path / "run.py"
    script.write_text(
        "import sys, os\n"
        f"sys.path.insert(0, {str(Path(explorer_helper.__file__).parent)!r})\n"
        "import explorer_helper, explorer\n"
        "class S:\n"
        "    def types(self):\n"
        "        print('python noise'); os.write(1, b'c noise\\n'); return [2]\n"
        "orig = explorer_helper.Handler.__init__\n"
        "def init(self):\n"
        "    orig(self); self.source = S()\n"
        "explorer_helper.Handler.__init__ = init\n"
        "sys.exit(explorer_helper.main())\n")
    request = json.dumps({"id": 1, "method": "types", "args": [],
                          "config": {"url": "u", "user": "x", "password": "y"}}) + "\n"
    done = subprocess.run([sys.executable, str(script)], input=request, capture_output=True, text=True, timeout=60)
    assert [json.loads(line) for line in done.stdout.splitlines()] == [{"id": 1, "result": [2]}]
    assert "python noise" in done.stderr and "c noise" in done.stderr


def test_app_side_imports_no_chemotion_api_and_no_gtk():
    code = ("import sys, explorer; from explorer import ExplorerProxy; ExplorerProxy(); "
            "assert 'chemotion_api' not in sys.modules and 'gi' not in sys.modules, sorted(sys.modules)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          cwd=str(Path(explorer.__file__).parent))
    assert done.returncode == 0, done.stderr


def test_frozen_program_and_command():
    import workspace

    assert workspace.FROZEN_PROGRAMS["explorer_helper.py"] == ["chem-assistent-runner.exe", "--explorer-helper"]
    assert workspace.command("explorer_helper.py", console=True)[-1].endswith("explorer_helper.py")


# -- the real helper ----------------------------------------------------------

@pytest.mark.chemotion
def test_proxy_against_the_test_instance(chemotion, test_collection, instance, cleanup_samples):
    collection = instance.get_root_collection().get_collection(test_collection)
    sample = collection.new_sample_smiles("CCO")
    sample.save()
    cleanup_samples.append(sample.id)

    login = SimpleNamespace(verify_tls=True, explorer_types=None, **chemotion)
    p = ExplorerProxy(load_config=lambda: login, save_setting=lambda k, v: None)
    try:
        nodes = p.collections()

        def find(nodes, path):
            for node in nodes:
                if node["path"] == path:
                    return node
                found = find(node["children"], path)
                if found:
                    return found

        node = find(nodes, test_collection.strip("/"))
        assert node and not node["shared"]
        assert "sample" in [t["name"] for t in p.types()]
        result = p.page(node, "sample")
        assert sample.id in [item["id"] for item in result["items"]]
        # a request that fails in the helper comes back as an ExplorerError
        with pytest.raises(ExplorerError, match="no element type"):
            p.page(node, "no_such_type")
    finally:
        p.close()
