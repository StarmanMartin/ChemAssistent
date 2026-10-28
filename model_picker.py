"""A model chooser that stays usable with hundreds of models (the Gemini API
lists embeddings, image, video and audio models too): a search field over a
scrolled list whose height fits the window. app.py's header menu uses
ModelList in its popover, settings.py the ModelEntry (entry + list)."""
from __future__ import annotations

import re

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402


# Model ids that are no chat models with tool calls (embeddings, image, video,
# music and speech generation, realtime audio); "Only chat models" hides them.
NON_CHAT = re.compile(r"embed|tts|imagen|image|veo|lyria|aqa|audio|live|whisper|transcri|dall-e|moderation|rerank",
                      re.IGNORECASE)
# Height of the list: at most this, and no more than the window has room for
# (a GTK 3 popover cannot leave its window) ...
MAX_LIST_HEIGHT = 420
# ... but at least this.
MIN_LIST_HEIGHT = 120
# What the popover needs besides the list: search field, filter, count,
# footer, arrow and margins.
POPOVER_EXTRA_HEIGHT = 190


def matches(text: str, query: str) -> bool:
    """Every word of `query` occurs in `text` (case-insensitive), in any order."""
    text = text.lower()
    return all(word in text for word in query.lower().split())


def is_chat_model(model: str) -> bool:
    return not NON_CHAT.search(model)


class ModelRow(Gtk.ListBoxRow):
    def __init__(self, model: str, text: str) -> None:
        super().__init__()
        self.model = model
        self.text = text
        box = Gtk.Box(spacing=8, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
        self.check = Gtk.Image.new_from_icon_name("object-select-symbolic", Gtk.IconSize.MENU)
        self.check.set_no_show_all(True)
        box.pack_start(self.check, False, False, 0)
        box.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        self.add(box)
        self.show_all()


class ModelList(Gtk.Box):
    """Search field, "Only chat models" and the scrolled list of models.
    `on_chosen(model)` runs on a click, or on Enter in the search field for
    the first match; `name(model)` is the shown text."""

    def __init__(self, on_chosen, name=lambda model: model, empty: str = "No models loaded.") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_chosen = on_chosen
        self.name = name
        self.empty = empty
        self.current = ""
        self.rows: list[ModelRow] = []

        self.search = Gtk.SearchEntry(placeholder_text="Search models")
        self.search.connect("search-changed", lambda *_: self._refilter())
        self.search.connect("activate", lambda *_: self._choose_first())
        self.search.connect("key-press-event", self._on_search_key)
        self.pack_start(self.search, False, False, 0)

        self.chat_only = Gtk.CheckButton(label="Only chat models")
        self.chat_only.set_tooltip_text("Hide embedding, image, video, audio and realtime models")
        self.chat_only.set_active(True)
        self.chat_only.set_no_show_all(True)  # only when there is something to hide
        self.chat_only.connect("toggled", lambda *_: self._refilter())
        self.pack_start(self.chat_only, False, False, 0)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.set_filter_func(self._visible)
        self.list.connect("row-activated", lambda _list, row: self.on_chosen(row.model))
        self.placeholder = Gtk.Label(label=empty, margin=12, wrap=True, max_width_chars=40)
        self.placeholder.get_style_context().add_class("dim-label")
        self.placeholder.show()
        self.list.set_placeholder(self.placeholder)
        self.scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                           propagate_natural_height=True, min_content_width=300,
                                           max_content_height=MAX_LIST_HEIGHT)
        self.scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.scroller.add(self.list)
        self.pack_start(self.scroller, True, True, 0)

        self.count = Gtk.Label(xalign=0)
        self.count.get_style_context().add_class("dim-label")
        self.pack_start(self.count, False, False, 0)

    def set_models(self, models, current: str = "") -> None:
        """Show `models` (and `current`, also when it is not among them)."""
        for row in self.rows:
            self.list.remove(row)
        self.current = current
        listed = sorted(set(models) | ({current} if current else set()), key=str.lower)
        self.rows = [ModelRow(model, self.name(model)) for model in listed]
        for row in self.rows:
            self.list.add(row)
        self.chat_only.set_visible(any(not is_chat_model(model) for model in listed))
        self._mark_current()
        self._refilter()

    def set_current(self, current: str) -> None:
        self.current = current
        self._mark_current()
        self._refilter()

    def prepare(self, anchor: Gtk.Widget) -> None:
        """Before the popover opens below/above `anchor`: empty search, focus
        in the search field, and a list height that fits the window."""
        self.search.set_text("")
        self.fit(anchor)
        GLib.idle_add(lambda: self.search.grab_focus() and False)

    def fit(self, anchor: Gtk.Widget) -> None:
        """Open the popover on the side of `anchor` with more room, with a list
        that fits there (else GTK puts it beside the anchor, or cuts it off)."""
        toplevel = anchor.get_toplevel()
        position = anchor.translate_coordinates(toplevel, 0, 0)
        if position is None:
            return
        above = position[1]
        below = toplevel.get_allocated_height() - above - anchor.get_allocated_height()
        room = max(above, below) - POPOVER_EXTRA_HEIGHT
        self.scroller.set_max_content_height(max(MIN_LIST_HEIGHT, min(MAX_LIST_HEIGHT, room)))
        popover = self.get_ancestor(Gtk.Popover)
        if popover is not None:
            popover.set_position(Gtk.PositionType.BOTTOM if below >= above else Gtk.PositionType.TOP)

    # --- filter ------------------------------------------------------------------
    def _visible(self, row: ModelRow) -> bool:
        if row.model == self.current:
            return matches(f"{row.model} {row.text}", self.search.get_text())
        if self.chat_only.get_visible() and self.chat_only.get_active() and not is_chat_model(row.model):
            return False
        return matches(f"{row.model} {row.text}", self.search.get_text())

    def _refilter(self) -> None:
        self.list.invalidate_filter()
        shown = sum(1 for row in self.rows if self._visible(row))
        self.placeholder.set_text("No model matches." if self.rows else self.empty)
        total = f"{len(self.rows)} model{'' if len(self.rows) == 1 else 's'}"
        self.count.set_text(total if shown == len(self.rows) else f"{shown} of {total}")
        self.count.set_visible(bool(self.rows))

    def _mark_current(self) -> None:
        for row in self.rows:
            row.check.set_visible(row.model == self.current)

    def _first_visible(self) -> ModelRow | None:
        return next((row for row in self.rows if self._visible(row)), None)

    def _choose_first(self) -> None:
        row = self._first_visible()
        if row is not None:
            self.on_chosen(row.model)

    def _on_search_key(self, _entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Down:
            row = self._first_visible()
            if row is not None:
                row.grab_focus()
                return True
        return False


class ModelEntry(Gtk.Box):
    """An entry for any model id with a button that opens a ModelList of the
    known models (the settings: the list may come from "Load models")."""

    def __init__(self) -> None:
        super().__init__(spacing=0)
        self.get_style_context().add_class("linked")
        self.entry = Gtk.Entry(hexpand=True)
        self.pack_start(self.entry, True, True, 0)
        self.button = Gtk.MenuButton()
        self.button.add(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON))
        self.button.set_tooltip_text("Choose from the known models")
        self.pack_start(self.button, False, False, 0)
        self.popover = Gtk.Popover(relative_to=self.button)
        self.list = ModelList(self._chosen, empty="No models loaded: click “Load models”.")
        self.list.set_border_width(6)
        self.list.show_all()
        self.popover.add(self.list)
        self.popover.connect("show", lambda *_: self._prepare())
        self.button.set_popover(self.popover)

    def set_models(self, models) -> None:
        self.list.set_models(models, self.get_text())

    def get_text(self) -> str:
        return self.entry.get_text().strip()

    def set_text(self, text: str) -> None:
        self.entry.set_text(text)

    def set_placeholder_text(self, text: str) -> None:
        self.entry.set_placeholder_text(text)

    def _prepare(self) -> None:
        self.list.set_current(self.get_text())
        self.list.prepare(self.button)

    def _chosen(self, model: str) -> None:
        self.popover.popdown()
        self.entry.set_text(model)
