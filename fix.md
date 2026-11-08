# Fix plan: issue #23 — attached PDFs (text and figures)

Issue: "Attached PDFs: the agent sees neither their text nor their figures
(drawn structures are guessed)". Branch:
`23-attached-pdfs-the-agent-sees-neither-their-text-nor-their-figures-drawn-structures-are-guessed`
(rebase on `main` first: it is 4 commits behind, `evals/` is missing).

## Current state (checked against the code)

- `agent.read_file()` (agent.py:534) answers for any file with a NUL byte
  "is a binary file … execute_python code can open it". `grep()` (agent.py:562)
  skips such files silently. `describe_attachment()` (agent.py:703) only says
  "binary file, n kB".
- Every message is a plain string: `send()` appends
  `{"role": "user", "content": text}` (agent.py:861), tool results are
  `{"role": "tool", "content": result}` (`_run_tool()`). Several readers
  assume `content` is a `str`: `app.replay_items()` (app.py:1370, a list
  content is silently skipped), `CliAgent` transcript (cli_agent.py:190),
  `headless.py:171`.
- `list_models()` (agent.py:455) already reads `info.meta` but returns only
  ids. Used by `app.load_models()` (app.py:2651) and `settings._load_models()`.
- The MCP bridge answers only `{"type": "text"}` (mcp_bridge.py:125); its
  `read_file` is agent.py's, so a fix there reaches the CLI agents for free.
- Libraries: no PDF library in the venv. Pillow is importable in the Linux
  venv only through `--system-site-packages` (apt), not a declared dependency
  and not in the Windows bundle. poppler (`pdftotext`/`pdftoppm`) exists on
  this machine, not on Windows.

## Decisions

1. **`pypdfium2`** (wheel bundles pdfium; BSD-3/Apache-2.0) for text,
   page count and rendering, on both platforms. No poppler dependency.
   Imported lazily, so agent.py stays importable without it (headless
   package, tests): missing → a clear "PDF support is not installed" answer.
2. **PNG encoding without Pillow**: render with pdfium to a raw RGB bitmap and
   write the PNG with stdlib `zlib`/`struct` (~30 lines). Attached PNG/JPEG
   are sent as they are when within the limits; downscaling them uses Pillow
   only if importable, else they are refused above the byte limit with a hint.
   SVG is text: `read_file` already shows it; no rasterising (would need GTK
   or cairosvg in a GTK-free module).
3. **Images go into one `user` message after the round's tool results**, not
   after each tool message: the OpenAI API requires all `tool` answers of an
   assistant message to follow it directly. `role: tool` stays text-only.
4. **Images do not pile up**: every request resends all messages. Only the
   newest `MAX_INLINE_IMAGES` (e.g. 4) image parts stay inline; older ones are
   replaced by a text part "[page 3 of x.pdf was shown here; call view_page
   again to see it]". History never stores base64 (same placeholder).

## Milestone 1 — PDF/image backend and packaging

Goal: a GTK-free module that every later milestone uses; installable on
Linux, Windows bundle and headless package.

- **WP 1.1 `documents.py`** (new, stdlib + lazy `pypdfium2`):
  - `is_pdf(path)` (magic `%PDF-`), `page_count(path)`.
  - `pdf_text(path) -> list[str]` (one string per page), cached per
    (path, size, mtime_ns) in memory; `MAX_PDF_PAGES` guard.
  - `render_page(path, page, region="full") -> Image`: scale so the longer
    side is `IMAGE_MAX_SIDE` (1600 px), shrunk below `MAX_IMAGE_BYTES`.
    Instead of a `zoom` (a 3× page would be far larger than models read) a
    `region` ("top", "bottom-left", … `REGIONS`) renders part of the page at
    the full size: small print gets bigger, the image does not.
  - `load_image(path)` for PNG/JPEG (size, MIME type, optional Pillow scaling).
  - `png_encode(width, height, rgb)` (stdlib).
  - `DocumentError` for broken/encrypted PDFs, page out of range, missing lib.
- **WP 1.2 Packaging**
  - `requirements.txt`: `pypdfium2>=4.30,<6`.
  - `install.sh`: `documents.py` into `APP_FILES`.
  - `setup.py`: `documents` into `PACKAGE_MODULES`; `pyproject.toml`: an
    optional extra `pdf = ["pypdfium2…"]` (headless works without it). Use the
    `if __package__:` relative-import switch.
  - Windows: `windows/build.py` venv installs it; `windows/chem-assistent.spec`
    `collect_all("pypdfium2")` / `pypdfium2_raw` (the DLL lives there);
    `windows/licenses.py`: a `COMPONENTS` entry for `pdfium.dll` with its
    license text (pdfium's BSD-3 + third-party notices; add to
    `windows/licenses/` if the wheel does not ship them), so the build does
    not fail on an unknown DLL.
- **WP 1.3 Tests** `tests/test_documents.py`: generate a tiny 2-page PDF in
  the test (pdfium can create documents, or a hand-written minimal PDF with a
  text object), check page count, text per page, render size, PNG signature,
  page out of range, encrypted/broken file, the missing-library message
  (monkeypatch the import).

Done when: tests pass on Linux, the Windows workflow builds (licenses check
and `--self-test` green; add a render of a bundled tiny PDF to `--self-test`).

## Milestone 2 — PDF text for every agent (read_file / grep / attachments)

Goal: the model reads the paper's text without writing a snippet. Reaches the
CLI agents through the bridge automatically.

- **WP 2.1 `read_file`**: for a PDF, the text of all pages joined with
  `--- page N of M ---` marker lines, then the existing line numbering and
  `offset`/`limit` paging (so long papers page like long text files). Other
  binaries unchanged. The tool description mentions PDFs.
- **WP 2.2 `grep`**: searches the same page text for PDFs; match lines as
  `x.pdf:p3:<line>: …` (page plus line of the joined text, so the model can
  `read_file` at that offset).
- **WP 2.3 `describe_attachment`**: "PDF, 12 pages, 1.4 MB"; images
  "PNG image, 1200×800". `replay_items()` parses paths by `" ("`, unaffected.
- **WP 2.4 Tests** in `tests/test_agent.py` (read_file paging over page
  markers, grep hit with page, describe_attachment) and one bridge test that
  `read_file` over MCP returns the text.

Done when: attaching a paper and asking for its reactions needs no
`execute_python` for extraction (check with the fake AI and once by hand).

## Milestone 3 — `view_page` for the built-in agent (vision)

Goal: the model looks at schemes and figures before deriving structures.

- **WP 3.1 Vision capability**
  - `list_models()` keeps its return type; a new `model_infos()` (or
    `list_models(…, details=True)`) returns `{id: {"vision": bool | None}}`
    from `info.meta.capabilities.vision` (None when the server says nothing,
    e.g. Ollama/OpenAI). `app.load_models()` caches it with the ids.
  - `Agent.vision: bool | None`; `build_tools(…, vision=…)` offers
    `view_page` unless `vision is False`. `set_model()` updates it (model
    menu); the evals/headless default is "unknown → offer".
  - Fallback: a request error that looks like "image input not supported"
    (match a few known texts, LiteLLM/Ollama/Open WebUI) → drop the image
    parts from the messages, set `vision = False`, rebuild the tools, retry
    once, and emit a `warning` note "This model cannot see images; page
    images are off for this conversation".
- **WP 3.2 The tool**: `VIEW_PAGE_TOOL` = `view_page(path, page?, region?)`;
  `page` required for PDFs, ignored for images; confined to `Agent.roots`
  via `_resolve()` like `read_file`. The tool answer is text ("Page 3 of
  x.pdf, 1600×2070 px; the image follows."); the rendered image is queued in
  `self._pending_images`.
- **WP 3.3 Message handling** (agent.py `_run()`)
  - After the round's tool calls: if images are queued, append one
    `{"role": "user", "content": [{"type": "text", "text": "Images requested
    with view_page:"}, {"type": "image_url", "image_url": {"url":
    "data:image/png;base64,…"}}…]}`; each image part carries a private
    `"_source": {"path", "page", "region"}`, stripped from the request body.
  - `_prune_images()` before each request (Decision 4).
  - `_close_open_calls()`/`retry()` keep working with list content.
- **WP 3.4 Everything that reads `messages`**
  - `history.save` (app's `_save_history`): image parts → text placeholder
    with path/page; `restore()` leaves them as placeholders (the model can
    call `view_page` again).
  - `app.replay_items()`: a list-content user message from `view_page` →
    a note "Looked at page 3 of x.pdf" (no image in the replay).
  - `cli_agent.py:190` transcript and `headless.py:171`: take only the text
    parts (a small `agent.message_text(content)` helper used by all three).
  - Token/size: evals record usage as before; check one request with 4 pages
    stays under the KI-Toolbox body limit.
- **WP 3.5 Chat UI**: a new agent event `image` (path, page, png bytes) →
  `ChatPanel` note with a small thumbnail (`GdkPixbuf.PixbufLoader`, click
  opens the file); `tool_summary("view_page")` = "Looked at page 3 of x.pdf".
- **WP 3.6 Prompt**: in `workspace.SYSTEM_PROMPT` (desktop and headless): for
  structures from a paper, look at the schemes/figures with `view_page`
  (find the page with `grep` for "Scheme"/"Figure"), compare with formulas
  and m/z, and say explicitly when a structure was derived from text alone.
  Without vision: the last sentence only.
- **WP 3.7 Tests** (`tests/fake_ai.py` gets a check hook for request bodies):
  view_page with two tool calls in one round (one image message after both
  tool answers), pruning to `MAX_INLINE_IMAGES`, `_source` not sent, history
  without base64, restore, `vision=False` hides the tool, the "not supported"
  fallback and retry, path outside the roots refused.

Done when: with `alias.complex-local` (vision) the agent opens the scheme
pages of a real paper on its own; with `alias.medium-local` (no vision) the
tool is absent and the answer says it worked from text.

## Milestone 4 — CLI agents (Claude Code, Codex)

- **WP 4.1** `mcp_bridge.py`: `view_page` in `tools/list` (same schema,
  read-only hint), answered with
  `{"content": [{"type": "text", …}, {"type": "image", "data": b64,
  "mimeType": "image/png"}]}`; `_call()` must pass a list of blocks instead
  of one text (keep `isError`).
- **WP 4.2** Check by hand that Claude Code 2.1 and codex-cli show the image
  to the model (ask for a description of a page with a known figure). If
  Codex drops images, offer the tool to Claude only (session file flag).
- **WP 4.3** Tests in `tests/test_mcp.py`/bridge tests: image block shape,
  roots confinement.

## Milestone 5 — Docs, evals, release

- **WP 5.1** `CLAUDE.md` (documents.py, view_page, image messages and
  pruning, packaging entries), README feature list.
- **WP 5.2** Eval task in `evals/tasks.json`: a short generated PDF with a
  drawn scheme (RDKit draws it to the PDF in the setup; no copyrighted paper
  in the repo) and "create the reaction"; check the product's SMILES. Run for
  `alias.complex-local` and `alias.medium-local`, compare with/without
  `view_page`.
- **WP 5.3** Manual window test (attach a PDF on prompt page and chat,
  thumbnail notes, continue from history), Windows bundle smoke test via the
  workflow's release `windows-23-…`.
- **WP 5.4** PR "Closes #23".

## Order and size

| Milestone | Depends on | Size |
|---|---|---|
| 1 Backend + packaging | — | M (Windows licenses are the unknown) |
| 2 PDF text | 1 | S |
| 3 view_page built-in | 1 (2 for the prompt's grep hint) | L |
| 4 CLI agents | 3 (shares the tool schema) | S–M |
| 5 Docs, evals, PR | 2–4 | S |

Milestone 2 alone already fixes the "text is a workaround" half of the issue
and could be merged first.

## Risks / open questions

- Whether the KI-Toolbox (Open WebUI → LiteLLM) really forwards `image_url`
  parts for every `vision: true` alias, and its request size limit; test with
  one curl request per alias before WP 3.3.
- pdfium's license files in the Windows bundle (WP 1.2) could block the build.
- Token cost: a 1600 px page is roughly 1–2k input tokens per request while
  inline; `MAX_INLINE_IMAGES` and `IMAGE_MAX_SIDE` are the knobs.

## Out of scope (follow-up issue)

Proper structure recognition (OCSR: DECIMER, MolScribe) in JupyterLab as a
`skills/` entry: extract structure images of a page, return checked SMILES
through the exchange folder.

## Status (2026-11-08)

All work packages are done; one commit per WP on this branch.

Live checks:

- Image pass-through on the KI-Toolbox (tool call, tool answer, then a user
  message with the image): `alias.complex-gpt`, `alias.complex-claude`,
  `google.gemini-3.5-flash` and `alias.simple-local` (Mistral) read a number
  from a rendered page; `alias.medium-local` (DeepSeek) refuses with
  "deepseek-v4-flash is not a multimodal model" (the text the fallback
  matches, `IMAGE_REFUSALS`). The GLM models (`alias.complex-local`) answered
  every request, also without images, with 504 that evening.
- CLI agents (WP 4.2): Claude Code 2.1.319 and codex-cli 0.167.0 both read a
  number that exists only as a picture in a PDF through the bridge's image
  block.
- Eval `paper-structure-name` (2 runs each, `--no-chemotion`): GPT 2/2 and
  Claude 2/2, both call view_page and name 3b correctly (Claude says it took
  the structure from the drawing); Mistral 0/2 (once it did not look and said
  it cannot tell, once it looked and still answered wrong: the small model's
  limit); DeepSeek 504 (the gateway). `paper-structure` (creates 3b in
  Chemotion) could not run: the test instance runs ELN 3.1.2, the installed
  chemotionapi (branch `chemotion/main`) only supports ELN v4.
- Window (WP 5.3; a real AssistantWindow with `alias.complex-gpt`): PDF
  attached on the prompt page, notes "Read …" and "Looked at page 1 of
  regio-3b.pdf" with the thumbnail, correct answer; the saved conversation has
  no base64 (3 kB) and continues from the history without image messages in
  the chat.

Open:

- The Windows bundle (pdfium.dll in the license check, the PDF part of
  `--self-test`) is only checked by the GitHub workflow after a push.
- `paper-structure` against a Chemotion whose ELN version matches the
  installed chemotionapi.
