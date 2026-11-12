"""The Explorer tab (GTK): the Chemotion ELN as a file explorer.

Collections are folders, elements are the files. The collection tree is
loaded when the tab is first shown; a collection's elements only when it is
opened: per element type the first PER_PAGE, then a "More…" row while the
type has more. All requests go through explorer.ExplorerWorker (one thread);
its answers come back on the main loop.

Rows of one collection, in this order: its subcollections, then per type
(in the order of `types`) its elements. Every type of an opened collection
has one "anchor" row: "Loading…" while a page is on its way, "More…" when
there are more, an error row when the page failed; it is removed when the
type is complete. New rows go in before their anchor, so the types stay in
order whatever order the pages arrive in.
"""
from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk, Pango  # type: ignore  # noqa: E402

from explorer import DEFAULT_TYPES, PER_PAGE, ExplorerSource, ExplorerWorker  # noqa: E402

# TreeStore columns.
ICON, TEXT, TOOLTIP, KIND, DATA = range(5)
# KIND values.
COLLECTION = "collection"  # DATA: the node dict of ExplorerSource.collections()
GROUP = "group"  # "Shared with me": a folder without elements of its own
PENDING = "pending"  # the child of an unopened collection (gives it an expander)
ELEMENT = "element"  # DATA: the item dict of ExplorerSource.page()
ANCHOR = "anchor"  # DATA: {"collection", "type", "page", "state"} (loading/more/error)
NOTE = "note"  # "Empty", "No element types chosen"

FOLDER_ICON = "folder-symbolic"
SHARED_ICON = "folder-publicshare-symbolic"
ALL_ICON = "view-list-symbolic"
TYPE_ICONS = {
    "sample": "applications-science-symbolic",
    "reaction": "media-playlist-shuffle-symbolic",
    "wellplate": "view-grid-symbolic",
    "screen": "view-app-grid-symbolic",
    "research_plan": "x-office-document-symbolic",
    "cell_line": "emblem-shared-symbolic",
    "device_description": "applications-engineering-symbolic",
}
GENERIC_ICON = "text-x-generic-symbolic"
STATE_ICONS = {"loading": "content-loading-symbolic", "more": "view-more-horizontal-symbolic",
               "error": "dialog-warning-symbolic"}


def plural(label: str) -> str:
    return label if label.endswith("s") else label + "s"


class ExplorerPanel(Gtk.Box):
    """`open_url(url)` opens an element, `open_settings()` the settings (for
    "Chemotion is not set up"). `source` replaces the real ExplorerSource
    (tests); `deliver` how answers reach the main loop (default GLib.idle_add)."""

    def __init__(self, open_url: Callable[[str], None], open_settings: Callable[[], None],
                 source: ExplorerSource | None = None,
                 deliver: Callable[[Callable[[], None]], object] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.open_url = open_url
        self.open_settings = open_settings
        self.worker = ExplorerWorker(source or ExplorerSource(), deliver or GLib.idle_add)
        self.types: list[dict] = []  # ExplorerSource.types()
        self.enabled: list[str] = []  # the type names shown, in the order of self.types
        self.started = False
        self.closed = False

        toolbar = Gtk.Box(spacing=6, border_width=6)
        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim")
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        toolbar.pack_start(self.status, True, True, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_relief(Gtk.ReliefStyle.NONE)
        refresh.set_tooltip_text("Load the collections again")
        refresh.connect("clicked", lambda *_: self.refresh())
        toolbar.pack_end(refresh, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        # Shown instead of the tree while it cannot be loaded.
        self.problem = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=12)
        self.problem_text = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.problem_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.problem.pack_start(self.problem_text, False, False, 0)
        buttons = Gtk.Box(spacing=6)
        retry = Gtk.Button(label="Try again")
        retry.connect("clicked", lambda *_: self.refresh())
        buttons.pack_start(retry, False, False, 0)
        self.settings_button = Gtk.Button(label="Open settings…")
        self.settings_button.connect("clicked", lambda *_: self.open_settings())
        self.settings_button.set_no_show_all(True)
        buttons.pack_start(self.settings_button, False, False, 0)
        self.problem.pack_start(buttons, False, False, 0)
        self.problem.set_no_show_all(True)
        self.pack_start(self.problem, False, False, 0)

        self.store = Gtk.TreeStore(str, str, str, str, GObject.TYPE_PYOBJECT)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_headers_visible(False)
        self.view.set_enable_search(True)
        self.view.set_search_column(TEXT)
        self.view.set_search_equal_func(self._search_equal)
        self.view.set_tooltip_column(TOOLTIP)
        column = Gtk.TreeViewColumn()
        icon = Gtk.CellRendererPixbuf()
        column.pack_start(icon, False)
        column.add_attribute(icon, "icon-name", ICON)
        text = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        column.pack_start(text, True)
        column.add_attribute(text, "markup", TEXT)
        self.view.append_column(column)
        self.view.connect("row-expanded", self._on_expanded)
        self.view.connect("row-activated", self._on_activated)
        self.view.connect("button-release-event", self._on_click)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)

        # Nothing is fetched before the tab is shown the first time.
        self.connect("map", lambda *_: self.start())

    # -- loading the tree ---------------------------------------------------------

    def start(self) -> None:
        if self.started or self.closed:
            return
        self.started = True
        self.refresh()

    def refresh(self) -> None:
        """Everything again: types, collections; opened collections are closed."""
        self.started = True
        self.worker.reset()
        self.store.clear()
        self._show_problem(None)
        self.status.set_text("Loading the collections…")
        self.worker.request("types", (), self._got_types, self._failed)

    def _got_types(self, types: list[dict]) -> None:
        self.types = types
        self.enabled = [entry["name"] for entry in types if entry["name"] in DEFAULT_TYPES]
        self.worker.request("collections", (), self._got_collections, self._failed)

    def _got_collections(self, nodes: list[dict]) -> None:
        self.store.clear()
        for node in nodes:
            self._add_collection(None, node)
        self.status.set_text("Open a collection to see its elements.")

    def _failed(self, message: str) -> None:
        self.store.clear()
        self.status.set_text("")
        self._show_problem(message)

    def _show_problem(self, message: str | None) -> None:
        if message is None:
            self.problem.hide()
            return
        self.problem_text.set_text(message)
        self.settings_button.set_visible("settings" in message)
        self.problem.show_all()

    def _add_collection(self, parent: Gtk.TreeIter | None, node: dict) -> None:
        if node["id"] is None:  # "Shared with me"
            row = self.store.append(parent, [SHARED_ICON, GLib.markup_escape_text(node["label"]),
                                             "Collections others share with you", GROUP, node])
        else:
            icon = ALL_ICON if parent is None and node["label"] == "All" and not node["children"] else FOLDER_ICON
            row = self.store.append(parent, [icon, GLib.markup_escape_text(node["label"]), node["path"],
                                             COLLECTION, node])
        for child in node["children"]:
            self._add_collection(row, child)
        if node["id"] is not None:
            self.store.append(row, ["", "<i>Loading…</i>", None, PENDING, None])

    # -- elements -----------------------------------------------------------------

    def _on_expanded(self, _view, row: Gtk.TreeIter, _path) -> None:
        if self.store[row][KIND] != COLLECTION:
            return
        pending = self._child_of_kind(row, PENDING)
        if pending is None:
            return  # opened before
        node = self.store[row][DATA]
        if not self.enabled:
            self.store.set(pending, [TEXT, KIND], ["<i>No element types chosen</i>", NOTE])
            return
        anchors = [self._reference(self.store.append(row, ["", "", None, ANCHOR, {
            "collection": node, "type": type_name, "page": 1, "state": ""}])) for type_name in self.enabled]
        self.store.remove(pending)  # after the anchors: a row without children would collapse
        for anchor in anchors:
            self._load_page(anchor)

    def _child_of_kind(self, row: Gtk.TreeIter, kind: str) -> Gtk.TreeIter | None:
        child = self.store.iter_children(row)
        while child is not None:
            if self.store[child][KIND] == kind:
                return child
            child = self.store.iter_next(child)
        return None

    def _reference(self, row: Gtk.TreeIter) -> Gtk.TreeRowReference:
        return Gtk.TreeRowReference.new(self.store, self.store.get_path(row))

    def _row(self, reference: Gtk.TreeRowReference) -> Gtk.TreeIter | None:
        path = reference.get_path() if reference.valid() else None
        return self.store.get_iter(path) if path is not None else None

    def _type_label(self, type_name: str) -> str:
        for entry in self.types:
            if entry["name"] == type_name:
                return entry["label"]
        return type_name.replace("_", " ").capitalize()

    def _set_anchor(self, row: Gtk.TreeIter, state: str, text: str, tooltip: str | None = None) -> None:
        self.store[row][DATA]["state"] = state
        self.store.set(row, [ICON, TEXT, TOOLTIP], [STATE_ICONS[state], text, tooltip])

    def _load_page(self, reference: Gtk.TreeRowReference) -> None:
        row = self._row(reference)
        if row is None:
            return
        anchor = self.store[row][DATA]
        label = plural(self._type_label(anchor["type"])).lower()
        self._set_anchor(row, "loading", f"<i>Loading {GLib.markup_escape_text(label)}…</i>")
        self.worker.request("page", (anchor["collection"], anchor["type"], anchor["page"], PER_PAGE),
                            lambda page: self._got_page(reference, page),
                            lambda message: self._page_failed(reference, message))

    def _got_page(self, reference: Gtk.TreeRowReference, page: dict) -> None:
        row = self._row(reference)
        if row is None:  # refreshed or removed meanwhile
            return
        anchor = self.store[row][DATA]
        parent = self.store.iter_parent(row)
        icon = TYPE_ICONS.get(anchor["type"], GENERIC_ICON)
        for item in page["items"]:
            name = GLib.markup_escape_text(item["name"])
            short = GLib.markup_escape_text(item["short_label"])
            text = f"<b>{short}</b>  {name}" if short and name else (f"<b>{short}</b>" if short else name)
            self.store.insert_before(parent, row, [icon, text or f"#{item['id']}", item["tooltip"], ELEMENT, item])
        loaded = (page["page"] - 1) * PER_PAGE + len(page["items"])
        if page["more"]:
            anchor["page"] = page["page"] + 1
            label = GLib.markup_escape_text(plural(self._type_label(anchor["type"])).lower())
            self._set_anchor(row, "more", f"<i>More {label}… ({loaded} of {page['total']})</i>",
                             f"Load the next {PER_PAGE}")
        else:
            row = self._row(reference)
            self.store.remove(row)
            self._mark_empty(parent)

    def _page_failed(self, reference: Gtk.TreeRowReference, message: str) -> None:
        row = self._row(reference)
        if row is None:
            return
        label = GLib.markup_escape_text(plural(self._type_label(self.store[row][DATA]["type"])).lower())
        self._set_anchor(row, "error", f"<i>Could not load {label}: click to try again</i>", message)

    def _mark_empty(self, collection: Gtk.TreeIter) -> None:
        """"Empty" for an opened collection without subcollections or elements."""
        if self.store.iter_n_children(collection) == 0:
            self.store.append(collection, ["", "<i>Empty</i>", None, NOTE, None])

    # -- clicks -------------------------------------------------------------------

    def _activate(self, row: Gtk.TreeIter) -> None:
        kind = self.store[row][KIND]
        if kind == ELEMENT:
            self.open_url(self.store[row][DATA]["url"])
        elif kind == ANCHOR and self.store[row][DATA]["state"] in ("more", "error"):
            self._load_page(self._reference(row))
        elif kind in (COLLECTION, GROUP):
            path = self.store.get_path(row)
            if self.view.row_expanded(path):
                self.view.collapse_row(path)
            else:
                self.view.expand_row(path, False)

    def _on_activated(self, _view, path, _column) -> None:
        self._activate(self.store.get_iter(path))

    def _on_click(self, _view, event) -> bool:
        """A single click is enough for "More…" and "try again" (an element
        opens with a double click or Enter, like a file)."""
        if event.button != 1:
            return False
        hit = self.view.get_path_at_pos(int(event.x), int(event.y))
        if hit is None:
            return False
        row = self.store.get_iter(hit[0])
        if self.store[row][KIND] == ANCHOR and self.store[row][DATA]["state"] in ("more", "error"):
            self._activate(row)
            return True
        return False

    def _search_equal(self, model, _column, key: str, row) -> bool:
        """Type-ahead over the visible rows: False = a match (GTK's convention)."""
        if model[row][KIND] not in (COLLECTION, GROUP, ELEMENT):
            return True
        data = model[row][DATA] or {}
        text = " ".join(str(data.get(field) or "") for field in ("label", "short_label", "name"))
        return key.lower() not in text.lower()

    def close(self) -> None:
        self.closed = True
        self.worker.close()
