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
**Status:** planned (draft – owner to confirm)
**Goal:** extract teaching data from saved conversations for fine-tuning a small model (#21).
**Work packages:**
- WP1.1 Export saved conversations (`history.py`) as training examples (to be designed)
**Acceptance criteria:**
- to be defined with the owner
**Issues:** #21
**Open owner decisions:**
- Scope, output format and privacy rules (conversations contain ELN data).

## M2 – Docking layout
**Status:** in progress
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
