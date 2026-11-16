"""The Details panel (GTK): the basics of the elements and collections
selected in the Explorer.

`show_items(items)` takes the dicts of ExplorerPanel.describe(): one card per
item (icon and label, a key/value grid from explorer.detail_rows(), "Open in
Chemotion" and "Mention in message"). Nothing is requested from the server;
no threads. More than MAX_DETAIL_ITEMS items show the first ones and a note.
"""
from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # type: ignore  # noqa: E402

from explorer import detail_rows  # noqa: E402

MAX_DETAIL_ITEMS = 20
EMPTY_HINT = "Select elements or collections in the Explorer to see their details here."


def _label(text: str, css: str | None = None, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if css:
        widget.get_style_context().add_class(css)
    return widget


class DetailsPanel(Gtk.Box):
    """`open_url(url)` and `mention(text)` are called by the card buttons."""

    def __init__(self, open_url: Callable[[str], None], mention: Callable[[str], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.open_url = open_url
        self.mention = mention
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.set_border_width(12)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.content)
        self.pack_start(scroller, True, True, 0)
        self.show_items([])

    def show_items(self, items: list[dict]) -> None:
        """Replace the cards by those of `items` (empty: the hint)."""
        for child in self.content.get_children():
            self.content.remove(child)
        if not items:
            self.content.pack_start(_label(EMPTY_HINT, "dim"), False, False, 0)
        for item in items[:MAX_DETAIL_ITEMS]:
            self.content.pack_start(self._card(item), False, False, 0)
        if len(items) > MAX_DETAIL_ITEMS:
            self.content.pack_start(_label(f"… and {len(items) - MAX_DETAIL_ITEMS} more", "dim"), False, False, 0)
        self.content.show_all()

    def _card(self, item: dict) -> Gtk.Widget:
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        frame.add(box)

        head = Gtk.Box(spacing=8)
        if item.get("icon"):
            head.pack_start(Gtk.Image.new_from_icon_name(item["icon"], Gtk.IconSize.MENU), False, False, 0)
        title = _label(item.get("label") or "", selectable=True)
        title.set_markup("<b>%s</b>" % GLib.markup_escape_text(item.get("label") or ""))
        head.pack_start(title, True, True, 0)
        box.pack_start(head, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        for row, (name, value) in enumerate(detail_rows(item)):
            grid.attach(_label(name, "dim"), 0, row, 1, 1)
            value_label = _label(value, selectable=True)
            value_label.set_hexpand(True)
            grid.attach(value_label, 1, row, 1, 1)
        box.pack_start(grid, False, False, 0)

        buttons = Gtk.Box(spacing=6)
        if item.get("url"):
            link = Gtk.Button.new_from_icon_name("web-browser-symbolic", Gtk.IconSize.BUTTON)
            link.set_label("Open in Chemotion")
            link.set_always_show_image(True)
            link.connect("clicked", lambda *_: self.open_url(item["url"]))
            buttons.pack_start(link, False, False, 0)
        if item.get("text"):
            mention = Gtk.Button.new_from_icon_name("insert-text-symbolic", Gtk.IconSize.BUTTON)
            mention.set_label("Mention in message")
            mention.set_always_show_image(True)
            mention.connect("clicked", lambda *_: self.mention(item["text"]))
            buttons.pack_start(mention, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        return frame
