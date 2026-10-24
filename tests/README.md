# Tests

`pytest` tests for everything without GTK (`agent.py`, `chemotion_runner.py`,
`config.py`, `history.py`, `jobs.py` and the Chemotion end-to-end path).
The AI model is simulated: `fake_ai.py` is a scripted OpenAI-compatible
endpoint that answers `Agent`'s real HTTP requests (streamed SSE chunks,
tool calls split across deltas, HTTP errors), so the agent loop runs
unchanged.

Run:

```
.venv/bin/pip install pytest          # once; not in requirements.txt
.venv/bin/python -m pytest tests/
```

## Without a Chemotion test instance

Most tests need nothing but a local Chemotion test instance; it is auto-skipped
when it is not reachable. To run the Chemotion tests (`test_runner.py`,
`test_end_to_end.py`) against the local docker test instance from chemotionapi's
`test_config` (port 3579), just start it. Its login is read from
`~/Deveolop/KIT/chemotionapi/tests/config.json` (override with the environment
variable `CHEMOTION_TEST_CONFIG`); credentials are never committed or printed.

What the Chemotion tests write goes into the collection
`AIExamples/ChemAssistent tests` on the test instance, and every sample is
deleted again afterwards (plain `DELETE /api/v1/samples/<id>`). They are the
only tests that talk to a server; deselect them with `pytest -m "not chemotion"`.

## What tests what

- `test_agent.py`: the agent loop against the scripted AI — streamed answers
  and delta merging, tool round trips (`read_file`/`grep`/`list_files` and the
  execute callback), attachments and the workspace-only path guard,
  `reasoning_content` handling, `set_model`, `retry` after errors, `cancel`
  mid-stream (every open tool call gets an answer), `MAX_ROUNDS`, `restore()`,
  `list_models()`, and the auto-run guard `looks_read_only()`.
- `test_end_to_end.py`: whole conversations. The scripted AI asks for
  `execute_python`, the "user" confirms like the result panel would, and the
  snippet really runs in `chemotion_runner.py` against the test instance:
  creating, reading and describing samples, rejection with a reason, edited
  code, failing snippets and their recovery, the exchange folder, a
  background-job report that continues a conversation, and a
  save-history/restore round trip.
- `test_runner.py`: `to_jsonable()`/`describe_object()`, login and connection
  errors, tracebacks that start in the snippet, and one run as a subprocess
  (stdin/stdout JSON), as `app.py` starts it.
- `test_config.py`, `test_history.py`, `test_jobs.py`: settings validation
  (private file mode, env overrides, legacy model ids), the conversation files
  (0600, prune, exchange folders go with them) and the job registry
  (restart-safe, report texts, `jupyter_jobs` info, delivered jobs forgotten).

Nothing here touches the user's real config, history, or ELN: `workspace/`,
state and history directories are throwaway `tmp_path` folders, and the
Chemotion URL, user and password come from the test instance's config.
