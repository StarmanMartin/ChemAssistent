"""The docking layout (no GTK, stdlib): which panel sits in which zone.

The conversation page has four zones (left, center, right, bottom); every
panel lives in exactly one. The layout is a small JSON-able dict (also what
the window saves in the config):

    {"zones": {"left": [ids], "center": [...], "right": [...], "bottom": [...]},
     "current": {zone: id},      # the tab shown in a zone (optional per zone)
     "hidden": [zone, ...],      # zones the user hid
     "sizes": {"left": f, "right": f, "bottom": f}}   # fractions of the window

`normalize_layout()` turns anything (a saved value from an older version, a
hand-edited or broken config) into that shape and never raises, so the window
can always start. dock.py mirrors the result into widgets.

The sizes are fractions, the paned widgets want pixels:
`positions_from_fractions()` and `fractions_from_positions()` convert for the
three nested panes (outer = top | bottom zone, top = left zone | inner,
inner = center zone | right zone). The right zone is measured against the
whole window width, so hiding the left zone does not resize it.
"""
from __future__ import annotations

import math

ZONES = ("left", "center", "right", "bottom")
DEFAULT_SIZES = {"left": 0.22, "right": 0.32, "bottom": 0.40}
MIN_SIZE, MAX_SIZE = 0.08, 0.85


def _is_number(value) -> bool:
    """A real number: bool is an int in Python but never a size."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp(value: float) -> float:
    return max(MIN_SIZE, min(MAX_SIZE, value))


def _size(value, default: float) -> float:
    if not _is_number(value) or math.isnan(value):
        return default
    return _clamp(float(value))


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _known_id(value, panels: dict) -> bool:
    """A panel id we know (the type check keeps lists/dicts out of `in`)."""
    return isinstance(value, str) and value in panels


def normalize_layout(saved, panels: dict[str, str]) -> dict:
    """A valid layout for `panels` ({id: default zone}) from any `saved` value."""
    defaults = {pid: (zone if zone in ZONES else "center") for pid, zone in panels.items()}
    saved = _dict(saved)
    saved_zones = _dict(saved.get("zones"))
    zones: dict[str, list[str]] = {zone: [] for zone in ZONES}
    placed: set[str] = set()
    for zone in ZONES:
        ids = saved_zones.get(zone)
        for pid in ids if isinstance(ids, list) else []:
            if _known_id(pid, defaults) and pid not in placed:
                zones[zone].append(pid)
                placed.add(pid)
    for pid, zone in defaults.items():  # missing panels: end of their default zone
        if pid not in placed:
            zones[zone].append(pid)

    saved_current = _dict(saved.get("current"))
    current = {zone: saved_current[zone] for zone in ZONES
               if isinstance(saved_current.get(zone), str) and saved_current[zone] in zones[zone]}

    saved_hidden = saved.get("hidden")
    saved_hidden = saved_hidden if isinstance(saved_hidden, list) else []
    hidden = [zone for zone in ZONES if zone in saved_hidden]
    if all(zone in hidden for zone in ZONES if zones[zone]):
        hidden = []  # never an invisible window

    saved_sizes = _dict(saved.get("sizes"))
    sizes = {key: _size(saved_sizes.get(key), default) for key, default in DEFAULT_SIZES.items()}
    return {"zones": zones, "current": current, "hidden": hidden, "sizes": sizes}


def default_layout(panels: dict[str, str]) -> dict:
    """The layout with every panel in its default zone."""
    return normalize_layout(None, panels)


def positions_from_fractions(sizes: dict, width: float, height: float) -> dict[str, int]:
    """Pixel positions of the paned widgets for the zone `sizes`."""
    left = _size(sizes.get("left"), DEFAULT_SIZES["left"])
    right = _size(sizes.get("right"), DEFAULT_SIZES["right"])
    bottom = _size(sizes.get("bottom"), DEFAULT_SIZES["bottom"])
    top = width * left
    return {
        "outer": round(height * (1 - bottom)),
        "top": round(top),
        "inner": round((width - top) - width * right),
    }


def fractions_from_positions(positions: dict, width: float, height: float) -> dict[str, float]:
    """The inverse of positions_from_fractions(), clamped to MIN_SIZE..MAX_SIZE."""
    if width <= 0 or height <= 0:
        return dict(DEFAULT_SIZES)
    return {
        "left": _clamp(positions["top"] / width),
        "right": _clamp((width - positions["top"] - positions["inner"]) / width),
        "bottom": _clamp(1 - positions["outer"] / height),
    }
