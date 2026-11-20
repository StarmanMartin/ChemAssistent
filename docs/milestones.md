# Milestones

The roadmap for ChemAssistent. Milestones are executed one at a time with the
`milestone-development` skill (`.claude/skills/milestone-development/SKILL.md`):
the first milestone with status `planned` is next. Design documents and worker
reports live in the git-ignored `dev/mN/`.

Format of a milestone:

```
## MN – <title>
**Status:** planned | in progress | done (#PR)
**Goal:** what the user gets.
**Work packages:**
- WPN.1 <summary> (modules)
**Acceptance criteria:**
- <checkable statement>
**Issues:** #n
**Open owner decisions:**
- <question>
```

## M1 – Prompt distillation
**Status:** done (#31)
**Goal:** with an opt-in setting, every conversation of the built-in agent records teaching
data for fine-tuning a small model (#21): the trajectory, structured decisions per code card
(run / edited / rejected / auto, outcome) and 👍/👎 ratings of answers; the settings export
it as SFT, preference (DPO) and KTO datasets with basic curation and scrubbing.
**Work packages:**
- WP1.1 Recording store and export (`training.py`, GTK-free, stdlib, tests): per-conversation record (messages, tool schemas, events, ratings, metadata), export to `sft.jsonl`, `preference.jsonl`, `kto.jsonl`, `meta.jsonl`, `README.md`; curation: outcome filter, edited code as gold, failed-then-fixed rewrite (flag), format check (`ast`/JSON), exact dedup, scrubbing (URL, login, paths, secrets asserted absent), train/validation/test split by conversation
- WP1.2 Capture (`agent.py`, `app.py`): structured event per code card linked by tool call id, turn outcomes, metadata, record after every saved turn; 👍/👎 under the assistant's answers while recording is on
- WP1.3 Settings (`config.py`, `settings.py`): `training_data` switch (default off) with what is stored, count, "Export training data…" and "Delete training data"
**Acceptance criteria:**
- Off by default; while off nothing is written to `STATE_DIR/training` and no rating buttons show.
- On (built-in agent only): after each turn the conversation's record holds the messages, the tools, one event per code card with original and executed code, decision, outcome; CLI-agent conversations are not recorded.
- 👍/👎 under each finished answer of the session toggle a rating that is saved in the record.
- Export writes the four JSONL files and a dataset card in a chosen folder; SFT samples use the user's edited code; preference pairs come from edited code and failed/rejected → fixed calls; KTO labels from run outcomes and ratings; splits never cut a conversation.
- The export contains no Chemotion URL, login, home/state paths or secret values (tested), and its summary says how many samples, skipped conversations and replacements there are.
- `pytest tests/` passes; `training.py` has tests for record, curation, formats and scrubbing.
**Issues:** #21
**Open owner decisions:** – (scope: record + export, CLI agents excluded, ratings in M1; 2026-11-17)

## M2 – Docking layout
**Status:** done (#30)
**Goal:** the conversation window becomes four docking containers (left, center, right,
bottom). Every panel can be moved into any container (drag its tab, or its tab menu), each
container can be hidden and shown with header buttons, and the layout is remembered.
Panels: Conversation (chat, default bottom), Results (code cards, outputs, Links; default
right), Explorer (default left) and a new Details panel (default center) for the elements
selected in the Explorer.
**Work packages:**
- WP2.1 Layout model: zones, defaults, validation of a saved layout, sizes (`dock_layout.py`, GTK-free, tests; `config.py` key `dock_layout`)
- WP2.2 Dock widget: four notebooks in nested paneds, detachable tabs between them, tab menu "Move to …", zone toggle buttons, empty/hidden zones collapse (`dock.py`)
- WP2.3 Details panel, first version: name, type, short label, collection and "Open in Chemotion" of the selected elements (`details_view.py`, richer `ExplorerPanel.describe()`)
- WP2.4 Integration: the conversation page uses the dock, Explorer leaves the Results notebook, header buttons, layout saved off the main thread (`app.py`, `docs/screencast.py`)
**Acceptance criteria:**
- A new conversation shows Explorer left, Details center, Results right, Conversation bottom.
- Four header toggle buttons hide/show the containers; the last visible one cannot be hidden; an empty container takes no space.
- A panel tab can be dragged into another container and moved with its tab menu; dropping outside the window does nothing.
- Layout (panel places, order, hidden containers, sizes) survives a new conversation and an app restart; a broken or outdated saved layout falls back to the defaults per panel.
- Selecting elements in the Explorer fills the Details panel and still sets the selection chip of the chat.
- Code cards, auto-run, Links, jobs, continuing a saved conversation and Escape/close behave as before.
- `pytest tests/` passes; new GTK-free code has tests; icons are in Adwaita.
**Issues:** –
**Open owner decisions:** –

## M3 – Open the conversation view without a message
**Status:** done (#32)
**Goal:** the big conversation view (dock with Explorer, Details, Results, Conversation) can
be opened without asking anything first, e.g. to browse the ELN in the Explorer and ask later.
**Work packages:**
- WP3.1 Empty prompt: Enter (or Send) on the initial prompt with no text and no attachments grows the window to the conversation view with an empty chat, focus in the chat input; no request goes to the model (`app.py`: `_submit()`, `_start_conversation(text=None)` without a saved conversation)
- WP3.2 "New conversation" in the top-bar icon's menu opens the conversation view directly, empty, instead of the small prompt (`app.py`: `ChemAssistentApp.new_conversation()`, `_build_menu()`)
**Acceptance criteria:**
- Enter on an empty initial prompt opens the conversation view with an empty chat; the agent has sent nothing and the first typed message starts the conversation as usual (same agent, MCP, JupyterLab exchange, auto-run mode, selection chip).
- Text or attachments in the initial prompt still start the conversation with that message, as before.
- "New conversation" in the tray/top-bar menu closes the open conversation (as before) and shows an empty conversation view, not the prompt.
- A conversation that never got a message is not written to the history and does not appear under "Recent conversations".
- Config problems (`_ready_config()`: Chemotion/AI not set up) still show in the prompt instead of opening the view.
- "Ask Chemotion…", the hotkey, a second app start and continuing a saved conversation behave as before.
- `pytest tests/` passes; the window change is checked from source with a capture.
**Issues:** –
**Open owner decisions:** – (the header's "New conversation" button keeps going back to the small prompt; 2026-11-17) 
  - Still small prompt

## M4 – Choose the Chemotion version, not the chemotionapi release
**Status:** done (#34)
**Goal:** users pick the version of their Chemotion ELN (or it is read from the server), and
the app installs the matching chemotionapi release by itself. ELN X.Y.Z maps to the release
line `vXYZ`; of that line the highest release `vXYZ.a.b` is used (ELN 3.1.2 → `v312.1.1`,
ELN 4.0.1 → `v401.1.1` with today's tags); an ELN version without a line of its own gets the
nearest lower line. Only developer mode keeps the free choice of tags and branches.
**Work packages:**
- WP4.1 Version mapping and server version (`api_versions.py`, GTK-free, tests; `issues.py` uses the shared `/about` parser): ELN version ↔ release line, `release_for()` (highest of the line, else nearest lower line), `server_eln_version()`, `check_installed()` for warnings
- WP4.2 Settings, Chemotion tab (`settings.py`): one "Chemotion version" choice (ELN versions with releases), the server's version detected when the URL is set and preselected, Install installs the newest release of that line; developer mode keeps today's tag/branch list
- WP4.3 App warning (`app.py`): at the start of a conversation, in a thread, the server's ELN version is compared with the installed release; a mismatch gives a warning note in the chat with "Open settings"
- WP4.4 Installers (`api_versions.py` script modes, `install.sh`, `setup_workspace.sh`): no version question; an installed release is kept, otherwise the newest is installed; `CHEMOTION_API_TAG` still wins
**Acceptance criteria:**
- With a URL set, the settings show the server's ELN version preselected; installing installs the highest `vXYZ.a.b` of its line (or of the nearest lower line).
- Non-developers cannot pick an individual tag or a branch; developer mode can, as before.
- Mapping is tested: 3.1.2 → highest `v312.*`, 4.0.1 → highest `v401.*`, a version without a line → nearest lower line, nothing lower → a clear error.
- A reinstall keeps the installed release and asks nothing; a fresh install gets the newest release; `CHEMOTION_API_TAG` still wins.
- A conversation against a server whose version does not fit the installed release shows a warning note naming the release to install; an unreachable server or `/about` without a version shows nothing and never blocks the window.
- `pytest tests/` passes; CLAUDE.md describes the new flow.
**Issues:** –
**Open owner decisions:** – (2026-11-17: reinstall keeps the installed release; no own line → nearest lower line; the app warns on a mismatch; Windows keeps its built-in release, per-version bundles later)

## M5 – Explorer: chemotionapi/ELN version mismatch
**Status:** done (#35)
**Goal:** when the installed chemotionapi does not fit the server, the Explorer says so in
plain words, offers "Change to <release>", and works with the new release at once — without
restarting the app.
**Findings (2026-11-17, reproduced):** the Explorer runs chemotion_api inside the app process;
after another release is installed (settings or setup_workspace.sh) Python keeps the old modules,
so "Try again", Refresh and even a new `ExplorerSource` keep failing until the app restarts (a new
process works at once). Snippets are not affected (each runs in its own `chemotion_runner.py`
process). The message itself depends on the direction: v401 against ELN 3.1.2 gives
chemotion_api's `UnsupportedServerVersion` text, v312 against ELN 4.0.1 only
`400 {"error":"id is invalid"}`.
**Work packages:**
- WP5.1 Explorer helper process: `ExplorerSource`'s chemotion_api calls run in a helper process (stdin/stdout JSON requests like `chemotion_runner.py`, started with the venv Python / the frozen runner exe), restarted when the installed release (`workspace/chemotionapi.tag`/`.branch`) changes or the helper dies; `ExplorerWorker`/`ExplorerPanel` keep their interface (`explorer.py`, new helper module, tests with the Chemotion test instance)
- WP5.2 Clear message: on an Explorer error the server's ELN version is checked (`api_versions.check_installed()`); a mismatch shows the M4 warning text instead of the raw error (`explorer.py`, `explorer_view.py`)
- WP5.3 "Change to <release>" button on that message: installs `release_for()`'s tag (setup_workspace.sh, as the settings do; Linux; Windows: the advice only), then the helper restarts and the Explorer reloads (`explorer_view.py`, shared install helper with `settings.py`)
**Acceptance criteria:**
- chemotionapi `v312.*` against ELN 4.0.1 (and `v401.*` against 3.1.2): the Explorer shows the version message with "Change to v401.1.1" (resp. `v312.*`), not the raw error.
- After the change (button or settings) the Explorer loads the collections without restarting the app.
- The app process no longer imports chemotion_api for the Explorer; Explorer behaviour (paging, types, shared collections, refresh, mention, details) is unchanged.
- Windows bundle: the helper runs from the frozen runner; the `--self-test` covers starting it.
- `pytest tests/` passes; the helper protocol and the restart on a release change are tested.
**Issues:** –
**Open owner decisions:** – (2026-11-17: fix by an Explorer helper process, not a restart button or module reloading)

## M6 – Element details: foundation and samples
**Status:** done (#36)
**Goal:** selecting an element in the Explorer shows a detailed, read-only default view in the
Details panel, starting with samples: their main properties as the ELN web client shows them,
with the structure image. The view is built from a version-independent description, so it works
with both chemotionapi lines (v312 for ELN 3.1, v401 for ELN 4.0).
**Reference:** the web client in `~/Deveolop/KIT/chemotion_ELN` (`app/javascript/src/apps/mydb/elements/details/samples/`, `propertiesTab/`) decides which values are relevant and their order/units; chemotion_api (`chemotion_api/elements/sample.py`, both lines) how to read them.
**Work packages:**
- WP6.1 View model (`element_views.py`, GTK-free, tests): a JSON-able description `{type, title, subtitle, url, sections: [{title, rows: [{label, value, unit}] | table | text | image}], images, links}` plus per-line adapters (v312 / v401, chosen by what the installed chemotion_api offers, like `explorer._uses_sync_ids()`); sample adapter: name, short label, external label, IUPAC/molecule name, sum formula, molecular weight, exact mass, SMILES/InChI(Key), CAS, stereo, amount (mass/volume/mol), purity, density/molarity, melting/boiling point, location, description, solvents, the decoupled/mixture variants where present
- WP6.2 Helper method `details(type, id)` (`explorer_helper.py`, `ExplorerProxy.details()`): loads the element in the helper process, runs the adapter, returns the view model; the structure SVG (`load_image()` / `sample_svg_file`) as text in the model, size-limited
- WP6.3 Details panel (`details_view.py`): renders any view model generically (sections, key/value grids, tables, text, images: SVG via GdkPixbuf/librsvg, scaled to the panel), loading and error states, "Open in Chemotion" / "Mention in message" as now, a refresh button; one element in detail, several selected elements as today's short cards
- WP6.4 Cache per (type, id, updated_at) in the app for the conversation; nothing written to the ELN
**Acceptance criteria:**
- Selecting a sample shows its structure image and the main properties in the web client's grouping and order; empty values are left out; units as in the web client.
- Works against ELN 4.0.1 (v401) and ELN 3.1.2 (v312; checked with a scratch venv and recorded fixtures); a field one line does not have is simply absent.
- Read only: no request that changes data (checked: only GET requests, or chemotion_api calls that do not save).
- Large or missing images, slow servers and errors show a message, never block the window.
- `pytest tests/` passes; adapters tested against the Chemotion test instance and against recorded JSON of both lines.
**Issues:** –
**Open owner decisions:** – (2026-11-17: "Mention in message" inserts the reference only, as today; several selected elements: their short cards, a click opens one in detail, with a way back)

## M7 – Element details: reactions and the other built-in types
**Status:** done (#37)
**Goal:** every built-in element type gets its default view on M6's foundation, with the same
values as the web client's main tab.
**Reference:** `chemotion_ELN/app/javascript/src/apps/mydb/elements/details/{reactions,researchPlans,wellplates,screens,cellLines,deviceDescriptions,vessels,sequenceBasedMacromoleculeSamples}/`; chemotion_api per line (v312 has fewer types: types the installed line or the server does not have are skipped).
**Work packages:**
- WP7.1 Reaction: the scheme image (stored `reaction_svg_file` / `load_image()`; v401 can also render it), materials table per role (starting materials, reactants, solvents, products: label, name, amount, equivalents, yield), conditions, temperature, duration, status, rf value, purification/dangerous products, observation/description as text (`element_views.py` adapter)
- WP7.2 Research plan: its body in order (rich text → readable text with headings/lists, tables as tables, images), read only
- WP7.3 Wellplate (size, wells as a compact grid with the samples they hold), screen (conditions, requirements, wellplates), cell line, device description, SBMM sample (v401 only): main tab values (vessels are no Explorer element type: not in M7)
- WP7.4 Rich-text helper (Quill delta → Pango markup / plain text), shared by descriptions everywhere
**Acceptance criteria:**
- Each built-in type of the test instance shows a view with the main-tab values of the web client; reactions show the scheme image and the materials table.
- Types the installed chemotionapi line does not support show today's short card, not an error.
- `pytest tests/` passes; one adapter test per type (test instance + recorded JSON).
**Issues:** –
**Open owner decisions:** – (2026-11-17: material rows link to their sample's detail view, with a way back to the reaction)

## M8 – Element details: generic elements (LabIMotion)
**Status:** done (#38)
**Goal:** generic elements (and generic segments on built-in elements) are shown from their
class definition, without code per class: layers in order, fields with label, value and unit.
**Reference:** `~/Deveolop/KIT/LabIMotion` (field types and how the web client renders them), chemotion_api `generic_element.py` / `generic_segments.py` (both lines).
**Work packages:**
- WP8.1 Generic adapter (`element_views.py`): layers (`layers` with position/cols, `layers_without_fields`), fields by type — text, textarea, number/system-defined with unit, select (option labels), checkbox, date/datetime, integer, formula field, table (as a table), drag sample/element (label + link), upload (file names), text-formula — hidden/conditional fields and empty layers left out; the workflow/klass version as subtitle
- WP8.2 Generic segments on samples/reactions/…: shown as extra sections in M6/M7 views
- WP8.4 "Mention in message" also on detail views opened from a link (a reaction's material, a research plan's sample, a wellplate's samples, …): the reference is built from the view model (type, id, short label, name) and the collection of the element the link came from (owner request, 2026-11-17)
- WP8.3 Tests with the generic classes of the test instance (created by the test fixtures if none exist, deleted afterwards) and recorded JSON of both lines
**Acceptance criteria:**
- Any generic element of the test instance shows all its filled fields in layer order with units, read only, without class-specific code.
- Unknown field types show their raw value instead of failing.
- `pytest tests/` passes.
**Issues:** –
**Open owner decisions:** –

## M9 – Element details: analyses and datasets
**Status:** in progress
**Goal:** the analyses of an element and their datasets and files are reachable from its view:
see what was measured, preview images and spectra, and open or hand files to the assistant.
**Reference:** `chemotion_ELN/app/javascript/src/apps/mydb/elements/details/analyses/`; chemotion_api `Analyses`, `Dataset`, attachments (`preview_image()`, `load_image()`, downloads; both lines).
**Work packages:**
- WP9.1 Analyses section in every element view (`element_views.py`, helper): analyses in order with kind (CHMO term label), status, description (rich text), and their datasets (name, instrument, metadata fields), each dataset's attachments (name, size, type)
- WP9.2 Previews: the analysis/attachment preview images (size-limited, loaded on expand, cached)
- WP9.3 Files: a dropdown per file — "Download to JupyterLab" (into the conversation's exchange folder `chemotion/<element>/<analysis>/`, then synced to JupyterLab; only with JupyterLab on), "Download to…" (a folder/file the user chooses), "Open" (temp folder under `STATE_DIR`, system viewer); downloads go through the helper; "Mention in message" for an analysis or file inserts its reference (ids), like M6 — the assistant fetches it itself
- WP9.4 Limits: previews up to 5 MB (larger: "too large"), files up to 50 MB at once, larger ones ask first with their size; nothing uploaded to the ELN or changed
**Acceptance criteria:**
- A sample with analyses shows them with their datasets and file names; previews appear on expand; a file opens after one click.
- Files the user opened can be read by the assistant when mentioned (they are inside its roots).
- Works with both chemotionapi lines; read only.
- `pytest tests/` passes; tests create an analysis with a small dataset on the test instance and delete it afterwards.
**Issues:** –
**Open owner decisions:** – (2026-11-17: a dropdown per file: download to JupyterLab / download to a chosen place (+ open); previews 5 MB, files 50 MB)
