"""history.py: the saved conversations (with a throwaway state directory)."""
from __future__ import annotations

import json

import history
from history import Entry, age, clear, delete, entries, exchange_dir, load, new_id, save


def test_save_load_round_trip(state_dir, tmp_path):
    conversation = new_id()
    save(conversation, "Hello   world!", "test-model", [{"role": "user", "content": "Hi"}], ["/tmp/attached.csv"])

    data = load(conversation)
    assert data["title"] == "Hello world!"  # whitespace collapsed
    assert data["model"] == "test-model"
    assert data["messages"] == [{"role": "user", "content": "Hi"}]
    assert data["roots"] == ["/tmp/attached.csv"]
    assert isinstance(data["created"], float) and isinstance(data["updated"], float)


def test_save_keeps_the_created_time(state_dir):
    conversation = new_id()
    save(conversation, "First", "m", [], [])
    created = load(conversation)["created"]
    save(conversation, "Second", "m", [], [])
    assert load(conversation)["created"] == created


def test_entries_are_newest_first(state_dir):
    first, second = new_id(), new_id()
    save(first, "Old one", "m1", [], [])
    save(second, "New one", "m2", [], [])

    assert [entry.id for entry in entries()] == [second, first]
    assert [entry.title for entry in entries()] == ["New one", "Old one"]
    assert entries()[0] == Entry(second, "New one", entries()[0].updated, "m2")
    assert entries(limit=1) == entries()[:1]


def test_broken_files_are_skipped(state_dir):
    (history.HISTORY_DIR / "broken.json").write_text("{not json", encoding="utf-8")
    save(new_id(), "Good", "m", [], [])

    assert len(entries()) == 1
    assert load("broken.json") is None


def test_delete_removes_the_exchange_folder(state_dir, monkeypatch):
    conversation = new_id()
    folder = exchange_dir(conversation)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "figure.png").write_bytes(b"png")
    save(conversation, "With exchange", "m", [], [])

    delete(conversation)

    assert load(conversation) is None
    assert not folder.exists()


def test_clear_counts_and_removes_everything(state_dir):
    for _ in range(3):
        save(new_id(), "A conversation", "m", [], [])
    exchange_dir("never-saved").mkdir(parents=True, exist_ok=True)

    assert clear() == 3
    assert entries() == []
    assert list(history.HISTORY_DIR.glob("*.json")) == []
    assert not history.EXCHANGE_DIR.exists()


def test_ids_cannot_leave_the_directory():
    assert history._path("../evil").name == "evil.json"
    assert history._path("a/b").parent == history.HISTORY_DIR


def test_prune_keeps_the_newest(state_dir, monkeypatch):
    monkeypatch.setattr(history, "MAX_CONVERSATIONS", 2)
    ids = [new_id() for _ in range(4)]
    for conversation in ids:
        save(conversation, conversation, "m", [], [])

    assert sorted(entry.id for entry in entries()) == sorted(ids[2:])


def test_age_wording():
    now = history.time.time()
    assert age(now - 10) == "just now"
    assert age(now - 120) == "2 min ago"
    assert age(now - 3 * 86400) != ""
    # "yesterday" needs a real timezone change; the wording is app.py's business.


def test_files_are_private(state_dir):
    conversation = new_id()
    save(conversation, "Secret", "m", [], [])
    mode = (history.HISTORY_DIR / f"{conversation}.json").stat().st_mode & 0o777
    assert mode == 0o600
    assert json.loads((history.HISTORY_DIR / f"{conversation}.json").read_text(encoding="utf-8"))
