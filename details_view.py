"""The Details panel (GTK): the elements and collections selected in the Explorer.

`show_items(items)` takes the dicts of ExplorerPanel.describe(). One selected
element shows its detail view at once (view model of element_views.view(),
fetched with `request("details", (type, id, collection_id), …)` on the
Explorer's worker; cached by (type, id, updated_at)); several selected items
(and collections) show short cards (icon and label, key/value grid from
explorer.detail_rows(), "Open in Chemotion", "Mention in message"), an element
card opens its detail view ("<- All selected" goes back). A table row of a
detail view with a target (`targets` of the view model: a reaction's materials,
a screen's wellplates) has a button that opens the target's detail view (it also gets
"Mention in message": details_logic.linked_mention(), the collection is the one of the
element the trail started from); a
navigation trail of the views left that way gives "<- <title>" back buttons
(the first one in the trail returns to "All selected" again). More than
MAX_DETAIL_ITEMS items show the first ones and a note. A selection change
while loading drops the late answer (request counter).
"""
from __future__ import annotations

import base64
import binascii
import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from details_logic import (  # noqa: E402
    MAX_DOWNLOAD_BYTES, analysis_mention, file_mention, human_size, inherit_collection, jupyter_path, linked_mention,
    needs_confirmation, open_path, relative_to_exchange, sanitize_name,
)
from explorer import detail_rows  # noqa: E402

MAX_DETAIL_ITEMS = 20
MAX_IMAGE_HEIGHT = 300
ELEMENT = "element"  # the describe() kind of an element row (explorer_view.ELEMENT)
EMPTY_HINT = "Select elements or collections in the Explorer to see their details here."


def _label(text: str, css: str | None = None, selectable: bool = False) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    widget.set_selectable(selectable)
    if css:
        widget.get_style_context().add_class(css)
    return widget



def detail_target(items: list[dict]) -> dict | None:
    """The item whose detail view `show_items` opens at once: the only selected
    element; None for no selection, several items or a collection."""
    if len(items) == 1 and items[0].get("kind") == ELEMENT and items[0].get("type") and items[0].get("id") is not None:
        return items[0]
    return None


def cache_key(item: dict) -> tuple:
    """Key of the panel's view-model cache."""
    return (item.get("type"), item.get("id"), item.get("updated_at"))


def valid_markup(markup: str) -> bool:
    """True when Pango accepts the view model's `markup` of a text section."""
    try:
        Pango.parse_markup(markup, -1, "\0")
    except GLib.Error:
        return False
    return True


def pixbuf_from_image(image: dict | None, max_width: int, max_height: int = MAX_IMAGE_HEIGHT):
    """GdkPixbuf of a view model's `image` ({"mime", "data"[, "encoding": "base64"]}) scaled down to fit
    `max_width` x `max_height` (never up beyond its own size); None when there is
    none or it cannot be read."""
    if not image or not image.get("data"):
        return None
    data = image["data"]
    try:
        if image.get("encoding") == "base64":
            raw = base64.b64decode(data)
        else:
            raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    except (binascii.Error, ValueError, TypeError):
        return None
    mime = image.get("mime") or "image/svg+xml"
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_mime_type(mime)
        loader.write(raw)
        loader.close()
        pixbuf = loader.get_pixbuf()
        if pixbuf is None:
            return None
        width, height = pixbuf.get_width(), pixbuf.get_height()
        if width <= 0 or height <= 0:
            return None
        scale = min(max(max_width, 40) / width, max_height / height)
        if scale < 1:
            pixbuf = pixbuf.scale_simple(max(1, int(width * scale)), max(1, int(height * scale)),
                                         GdkPixbuf.InterpType.BILINEAR)
        return pixbuf
    except (GLib.Error, ValueError, TypeError):
        return None

class DetailsPanel(Gtk.Box):
    """`open_url(url)` and `mention(text)` are called by the buttons;
    `request(method, args, on_done, on_error)` is the Explorer worker's request
    (None: no detail views, short cards only). `download_targets()` gives
    `{"jupyter": (exchange Path, sync() -> notes) | None, "downloads": Path}` for the
    files of the analyses section (None: "Open" and "Download to…" only, in a temp folder)."""

    def __init__(self, open_url: Callable[[str], None], mention: Callable[[str], None],
                 request: Callable | None = None, download_targets: Callable[[], dict] | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.open_url = open_url
        self.mention = mention
        self.request = request
        self.download_targets = download_targets
        self.previews: dict[tuple, dict | None] = {}  # (kind, id) -> IMAGE, kept per panel
        self.items: list[dict] = []
        self.counter = 0  # bumped by every view change; a late answer of an older one is dropped
        self.cache: dict[tuple, dict] = {}
        self.trail: list[tuple[dict, bool, str]] = []  # (item, came from the cards, title) left through a target
        self.current_title = ""
        self.current_item: dict | None = None  # the detail view shown (the trail's next entry)
        self.current_back = False
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content.set_border_width(12)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.content)
        self.pack_start(scroller, True, True, 0)
        self.show_items([])

    def _clear(self) -> None:
        for child in self.content.get_children():
            self.content.remove(child)

    def show_items(self, items: list[dict]) -> None:
        """The selection changed: one element opens its detail view, else cards."""
        self.items = list(items)
        self.trail = []
        target = detail_target(self.items) if self.request is not None else None
        if target is not None:
            self._open_detail(target, back=False)
        else:
            self._show_cards()

    def _show_cards(self) -> None:
        self.counter += 1
        self.trail = []
        self._clear()
        items = self.items
        if not items:
            self.content.pack_start(_label(EMPTY_HINT, "dim"), False, False, 0)
        for item in items[:MAX_DETAIL_ITEMS]:
            self.content.pack_start(self._card(item), False, False, 0)
        if len(items) > MAX_DETAIL_ITEMS:
            self.content.pack_start(_label(f"… and {len(items) - MAX_DETAIL_ITEMS} more", "dim"), False, False, 0)
        self.content.show_all()

    # --- detail view -------------------------------------------------------------
    def _open_detail(self, item: dict, back: bool, refresh: bool = False) -> None:
        self.counter += 1
        number = self.counter
        key = cache_key(item)
        if refresh:
            self.cache.pop(key, None)
        cached = self.cache.get(key)
        if cached is not None:
            self._render(item, cached, back)
            return
        self._clear()
        self.current_title = item.get("label") or ""
        if back or self.trail:
            self.content.pack_start(self._back_button(back), False, False, 0)
        box = Gtk.Box(spacing=8)
        spinner = Gtk.Spinner()
        spinner.start()
        box.pack_start(spinner, False, False, 0)
        box.pack_start(_label("Loading…", "dim"), False, False, 0)
        self.content.pack_start(box, False, False, 0)
        self.content.show_all()

        def done(model: dict) -> None:
            if number != self.counter:
                return
            self.cache[key] = model
            self._render(item, model, back)

        def failed(message: str) -> None:
            if number != self.counter:
                return
            self._render_error(item, str(message), back)

        try:
            self.request("details", (item["type"], item["id"], item.get("collection_id")), done, failed)
        except Exception as exc:  # noqa: BLE001 - the panel must stay usable
            self._render_error(item, f"{type(exc).__name__}: {exc}", back)

    def _back_button(self, back: bool = False) -> Gtk.Widget:
        """"<- <title>" of the view left through a target, else "All selected"."""
        button = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        label = self.trail[-1][2] if self.trail else "All selected"
        button.set_label(label if len(label) <= 40 else label[:39] + "…")
        button.set_tooltip_text(label)
        button.set_always_show_image(True)
        button.set_halign(Gtk.Align.START)
        button.connect("clicked", lambda *_: self._go_back())
        return button

    def _go_back(self) -> None:
        if self.trail:
            item, back, _title = self.trail.pop()
            self._open_detail(item, back)
        else:
            self._show_cards()

    def open_target(self, target: dict, label: str = "") -> None:
        """Open the detail view of a table row's target ({"type", "id"}); the
        current view goes onto the trail (its back button returns to it)."""
        if self.request is None or not target.get("type") or target.get("id") is None:
            return
        current = self.current_item
        if current is None:
            return
        self.trail.append((current, self.current_back, self.current_title or current.get("label") or ""))
        self._open_detail({"kind": ELEMENT, "type": target["type"], "id": target["id"],
                           "collection_id": current.get("collection_id"), "linked": True,
                           **inherit_collection(current),
                           "label": label or f"{target['type'].replace('_', ' ')} {target['id']}"}, back=False)

    def _render_error(self, item: dict, message: str, back: bool) -> None:
        self._clear()
        self.current_item, self.current_back = item, back
        if back or self.trail:
            self.content.pack_start(self._back_button(back), False, False, 0)
        self.content.pack_start(_label(item.get("label") or "", None), False, False, 0)
        error = _label(message, "dim", selectable=True)
        self.content.pack_start(error, False, False, 0)
        retry = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        retry.set_label("Try again")
        retry.set_always_show_image(True)
        retry.set_halign(Gtk.Align.START)
        retry.connect("clicked", lambda *_: self._open_detail(item, back, refresh=True))
        self.content.pack_start(retry, False, False, 0)
        self.content.show_all()

    def _render(self, item: dict, model: dict, back: bool) -> None:
        self._clear()
        self.current_item, self.current_back = item, back
        self.current_title = model.get("title") or item.get("label") or ""
        if back or self.trail:
            self.content.pack_start(self._back_button(back), False, False, 0)
        if model.get("unsupported"):  # no view for this type yet: the short card
            self.content.pack_start(self._card(item), False, False, 0)
            self.content.show_all()
            return
        head = Gtk.Box(spacing=8)
        if item.get("icon"):
            head.pack_start(Gtk.Image.new_from_icon_name(item["icon"], Gtk.IconSize.MENU), False, False, 0)
        title = _label(model.get("title") or item.get("label") or "", selectable=True)
        title.set_markup("<b>%s</b>" % GLib.markup_escape_text(model.get("title") or item.get("label") or ""))
        head.pack_start(title, True, True, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Load again")
        refresh.set_relief(Gtk.ReliefStyle.NONE)
        refresh.connect("clicked", lambda *_: self._open_detail(item, back, refresh=True))
        head.pack_end(refresh, False, False, 0)
        self.content.pack_start(head, False, False, 0)
        if model.get("subtitle"):
            self.content.pack_start(_label(model["subtitle"], "dim", selectable=True), False, False, 0)
        # The listing's link first: it knows the route of shared collections.
        if item.get("linked") and not item.get("text"):  # opened from a link: the reference from the view
            item = {**item, "text": linked_mention(item, model)}
        self.content.pack_start(self._buttons(item, item.get("url") or model.get("url")), False, False, 0)
        image = self._image(model.get("image"))
        if image is not None:
            self.content.pack_start(image, False, False, 0)
        for section in model.get("sections") or []:
            self.content.pack_start(self._section(section, item), False, False, 0)
        for note in model.get("notes") or []:
            self.content.pack_start(_label(note, "dim"), False, False, 0)
        self.content.show_all()

    def _image(self, image: dict | None) -> Gtk.Widget | None:
        width = self.get_allocated_width() - 30
        pixbuf = pixbuf_from_image(image, width if width > 60 else 300)
        if pixbuf is None:
            return None
        widget = Gtk.Image.new_from_pixbuf(pixbuf)
        widget.set_halign(Gtk.Align.START)
        return widget

    def _section(self, section: dict, item: dict | None = None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading = _label("", None)
        heading.set_markup("<b>%s</b>" % GLib.markup_escape_text(section.get("title") or ""))
        box.pack_start(heading, False, False, 0)
        kind = section.get("kind")
        if kind == "analyses":
            for analysis in section.get("analyses") or []:
                box.pack_start(self._analysis(analysis, item or {}), False, False, 0)
            return box
        if kind == "text":
            body = _label(section.get("text") or "", selectable=True)
            markup = section.get("markup")
            if markup and valid_markup(markup):
                body.set_markup(markup)
            box.pack_start(body, False, False, 0)
            return box
        if kind == "image":
            image = self._image(section.get("image"))
            if image is not None:
                box.pack_start(image, False, False, 0)
            else:
                box.pack_start(_label("The image cannot be shown.", "dim"), False, False, 0)
            return box
        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        if kind == "table":
            columns = section.get("columns") or []
            targets = section.get("targets") or []
            for col, name in enumerate(columns):
                head = _label(name, "dim")
                head.set_hexpand(col == len(columns) - 1)
                grid.attach(head, col, 0, 1, 1)
            for row, cells in enumerate(section.get("rows") or [], start=1):
                for col, cell in enumerate(cells):
                    grid.attach(_label(str(cell), selectable=True), col, row, 1, 1)
                target = targets[row - 1] if row - 1 < len(targets) else None
                if target:
                    grid.attach(self._target_button(target, str(cells[0]) if cells else ""), len(columns), row, 1, 1)
        else:  # "fields"
            for row, entry in enumerate(section.get("rows") or []):
                grid.attach(_label(entry.get("label") or "", "dim"), 0, row, 1, 1)
                value = " ".join(part for part in (entry.get("value"), entry.get("unit")) if part)
                value_label = _label(value, selectable=True)
                value_label.set_hexpand(True)
                grid.attach(value_label, 1, row, 1, 1)
        box.pack_start(grid, False, False, 0)
        return box

    def _target_button(self, target: dict, label: str) -> Gtk.Widget:
        button = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_valign(Gtk.Align.START)
        button.set_tooltip_text("Show details")
        button.connect("clicked", lambda *_: self.open_target(target, label))
        return button

    def _buttons(self, item: dict, url: str | None) -> Gtk.Widget:
        buttons = Gtk.Box(spacing=6)
        if url:
            link = Gtk.Button.new_from_icon_name("web-browser-symbolic", Gtk.IconSize.BUTTON)
            link.set_label("Open in Chemotion")
            link.set_always_show_image(True)
            link.connect("clicked", lambda *_: self.open_url(url))
            buttons.pack_start(link, False, False, 0)
        if item.get("text"):
            mention = Gtk.Button.new_from_icon_name("insert-text-symbolic", Gtk.IconSize.BUTTON)
            mention.set_label("Mention in message")
            mention.set_always_show_image(True)
            mention.connect("clicked", lambda *_: self.mention(item["text"]))
            buttons.pack_start(mention, False, False, 0)
        return buttons

    # --- analyses (M9) -----------------------------------------------------------
    def _targets(self) -> dict:
        try:
            return (self.download_targets() if self.download_targets else None) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _load_preview(self, kind: str, ident, show: Callable[[dict], None], number: int) -> None:
        """Preview image of an analysis/attachment, once per panel (None cached too)."""
        if self.request is None:
            return
        key = (kind, ident)
        if key in self.previews:
            if self.previews[key]:
                show(self.previews[key])
            return

        def done(image) -> None:
            self.previews[key] = image or None
            if image and number == self.counter:
                show(image)

        def failed(_message: str) -> None:
            pass  # a missing preview is no error worth a message

        try:
            self.request("preview", (kind, ident), done, failed)
        except Exception:  # noqa: BLE001
            pass

    def _analysis(self, analysis: dict, item: dict) -> Gtk.Widget:
        number = self.counter
        title = " · ".join(str(part) for part in (analysis.get("name"), analysis.get("kind"), analysis.get("status")) if part)
        expander = Gtk.Expander(label=title or f"Analysis {analysis.get('id')}")
        label = expander.get_label_widget()
        if isinstance(label, Gtk.Label):
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_tooltip_text(title)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_top=4, margin_bottom=6)
        if item.get("text"):
            mention = Gtk.Button.new_from_icon_name("insert-text-symbolic", Gtk.IconSize.BUTTON)
            mention.set_label("Mention in message")
            mention.set_always_show_image(True)
            mention.set_halign(Gtk.Align.START)
            mention.connect("clicked", lambda *_: self.mention(analysis_mention(item, analysis)))
            body.pack_start(mention, False, False, 0)
        holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)  # the preview comes in here
        body.pack_start(holder, False, False, 0)
        if analysis.get("text"):
            text = _label(analysis["text"], selectable=True)
            if analysis.get("markup") and valid_markup(analysis["markup"]):
                text.set_markup(analysis["markup"])
            body.pack_start(text, False, False, 0)
        thumbs: list[tuple[dict, Gtk.Box]] = []
        for dataset in analysis.get("datasets") or []:
            body.pack_start(self._dataset(dataset, analysis, item, thumbs), False, False, 0)
        expander.add(body)
        loaded = []

        def show_preview(image: dict) -> None:
            widget = self._image(image)
            if widget is not None and holder.get_parent() is not None:
                holder.pack_start(widget, False, False, 0)
                widget.show()

        def expanded(*_args) -> None:
            if not expander.get_expanded() or loaded:
                return
            loaded.append(True)
            if analysis.get("preview"):
                self._load_preview("analysis", analysis.get("id"), show_preview, number)
            for file, slot in thumbs:
                def show_thumb(image: dict, slot=slot) -> None:
                    pixbuf = pixbuf_from_image(image, 160, 100)
                    if pixbuf is not None:
                        thumb = Gtk.Image.new_from_pixbuf(pixbuf)
                        thumb.set_halign(Gtk.Align.START)
                        slot.pack_start(thumb, False, False, 0)
                        thumb.show()
                self._load_preview("attachment", file.get("id"), show_thumb, number)

        expander.connect("notify::expanded", expanded)
        return expander

    def _dataset(self, dataset: dict, analysis: dict, item: dict, thumbs: list) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head = _label("", None)
        text = "<b>%s</b>" % GLib.markup_escape_text(str(dataset.get("name") or "Dataset"))
        if dataset.get("instrument"):
            text += "  <span alpha='60%%'>%s</span>" % GLib.markup_escape_text(str(dataset["instrument"]))
        head.set_markup(text)
        box.pack_start(head, False, False, 0)
        rows = dataset.get("rows") or []
        if rows:
            grid = Gtk.Grid(column_spacing=12, row_spacing=2)
            for row, entry in enumerate(rows):
                grid.attach(_label(entry.get("label") or "", "dim"), 0, row, 1, 1)
                value = " ".join(part for part in (entry.get("value"), entry.get("unit")) if part)
                value_label = _label(value, selectable=True)
                value_label.set_hexpand(True)
                grid.attach(value_label, 1, row, 1, 1)
            box.pack_start(grid, False, False, 0)
        for file in dataset.get("files") or []:
            box.pack_start(self._file_row(file, analysis, item, thumbs), False, False, 0)
        return box

    def _file_row(self, file: dict, analysis: dict, item: dict, thumbs: list) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        line = Gtk.Box(spacing=6)
        line.pack_start(Gtk.Image.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.MENU), False, False, 0)
        name = _label(str(file.get("name") or ""), selectable=True)
        name.set_line_wrap(False)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_tooltip_text(str(file.get("name") or ""))
        line.pack_start(name, True, True, 0)
        size = human_size(file.get("size"))
        if size:
            line.pack_start(_label(size, "dim"), False, False, 0)
        button = Gtk.MenuButton()
        button.set_image(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_tooltip_text("Open or download")
        status = _label("", "dim", selectable=True)
        status.set_no_show_all(True)

        def say(text: str) -> None:
            status.set_text(text)
            status.set_visible(bool(text))

        # Rows are built when the popover opens: JupyterLab may come or go meanwhile.
        popover = Gtk.Popover(relative_to=button)
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, border_width=6)
        popover.add(menu)
        button.set_popover(popover)

        def fill(*_args) -> None:
            for child in menu.get_children():
                menu.remove(child)
            rows = [("Open", lambda: self.file_action("open", item, analysis, file, say)),
                    ("Download to…", lambda: self.file_action("to", item, analysis, file, say))]
            if self._targets().get("jupyter"):
                rows.insert(1, ("Download to JupyterLab", lambda: self.file_action("jupyter", item, analysis, file, say)))
            if item.get("text"):
                rows.append(("Mention in message", lambda: self.mention(file_mention(item, analysis, file))))
            for text, callback in rows:
                row = Gtk.ModelButton(text=text)

                def clicked(_row, callback=callback) -> None:
                    popover.popdown()
                    GLib.idle_add(lambda: callback() and False)
                row.connect("clicked", clicked)
                menu.pack_start(row, False, False, 0)
            menu.show_all()

        popover.connect("show", fill)
        line.pack_end(button, False, False, 0)
        box.pack_start(line, False, False, 0)
        if file.get("thumb"):
            slot = Gtk.Box()
            box.pack_start(slot, False, False, 0)
            thumbs.append((file, slot))
        box.pack_start(status, False, False, 0)
        return box

    def file_action(self, mode: str, item: dict, analysis: dict, file: dict, say: Callable[[str], None]) -> None:
        """"open" | "jupyter" | "to": choose the target path, confirm a big file, download it
        through the worker, then open it / sync it / say where it went."""
        if self.request is None:
            return
        targets = self._targets()
        if mode == "to":
            dialog = Gtk.FileChooserNative.new("Download to…", self.get_toplevel(), Gtk.FileChooserAction.SAVE, "Save", "Cancel")
            dialog.set_current_name(sanitize_name(str(file.get("name") or ""), "file"))
            dialog.set_do_overwrite_confirmation(True)

            def response(chooser, code) -> None:
                chosen = chooser.get_filename() if code == Gtk.ResponseType.ACCEPT else None
                self._keep.discard(chooser)
                if chosen:
                    self._confirm_and_download(Path(chosen), mode, file, say, targets)
            self._keep.add(dialog)
            dialog.connect("response", response)
            dialog.show()
            return
        if mode == "jupyter":
            jupyter = targets.get("jupyter")
            if not jupyter:
                say("JupyterLab is not available in this conversation.")
                return
            path = jupyter_path(jupyter[0], item, analysis, file)
        else:
            downloads = targets.get("downloads") or Path(GLib.get_user_cache_dir()) / "chem-assistent" / "downloads"
            path = open_path(downloads, file)
            if path.exists() and file.get("size") is not None and path.stat().st_size == file.get("size"):
                self._finished(path, mode, say, targets)
                return
        self._confirm_and_download(path, mode, file, say, targets)

    _keep: set = set()  # native dialogs stay referenced until they answer

    def _confirm_and_download(self, path: Path, mode: str, file: dict, say, targets: dict) -> None:
        if not needs_confirmation(file.get("size")):
            self._download(path, mode, file, say, targets, MAX_DOWNLOAD_BYTES)
            return
        dialog = Gtk.MessageDialog(transient_for=self.get_toplevel(), modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text="Download a large file?")
        dialog.format_secondary_text(f"{file.get('name')} is {human_size(file.get('size'))}. Download it anyway?")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Download", Gtk.ResponseType.OK)

        def response(box, code) -> None:
            box.destroy()
            if code == Gtk.ResponseType.OK:
                self._download(path, mode, file, say, targets, None)
        dialog.connect("response", response)
        dialog.show()

    def _download(self, path: Path, mode: str, file: dict, say, targets: dict, max_bytes) -> None:
        say("Downloading…")

        def done(_result) -> None:
            self._finished(path, mode, say, targets)

        def failed(message: str) -> None:
            say(f"Download failed: {message}")

        try:
            self.request("download", (file.get("id"), str(path), max_bytes), done, failed)
        except Exception as exc:  # noqa: BLE001
            say(f"Download failed: {exc}")

    def _finished(self, path: Path, mode: str, say, targets: dict) -> None:
        if mode == "open":
            say(f"Opened {path.name}")
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
            except GLib.Error as exc:
                say(f"Saved to {path}, but it cannot be opened: {exc.message}")
        elif mode == "jupyter":
            jupyter = targets.get("jupyter")
            relative = relative_to_exchange(jupyter[0], path) if jupyter else str(path)
            say("Sending to JupyterLab…")

            def work() -> None:
                try:
                    notes = jupyter[1]() or []
                    text = f"Saved to JupyterLab: {relative}" + "".join(f"\n{note}" for note in notes)
                except Exception as exc:  # noqa: BLE001
                    text = f"Saved locally ({relative}), but not sent to JupyterLab: {exc}"
                GLib.idle_add(lambda: say(text) and False)
            threading.Thread(target=work, daemon=True).start()
        else:
            say(f"Saved to {path}")

    # --- short cards -------------------------------------------------------------
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

        buttons = self._buttons(item, item.get("url"))
        if self.request is not None and len(self.items) > 1 and detail_target([item]) is not None:
            more = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
            more.set_label("Show details")
            more.set_always_show_image(True)
            more.connect("clicked", lambda *_: self._open_detail(item, back=True))
            buttons.pack_start(more, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        return frame
