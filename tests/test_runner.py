"""chemotion_runner.py: the process that runs one confirmed snippet.

The conversion tests need nothing; the rest runs against the local Chemotion
test instance (chemotionapi's test_config, docker on port 3579) and is
skipped when it is not up. Everything those tests create is deleted again.
"""
from __future__ import annotations

import json
import subprocess
import sys
from enum import Enum
from pathlib import Path

import pytest

import chemotion_runner
from chemotion_runner import describe_object, run, to_jsonable

HERE = Path(__file__).resolve().parent


class FakeElement:
    """What describe_object() is meant for: an element with a url()."""

    def __init__(self) -> None:
        self.id = 42
        self.short_label = "SA-1"
        self.name = "Ethanol"
        self.element_type = "sample"

    def url(self) -> str:
        return "http://chemotion.test/sample/42"


class Exploding:
    """Lazy properties may hit the server and fail; that must not crash."""

    @property
    def name(self):
        raise RuntimeError("the server said no")

    @property
    def id(self):
        return 7


# --- making results JSON-safe --------------------------------------------------
def test_to_jsonable_passes_scalars_through():
    assert to_jsonable({"a": 1, "b": [True, None, 2.5, "x"]}) == {"a": 1, "b": [True, None, 2.5, "x"]}


def test_to_jsonable_converts_enums_and_dates():
    import datetime

    class Unit(Enum):
        GRAM = "g"

    assert to_jsonable(Unit.GRAM) == "g"
    assert to_jsonable(datetime.date(2026, 10, 24)) == "2026-10-24"


def test_to_jsonable_describes_elements():
    described = to_jsonable([FakeElement()])
    assert described == [{"short_label": "SA-1", "name": "Ethanol", "element_type": "sample", "id": 42,
                          "url": "http://chemotion.test/sample/42"}]
    # Every described element is JSON-serializable, which is what the panel needs.
    json.dumps(described)


def test_describe_object_survives_failing_properties():
    assert describe_object(Exploding()) == {"type": "Exploding", "id": 7}
    assert describe_object(object()) is None


def test_to_jsonable_caps_long_lists(monkeypatch):
    monkeypatch.setattr(chemotion_runner, "MAX_ITEMS", 3)
    result = to_jsonable(list(range(10)))
    assert result[:3] == [0, 1, 2]
    assert "more than 3 items" in result[3]


def test_to_jsonable_stops_at_max_depth():
    deep: dict = {}
    node = deep
    for _ in range(10):
        node["next"] = {}
        node = node["next"]
    json.dumps(to_jsonable(deep))  # no RecursionError, still serializable


# --- login problems (no instance needed) ---------------------------------------
def test_unreachable_chemotion_is_reported():
    answer = run({"url": "http://127.0.0.1:1", "user": "u", "password": "p", "code": "result = 1"})
    assert answer["ok"] is False
    assert "Cannot connect to Chemotion" in answer["error"]


# --- against the test instance --------------------------------------------------
@pytest.mark.chemotion
def test_wrong_password_is_reported(chemotion):
    answer = run({**chemotion, "password": "definitely-wrong", "code": "result = 1"})
    assert answer["ok"] is False
    assert "Login to Chemotion failed" in answer["error"]


@pytest.mark.chemotion
def test_snippet_prints_and_returns_a_result(chemotion):
    code = ("print('looking at the root collection')\n"
            "result = {'label': instance.get_root_collection().label}\n")
    answer = run({**chemotion, "code": code})

    assert answer["ok"] is True, answer["error"]
    assert answer["stdout"].strip() == "looking at the root collection"
    assert answer["result"] == {"label": "root"}
    assert answer["error"] == ""


@pytest.mark.chemotion
def test_traceback_starts_in_the_snippet(chemotion):
    answer = run({**chemotion, "code": "result = 1 / 0\n"})

    assert answer["ok"] is False
    assert "ZeroDivisionError" in answer["error"]
    assert "<chemotion>" in answer["error"]
    # The runner's own frame is cut off.
    assert "chemotion_runner.py" not in answer["error"]


@pytest.mark.chemotion
def test_exchange_folder_is_predefined(chemotion, tmp_path):
    exchange = tmp_path / "exchange" / "conversation"
    code = ("(exchange / 'from_chemotion.json').write_text('{\"ok\": true}')\n"
            "result = sorted(p.name for p in exchange.iterdir())\n")

    answer = run({**chemotion, "code": code, "exchange": str(exchange)})

    assert answer["ok"] is True, answer["error"]
    assert answer["result"] == ["from_chemotion.json"]
    assert (exchange / "from_chemotion.json").is_file()


@pytest.mark.chemotion
def test_creates_reads_and_describes_a_sample(chemotion, test_collection, cleanup_samples):
    code = (f"col = instance.get_root_collection().get_collection('{test_collection}')\n"
            "sample = col.new_sample_smiles('CCO')\n"
            "sample.properties['external_label'] = 'ChemAssistent runner test'\n"
            "sample.save()\n"
            "result = sample\n")

    answer = run({**chemotion, "code": code})

    assert answer["ok"] is True, answer["error"]
    assert answer["result"]["element_type"] == "sample"
    assert answer["result"]["url"].endswith(str(answer["result"]["id"]))
    cleanup_samples.append(answer["result"]["id"])

    # Read it back in a second snippet, as a second turn of a conversation would.
    read_back = run({**chemotion, "code": (f"sample = instance.get_sample({answer['result']['id']})\n"
                                           "result = sample.properties['external_label']\n")})
    assert read_back["result"] == "ChemAssistent runner test"


@pytest.mark.chemotion
def test_runs_as_its_own_process(chemotion):
    """How app.py starts it: JSON on stdin, JSON on stdout."""
    request = {**chemotion, "code": "print('hello'); result = instance.get_root_collection().label"}
    finished = subprocess.run([sys.executable, str(HERE.parent / "chemotion_runner.py")],
                              input=json.dumps(request), capture_output=True, text=True, timeout=300)

    assert finished.returncode == 0, finished.stderr
    answer = json.loads(finished.stdout)
    assert answer["ok"] is True
    assert answer["stdout"].strip() == "hello"
    assert answer["result"] == "root"
