"""Old conversations: one JSON file per conversation in HISTORY_DIR (no GTK,
standard library only). The app saves a conversation after every answer and
when it closes; the prompt offers the newest ones to continue, the settings
can delete them all. A conversation's exchange folder (files shared with
JupyterLab, config.EXCHANGE_DIR) goes with it; the copy in Jupyter stays."""
from __future__ import annotations

import json
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from config import EXCHANGE_DIR, STATE_DIR, write_private_json


HISTORY_DIR = STATE_DIR / "history"
# Older conversations are deleted when a new one is saved.
MAX_CONVERSATIONS = 200
MAX_TITLE_CHARS = 80


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    updated: float
    model: str


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def _path(conversation_id: str) -> Path:
    # Ids come from new_id(); never let one leave the directory.
    return HISTORY_DIR / f"{Path(conversation_id).name}.json"


def save(conversation_id: str, title: str, model: str, messages: list[dict], roots: list[str],
         agent: str = "api", session: str = "") -> None:
    """`messages` without the system prompt; `roots` are the attachments;
    `agent` is config.AGENTS' name, `session` a CLI agent's own session id."""
    path = _path(conversation_id)
    created = time.time()
    try:
        created = float(json.loads(path.read_text(encoding="utf-8")).get("created", created))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    # It may hold data from the ELN: readable only by the user, like the config.
    write_private_json(path, {
        "title": " ".join(title.split())[:MAX_TITLE_CHARS],
        "model": model,
        "created": created,
        "updated": time.time(),
        "messages": messages,
        "roots": roots,
        "agent": agent,
        "session": session,
    })
    _prune()


def load(conversation_id: str) -> dict | None:
    try:
        data = json.loads(_path(conversation_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None
    return data


def entries(limit: int | None = None) -> list[Entry]:
    """Saved conversations, newest first."""
    paths = sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for path in paths[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result.append(Entry(path.stem, str(data.get("title") or "Untitled"),
                                float(data.get("updated") or path.stat().st_mtime), str(data.get("model") or "")))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return result


def count() -> int:
    return sum(1 for _ in HISTORY_DIR.glob("*.json"))


def exchange_dir(conversation_id: str) -> Path:
    return EXCHANGE_DIR / Path(conversation_id).name


def _remove(path: Path) -> None:
    path.unlink(missing_ok=True)
    shutil.rmtree(exchange_dir(path.stem), ignore_errors=True)


def delete(conversation_id: str) -> None:
    _remove(_path(conversation_id))


def clear() -> int:
    """Delete every saved conversation; returns how many there were."""
    deleted = 0
    for path in HISTORY_DIR.glob("*.json"):
        _remove(path)
        deleted += 1
    # Also the folders of conversations that were never saved.
    shutil.rmtree(EXCHANGE_DIR, ignore_errors=True)
    return deleted


def _prune() -> None:
    paths = sorted(HISTORY_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[MAX_CONVERSATIONS:]:
        _remove(path)


def age(timestamp: float) -> str:
    """“5 min ago”, “yesterday”, “3 Oct”, ..."""
    seconds = time.time() - timestamp
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if time.localtime(timestamp)[:3] == time.localtime()[:3]:
        return time.strftime("%H:%M", time.localtime(timestamp))
    if seconds < 2 * 86400 and time.localtime(timestamp + 86400)[:3] == time.localtime()[:3]:
        return "yesterday"
    if time.localtime(timestamp).tm_year == time.localtime().tm_year:
        return time.strftime("%d %b", time.localtime(timestamp))
    return time.strftime("%d %b %Y", time.localtime(timestamp))
