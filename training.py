"""Teaching data: record conversations of the built-in agent and export them
as datasets for fine-tuning or preference training (no GTK, stdlib only).

Opt-in (config `training_data`). The app writes one record per conversation to
TRAINING_DIR (messages incl. the system prompt, tool schemas, one event per
code card with the user's Run/Edit/Reject decision, ratings, turn outcomes).
`export()` curates the records (only turns that ended well, failed attempts
removed, edited code as the gold version, a format check), scrubs URL, login,
secrets, e-mail addresses and local paths and writes `sft.jsonl`,
`preference.jsonl`, `kto.jsonl`, `meta.jsonl` and `README.md`.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable

if __package__:
    from .agent import LESSON_REMINDER
    from .api_versions import installed_label
    from .config import CLI_AGENTS, SKILLS_DIR, STATE_DIR, WORKSPACE_DIR, write_private_json
    from .lessons import api_commit
else:
    from agent import LESSON_REMINDER
    from api_versions import installed_label
    from config import CLI_AGENTS, SKILLS_DIR, STATE_DIR, WORKSPACE_DIR, write_private_json
    from lessons import api_commit


TRAINING_DIR = STATE_DIR / "training"
RECORD_VERSION = 1
DECISIONS = ("run", "edited", "rejected", "auto", "abandoned")
SPLITS = ("train", "validation", "test")
CODE_TOOLS = ("execute_python", "execute_jupyter", "start_jupyter_job")
MAX_ERROR_CHARS = 500
MIN_SECRET_CHARS = 4
MIN_USER_CHARS = 3
EXAMPLE_URL = "https://eln.example.org"
EDITED_PREFIX = "The user edited the code before running it. This code ran:\n```python\n"
EDITED_PATTERN = re.compile(
    r"The user edited the code before running it\. This code ran:\n```python\n.*?\n```(?:\n\n)?", re.S)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

_lock = threading.Lock()


# --- recording -------------------------------------------------------------

def recording_enabled(config) -> bool:
    """Teaching data is recorded for the built-in agent only, and only when switched on."""
    return bool(getattr(config, "training_data", False)) and getattr(config, "agent", "api") not in CLI_AGENTS


def new_event(call_id: str, tool: str, target: str, purpose: str, original_code: str) -> dict:
    """The event of a code card that has not been decided yet."""
    return {"call_id": call_id, "tool": tool, "target": target, "purpose": purpose,
            "original_code": original_code, "executed_code": "", "decision": "abandoned",
            "auto_mode": "", "reject_reason": "", "ok": None, "error": "", "duration_s": None,
            "at": time.time()}


def decide(event: dict, decision: str, *, executed_code: str = "", ok: bool | None = None, error: str = "",
           duration_s: float | None = None, reject_reason: str = "", auto_mode: str = "") -> dict:
    """Fills `event` in place with the outcome of the card and returns it."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision {decision!r}")
    event.update(decision=decision, executed_code=executed_code, ok=ok, error=(error or "")[:MAX_ERROR_CHARS],
                 duration_s=duration_s, reject_reason=reject_reason, auto_mode=auto_mode or "",
                 at=time.time())
    return event


def _read_hash(git: Path, name: str) -> str:
    """The commit that `name` (HEAD or a ref) names inside the git dir `git`, or ""."""
    try:
        head = (git / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    common = git
    try:
        common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        pass
    for base in (git, common):
        found = _read_hash(base, ref)
        if found:
            return found
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return ""


def app_commit(root: Path | None = None) -> str:
    """The short commit of the app's git checkout (read from .git, also a worktree's), or ""."""
    git = (root or Path(__file__).resolve().parent) / ".git"
    try:
        if git.is_file():
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return ""
            git = (git.parent / text[7:].strip()).resolve()
        head = _read_hash(git, "HEAD")
    except OSError:
        return ""
    return head[:7] if re.fullmatch(r"[0-9a-f]{40}", head) else ""


def conversation_meta(*, config, model: str, models: Iterable[str], auto_run: str, jupyter: bool,
                      system_prompt: str) -> dict:
    """The `meta` of a record."""
    return {"app_commit": app_commit(), "chemotionapi": installed_label(), "chemotionapi_commit": api_commit(),
            "teacher_model": model, "models": list(dict.fromkeys(m for m in models if m)),
            "agent": getattr(config, "agent", ""), "auto_run": auto_run, "jupyter": bool(jupyter),
            "system_prompt_sha256": hashlib.sha256((system_prompt or "").encode("utf-8")).hexdigest(),
            "platform": sys.platform}


def _path(conversation_id: str) -> Path:
    if not conversation_id or Path(conversation_id).name != conversation_id or conversation_id.startswith("."):
        raise ValueError(f"bad conversation id {conversation_id!r}")
    return TRAINING_DIR / f"{conversation_id}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record(conversation_id: str, *, messages: list[dict], tools: list[dict], events: dict, ratings: dict,
           turns: list[dict], meta: dict) -> None:
    """Writes (replaces) the record of a conversation; `created` of an existing one is kept."""
    path = _path(conversation_id)
    with _lock:
        old = _read(path) or {}
        now = time.time()
        write_private_json(path, {
            "version": RECORD_VERSION, "id": conversation_id,
            "created": old.get("created") or now, "updated": now,
            "messages": messages, "tools": tools, "events": events,
            "ratings": {str(index): value for index, value in ratings.items()},
            "turns": turns, "meta": meta})


def load(conversation_id: str) -> dict | None:
    """The record, or None when it is missing or unreadable; rating keys are ints."""
    try:
        data = _read(_path(conversation_id))
    except ValueError:
        return None
    if data is None:
        return None
    ratings = {}
    for key, value in (data.get("ratings") or {}).items():
        try:
            ratings[int(key)] = value
        except (TypeError, ValueError):
            pass
    data["ratings"] = ratings
    return data


def _files() -> list[Path]:
    try:
        return sorted(TRAINING_DIR.glob("*.json"))
    except OSError:
        return []


def count() -> int:
    return len(_files())


def clear() -> int:
    """Deletes all records; returns how many."""
    removed = 0
    with _lock:
        for path in _files():
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        for path in TRAINING_DIR.glob("*.tmp") if TRAINING_DIR.is_dir() else ():
            try:
                path.unlink()
            except OSError:
                pass
    return removed


# --- scrubbing -------------------------------------------------------------

class Scrubber:
    """The replacement rules of one export (longest first) and their counts."""

    def __init__(self, private: dict[str, str] | None = None, secrets: Iterable[str] = ()) -> None:
        private = private or {}
        self.replacements = 0
        self.secret_hits = 0
        self.rules: list[tuple[re.Pattern, str, bool]] = []
        for secret in sorted({s for s in secrets if s and len(s) >= MIN_SECRET_CHARS}, key=len, reverse=True):
            self.rules.append((re.compile(re.escape(secret)), "<secret>", True))
        url = (private.get("url") or "").strip().rstrip("/")
        if url:
            bare = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url, flags=re.I)
            for text, replacement in ((url, EXAMPLE_URL), (bare, EXAMPLE_URL.split("://", 1)[1])):
                if text and not any(p.pattern == re.escape(text) for p, _, _ in self.rules):
                    self.rules.append((re.compile(re.escape(text), re.I), replacement, False))
        paths = [(STATE_DIR, "<state>"), (WORKSPACE_DIR, "<workspace>"), (SKILLS_DIR, "<skills>"),
                 (Path.home(), "~")]
        for path, replacement in sorted(paths, key=lambda item: len(str(item[0])), reverse=True):
            text = str(path)
            if len(text) > 1:
                self.rules.append((re.compile(re.escape(text)), replacement, False))
        user = (private.get("user") or "").strip()
        if len(user) >= MIN_USER_CHARS:
            self.rules.append((re.compile(r"(?<!\w)" + re.escape(user) + r"(?!\w)"), "<user>", False))
        self.rules.append((EMAIL_PATTERN, "<email>", False))

    def text(self, value: str) -> str:
        for pattern, replacement, secret in self.rules:
            value, n = pattern.subn(replacement.replace("\\", "\\\\"), value)
            if n:
                self.replacements += n
                if secret:
                    self.secret_hits += n
        return value


def scrub(value, scrubber: Scrubber):
    """`value` with every string (not dict keys) scrubbed, recursively; a new object."""
    if isinstance(value, str):
        return scrubber.text(value)
    if isinstance(value, list):
        return [scrub(item, scrubber) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, scrubber) for key, item in value.items()}
    return value


# --- curation --------------------------------------------------------------

class _Skip(Exception):
    """A record that is not used; the message is the reason."""


def _strip_private(value):
    if isinstance(value, dict):
        return {k: _strip_private(v) for k, v in value.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(value, list):
        return [_strip_private(v) for v in value]
    return value


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _clean(message: dict, include_reasoning: bool) -> dict:
    message = _strip_private(copy.deepcopy(message))
    if not include_reasoning:
        message.pop("reasoning_content", None)
    if message.get("role") == "tool" and isinstance(message.get("content"), str):
        content = message["content"]
        content = content.replace("\n\n" + LESSON_REMINDER, "").replace(LESSON_REMINDER, "")
        message["content"] = content.rstrip()
    return message


def _arguments(call: dict) -> dict | None:
    """The tool call's arguments as a dict, None when they are not a JSON object."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _tool_name(call: dict) -> str:
    return (call.get("function") or {}).get("name") or ""


def _with_code(call: dict, code: str) -> dict:
    """A copy of the call with arguments.code = `code`."""
    call = copy.deepcopy(call)
    args = _arguments(call)
    if args is None or "code" not in args:
        return call
    args["code"] = code
    call["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    return call


def _only(message: dict, call: dict) -> dict:
    return {**message, "tool_calls": [call]}


def _strip_edit_note(content: str, code: str) -> str:
    exact = EDITED_PREFIX + code + "\n```"
    if exact in content:
        content = content.replace(exact, "", 1)
        return re.sub(r"^\n\n", "", content, count=1) if content.startswith("\n\n") else content
    return EDITED_PATTERN.sub("", content, count=1)


def _status(event: dict | None) -> str:
    """"bad" (rejected or failed), "good" (ran ok), "abandoned" or ""."""
    if not event:
        return ""
    if event.get("decision") == "abandoned":
        return "abandoned"
    if event.get("decision") == "rejected" or event.get("ok") is False:
        return "bad"
    return "good" if event.get("ok") is True else ""


def _python(code: str, tool: str) -> bool:
    if tool != "execute_python":  # IPython magics and shell lines are fine in a notebook
        code = "\n".join("pass" if line.lstrip().startswith(("%", "!")) else line for line in code.splitlines())
    try:
        ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return True


@dataclass
class _Curated:
    messages: list[dict]
    ratings: dict[int, int]
    preference: list[dict] = field(default_factory=list)
    kto: list[dict] = field(default_factory=list)


def _curate(data: dict, *, recovery: bool, include_reasoning: bool) -> _Curated:
    """One record -> its curated messages and the pairs; raises _Skip."""
    raw = data.get("messages")
    if data.get("version") != RECORD_VERSION or not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise _Skip("unreadable")
    events = data.get("events") if isinstance(data.get("events"), dict) else {}
    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    users = [i for i, m in enumerate(raw) if m.get("role") == "user"]
    if not users:
        raise _Skip("empty")

    def outcome(k: int) -> str:
        return turns[k].get("outcome", "") if k < len(turns) and isinstance(turns[k], dict) else ""

    done = [k for k in range(len(users)) if outcome(k) == "done"]
    if not done:
        raise _Skip("no_done_turn")

    base = [_clean(m, include_reasoning) for m in raw]
    bounds = users + [len(raw)]

    def calls_of(k: int) -> list[dict]:
        found = []
        for i in range(bounds[k], bounds[k + 1]):
            if base[i].get("role") == "assistant":
                for call in base[i].get("tool_calls") or []:
                    event = events.get(call.get("id"))
                    found.append({"i": i, "call": call, "id": call.get("id"), "tool": _tool_name(call),
                                  "event": event if isinstance(event, dict) else None})
        return found

    # Turns up to the last done one, cut earlier at a turn with an unresolved or abandoned call.
    limit = done[-1] + 1
    for k in range(limit):
        calls = calls_of(k)
        statuses = [_status(c["event"]) for c in calls]
        problem = "abandoned" in statuses
        if not recovery and not problem:
            for n, status in enumerate(statuses):
                if status == "bad" and not any(s == "good" and calls[m]["tool"] == calls[n]["tool"]
                                               for m, s in enumerate(statuses) if m > n):
                    problem = True
        if problem:
            limit = k
            break
    kept = [k for k in done if k < limit]
    if not kept:
        raise _Skip("unresolved_failure" if limit < done[-1] + 1 else "no_done_turn")
    last = kept[-1]
    end = bounds[last + 1]
    ratings = {}
    for key, value in (data.get("ratings") if isinstance(data.get("ratings"), dict) else {}).items():
        try:
            if int(key) < end and value in (1, -1):
                ratings[int(key)] = value
        except (TypeError, ValueError):
            pass

    # Edited code as gold.
    clean = copy.deepcopy(base[:end])
    for k in range(last + 1):
        for c in calls_of(k):
            event = c["event"]
            if not (event and event.get("decision") == "edited" and event.get("ok") is True
                    and event.get("executed_code")):
                continue
            args = _arguments(c["call"])
            if args is None or "code" not in args:
                continue
            for call in clean[c["i"]]["tool_calls"]:
                if call.get("id") == c["id"]:
                    call.update(_with_code(call, event["executed_code"]))
            for i in range(c["i"] + 1, bounds[k + 1]):
                m = clean[i]
                if m.get("role") == "tool" and m.get("tool_call_id") == c["id"] and isinstance(m.get("content"), str):
                    m["content"] = _strip_edit_note(m["content"], event["executed_code"])

    # Pairs, from the uncut messages of the kept turns.
    tools = data.get("tools") if isinstance(data.get("tools"), list) else []
    result = _Curated([], ratings)
    removed: set[str] = set()
    for k in range(last + 1):
        calls = calls_of(k)
        for n, c in enumerate(calls):
            event, status = c["event"], _status(c["event"])
            src = base[c["i"]]
            # chosen: the cleaned (edited) version of this call
            mine = next((x for x in clean[c["i"]]["tool_calls"] if x.get("id") == c["id"]), c["call"])
            if event and event.get("decision") == "edited" and status == "good" and event.get("executed_code"):
                if _arguments(c["call"]) is not None and "code" in (_arguments(c["call"]) or {}):
                    result.preference.append({"source": "edited", "prompt": clean[:c["i"]],
                                              "chosen": [_only(clean[c["i"]], mine)],
                                              "rejected": [_only(src, c["call"])]})
            if status == "bad":
                later = next((d for d in calls[n + 1:] if _status(d["event"]) == "good" and d["tool"] == c["tool"]), None)
                if later:
                    good = next((x for x in clean[later["i"]]["tool_calls"] if x.get("id") == later["id"]), later["call"])
                    result.preference.append({"source": "fixed", "prompt": clean[:c["i"]],
                                              "chosen": [_only(clean[later["i"]], good)],
                                              "rejected": [_only(clean[c["i"]], mine)]})
                    removed.add(c["id"])
            if status in ("good", "bad"):
                result.kto.append({"source": "run", "prompt": clean[:c["i"]],
                                   "completion": [_only(clean[c["i"]], mine)], "label": status == "good"})
    for i, value in sorted(ratings.items()):
        message = clean[i] if i < len(clean) else None
        if message and message.get("role") == "assistant" and _text(message.get("content")).strip():
            result.kto.append({"source": "rating", "prompt": clean[:i], "completion": [message], "label": value > 0})
    for item in result.preference + result.kto:
        item["tools"] = tools

    # Failed and rejected attempts that a later call fixed leave the conversation.
    if recovery:
        removed = set()
    final = []
    for i, message in enumerate(clean):
        message = copy.deepcopy(message)
        if message.get("role") == "tool" and message.get("tool_call_id") in removed:
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            message["tool_calls"] = [c for c in message["tool_calls"] if c.get("id") not in removed]
            if not message["tool_calls"]:
                del message["tool_calls"]
                if not _text(message.get("content")).strip():
                    continue
        final.append(message)

    # Format check.
    for message in final:
        for call in message.get("tool_calls") or []:
            args = _arguments(call)
            if args is None:
                raise _Skip("format")
            if _tool_name(call) in CODE_TOOLS:
                code = args.get("code")
                if not isinstance(code, str) or not _python(code, _tool_name(call)):
                    raise _Skip("format")
    result.messages = final
    return result


def _fingerprint(messages: list[dict]) -> str:
    first = next((_text(m.get("content")) for m in messages if m.get("role") == "user"), "")
    codes = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            if _tool_name(call) in CODE_TOOLS:
                codes.append((_arguments(call) or {}).get("code", ""))
    return hashlib.sha256("\x00".join([first, *codes]).encode("utf-8")).hexdigest()


def split_of(conversation_id: str, split: tuple[float, float, float]) -> str:
    """The split of a conversation, stable for its id."""
    total = sum(split) or 1.0
    u = int(hashlib.sha256(conversation_id.encode("utf-8")).hexdigest(), 16) / 16 ** 64
    if u < split[0] / total:
        return "train"
    return "validation" if u < (split[0] + split[1]) / total else "test"


# --- export ----------------------------------------------------------------

@dataclass
class ExportSummary:
    conversations: int = 0
    used: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    sft: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SPLITS, 0))
    preference: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SPLITS, 0))
    kto: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SPLITS, 0))
    replacements: int = 0
    secret_hits: int = 0
    files: list[Path] = field(default_factory=list)

    def text(self) -> str:
        parts = [f"{self.used} of {self.conversations} conversations used"]
        if self.skipped:
            parts.append("skipped: " + ", ".join(f"{n} {why}" for why, n in sorted(self.skipped.items())))
        for name in ("sft", "preference", "kto"):
            counts = getattr(self, name)
            parts.append(f"{name}: {sum(counts.values())}")
        parts.append(f"{self.replacements} items scrubbed")
        if self.secret_hits:
            parts.append(f"WARNING: {self.secret_hits} secret values found and replaced")
        return "; ".join(parts) + "."


def _dump(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def export(out_dir: Path, *, private: dict[str, str] | None = None, secrets: Iterable[str] = (),
           recovery: bool = False, include_reasoning: bool = False,
           split: tuple[float, float, float] = (0.8, 0.1, 0.1)) -> ExportSummary:
    """Curates all records and writes the datasets into `out_dir`."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    scrubber = Scrubber(private, secrets)
    summary = ExportSummary()
    sft, preference, kto, metas = [], [], [], []
    seen: set[str] = set()
    for path in _files():
        summary.conversations += 1
        data = _read(path) or {}
        cid = data.get("id") if isinstance(data.get("id"), str) else path.stem
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        reason = None
        try:
            curated = _curate(data, recovery=recovery, include_reasoning=include_reasoning)
            key = _fingerprint(curated.messages)
            if key in seen:
                raise _Skip("duplicate")
            seen.add(key)
        except _Skip as skip:
            reason = str(skip)
        except Exception:  # one bad record never stops the export
            reason = "error"
        if reason:
            summary.skipped[reason] = summary.skipped.get(reason, 0) + 1
            metas.append({"id": cid, "used": False, "skipped": reason, "meta": scrub(meta, scrubber)})
            continue
        summary.used += 1
        which = split_of(cid, split)
        tools = scrub(data.get("tools") or [], scrubber)
        smeta = scrub(meta, scrubber)
        metas.append({"id": cid, "used": True, "skipped": None, "meta": smeta})
        sft.append({"id": cid, "split": which, "messages": scrub(curated.messages, scrubber),
                    "tools": tools, "meta": smeta})
        summary.sft[which] += 1
        for item in curated.preference:
            preference.append({"id": cid, "split": which, **scrub(item, scrubber)})
            summary.preference[which] += 1
        for item in curated.kto:
            kto.append({"id": cid, "split": which, **scrub(item, scrubber)})
            summary.kto[which] += 1
    summary.replacements = scrubber.replacements
    summary.secret_hits = scrubber.secret_hits
    for name, rows in (("sft", sft), ("preference", preference), ("kto", kto), ("meta", metas)):
        target = out_dir / f"{name}.jsonl"
        _dump(target, rows)
        summary.files.append(target)
    readme = out_dir / "README.md"
    readme.write_text(_readme(summary, recovery, include_reasoning, split), encoding="utf-8")
    summary.files.append(readme)
    return summary


def _readme(summary: ExportSummary, recovery: bool, include_reasoning: bool, split: tuple) -> str:
    def row(name: str, counts: dict) -> str:
        return f"| `{name}.jsonl` | {sum(counts.values())} | " + " | ".join(str(counts[s]) for s in SPLITS) + " |"

    skipped = "".join(f"- {why}: {n}\n" for why, n in sorted(summary.skipped.items())) or "- none\n"
    return f"""# ChemAssistent teaching data

Exported {date.today().isoformat()} from {summary.conversations} recorded conversations of the built-in
agent; {summary.used} were used.

| file | rows | train | validation | test |
|---|---|---|---|---|
{row("sft", summary.sft)}
{row("preference", summary.preference)}
{row("kto", summary.kto)}

Split by conversation (never within one), from a hash of its id: {split[0]:.0%} / {split[1]:.0%} / {split[2]:.0%}.

Skipped conversations:
{skipped}
## Files

- `sft.jsonl`: one conversation per line, `messages` (system, user, assistant with `tool_calls`,
  `tool`) plus `tools` (OpenAI tool schemas) and `meta`. For TRL `SFTTrainer` (conversational format,
  `assistant_only_loss=True` needs a chat template with generation markers).
- `preference.jsonl`: `prompt`, `chosen`, `rejected` (lists of messages), TRL `DPOTrainer`
  conversational format. `source` is `edited` (the user changed the code before running it: chosen =
  the code that ran) or `fixed` (a failed or rejected call that a later call of the same turn fixed).
- `kto.jsonl`: `prompt`, `completion`, `label` (bool), TRL `KTOTrainer` unpaired format. `source` is
  `run` (a code call: label = it ran successfully) or `rating` (the user's good/bad answer rating).
- `meta.jsonl`: one line per recorded conversation: used or the reason it was skipped, and its metadata.

## Curation

- Only turns up to the last one that ended normally; a turn with an abandoned call, or (default) with a
  rejected or failed call that no later call fixed, ends the conversation before it.
- Failed and rejected calls that a later call of the same tool fixed are removed from the conversation
  (recovery kept in the data: {"yes" if recovery else "no"}).
- Code the user edited before it ran replaces the model's version in the conversation.
- Reasoning content kept: {"yes" if include_reasoning else "no"}. Reminders the app added to tool answers
  are removed.
- Every code call must parse as Python and every tool call needs JSON object arguments.
- Exact duplicates (same first message and code) are dropped.

## Privacy

Removed or replaced: the ELN URL, the login name, secrets (tokens, passwords), e-mail addresses and local
paths (state and workspace folders, home directory). {summary.replacements} replacements were made
({summary.secret_hits} of them secrets).
The ELN data itself (sample and reaction names, structures, values, files) is NOT removed: check the data
before you share it.
"""
