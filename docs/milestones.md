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
**Status:** planned
**Goal:** users pick the version of their Chemotion ELN (or it is read from the server), and
the app installs the matching chemotionapi release by itself. ELN X.Y.Z maps to the release
line `vXYZ`; of that line the highest release `vXYZ.a.b` is used (ELN 3.1.2 → `v312.1.1`,
ELN 4.0.1 → `v401.1.1` with today's tags). Only developer mode keeps the free choice of
tags and branches.
**Work packages:**
- WP4.1 Version mapping (`api_versions.py`, GTK-free, tests): `eln_versions(tags)` = the ELN versions that have releases (from `vXYZ`), `latest_for(eln_version, tags)` = the highest `vXYZ.a.b`, `eln_version_of(tag)`; config key for the chosen ELN version (`config.py`)
- WP4.2 Detection: read the ELN version from the server's public `/about` page (shared with `issues.eln_version()`, parsed into X.Y.Z) when the URL is set; in a thread, never on the main loop
- WP4.3 Settings, Chemotion tab (`settings.py`): users see one "Chemotion version" combo (ELN versions with releases, "Detect from server" filled automatically once the URL is set), the chosen release shown as information; on change setup_workspace.sh installs the latest release of that line. Developer mode keeps today's tag/branch list
- WP4.4 Installers (`install.sh`, `api_versions.py choose/default`, `setup_workspace.sh`): no version question any more; if a chemotionapi is already selected/installed it is kept, otherwise the latest release is installed. `CHEMOTION_API_TAG` still overrides. Windows: the bundle's fixed `CHEMOTION_REF` (`windows/build.py`) is documented as is
**Acceptance criteria:**
- With a URL set, the settings show the server's ELN version preselected; picking an ELN version installs the highest `vXYZ.a.b` of its line.
- Non-developers cannot pick an individual tag or a branch; developer mode can, as before.
- Mapping is tested: ELN 3.1.2 → highest `v312.*`, 4.0.1 → highest `v401.*`; an ELN version without a release line gives a clear message (no silent fallback).
- A reinstall (`install.sh`, also the curl one-liner) keeps an installed chemotionapi and asks nothing; a fresh install gets the newest release; `CHEMOTION_API_TAG` still wins.
- An unreachable server or `/about` without a version leaves the choice to the user; nothing blocks the settings window.
- `pytest tests/` passes; CLAUDE.md describes the new flow.
**Issues:** –
**Open owner decisions:**
- Reinstall with a selection: keep exactly the installed release, or update to the newest release of the same ELN line (e.g. `v312.0.1` → `v312.1.1`)? 
  - keep installed version
- ELN version without its own release line (e.g. a future 3.1.3): show an error, or offer the nearest lower line? 
  - use nearest lower one
- Should the app (not only the settings) warn when the detected server version does not match the installed release line? 
  - yes
- Windows bundle: stays on its built-in release, or should it get per-version bundles later? 
  - get per-version later

## M5 – Explorer: chemotionapi/ELN version mismatch message
**Status:** planned
**Goal:** when the installed chemotionapi does not fit the server (e.g. `v401.*` against ELN
3.1.2), the Explorer says so, offers to fix it, and forgets the message once it is fixed.
**Work packages:**
- WP5.1 The mismatch message disappears after the version was changed: the Explorer's source logs in again with the newly installed chemotion_api (today it keeps showing the old message) (`explorer.py`, `explorer_view.py`)
- WP5.2 A "Change to <matching version>" button on the message: installs the release that fits the server's ELN version (with M4's mapping) and refreshes the Explorer (`explorer_view.py`, `settings.py`/`api_versions.py`)
**Acceptance criteria:**
- chemotionapi `v401.*` against ELN 3.1.2: the Explorer shows the mismatch message with a "Change to v312.…" button.
- After the change (button or settings) the Explorer loads the collections without restarting the app.
- `pytest tests/` passes; the reload is covered by a GTK-free test.
**Issues:** –
**Open owner decisions:**
- Depends on M4 (version mapping): do M4 first, or a simple button "Open settings" until then?
  - Do M4 first. Then i need to check if M5 is fixed already!
