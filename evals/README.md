# Evals

`run_eval.py` runs every task of a task file with one or more models, each
task `--runs` times, through the headless built-in agent (same agent, tools
and system prompt as the app, no window). Snippets run automatically, so the
tasks need a Chemotion they may write to: by default the local test instance
(chemotionapi's `tests/config.json`, `CHEMOTION_TEST_CONFIG` overrides the
path); `--saved-chemotion` uses the app's saved Chemotion instead.

```sh
.venv/bin/python `evals/run_eval.py --model alias.complex-gpt --runs 3`
.venv/bin/python evals/run_eval.py --model alias.complex-gpt,alias.complex-local --runs 5 --only create-sample
```

The AI endpoint and token come from the saved settings (`--ai-url`,
`KI_TOOLBOX_API_KEY`). Options: `--tasks FILE`, `--only ID`, `--out DIR`,
`--approve all|readonly|none`, `--timeout S` (per run), `--keep` (no cleanup),
`--with-lessons` (default: the agent gets no saved lessons, so runs are
comparable), `--no-transcripts`, `--no-chemotion`, `--server-cache`.
Every request carries LiteLLM's `"cache": {"no-cache": true}`: the
KI-Toolbox answers an identical request from its response cache (in 0.1 s,
the same text), so repeated runs would otherwise only repeat the first one.
`--server-cache` leaves the field out (for servers that refuse it). The exit code is 0 only if
every run passed.

## Output

`evals/results/<time>_<models>/` (or `--out`), rewritten after every run, so
an interrupted eval keeps its finished runs:

- `results.json`: `meta` (date, models, runs, task file + hash, approve mode,
  AI URL, Chemotion URL, chemotionapi tag, git commit, system prompt),
  `summary` (per model, overall and per task: runs, passed, success rate,
  pass@n = share of tasks with at least one passing run, pass^n = with every
  run passing, errors, timeouts, wall time and model time mean/median/min/max,
  input/output tokens mean and total, requests, snippets, cost) and `runs`
  (per run: success, reasons, error, timed_out, seconds, `tokens` = totals
  of input/output/cached/reasoning tokens, cost, requests and model seconds,
  `requests` = the same per chat completion, tool calls by name, tool rounds,
  every executed snippet with purpose, code, seconds and outcome, the
  answers, every check, the setup result, the cleanup and the transcript).
- `runs.csv`: one row per run with the numbers.
- `summary.md`: the summary table (also printed at the end).

Token counts are what the server reports (`stream_options.include_usage`);
a server that reports none gives `–`.

## Tasks

```json
{
  "collection": "/AIExamples/ChemAssistent evals",
  "tasks": [
    {
      "id": "set-amount",
      "description": "…",
      "setup": "Python; result = {'label': …, 'id': …}",
      "prompt": "Set the amount of '{setup.label}' in {collection} to 250 mg.",
      "expect": {
        "answer_contains": ["…"], "answer_not_contains": ["…"], "answer_regex": "…",
        "check": "Python; result = True, or a text saying what is wrong"
      },
      "cleanup": "Python",
      "timeout_s": 600
    }
  ]
}
```

- `prompt`: a text, or a list of texts sent one after the other in one
  conversation (the checks see the last answer). Placeholders: `{run}` (a
  random tag per run, e.g. `ev3f9a2c`; use it in names so runs do not see each
  other's data), `{collection}` (the task collection's path) and
  `{setup.<key>}` (the setup code's `result`).
- `setup`, `expect.check` and `cleanup` run like the agent's snippets
  (`chemotion_runner.py`, logged-in `instance`) with `run`, `collection_path`,
  `collection` (the task collection, created if missing), `setup` (the setup
  result), `answer` (the last answer) and `delete_element(kind, id)` (REST
  delete, kind e.g. `"samples"`) predefined.
- A run passes if it ended without error or timeout and every check holds.
  Answer checks compare NFKC-normalized (C₈H₁₀N₄O₂ = C8H10N4O2), case-folded
  text; `answer_regex` is a pattern or a list (case-insensitive).
- Cleanup, after the checks: first the task's own `cleanup`, then
  everything that is new in the task collection since the run started
  (samples, reactions, research plans, wellplates) is deleted. Elements the
  agent created elsewhere are not found.
