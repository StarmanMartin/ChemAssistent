# Plan: the "Explorer" tab

A file-explorer view of the Chemotion ELN in the result panel. Collections are
the folders, and elements (samples, reactions, …) are the files. A dropdown at
the top hides or shows element types with checkboxes. Elements load lazily,
only for collections the user opens: 10 per type, with a "More…" row at the
end while there are more. Data comes from chemotion_api.

## Current state (checked against the code)

- **ResultsPanel** (`app.py:1140`) is a `Gtk.Notebook` with `no_show_all`. It
  is hidden until the first tab exists, and until then the hint `self.empty`
  is shown. The first tab is "Links" (`links_page`, made with the first link,
  never dropped). `_prune()` drops the oldest finished code tabs above
  `MAX_RESULT_TABS`.
- **chemotion_api** (workspace: the v4 branch `chemotion/main`; the Windows
  bundle and older installs: `v312.0.1`):
  - `Instance.get_root_collection()` loads the collection tree: the user's
    own collections (`children`, nested by `nest_by_ancestry`) plus
    `load_sync_collection()` for the shared ones. On v4 one request covers
    both halves.
  - `ElementSet(session, element_type, collection_id)` with
    `load_elements(per_page=10)` loads one page of one type. It reads the
    total from `X-Total` (`len(set)`) and the page count from
    `X-Total-Pages` (`max_page`); `set.page = n` loads page n.
    - The listing is partial: `id`, `name`, `short_label` and `json_data` do
      not trigger a full load.
    - `element.url(collection_id)` gives the ELN link.
  - The types are `root._element_manager.all_classes`. This includes generic
    (LabIMotion) elements and, on v4, cell lines, device descriptions and SBMM.
  - **v312 vs v4:**
    - v312's `ElementSet.__init__` has `collection_is_sync`, and shared
      collections need `True` there. v4 has no such parameter.
    - v312 lacks some `get_<type>s()` methods (cell lines, devices, SBMM).
- **chemotion_api in the app process:** the settings' "Test connection"
  (`settings.py:1033`) logs in with chemotion_api in a thread. The app itself
  runs Chemotion code only through `chemotion_runner.py` (agent code that
  needs confirmation).
- **Config:** `config.py` with defaults/validation per key; `save_raw_config()`
  is the only writer.

## Decisions

1. **Placement:** "Explorer" is the fixed first tab of the result panel;
   "Links" moves to second place. The notebook is visible from the start, and
   the empty hint becomes a dim line under the "Results" heading. Nothing is
   fetched until the tab is shown for the first time.
2. **One tree** (`Gtk.TreeView` + `Gtk.TreeStore`), like an IDE's file
   explorer:
   - Own collections come first, then a folder "Shared with me".
   - Each unopened collection has a placeholder child "Loading…";
     `test-expand-row` starts loading it.
   - Inside a collection: its subcollections, then the elements.
3. **Elements are grouped by type** in the order of the dropdown. Each type
   loads 10 per request. While `loaded < total`, the type ends with a row
   "More samples… (10 of 42)"; activating it loads the next 10. A type with 0
   elements shows no rows.
4. **Element rows** show a type icon and `short_label  name`. The tooltip
   gives the type, id and `updated_at` (from `json_data`); for samples it also
   gives the sum formula if the listing has it.
5. **Type dropdown:**
   - A `Gtk.MenuButton` with a popover (not a `Gtk.Menu`: Wayland, see
     CLAUDE.md) holding one `Gtk.CheckButton` per type the server knows.
   - Defaults: sample, reaction, research_plan, wellplate and screen are on.
     The choice is saved as `explorer_types`.
   - Hiding uses a `Gtk.TreeModelFilter`, so loaded rows stay in the store.
     Turning a type on loads its first page for the collections that are
     already open.
6. **chemotion_api runs in the app process, in one worker thread**, not in
   `chemotion_runner.py`: the code is read-only and started by the user, so it
   needs no card.
   - One `Instance`, logged in with fresh `load_config()` credentials on the
     first request.
   - It logs in again after a 401 and when the Chemotion URL or user changes.
   - Results reach the main loop through `GLib.idle_add`.
   - Every request carries the tree's generation number; answers from before
     a refresh are dropped.
7. **Version adapter:** the data layer builds `ElementSet`s itself and passes
   `collection_is_sync=True` for shared collections only where the signature
   has the parameter (`inspect.signature`). It does not use the
   `get_<type>s()` methods.
8. **Not in this version:** automatic refresh after agent code changed
   something (refresh is manual), search, drag and drop, editing.
   **Limitation:** after another chemotion_api version is installed in the
   settings, the Explorer keeps the imported one until the app restarts.

## Milestone 1 — data layer `explorer.py` (no GTK)

Goal: everything the tab needs, testable without GTK and without a server.

- **WP 1.1 `ExplorerSource`**:
  - Created with a `load_config` callable.
  - `collections()` returns
    `[{id, label, path, shared, children}]` (own first, then the shared ones
    under a pseudo-folder).
  - `types()` returns `[{name, label}]` from `all_classes`, standard types
    first in a fixed order, then generic ones by label.
  - `page(collection, type_name, page, per_page=10)` returns
    `{items: [{id, type, short_label, name, url, tooltip}], total, more}`.
  - Lazy login and re-login (401, config change). Errors become
    `ExplorerError` with a readable message: not configured, login failed,
    server unreachable.
- **WP 1.2 `ExplorerWorker`**:
  - A thread with a queue; `request(kind, args, callback)` with callbacks
    given by the caller.
  - Generation number; `reset()` drops queued requests and makes running
    answers stale.
  - `close()` stops the thread.
- **WP 1.3 Tests** `tests/test_explorer.py`:
  - With a fake `Instance` and `ElementSet`: paging (10, `more`, last page),
    totals, the `collection_is_sync` adapter for both signatures, type order,
    dropped stale answers, re-login after 401 and after a config change,
    error messages.
  - A `chemotion` test against the test instance: 12 samples in
    `AIExamples/ChemAssistent tests/<run>`; page 1 has 10 items with `more`
    and page 2 has the other 2. Delete the samples afterwards (REST DELETE).

Done when: the tests pass, and `pyflakes` is clean.

## Milestone 2 — the Explorer tab

Goal: browse collections and elements in the window.

- **WP 2.1 `ExplorerPanel`** (new `explorer_view.py`, GTK):
  - A toolbar with the types button (not yet functional) and Refresh.
  - A `TreeView` with icon and label columns. The store holds kind
    (collection / element / more / loading / error), ids, type and url.
  - The collection tree is loaded on the first `map`.
- **WP 2.2 Lazy loading:**
  - Expanding a collection loads its first page per enabled type.
  - "More…" rows load the next page and are replaced by its rows.
  - "Loading…" rows during requests; error rows with "try again" on
    activation.
  - Not configured: a message with "Open settings…".
- **WP 2.3 `ResultsPanel` integration:**
  - Explorer is a fixed first tab (no close button, never pruned), and Links
    is inserted after it.
  - The notebook is visible from the start and the empty hint is reworked.
    A `show_all()` of the window must not show hidden parts.
  - The worker is closed when the window closes.

Done when: in the window against the test instance, collections open lazily,
a collection with more than 10 samples shows "More…", and the code tabs and
Links still behave as before.

## Milestone 3 — types dropdown

- **WP 3.1 Popover:**
  - One checkbox per type of `types()`.
  - A `Gtk.TreeModelFilter` on the element and "More…" rows of hidden types.
  - Turning a type on loads its first page in the collections that are open.
- **WP 3.2 Config:**
  - `explorer_types` in `config.py`: the default list; unknown names are
    kept, because a generic type may exist on another server.
  - It is saved on change via `save_raw_config()` (in a thread, as the
    settings do).
  - Tests in `tests/test_config.py`.

Done when: hiding and showing types works without new requests for types
already loaded, and the choice survives a restart.

## Milestone 4 — actions

- **WP 4.1 Opening:** double-click or Enter on an element opens it in the
  browser (`open_url()`, `url(collection_id)`). On collections, these keys
  expand and collapse them.
- **WP 4.2 Context menu (popover):**
  - "Open in Chemotion" and "Copy link" (`Gtk.Clipboard`).
  - "Mention in message" inserts
    `sample CHI-12 "Benzaldehyde" (id 345, collection "Project/A")` at the
    cursor of the `ChatPanel` input.
- **WP 4.3 Refresh:** reloads the tree with a new generation number. Open
  folders and the loaded page counts are restored.

Done when: all three actions work in the window, and Refresh keeps the open
folders open.

## Milestone 5 — polish, packaging, docs, verification

- **WP 5.1 Icons:** themed symbolic icons per type. Where the theme has
  nothing suitable, small SVGs next to the others (and in the Windows spec's
  `datas`).
- **WP 5.2 Packaging and docs:**
  - Add `explorer.py` and `explorer_view.py` to install.sh `APP_FILES`.
    Windows finds the modules through imports.
  - CLAUDE.md (architecture, `explorer_types`) and the README (a short
    paragraph and a screenshot).
- **WP 5.3 Live test:**
  - Against the test instance (ELN 3.1.2), with the workspace temporarily on
    `v312.0.1` (`setup_workspace.sh`; back to the v4 branch afterwards).
    Check a shared collection and a generic type if the instance has one.
  - Against a v4 instance if one is available.
  - The Windows workflow builds (self-test).

Done when: the full test suite passes, and the tab works against both
chemotionapi versions (or the missing v4 test is stated).
