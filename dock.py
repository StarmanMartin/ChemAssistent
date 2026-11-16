"""The docking area of the conversation page (GTK): panels in four zones.

`Dock` holds four `Gtk.Notebook` zones (left, center, right, bottom) in nested
panes: `outer` = top | bottom zone, `top` = left zone | `inner`, `inner` =
center zone | right zone. All notebooks share one tab group, so a tab can be
dragged into another zone; the tab's right-click (or Menu key) popover offers
"Move to Left/Center/Right/Bottom" for the same. A zone with no panel or hidden
by the user is not shown, and the panes give its room to the neighbours (GTK
does that for an invisible pane child).

dock_layout.py does the bookkeeping (what a layout is, sizes <-> pane
positions); this module mirrors it into widgets and reports every change as a
layout dict through `on_change` (the window saves it). The widgets are the
truth for the order of tabs and the shown tab (`_read_zones()`), the Dock keeps
the rest (hidden zones, sizes) itself.

Details that are easy to get wrong:
- `show_all()` must not bring hidden zones back: the Dock overrides
  `do_show_all` (it shows the panels and sets visibility itself) and the
  notebooks/panes are never reached by GTK's recursion.
- During a tab drag nothing is hidden (the source notebook must stay mapped
  or the drag breaks); empty zones the user did not hide are shown as drop
  targets for the drag, and everything is settled on `drag-end`.
- Pane positions are only reported when the user dragged a handle (button
  held); programmatic positions (apply_sizes, resizing the window) are not.
- Menus are popovers with `Gtk.ModelButton` rows, never `Gtk.Menu`.
"""
from __future__ import annotations

import copy
from typing import Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from dock_layout import (  # noqa: E402
    DEFAULT_SIZES, ZONES, fractions_from_positions, positions_from_fractions)

GROUP = "chem-assistent-dock"
DROP_TARGET_SIZE = 90  # px: room an empty zone gets while a tab is dragged over the window
ZONE_TITLES = {"left": "Left", "center": "Center", "right": "Right", "bottom": "Bottom"}
ZONE_ICONS = {  # Adwaita symbolic icons (test_icons_are_in_adwaita scans this file)
    "left": "sidebar-show-symbolic",
    "center": "view-dual-symbolic",
    "right": "sidebar-show-right-symbolic",
    "bottom": "go-bottom-symbolic",
}


class Panel:
    """One docked panel: its widget and the tab label shown in a zone."""

    def __init__(self, panel_id: str, title: str, icon_name: str, widget: Gtk.Widget, tooltip: str):
        self.id = panel_id
        self.title = title
        self.widget = widget
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU), False, False, 0)
        box.pack_start(Gtk.Label(label=title), False, False, 0)
        self.label = Gtk.EventBox()  # an event box: right click on the tab
        self.label.set_visible_window(False)
        self.label.add(box)
        if tooltip:
            self.label.set_tooltip_text(tooltip)
        self.label.show_all()


class Dock(Gtk.Box):
    def __init__(self, layout: dict, on_change: Callable[[dict], None]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_change = on_change
        self._layout = copy.deepcopy(layout)  # zones/current/hidden/sizes as last settled
        self.panels: dict[str, Panel] = {}
        self._by_widget: dict[Gtk.Widget, str] = {}
        self._busy = 0  # >0: programmatic changes, no signal handling
        self._dragging = False
        self._pressed: Gtk.Paned | None = None  # the pane whose handle is held
        self._moved = False  # that pane's position changed during the press
        self._size: tuple[int, int] | None = None  # from apply_sizes()
        self._popover: Gtk.Popover | None = None

        self.zones: dict[str, Gtk.Notebook] = {}
        for zone in ZONES:
            notebook = Gtk.Notebook()
            notebook.set_group_name(GROUP)
            notebook.set_scrollable(True)
            notebook.connect("create-window", lambda *_: None)  # dropped outside: nothing
            notebook.connect("page-added", self._pages_changed)
            notebook.connect("page-removed", self._pages_changed)
            notebook.connect("page-reordered", self._pages_changed)
            notebook.connect("switch-page", self._switched, zone)
            notebook.connect("drag-begin", self._drag_begin)
            notebook.connect_after("drag-end", self._drag_end)
            notebook.connect("popup-menu", self._menu_key)
            self.zones[zone] = notebook

        self.outer = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.top = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.panes = {"outer": self.outer, "top": self.top, "inner": self.inner}
        for pane in self.panes.values():
            pane.set_wide_handle(True)
            pane.connect("button-press-event", self._pane_pressed)
            pane.connect("button-release-event", self._pane_released)
            pane.connect("notify::position", self._pane_moved)
        self.outer.pack1(self.top, True, True)
        self.outer.pack2(self.zones["bottom"], False, True)
        self.top.pack1(self.zones["left"], False, True)
        self.top.pack2(self.inner, True, True)
        self.inner.pack1(self.zones["center"], True, True)
        self.inner.pack2(self.zones["right"], False, True)
        self.pack_start(self.outer, True, True, 0)

        self.buttons: dict[str, Gtk.ToggleButton] = {}
        for zone in ZONES:
            button = Gtk.ToggleButton()
            button.set_image(Gtk.Image.new_from_icon_name(ZONE_ICONS[zone], Gtk.IconSize.MENU))
            button.connect("toggled", self._button_toggled, zone)
            self.buttons[zone] = button
        self._settle()

    # -- public API ------------------------------------------------------

    def add_panel(self, panel_id: str, title: str, icon_name: str, widget: Gtk.Widget,
                  tooltip: str = "") -> None:
        """Add a panel in the zone (and at the place) the layout gives it.

        An id the layout does not know goes to the end of the center zone.
        """
        panel = Panel(panel_id, title, icon_name, widget, tooltip)
        panel.label.connect("button-press-event", self._label_pressed, panel_id)
        self.panels[panel_id] = panel
        self._by_widget[widget] = panel_id
        zone = self._zone_in_layout(panel_id)
        if zone is None:
            zone = "center"
            self._layout["zones"][zone].append(panel_id)
        index = self._insert_index(zone, panel_id)
        widget.show()
        with self._quiet():
            self.zones[zone].insert_page(widget, panel.label, index)
            self._prepare_page(zone, widget)
            if self._layout["current"].get(zone) == panel_id:
                self.zones[zone].set_current_page(self.zones[zone].page_num(widget))
        self._settle()

    def move(self, panel_id: str, zone: str) -> None:
        """Put the panel at the end of `zone`, show the zone and the panel."""
        panel = self.panels.get(panel_id)
        if panel is None or zone not in self.zones:
            return
        source = self._zone_of(panel_id)
        with self._quiet():
            if source != zone:
                if source is not None:
                    self.zones[source].detach_tab(panel.widget)
                self.zones[zone].append_page(panel.widget, panel.label)
                self._prepare_page(zone, panel.widget)
            self.zones[zone].set_current_page(self.zones[zone].page_num(panel.widget))
            self._layout["hidden"] = [z for z in self._layout["hidden"] if z != zone]
        self._settle(emit=True)

    def present(self, panel_id: str) -> None:
        """Switch its zone to the panel and show the zone."""
        panel = self.panels.get(panel_id)
        zone = self._zone_of(panel_id)
        if panel is None or zone is None:
            return
        with self._quiet():
            self.zones[zone].set_current_page(self.zones[zone].page_num(panel.widget))
            self._layout["hidden"] = [z for z in self._layout["hidden"] if z != zone]
        self._settle(emit=True)

    def set_zone_visible(self, zone: str, visible: bool) -> bool:
        """Hide or show a zone; False when refused (the last visible zone stays)."""
        if zone not in self.zones:
            return False
        hidden = [z for z in self._layout["hidden"] if z != zone]
        if not visible:
            if not self._shown(zone, hidden=self._layout["hidden"]):
                hidden.append(zone)  # an empty zone: only remember the choice
            else:
                others = [z for z in ZONES if z != zone and self._shown(z, hidden=hidden)]
                if not others:
                    self._sync_buttons()
                    return False
                hidden.append(zone)
        hidden = [z for z in ZONES if z in hidden]
        changed = hidden != self._layout["hidden"]
        self._layout["hidden"] = hidden
        self._settle(emit=changed)
        return True

    def zone_visible(self, zone: str) -> bool:
        return self._shown(zone)

    def toggle_buttons(self) -> list[Gtk.ToggleButton]:
        """One button per zone (left, center, right, bottom) for the header."""
        return [self.buttons[zone] for zone in ZONES]

    def layout(self) -> dict:
        """The current layout (a copy): tabs from the widgets, sizes as last set."""
        result = copy.deepcopy(self._layout)
        zones = self._read_zones()
        for zone in ZONES:  # panels not added yet keep their place
            result["zones"][zone] = zones[zone] + [
                pid for pid in self._layout["zones"][zone] if pid not in self.panels]
        result["current"] = self._read_current()
        return result

    def apply_sizes(self, width: int, height: int) -> None:
        """Set the pane positions from the layout's sizes (after the window grew)."""
        self._size = (width, height)
        self._apply_positions()

    def do_show_all(self) -> None:
        """Show the panels; the zones decide their own visibility."""
        self.show()
        for panel in self.panels.values():
            panel.widget.show_all()
            panel.label.show_all()
        self._settle()

    # -- layout <-> widgets ---------------------------------------------

    def _quiet(self):
        """Context manager: signal handlers do nothing while we change widgets."""
        dock = self

        class Quiet:
            def __enter__(self):
                dock._busy += 1

            def __exit__(self, *_):
                dock._busy -= 1
        return Quiet()

    def _insert_index(self, zone: str, panel_id: str) -> int:
        """Where in the zone's notebook the panel goes so that it follows the layout's order."""
        order = self._layout["zones"][zone]
        present = self._read_zones()[zone]
        if panel_id not in order or any(other not in order for other in present):
            return -1
        return sum(1 for other in present if order.index(other) < order.index(panel_id))

    def _zone_in_layout(self, panel_id: str) -> str | None:
        for zone in ZONES:
            if panel_id in self._layout["zones"][zone]:
                return zone
        return None

    def _zone_of(self, panel_id: str) -> str | None:
        panel = self.panels.get(panel_id)
        if panel is None:
            return None
        for zone, notebook in self.zones.items():
            if notebook.page_num(panel.widget) >= 0:
                return zone
        return None

    def _read_zones(self) -> dict[str, list[str]]:
        """Panel ids per zone as the notebooks hold them."""
        result: dict[str, list[str]] = {}
        for zone, notebook in self.zones.items():
            result[zone] = [self._by_widget[notebook.get_nth_page(i)]
                            for i in range(notebook.get_n_pages())]
        return result

    def _read_current(self) -> dict[str, str]:
        current = {}
        for zone, notebook in self.zones.items():
            page = notebook.get_nth_page(notebook.get_current_page())
            if page is not None:
                current[zone] = self._by_widget[page]
        return current

    def _prepare_page(self, zone: str, widget: Gtk.Widget) -> None:
        notebook = self.zones[zone]
        notebook.set_tab_reorderable(widget, True)
        notebook.set_tab_detachable(widget, True)

    def _shown(self, zone: str, hidden: list[str] | None = None) -> bool:
        """Should the zone be on screen: not hidden and a panel in it (or room for a drop)."""
        hidden = self._layout["hidden"] if hidden is None else hidden
        if zone in hidden:
            return False
        return self.zones[zone].get_n_pages() > 0 or self._dragging

    def _settle(self, emit: bool = False) -> None:
        """Mirror layout state into widgets (visibility, buttons, positions)."""
        if self._busy or self._dragging:
            return
        self._layout["zones"].update(self._merged_zones())
        self._layout["current"] = self._read_current()
        if not any(self.zones[z].get_n_pages() for z in ZONES if z not in self._layout["hidden"]) \
                and any(self.zones[z].get_n_pages() for z in ZONES):
            self._layout["hidden"] = []  # never an invisible window
        self._update_visibility()
        self._sync_buttons()
        self._apply_positions()
        if emit:
            self._emit()

    def _merged_zones(self) -> dict[str, list[str]]:
        zones = self._read_zones()
        return {zone: zones[zone] + [pid for pid in self._layout["zones"][zone]
                                     if pid not in self.panels]
                for zone in ZONES}

    def _update_visibility(self) -> None:
        shown = {zone: self._shown(zone) for zone in ZONES}
        for zone, notebook in self.zones.items():
            empty_target = self._dragging and notebook.get_n_pages() == 0
            notebook.set_size_request(DROP_TARGET_SIZE if empty_target else -1,
                                      DROP_TARGET_SIZE if empty_target else -1)
            notebook.set_visible(shown[zone])
        self.inner.set_visible(shown["center"] or shown["right"])
        self.top.set_visible(shown["left"] or self.inner.get_visible())
        self.outer.set_visible(self.top.get_visible() or shown["bottom"])

    def _sync_buttons(self) -> None:
        shown = [zone for zone in ZONES if self._shown(zone) and self.zones[zone].get_n_pages()]
        with self._quiet():
            for zone, button in self.buttons.items():
                empty = self.zones[zone].get_n_pages() == 0
                button.set_active(not empty and zone not in self._layout["hidden"])
                last = shown == [zone]
                button.set_sensitive(not empty and not last)
                name = ZONE_TITLES[zone].lower()
                if empty:
                    button.set_tooltip_text("Empty: drag a panel here")
                elif last:
                    button.set_tooltip_text(f"The {name} container is the last one visible")
                else:
                    button.set_tooltip_text(f"Show/hide the {name} container")

    def _button_toggled(self, button: Gtk.ToggleButton, zone: str) -> None:
        if self._busy:
            return
        if not self.set_zone_visible(zone, button.get_active()):
            self._sync_buttons()

    def _emit(self) -> None:
        if not self._busy:
            self.on_change(self.layout())

    # -- sizes ------------------------------------------------------------

    def _dock_size(self) -> tuple[int, int]:
        width, height = self.get_allocated_width(), self.get_allocated_height()
        if width > 1 and height > 1:
            return width, height
        return self._size or (0, 0)

    def _apply_positions(self) -> None:
        """Set the pane positions from the layout's sizes (only after apply_sizes)."""
        if self._size is None:
            return
        width, height = self._dock_size()
        if width <= 0 or height <= 0:
            return
        positions = positions_from_fractions(self._layout["sizes"], width, height)
        if not self._shown("left"):  # right is measured against the whole width
            positions["inner"] += positions["top"]
        with self._quiet():
            for name, pane in self.panes.items():
                if pane.get_position() != positions[name]:
                    pane.set_position(positions[name])

    def _pane_pressed(self, pane: Gtk.Paned, event) -> bool:
        if event.button == 1:
            self._pressed, self._moved = pane, False
        return False

    def _pane_moved(self, pane: Gtk.Paned, _param) -> None:
        if not self._busy and pane is self._pressed:
            self._moved = True

    def _pane_released(self, pane: Gtk.Paned, event) -> bool:
        if pane is not self._pressed or event.button != 1:
            return False
        moved, self._pressed, self._moved = self._moved, None, False
        if moved and self._size is not None:
            GLib.idle_add(self._pane_dragged, pane)
        return False

    def _pane_dragged(self, pane: Gtk.Paned) -> bool:
        """Take the size the user dragged to, clamp it and settle the others."""
        width, height = self._dock_size()
        positions = {name: p.get_position() for name, p in self.panes.items()}
        if not self._shown("left"):
            positions["top"] = 0
        measured = fractions_from_positions(positions, width, height)
        key = {"outer": "bottom", "top": "left", "inner": "right"}[
            next(name for name, p in self.panes.items() if p is pane)]
        both = {"outer": self.top.get_visible() and self._shown("bottom"),
                "top": self._shown("left") and self.inner.get_visible(),
                "inner": self._shown("center") and self._shown("right")}[
            next(name for name, p in self.panes.items() if p is pane)]
        if both:
            self._layout["sizes"][key] = measured[key]
            self._apply_positions()
            self._emit()
        return False

    # -- tab signals ------------------------------------------------------

    def _pages_changed(self, notebook, *_args) -> None:
        if self._busy:
            return
        if self._dragging:
            return  # settled by drag-end
        GLib.idle_add(self._settle_idle)

    def _settle_idle(self) -> bool:
        self._settle(emit=True)
        return False

    def _switched(self, notebook, page, _num, zone) -> None:
        if self._busy or self._dragging:
            return
        # The notebook's current page is not updated yet inside this signal.
        panel_id = self._by_widget.get(page)
        if panel_id is not None and self._layout["current"].get(zone) != panel_id:
            self._layout["current"][zone] = panel_id
            self._emit_current(zone, panel_id)

    def _emit_current(self, zone: str, panel_id: str) -> None:
        if self._busy:
            return
        result = self.layout()
        result["current"][zone] = panel_id
        self.on_change(result)

    def _drag_begin(self, *_args) -> None:
        self._dragging = True
        self._update_visibility()  # empty zones become drop targets
        self._apply_positions()

    def _drag_end(self, *_args) -> None:
        # The drop's page-added/-removed have run; settle when the drag is over.
        GLib.idle_add(self._drag_done)

    def _drag_done(self) -> bool:
        self._dragging = False
        self._settle(emit=True)
        return False

    # -- tab menu -----------------------------------------------------------

    def _label_pressed(self, label, event, panel_id: str) -> bool:
        if event.button == 3:
            self._open_menu(panel_id)
            return True
        return False

    def _menu_key(self, notebook: Gtk.Notebook) -> bool:
        page = notebook.get_nth_page(notebook.get_current_page())
        panel_id = self._by_widget.get(page)
        if panel_id is not None:
            self._open_menu(panel_id)
        return True

    def _open_menu(self, panel_id: str) -> None:
        panel = self.panels[panel_id]
        current = self._zone_of(panel_id)
        popover = Gtk.Popover(relative_to=panel.label)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6)
        for zone in ZONES:
            item = Gtk.ModelButton(text=f"Move to {ZONE_TITLES[zone]}")
            item.set_sensitive(zone != current)
            item.connect("clicked", lambda _item, z=zone: self._menu_chosen(popover, panel_id, z))
            box.pack_start(item, False, False, 0)
        box.show_all()
        popover.add(box)
        self._popover = popover
        popover.popup()

    def _menu_chosen(self, popover: Gtk.Popover, panel_id: str, zone: str) -> None:
        popover.popdown()
        GLib.idle_add(lambda: self.move(panel_id, zone) and False)  # idle: may rebuild widgets
