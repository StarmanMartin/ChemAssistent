"""Runs a set of tasks with one or more models, each task a set number of
times, and records per run: success (with the reasons), wall time, model
time, input/output/cached/reasoning tokens, cost, tool calls, every executed
snippet, the answer and the whole transcript.

    .venv/bin/python evals/run_eval.py --model alias.complex-gpt --runs 3
    .venv/bin/python evals/run_eval.py --model a --model b --runs 5 --only create-sample

The built-in agent runs headless (chemassistent.headless, the same agent,
tools and system prompt as the app, without a window). Code runs
automatically (--approve), so the tasks need a Chemotion they may write to:
by default the local test instance (chemotionapi's tests/config.json);
--saved-chemotion uses the app's saved Chemotion instead. Everything a run
creates in the task collection is deleted afterwards (--keep: not).

Output in --out (default evals/results/<time>_<models>/): results.json (all
of it, rewritten after every run), runs.csv (one row per run) and summary.md
(per model and task), which is printed at the end too. The task format is in
evals/README.md.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime
import hashlib
import importlib.util
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_package() -> None:
    """`chemassistent` = this source tree (as tests/conftest.py does), with
    the source tree's workspace (skills, CHEMOTION_API.md)."""
    os.environ.setdefault("CHEMASSISTENT_WORKSPACE", str(ROOT / "workspace"))
    spec = importlib.util.spec_from_file_location("chemassistent", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules["chemassistent"] = package
    spec.loader.exec_module(package)


if "chemassistent" not in sys.modules:
    _load_package()

from chemassistent import agent as agent_module  # noqa: E402
from chemassistent import headless, lessons, workspace  # noqa: E402
from chemassistent.config import KI_TOOLBOX_URL, Config, ai_token, load_config, read_raw_config  # noqa: E402

DEFAULT_TASKS = Path(__file__).resolve().with_name("tasks.json")
DEFAULT_COLLECTION = "/AIExamples/ChemAssistent evals"
DEFAULT_TIMEOUT_S = 900
# The login of the local test instance (as tests/conftest.py looks for it).
TEST_CONFIG_PATHS = [
    Path(os.environ["CHEMOTION_TEST_CONFIG"]) if os.environ.get("CHEMOTION_TEST_CONFIG") else None,
    Path.home() / "Deveolop" / "KIT" / "chemotionapi" / "tests" / "config.json",
    ROOT.parent / "chemotionapi" / "tests" / "config.json",
]
# Element kinds a run may create in the task collection: collection getter
# and the REST path that deletes one (chemotion_api has no delete()).
ELEMENT_KINDS = {"sample": ("get_samples", "samples"), "reaction": ("get_reactions", "reactions"),
                 "research_plan": ("get_research_plans", "research_plans"),
                 "wellplate": ("get_wellplates", "wellplates")}
APPROVE_MODES = ("all", "readonly", "none")
NO_CACHE = {"cache": {"no-cache": True}}
PLACEHOLDER = re.compile(r"\{(run|collection|setup\.[A-Za-z_]\w*)\}")
# Defines the variables setup/check/cleanup code may use (see README.md).
PRELUDE = """\
import json as _json
_vars = _json.loads({variables!r})
run, collection_path, setup, answer = _vars["run"], _vars["collection"], _vars["setup"], _vars["answer"]
_parts = [part for part in collection_path.split("/") if part]
for _level in range(1, len(_parts) + 1):  # get_or_create_collection() creates the last level only
    collection = instance.get_root_collection().get_or_create_collection("/" + "/".join(_parts[:_level]))


def delete_element(kind, element_id):
    import requests
    response = requests.delete(f"{{instance.host_url}}/api/v1/{{kind}}/{{element_id}}",
                               headers={{"Authorization": f"Bearer {{instance.token}}"}}, timeout=60)
    response.raise_for_status()

"""
SNAPSHOT_CODE = """\
result = {{kind: [element.id for element in getattr(collection, getter)(per_page=100)]
          for kind, (getter, _) in {kinds!r}.items()}}
"""
DELETE_CODE = """\
problems = []
for kind, ids in {new!r}.items():
    for element_id in ids:
        try:
            delete_element({kinds!r}[kind][1], element_id)
        except Exception as exc:
            problems.append(f"{{kind}} {{element_id}}: {{exc}}")
result = problems
"""


# --- tasks -------------------------------------------------------------------
def load_tasks(path: Path, only: list[str] | None = None) -> tuple[dict, list[dict]]:
    """The task file's settings and its tasks (only those named in `only`)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{path}: no tasks")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id") or not task.get("prompt"):
            raise ValueError(f"{path}: every task needs an id and a prompt: {task!r}")
        if task["id"] in seen:
            raise ValueError(f"{path}: the id {task['id']!r} is used twice")
        seen.add(task["id"])
        prompts = task["prompt"] if isinstance(task["prompt"], list) else [task["prompt"]]
        if not all(isinstance(text, str) and text for text in prompts):
            raise ValueError(f"{path}: {task['id']}: prompt must be a text or a list of texts")
        unknown = set(task.get("expect") or {}) - {"answer_contains", "answer_regex", "answer_not_contains",
                                                   "check"}
        if unknown:
            raise ValueError(f"{path}: {task['id']}: unknown expect keys {sorted(unknown)}")
    if only:
        missing = set(only) - seen
        if missing:
            raise ValueError(f"unknown task ids: {', '.join(sorted(missing))}")
        tasks = [task for task in tasks if task["id"] in only]
    settings = {key: value for key, value in data.items() if key != "tasks"}
    return settings, tasks


def fill(text: str, variables: dict) -> str:
    """{run}, {collection} and {setup.<key>} in a prompt."""
    def value(match: re.Match) -> str:
        name = match.group(1)
        if name.startswith("setup."):
            setup = variables.get("setup")
            key = name[len("setup."):]
            if not isinstance(setup, dict) or key not in setup:
                raise ValueError(f"the setup code's result has no key {key!r}")
            return str(setup[key])
        return str(variables[name])
    return PLACEHOLDER.sub(value, text)


def normalized(text: str) -> str:
    """For the answer checks: NFKC (subscript digits → digits), case-folded,
    without thousands separators and spaces between formula parts."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"(?<=\w)[   ](?=\d)", "", text)


def answer_checks(expect: dict, answer: str) -> list[dict]:
    checks = []
    text = normalized(answer)
    for needle in expect.get("answer_contains") or []:
        checks.append({"check": f"answer contains {needle!r}", "ok": normalized(needle) in text})
    for needle in expect.get("answer_not_contains") or []:
        checks.append({"check": f"answer does not contain {needle!r}", "ok": normalized(needle) not in text})
    patterns = expect.get("answer_regex") or []
    for pattern in [patterns] if isinstance(patterns, str) else patterns:
        checks.append({"check": f"answer matches /{pattern}/",
                       "ok": re.search(pattern, unicodedata.normalize("NFKC", answer or ""), re.I) is not None})
    return checks


# --- Chemotion side (setup, checks, cleanup) ----------------------------------
class Chemotion:
    """Runs setup/check/cleanup code with chemotion_runner.py (logged-in
    `instance`, as the agent's snippets run)."""

    def __init__(self, config: Config, timeout_s: int = 300) -> None:
        self.config = config
        self.timeout_s = timeout_s

    def run(self, code: str, variables: dict) -> dict:
        """The runner's outcome: {ok, result, stdout, error}."""
        request = {"url": self.config.url, "user": self.config.user, "password": self.config.password,
                   "verify_tls": self.config.verify_tls,
                   "code": PRELUDE.format(variables=json.dumps(variables, ensure_ascii=False)) + code}
        process = subprocess.Popen([sys.executable, str(headless.RUNNER)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   start_new_session=os.name != "nt")
        try:
            stdout, stderr = process.communicate(json.dumps(request), timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            headless._kill_tree(process)
            return {"ok": False, "error": f"stopped after {self.timeout_s} s"}
        except BaseException:
            headless._kill_tree(process)
            raise
        try:
            outcome = json.loads(stdout)
        except ValueError:
            return {"ok": False, "error": (stderr or stdout or "").strip()[-3000:]
                    or f"runner exit code {process.returncode}"}
        return outcome if isinstance(outcome, dict) else {"ok": False, "error": "the runner sent no object"}

    def snapshot(self, variables: dict) -> dict:
        outcome = self.run(SNAPSHOT_CODE.format(kinds=ELEMENT_KINDS), variables)
        if not outcome.get("ok"):
            raise RuntimeError(f"cannot list the task collection: {outcome.get('error')}")
        return outcome["result"]

    def delete_new(self, before: dict, variables: dict) -> dict:
        """Deletes what appeared in the task collection since `before`."""
        after = self.snapshot(variables)
        new = {kind: sorted(set(after.get(kind, [])) - set(before.get(kind, []))) for kind in ELEMENT_KINDS}
        new = {kind: ids for kind, ids in new.items() if ids}
        if not new:
            return {"deleted": {}, "problems": []}
        outcome = self.run(DELETE_CODE.format(new=new, kinds=ELEMENT_KINDS), variables)
        problems = outcome.get("result") if outcome.get("ok") else [str(outcome.get("error"))]
        return {"deleted": new, "problems": problems or []}


# --- one run -------------------------------------------------------------------
@dataclasses.dataclass
class Options:
    approve: str = "all"
    timeout_s: int = DEFAULT_TIMEOUT_S
    collection: str = DEFAULT_COLLECTION
    keep: bool = False
    transcripts: bool = True
    # Request fields for every chat completion; by default LiteLLM's switch
    # that bypasses its response cache (the KI-Toolbox answers an identical
    # request from it in 0.1 s, so repeated runs would repeat the first one).
    extra_body: dict = dataclasses.field(default_factory=lambda: dict(NO_CACHE))


def _approver(mode: str):
    if mode == "all":
        return lambda purpose, code: True
    if mode == "readonly":
        return lambda purpose, code: agent_module.looks_read_only(code)[0]
    return None


def _tool_stats(messages: list[dict]) -> tuple[dict, int]:
    """Tool calls by name and the number of tool rounds (assistant messages
    with tool calls)."""
    calls: dict[str, int] = {}
    rounds = 0
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            rounds += 1
            for call in message["tool_calls"]:
                name = call["function"]["name"]
                calls[name] = calls.get(name, 0) + 1
    return calls, rounds


def run_task(config: Config, task: dict, model: str, index: int, chemotion: Chemotion | None,
             options: Options) -> dict:
    """One run of `task`; never raises for problems of the run itself."""
    run_tag = f"ev{secrets.token_hex(3)}"
    variables = {"run": run_tag, "collection": options.collection, "setup": {}, "answer": ""}
    record: dict = {"task": task["id"], "model": model, "run": index, "run_tag": run_tag,
                    "started": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                    "success": False, "reasons": [], "error": "", "timed_out": False}
    expect = task.get("expect") or {}
    before = None
    executions: list[dict] = []
    conversation = None
    started = time.monotonic()
    try:
        needs_chemotion = bool(task.get("setup") or task.get("cleanup") or expect.get("check"))
        if chemotion is not None:
            before = chemotion.snapshot(variables)
        elif needs_chemotion:
            raise RuntimeError("the task needs Chemotion (setup/check/cleanup), but none is configured")
        if task.get("setup"):
            outcome = chemotion.run(task["setup"], variables)
            record["setup"] = outcome
            if not outcome.get("ok"):
                raise RuntimeError(f"setup failed: {outcome.get('error')}")
            variables["setup"] = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
        prompts = [fill(text, variables) for text in
                   (task["prompt"] if isinstance(task["prompt"], list) else [task["prompt"]])]
        record["prompts"] = prompts

        assistant = headless.ChemAssistent(dataclasses.replace(config, ai_model=model),
                                           approve=_approver(options.approve))
        conversation = assistant.conversations.new_conversation()
        conversation._agent.extra_body = dict(options.extra_body)
        execute = conversation._agent.execute

        def recorded(purpose, code, cancelled, target, *rest):
            begun = time.monotonic()
            text = execute(purpose, code, cancelled, target, *rest)
            executions.append({"target": target, "purpose": purpose, "code": code,
                               "seconds": round(time.monotonic() - begun, 3),
                               "ran": not text.startswith("Not run"),
                               "ok": text.startswith(agent_module.RAN_TEXT), "answer": text})
            return text
        conversation._agent.execute = recorded

        timeout_s = int(task.get("timeout_s") or options.timeout_s)
        timer = threading.Timer(timeout_s, lambda: (record.update(timed_out=True), conversation._agent.cancel()))
        timer.daemon = True
        timer.start()
        answers = []
        try:
            for text in prompts:
                answers.append(conversation.prompt(text))
        finally:
            timer.cancel()
        record["answers"] = answers
        record["answer"] = answers[-1] if answers else ""
    except Exception as exc:
        record["error"] = (f"timed out after {int(task.get('timeout_s') or options.timeout_s)} s"
                           if record["timed_out"] else f"{type(exc).__name__}: {exc}")
    record["seconds"] = round(time.monotonic() - started, 3)

    usage = conversation._agent.usage if conversation is not None else []
    record["tokens"] = agent_module.usage_total(usage)
    record["requests"] = usage
    messages = conversation._agent.messages[1:] if conversation is not None else []
    record["tool_calls"], record["tool_rounds"] = _tool_stats(messages)
    record["executions"] = executions
    record["executions_failed"] = sum(1 for e in executions if e["ran"] and not e["ok"])
    record["executions_rejected"] = sum(1 for e in executions if not e["ran"])

    checks: list[dict] = []
    if not record["error"]:
        variables["answer"] = record.get("answer", "")
        checks = answer_checks(expect, record["answer"])
        if expect.get("check"):
            outcome = chemotion.run(expect["check"], variables)
            verdict = outcome.get("result")
            ok = bool(outcome.get("ok")) and verdict is True
            detail = (outcome.get("error") if not outcome.get("ok")
                      else "" if verdict is True else str(verdict) if isinstance(verdict, str) else repr(verdict))
            checks.append({"check": "check code", "ok": ok, "detail": detail,
                           "stdout": outcome.get("stdout", "")})
    record["checks"] = checks
    record["reasons"] = ([record["error"]] if record["error"] else
                         [c["check"] + (f": {c['detail']}" if c.get("detail") else "") for c in checks if not c["ok"]])
    record["success"] = not record["error"] and all(c["ok"] for c in checks)

    if chemotion is not None:
        cleanup: dict = {}
        if task.get("cleanup"):
            outcome = chemotion.run(task["cleanup"], variables)
            cleanup["code"] = {"ok": outcome.get("ok"), "error": outcome.get("error", "")}
        if before is not None and not options.keep:
            try:
                cleanup.update(chemotion.delete_new(before, variables))
            except Exception as exc:
                cleanup["problems"] = [str(exc)]
        record["cleanup"] = cleanup
    if options.transcripts:
        record["transcript"] = messages
    return record


# --- summary -----------------------------------------------------------------
def _stats(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "median": None, "min": None, "max": None}
    return {"mean": round(statistics.fmean(values), 3), "median": round(statistics.median(values), 3),
            "min": round(min(values), 3), "max": round(max(values), 3)}


def summarize(runs: list[dict]) -> dict:
    """Per model, per model and task: success rate, pass@n (one run passed),
    pass^n (all passed), times and tokens."""
    def group(records: list[dict]) -> dict:
        passed = sum(1 for r in records if r["success"])
        tokens = lambda key: [r["tokens"][key] for r in records if isinstance(r["tokens"].get(key), (int, float))]
        costs = tokens("cost")
        by_task: dict[str, list[bool]] = {}
        for r in records:
            by_task.setdefault(r["task"], []).append(r["success"])
        return {"runs": len(records), "passed": passed,
                "success_rate": round(passed / len(records), 3) if records else None,
                "pass_at_n": round(sum(any(v) for v in by_task.values()) / len(by_task), 3) if by_task else None,
                "pass_all_n": round(sum(all(v) for v in by_task.values()) / len(by_task), 3) if by_task else None,
                "errors": sum(1 for r in records if r["error"]),
                "timeouts": sum(1 for r in records if r["timed_out"]),
                "seconds": _stats([r["seconds"] for r in records]),
                "model_seconds": _stats([r["tokens"]["seconds"] for r in records]),
                "input_tokens": _stats(tokens("input_tokens")), "output_tokens": _stats(tokens("output_tokens")),
                "input_tokens_total": sum(tokens("input_tokens")), "output_tokens_total": sum(tokens("output_tokens")),
                "token_counts_missing": sum(1 for r in records if r["tokens"].get("input_tokens") is None),
                "requests": _stats([r["tokens"]["requests"] for r in records]),
                "executions": _stats([len(r["executions"]) for r in records]),
                "cost_total": round(sum(costs), 6) if costs else None}
    models: dict[str, dict] = {}
    for model in dict.fromkeys(r["model"] for r in runs):
        records = [r for r in runs if r["model"] == model]
        tasks = {task: group([r for r in records if r["task"] == task])
                 for task in dict.fromkeys(r["task"] for r in records)}
        models[model] = {"overall": group(records), "tasks": tasks}
    return models


def _number(value, digits: int = 1) -> str:
    if value is None:
        return "–"
    return f"{value:,.{digits}f}" if digits else f"{round(value):,}"


def summary_markdown(meta: dict, summary: dict) -> str:
    lines = [f"# Eval {meta['started']}", "",
             f"Models: {', '.join(meta['models'])} · runs per task: {meta['runs']} · approve: {meta['approve']}"
             f" · Chemotion: {meta['chemotion_url'] or '–'} · chemotionapi {meta['chemotionapi'] or '–'}"
             f" · commit {meta['commit'] or '–'}", "",
             "| Model | Task | Passed | Rate | Time mean (s) | Time median (s) | Model time mean (s) | "
             "Input tokens mean | Output tokens mean | Requests mean | Snippets mean | Cost |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for model, data in summary.items():
        rows = list(data["tasks"].items()) + [("**all**", data["overall"])]
        for task, g in rows:
            lines.append(
                f"| {model} | {task} | {g['passed']}/{g['runs']} | {g['success_rate']:.0%} | "
                f"{_number(g['seconds']['mean'])} | {_number(g['seconds']['median'])} | "
                f"{_number(g['model_seconds']['mean'])} | {_number(g['input_tokens']['mean'], 0)} | "
                f"{_number(g['output_tokens']['mean'], 0)} | {_number(g['requests']['mean'])} | "
                f"{_number(g['executions']['mean'])} | {_number(g['cost_total'], 4)} |")
    lines += ["", "pass@n / pass^n (share of tasks with at least one / with every run passed):", ""]
    for model, data in summary.items():
        g = data["overall"]
        lines.append(f"- {model}: pass@{meta['runs']} {g['pass_at_n']:.0%}, pass^{meta['runs']} "
                     f"{g['pass_all_n']:.0%}, errors {g['errors']}, timeouts {g['timeouts']}"
                     + (f", runs without token counts {g['token_counts_missing']}" if g["token_counts_missing"]
                        else ""))
    return "\n".join(lines) + "\n"


CSV_FIELDS = ["model", "task", "run", "run_tag", "success", "reasons", "error", "timed_out", "seconds",
              "model_seconds", "requests", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens",
              "cost", "tool_rounds", "tool_calls", "executions", "executions_failed", "executions_rejected",
              "started"]


def write_outputs(out: Path, meta: dict, runs: list[dict]) -> dict:
    summary = summarize(runs) if runs else {}
    out.mkdir(parents=True, exist_ok=True)
    temp = out / "results.json.tmp"
    temp.write_text(json.dumps({"meta": meta, "summary": summary, "runs": runs}, ensure_ascii=False, indent=1,
                               default=str), encoding="utf-8")
    temp.replace(out / "results.json")
    with (out / "runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, CSV_FIELDS)
        writer.writeheader()
        for r in runs:
            t = r["tokens"]
            writer.writerow({**{key: r.get(key) for key in CSV_FIELDS}, "reasons": "; ".join(r["reasons"]),
                             "model_seconds": t["seconds"], "requests": t["requests"],
                             **{key: t.get(key) for key in ("input_tokens", "output_tokens", "cached_tokens",
                                                            "reasoning_tokens", "cost")},
                             "tool_calls": json.dumps(r["tool_calls"]), "executions": len(r["executions"])})
    if summary:
        (out / "summary.md").write_text(summary_markdown(meta, summary), encoding="utf-8")
    return summary


# --- configuration -------------------------------------------------------------
def test_instance_login() -> dict | None:
    for path in TEST_CONFIG_PATHS:
        if path is None or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        url = str(data.get("ELN_URL") or "").strip().rstrip("/").replace("//0.0.0.0", "//localhost")
        if url and data.get("ELN_USER"):
            return {"url": url, "user": str(data["ELN_USER"]), "password": str(data.get("ELN_PASS") or "")}
    return None


def build_config(args) -> Config:
    """The AI from the saved settings (or --ai-url / KI_TOOLBOX_API_KEY), the
    Chemotion of the test instance unless --saved-chemotion."""
    raw = read_raw_config()
    try:
        base = load_config(keyring_required=False)
    except ValueError:
        base = None  # no complete Chemotion settings: only the AI part is needed
    if base is None or base.agent not in ("api", "ollama"):
        base = Config(url="", user="", password="", ai_url=raw.get("ai_url") or KI_TOOLBOX_URL,
                      ai_token=ai_token(raw), ai_model=raw.get("ai_model") or "")
    if args.ai_url:
        base = dataclasses.replace(base, ai_url=args.ai_url)
    if os.environ.get("KI_TOOLBOX_API_KEY") and not base.ai_token:
        base = dataclasses.replace(base, ai_token=os.environ["KI_TOOLBOX_API_KEY"])
    if args.no_chemotion:
        return dataclasses.replace(base, url="", user="", password="")
    if args.saved_chemotion:
        if not base.url:
            raise SystemExit("--saved-chemotion: the app has no complete Chemotion settings")
        return base
    login = test_instance_login()
    if login is None:
        raise SystemExit("No Chemotion test instance login found (set CHEMOTION_TEST_CONFIG to chemotionapi's "
                         "tests/config.json), or use --saved-chemotion, or --no-chemotion for tasks without it.")
    return dataclasses.replace(base, url=login["url"], user=login["user"], password=login["password"],
                               verify_tls=True)


def _git_commit() -> str:
    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return commit + ("+dirty" if commit and dirty else "")
    except OSError:
        return ""


def _chemotionapi_label() -> str:
    for name in ("chemotionapi.tag", "chemotionapi.branch"):
        try:
            return (ROOT / "workspace" / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", required=True,
                        help="model id at the AI endpoint; repeat (or comma-separate) to compare models")
    parser.add_argument("--runs", type=int, default=1, help="runs per task and model (default 1)")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS, help="task file (default evals/tasks.json)")
    parser.add_argument("--only", action="append", help="task id(s) to run; repeat or comma-separate")
    parser.add_argument("--out", type=Path, help="output folder (default evals/results/<time>_<models>)")
    parser.add_argument("--approve", choices=APPROVE_MODES, default="all",
                        help="which snippets run: all (default), readonly (agent.looks_read_only), none")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help=f"seconds per run (a task's timeout_s wins; default {DEFAULT_TIMEOUT_S})")
    parser.add_argument("--ai-url", help="AI endpoint instead of the saved one (token: saved or KI_TOOLBOX_API_KEY)")
    chemotion = parser.add_mutually_exclusive_group()
    chemotion.add_argument("--saved-chemotion", action="store_true",
                           help="use the app's saved Chemotion instead of the test instance (tasks write data!)")
    chemotion.add_argument("--no-chemotion", action="store_true",
                           help="no Chemotion: only tasks without setup/check/cleanup can pass")
    parser.add_argument("--keep", action="store_true", help="do not delete what the runs created")
    parser.add_argument("--with-lessons", action="store_true",
                        help="give the agent the app's saved lessons (default: none, for reproducible runs)")
    parser.add_argument("--server-cache", action="store_true",
                        help="let the AI gateway answer from its response cache (default: bypass it with "
                             "LiteLLM's cache.no-cache; use this for servers that refuse that field)")
    parser.add_argument("--no-transcripts", action="store_true", help="leave the transcripts out of results.json")
    args = parser.parse_args(argv)
    args.model = [m.strip() for value in args.model for m in value.split(",") if m.strip()]
    args.only = [t.strip() for value in args.only or [] for t in value.split(",") if t.strip()] or None
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    settings, tasks = load_tasks(args.tasks, args.only)
    config = build_config(args)
    chemotion = None if args.no_chemotion else Chemotion(config)
    options = Options(approve=args.approve, timeout_s=args.timeout,
                      collection=str(settings.get("collection") or DEFAULT_COLLECTION), keep=args.keep,
                      transcripts=not args.no_transcripts, extra_body={} if args.server_cache else dict(NO_CACHE))
    if not args.with_lessons:
        lessons.LESSONS_FILE = Path(os.devnull)  # read as empty; headless never writes it

    now = datetime.datetime.now().astimezone()
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", "+".join(args.model))[:60]
    out = args.out or ROOT / "evals" / "results" / f"{now:%Y%m%d-%H%M%S}_{slug}"
    task_bytes = args.tasks.read_bytes()
    meta = {"started": now.isoformat(timespec="seconds"), "models": args.model, "runs": args.runs,
            "tasks_file": str(args.tasks), "tasks_sha256": hashlib.sha256(task_bytes).hexdigest()[:16],
            "tasks": [t["id"] for t in tasks], "approve": args.approve, "timeout_s": args.timeout,
            "ai_url": config.ai_url, "chemotion_url": config.url if chemotion else "",
            "collection": options.collection, "chemotionapi": _chemotionapi_label(), "commit": _git_commit(),
            "lessons": args.with_lessons, "extra_body": options.extra_body, "python": sys.version.split()[0],
            "system_prompt": workspace.system_prompt(headless=True)}
    total = len(tasks) * len(args.model) * args.runs
    print(f"{total} runs: {len(tasks)} tasks × {len(args.model)} models × {args.runs} → {out}", flush=True)
    runs: list[dict] = []
    done = 0
    try:
        for model in args.model:
            for index in range(1, args.runs + 1):
                for task in tasks:
                    done += 1
                    print(f"[{done}/{total}] {model} · {task['id']} · run {index} … ", end="", flush=True)
                    record = run_task(config, task, model, index, chemotion, options)
                    runs.append(record)
                    t = record["tokens"]
                    print(f"{'PASS' if record['success'] else 'FAIL'} {record['seconds']:.1f} s, "
                          f"in {_number(t['input_tokens'], 0)} / out {_number(t['output_tokens'], 0)} tokens"
                          + ("" if record["success"] else f" – {'; '.join(record['reasons'])[:200]}"), flush=True)
                    for problem in (record.get("cleanup") or {}).get("problems") or []:
                        print(f"    cleanup: {problem}", flush=True)
                    meta["finished"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
                    write_outputs(out, meta, runs)
    except KeyboardInterrupt:
        print("\nInterrupted; the finished runs are saved.", flush=True)
        meta["interrupted"] = True
    summary = write_outputs(out, meta, runs)
    if summary:
        print()
        print(summary_markdown(meta, summary))
    print(f"Results: {out}")
    return 0 if runs and all(r["success"] for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
