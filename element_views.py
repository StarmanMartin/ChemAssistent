"""View models of Chemotion elements for the Details panel (no GTK, stdlib
only, no chemotion_api): the raw server JSON of an element (`json_data`) in,
a plain dict out that the panel only has to lay out.

    {"type", "id", "updated_at", "title", "subtitle", "url", "image": {"mime", "data"} | None,
     "sections": [{"title", "kind": "fields", "rows": [{"label", "value", "unit"}]}
                  | {"title", "kind": "table", "columns": [...], "rows": [[...]],
                     "targets": [{"type", "id"} | None, ...]}      # targets: optional, one per row
                  | {"title", "kind": "text", "text", "markup"}   # markup: optional Pango markup
                  | {"title", "kind": "image", "image": IMAGE}],
     "notes": [...]}

    IMAGE = {"mime": "image/svg+xml", "data": "<svg...>"}
          | {"mime": "image/png" | "image/jpeg" | "image/gif", "data": "<base64>", "encoding": "base64"}

Every value is a string, already formatted; empty values and empty sections
are left out here, never in the panel. A type without an adapter gives the
view of a generic element (element_generic.py) when its JSON has layers, else the
stub `{"type", "id", "unsupported": True}`; the generic segments of any element are appended as sections.

Adapters are `(data, images, url) -> view model` in `ADAPTERS`; `IMAGE_REFS[type](data)`
names the server paths the helper loads for it (`{key: "/images/..."}`, the loaded IMAGEs
come back as `images[key]`). More types are added to both dicts by element_types.py.
The shared builders (`fields`, `table`, `text`, `image_section`, `delta_markup`) are for
every adapter.

The sample follows the web client (ELN 4.0.1, details/samples/SampleDetails.js
and propertiesTab/SampleForm.js): its labels, grouping, order, units and the
derivation of the amounts (Sample.js `amount_g`/`amount_l`/`amount_mol`). The
JSON of ELN 3.1.x (chemotion_api v312) and 4.x is handled by the presence of
keys, no version switch.
"""
from __future__ import annotations

import json
import math
from collections.abc import Callable
from html import escape
from typing import Any

MAX_IMAGE_BYTES = 2_000_000
IMAGE_MIME = "image/svg+xml"
# Web client: MWPrecision (SampleDetails.js) and the numInput precisions of the amounts.
MW_DIGITS = 6
PRECISION = {"g": 4, "l": 5, "mol": 4}
# The metric prefix letters of Sample.metrics (utilities/metricPrefix.js): factor and symbol.
PREFIXES = {"m": (1e3, "m"), "u": (1e6, "µ"), "n": (1.0, "")}
IMAGE_TOO_LARGE = "The structure image is larger than 2 MB and is not shown."  # = image_too_large("structure")


def fmt_number(value: Any, digits: int = 4) -> str:
    """`value` with at most `digits` decimals and no trailing zeros ("" for
    None and ""; integers stay as they are; text that is no number is kept)."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            value = float(text)
        except ValueError:
            return text
        if text.lstrip("+-").isdigit():
            value = int(text)
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        return ""
    text = f"{value:.{max(digits, 0)}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def delta_text(value: Any) -> str:
    """Plain text of a Quill delta (`{"ops": [{"insert": …}]}`, also as JSON
    text) or of a string; embeds (images, formulas) are left out. The line
    breaks stay as they are stored."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
        if text.lstrip().startswith("{") and '"ops"' in text:
            try:
                value = json.loads(text, strict=False)
            except ValueError:
                return text.strip()
        else:
            return text.strip()
    if isinstance(value, dict):
        value = value.get("ops", value.get("insert"))
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for op in value:
            insert = op.get("insert") if isinstance(op, dict) else op
            if isinstance(insert, str):
                parts.append(insert)
        return "".join(parts).strip()
    return ""


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return fmt_number(value, MW_DIGITS)
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _row(label: str, value: Any, unit: str = "") -> dict | None:
    text = value if isinstance(value, str) else _text(value)
    text = text.strip()
    if not text:
        return None
    return {"label": label, "value": text, "unit": unit}


def fields(title: str, rows: list) -> dict | None:
    """A "fields" section from (label, value, unit) tuples (the unit may be
    left out; a row can also be the dict `_row()` makes). Empty values and
    None rows are dropped; None when nothing is left."""
    made = []
    for row in rows:
        if isinstance(row, (tuple, list)):
            row = _row(*row)
        if row:
            made.append(row)
    return {"title": title, "kind": "fields", "rows": made} if made else None


def table(title: str, columns: list[str], rows: list[list], targets: list | None = None) -> dict | None:
    """A "table" section; cells become text, rows without any text are dropped
    (their `targets` entry with them). `targets` = one `{"type", "id"}` or
    None per row: a row with a target is clickable in the panel."""
    kept, kept_targets = [], []
    for index, row in enumerate(rows):
        cells = [cell if isinstance(cell, str) else _text(cell) for cell in row]
        if any(cell.strip() for cell in cells):
            kept.append(cells)
            kept_targets.append(targets[index] if targets and index < len(targets) else None)
    if not kept:
        return None
    section: dict[str, Any] = {"title": title, "kind": "table", "columns": list(columns), "rows": kept}
    if any(kept_targets):
        section["targets"] = kept_targets
    return section


def text(title: str, value: Any) -> dict | None:
    """A "text" section from a Quill delta (also as JSON text) or a string:
    `text` is the plain text, `markup` the Pango markup; None when empty."""
    plain = delta_text(value)
    if not plain:
        return None
    return {"title": title, "kind": "text", "text": plain, "markup": delta_markup(value)}


def image_section(title: str, image: dict | None) -> dict | None:
    """An "image" section from an IMAGE dict (None when there is none)."""
    if not image or not image.get("data"):
        return None
    return {"title": title, "kind": "image", "image": image}


_fields = fields
_table = table


# -- Quill delta -> Pango markup ------------------------------------------------

LINK_SCHEMES = ("http://", "https://", "mailto:")
INLINE_TAGS = (("bold", "b"), ("italic", "i"), ("underline", "u"), ("strike", "s"), ("code", "tt"))


def _span_markup(chunk: str, attributes: dict) -> str:
    """One run of text with its inline attributes (Quill: bold, italic,
    underline, strike, script sub/super, code, link)."""
    out = escape(chunk, quote=False)
    if not out:
        return ""
    opened, closed = [], []
    for key, tag in INLINE_TAGS:
        if attributes.get(key):
            opened.append(f"<{tag}>")
            closed.append(f"</{tag}>")
    script = attributes.get("script")
    if script in ("sub", "super"):
        tag = "sub" if script == "sub" else "sup"
        opened.append(f"<{tag}>")
        closed.append(f"</{tag}>")
    link = attributes.get("link")
    if isinstance(link, str) and link.strip().lower().startswith(LINK_SCHEMES):
        opened.append(f'<a href="{escape(link.strip(), quote=True)}">')
        closed.append("</a>")
    return "".join(opened) + out + "".join(reversed(closed))


def _delta_ops(value: Any) -> list | None:
    """The ops list of a delta / JSON text of one; None for a plain string."""
    if isinstance(value, str):
        stripped = value.lstrip()
        if not (stripped.startswith("{") and '"ops"' in stripped):
            return None
        try:
            value = json.loads(value, strict=False)
        except ValueError:
            return None
    if isinstance(value, dict):
        value = value.get("ops")
    return value if isinstance(value, list) else None


def delta_markup(value: Any) -> str:
    """Pango markup of a Quill delta (`{"ops": [...]}`, also as JSON text) or
    a plain string, everything escaped. Paragraphs are lines, headers bold
    lines, bullet/ordered lists get "• " / "1. " (indented by `indent`),
    code blocks and quotes stay lines; bold, italic, underline, strike,
    sub/superscript, code and http(s)/mailto links are tags; embeds (images,
    formulas) are left out. Empty lines at the end are dropped."""
    if value is None or isinstance(value, bool):
        return ""
    ops = _delta_ops(value)
    if ops is None:
        if isinstance(value, (dict, list)):
            return ""
        return escape(str(value).strip(), quote=False)
    lines: list[tuple[str, dict]] = []
    current = ""
    for op in ops:
        insert = op.get("insert") if isinstance(op, dict) else op
        if not isinstance(insert, str):
            continue  # an embed
        attributes = op.get("attributes") if isinstance(op, dict) and isinstance(op.get("attributes"), dict) else {}
        parts = insert.split("\n")
        for index, part in enumerate(parts):
            current += _span_markup(part, attributes)
            if index < len(parts) - 1:  # a line ends: its attributes are on this "\n"
                lines.append((current, attributes))
                current = ""
    if current:
        lines.append((current, {}))
    out: list[str] = []
    counters: dict[int, int] = {}
    for body, attributes in lines:
        list_kind = attributes.get("list")
        indent = attributes.get("indent") if isinstance(attributes.get("indent"), int) else 0
        if list_kind:
            counters = {level: n for level, n in counters.items() if level <= indent}
            if list_kind == "ordered":
                counters[indent] = counters.get(indent, 0) + 1
                marker = f"{counters[indent]}. "
            else:
                marker = "• "
            body = "    " * indent + marker + body
        else:
            counters = {}
            header = attributes.get("header")
            if header and body:
                body = f"<b>{body}</b>"
        out.append(body)
    while out and not out[-1].strip():
        out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out)


def stub(type_name: str, data: dict | None = None) -> dict:
    return {"type": type_name, "id": (data or {}).get("id"), "unsupported": True}


# adapter: the raw JSON, the loaded images ({key: IMAGE}) and the element's url -> view model
Adapter = Callable[[dict, dict, str], dict]
# which images to load for an element: its JSON -> {key: relative server path}
Refs = Callable[[dict], dict]
ADAPTERS: dict[str, Adapter] = {}
IMAGE_REFS: dict[str, Refs] = {}
# Set by element_generic.py: the adapter for a type without one whose JSON has layers (a LabIMotion
# generic element) and the sections of an element's generic segments.
GENERIC_ADAPTER: Adapter | None = None
SEGMENT_SECTIONS: Callable[[dict], list] | None = None
SUPPORTED = ADAPTERS.keys()  # a live view: types added to ADAPTERS count at once
# What the notes call each type's image ("The structure image could not be loaded.").
IMAGE_NAMES = {"sample": "structure", "reaction": "scheme"}


def view(type_name: str, data: dict, images: dict | str | None = None, url: str = "") -> dict:
    """The view model of one element from its raw JSON. `images` = the loaded
    IMAGEs by key (see IMAGE_REFS); a str is SVG text for the key "image"
    (a sample's structure). A type without an adapter gives the generic view
    when the JSON carries layers (element_generic.py), else the stub. The
    element's generic segments are appended as sections; every model gets
    `short_label` and `name` (for the mention of a view opened from a link)."""
    adapter = ADAPTERS.get(type_name)
    if adapter is None:
        if GENERIC_ADAPTER is not None and isinstance(data, dict) and _has_layers(data):
            adapter = GENERIC_ADAPTER
        else:
            return stub(type_name, data)
    if isinstance(images, str):
        images = {"image": {"mime": IMAGE_MIME, "data": images}}
    model = adapter(data, images or {}, url)
    if SEGMENT_SECTIONS is not None and isinstance(data, dict) and data.get("segments"):
        extra = SEGMENT_SECTIONS(data)
        if extra:
            model["sections"] = list(model.get("sections") or []) + extra
    # What "Mention in message" of a linked view names (details_logic.linked_mention()).
    if isinstance(data, dict):
        model.setdefault("short_label", str(data.get("short_label") or ""))
        model.setdefault("name", str(data.get("name") or "") or str(model.get("title") or ""))
    return model


def _has_layers(data: dict) -> bool:
    properties = data.get("properties")
    return isinstance(properties, dict) and isinstance(properties.get("layers"), dict)


def _image(image: Any, notes: list[str], name: str = "structure") -> dict | None:
    """An IMAGE for the model from SVG text or an IMAGE dict; one over the
    size limit is left out with a note."""
    if not image:
        return None
    if isinstance(image, str):
        image = {"mime": IMAGE_MIME, "data": image}
    data = image.get("data") or ""
    if not data:
        return None
    if len(data.encode("utf-8")) > MAX_IMAGE_BYTES:
        notes.append(image_too_large(name))
        return None
    return image


def image_too_large(name: str) -> str:
    return f"The {name} image is larger than 2 MB and is not shown."


# -- sample -------------------------------------------------------------------

def sample_title(data: dict) -> str:
    """The name the ELN shows: a sample without a name shows its molecule."""
    name = data.get("name") or data.get("showed_name") or ""
    molecule = data.get("molecule") if isinstance(data.get("molecule"), dict) else {}
    if not str(name).strip():
        name = molecule.get("iupac_name") or ""
    return str(name).strip()


def _formula(data: dict, molecule: dict) -> str:
    """Sample.js molecule_formula."""
    if data.get("decoupled"):
        return str(data.get("sum_formula") or "").strip()
    if data.get("sample_type") == "Mixture":
        return "mixture structure"
    return str(molecule.get("sum_formular") or molecule.get("sum_formula") or "").strip()


def _molecular_weight(data: dict, molecule: dict) -> float | None:
    """Sample.js molecule_molecular_weight (a decoupled sample has its own mass)."""
    if data.get("decoupled"):
        return _number(data.get("molecular_mass")) or None
    return _number(molecule.get("molecular_weight")) or None


def _stored_amount(data: dict, kind: str) -> tuple[float | None, str]:
    value = _number(data.get(f"{kind}_amount_value"))
    unit = str(data.get(f"{kind}_amount_unit") or "g")
    return value, unit


def derived_amounts(data: dict, value: float, unit: str) -> dict[str, float | None]:
    """Mass (g), volume (l) and substance amount (mol) of a stored amount, as
    the web client derives them (Sample.js convertToGram/convertGramToUnit,
    ordinary samples; no gas, feedstock, mixture or polymer)."""
    molecule = data.get("molecule") if isinstance(data.get("molecule"), dict) else {}
    mw = _molecular_weight(data, molecule)
    purity = _number(data.get("purity")) or 1.0
    density = _number(data.get("density")) or 0.0
    molarity = _number(data.get("molarity_value")) or 0.0
    has_molarity = molarity > 0 and density == 0
    has_density = density > 0 and molarity == 0
    out: dict[str, float | None] = {"g": None, "l": None, "mol": None}
    if data.get("sample_type") == "Mixture" or data.get("residues") or data.get("_contains_residues"):
        out[{"g": "g", "mg": "g", "l": "l", "mol": "mol"}.get(unit, "g")] = value / 1000.0 if unit == "mg" else value
        return out
    gram: float | None
    if unit == "g":
        gram = value
    elif unit == "mg":
        gram = value / 1000.0
    elif unit == "l":
        out["l"] = value
        if has_molarity and mw:
            gram = value * molarity * mw
        elif has_density:
            gram = value * density * 1000
        else:
            gram = None
    elif unit == "mol":
        out["mol"] = value
        gram = (value / purity) * mw if mw else None
    else:
        gram = value
    out["g"] = gram
    if gram is None:
        return out
    if out["l"] is None:
        if has_molarity and mw:
            out["l"] = (gram * purity) / (molarity * mw)
        elif has_density:
            out["l"] = gram / (density * 1000)
    if out["mol"] is None and mw:
        out["mol"] = out["l"] * molarity if has_molarity and out["l"] is not None else (gram * purity) / mw
    return out


def _scaled(value: float | None, unit: str, prefix: str, digits: int) -> tuple[str, str]:
    if value is None:
        return "", ""
    factor, symbol = PREFIXES.get(prefix, PREFIXES["m"])
    return fmt_number(value * factor, digits), f"{symbol}{unit}"


def _amount_rows(data: dict, kind: str) -> list:
    value, unit = _stored_amount(data, kind)
    if not value:
        return []
    metrics = str(data.get("metrics") or "mmmm")
    metrics = metrics if len(metrics) > 3 else "mmmm"
    amounts = derived_amounts(data, value, unit)
    rows = []
    for key, label, prefix in (("g", "Mass", metrics[0]), ("l", "Volume", metrics[3]),
                               ("mol", "Amount of substance", metrics[2])):
        number, shown_unit = _scaled(amounts[key], key, prefix, PRECISION[key])
        rows.append(_row(label, number, shown_unit))
    return rows


def _range(data: dict, field: str) -> str:
    """melting_point/boiling_point: "121.0..123.0" (Postgres range as text) or
    the *_lowerbound/_upperbound pair; Sample.js shows `lower – upper`."""
    xref = data.get("xref") if isinstance(data.get("xref"), dict) else {}
    label = xref.get(f"{field}_label")
    if label:
        return str(label)
    lower = data.get(f"{field}_lowerbound")
    upper = data.get(f"{field}_upperbound")
    raw = data.get(field)
    if lower is None and upper is None and isinstance(raw, str) and ".." in raw:
        low, _, high = raw.replace("...", "..").partition("..")
        lower, upper = low, high
    elif lower is None and upper is None and raw not in (None, ""):
        lower = upper = raw
    low_n, high_n = _number(lower), _number(upper)
    if low_n is None and high_n is None:
        return ""
    if low_n is None:
        return f"<{fmt_number(high_n)}"
    if high_n is None or high_n == low_n:
        return fmt_number(low_n)
    return f"{fmt_number(low_n)} – {fmt_number(high_n)}"


def _flash_point(value: Any) -> tuple[str, str]:
    """xref.flash_point = {"value", "unit"} (the web client defaults to °C)."""
    if isinstance(value, dict):
        return fmt_number(value.get("value")), str(value.get("unit") or "\u00b0C")
    return fmt_number(value), "\u00b0C"


def _yes(value: Any) -> str:
    return "yes" if value is True else ""


def _user_labels(data: dict) -> str:
    names = []
    for label in data.get("user_labels") or []:
        if isinstance(label, dict):
            name = label.get("title") or label.get("name")
            if name:
                names.append(str(name))
    return ", ".join(names)


def _solvent_section(data: dict) -> dict | None:
    solvents = data.get("solvent")
    if isinstance(solvents, dict):
        solvents = [solvents]
    if not isinstance(solvents, list):
        return None
    entries = [item if isinstance(item, dict) else {"label": str(item)} for item in solvents if item]
    if not entries:
        return None
    mixture = data.get("sample_type") == "Mixture"
    columns = ["Label"]
    if mixture and any(_number(item.get("amount_l")) for item in entries):
        columns.append("Volume")
    columns.append("Ratio")
    if any(item.get("purity") not in (None, "") for item in entries):
        columns.append("Purity")
    if any(item.get("vendor") for item in entries):
        columns.append("Vendor")
    rows = []
    for item in entries:
        cells = {"Label": _text(item.get("label")), "Ratio": _text(item.get("ratio")),
                 "Purity": _text(item.get("purity")), "Vendor": _text(item.get("vendor")),
                 "Volume": ""}
        liters = _number(item.get("amount_l"))
        if liters:
            cells["Volume"] = f"{fmt_number(liters * 1000, 3)} ml"
        rows.append([cells[column] for column in columns])
    return _table("Solvents", columns, rows)


def _composition_section(data: dict) -> dict | None:
    """Elemental composition (calculated from the formula, found by analysis)."""
    found: dict[str, Any] = {}
    calculated: dict[str, Any] = {}
    for item in data.get("elemental_compositions") or []:
        if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
            continue
        kind = item.get("composition_type")
        if kind == "found":
            found = item["data"]
        elif kind == "formula":
            calculated = item["data"]
    elements = list(calculated) + [element for element in found if element not in calculated]
    rows = [[element, f"{_text(calculated.get(element))} %" if calculated.get(element) else "",
             f"{_text(found.get(element))} %" if found.get(element) else ""] for element in elements]
    return _table("Elemental composition", ["Element", "Calculated", "Found"], rows)


# Keys of `xref` the web client shows in its own field (or not at all); the rest are custom fields.
XREF_KNOWN = {"cas", "optical_rotation", "rfvalue", "rfsovents", "supplier", "private_notes", "color",
              "moisture", "particle_size", "physical_state", "form", "solubility", "refractive_index",
              "flash_point", "inventory_label", "melting_point_decomposed"}


def _custom_fields(xref: dict) -> list:
    rows = []
    for key, value in xref.items():
        if key in XREF_KNOWN or key.endswith("_label") or isinstance(value, (dict, list)):
            continue
        rows.append(_row(str(key), value))
    return rows


def sample_view(data: dict, image: str | dict | None = None, url: str = "") -> dict:
    """The view model of a sample (see the module docstring)."""
    molecule = data.get("molecule") if isinstance(data.get("molecule"), dict) else {}
    xref = data.get("xref") if isinstance(data.get("xref"), dict) else {}
    stereo = data.get("stereo") if isinstance(data.get("stereo"), dict) else {}
    mixture = data.get("sample_type") == "Mixture"
    notes: list[str] = []
    formula = _formula(data, molecule)
    mw = _molecular_weight(data, molecule)
    exact = None if data.get("decoupled") else _number(molecule.get("exact_molecular_weight"))
    short_label = str(data.get("short_label") or "").strip()

    molecule_name = ""
    name_hash = data.get("molecule_name_hash")
    if isinstance(name_hash, dict):
        molecule_name = str(name_hash.get("label") or name_hash.get("value") or "")
    molecule_name = molecule_name or str(molecule.get("iupac_name") or "")

    cas = str(xref.get("cas") or "").strip()
    if not cas and isinstance(molecule.get("cas"), list):
        cas = ", ".join(str(item) for item in molecule["cas"] if item)

    sections = [
        # SampleForm: sample type, molecule name, name, stereo, labels, location, anhydrous
        _fields("Sample", [
            _row("Sample type", data.get("sample_type")),
            _row("Molecule name", molecule_name),
            _row("Sample name", data.get("name")),
            _row("Stereo abs", stereo.get("abs")),
            _row("Stereo rel", stereo.get("rel")),
            _row("Short label", short_label),
            _row("External label", data.get("external_label")),
            _row("Inventory label", xref.get("inventory_label") if data.get("inventory_sample") else ""),
            _row("Location", data.get("location")),
            _row("Anhydrous", _yes(data.get("dry_solvent"))),
            _row("Decoupled", _yes(data.get("decoupled"))),
            _row("Labels", _user_labels(data)),
        ]),
        # SampleDetails.sampleInfo: molecular weight, exact mass, CAS; SampleForm: sum formula of a decoupled sample
        _fields("Molecule", [
            _row("Sum formula", formula),
            _row("Molecular mass", fmt_number(mw, MW_DIGITS) if mw else "", "g/mol"),
            _row("Exact mass", fmt_number(exact, MW_DIGITS) if exact else "", "g/mol"),
            _row("CAS", cas),
        ]),
        _fields("Amount (target)", _amount_rows(data, "target")),
        _fields("Amount (real)", _amount_rows(data, "real")),
        _fields("Purity and density", [
            _row("Purity/Concentration", fmt_number(data.get("purity"), 5)),
            _row("Density", fmt_number(data.get("density"), 5) if (_number(data.get("density")) or 0) > 0 else "",
                 "g/ml"),
            _row("Molarity", fmt_number(data.get("molarity_value"), 5)
                 if (_number(data.get("molarity_value")) or 0) > 0 else "", str(data.get("molarity_unit") or "M")),
        ]),
        # SampleForm: form, color, soluble in, moisture, particle size, physical state, melting/boiling/flash point
        _fields("Physical properties", [
            _row("Form", xref.get("form")),
            _row("Color", xref.get("color")),
            _row("Soluble in", xref.get("solubility")),
            _row("Moisture", fmt_number(xref.get("moisture")), "%"),
            _row("Particle size", fmt_number(xref.get("particle_size")), "µm"),
            _row("Physical state", str(xref.get("physical_state") or "").capitalize()),
            _row("Melting point", "Decomposed" if xref.get("melting_point_decomposed") and not _range(
                data, "melting_point") else _range(data, "melting_point"), "°C"),
            _row("Boiling point", _range(data, "boiling_point"), "°C"),
            _row("Flash point", *_flash_point(xref.get("flash_point"))),
            _row("Refractive index", xref.get("refractive_index")),
        ]),
        _fields("Other fields", _custom_fields(xref)),
        _solvent_section(data),
    ]
    sections.append(text("Description", data.get("description")))
    sections += [
        # SampleDetails: the "Chemical identifiers" accordion (InChI, canonical SMILES) and the elemental composition
        _fields("Chemical identifiers", [
            _row("InChIKey", molecule.get("inchikey") if molecule.get("inchikey") != "DUMMY" else ""),
            _row("InChI", molecule.get("inchistring")),
            _row("Canonical SMILES", molecule.get("cano_smiles")),
        ]),
        None if mixture or not formula else _composition_section(data),
    ]
    if mixture:
        notes.append("The components of a mixture are not shown yet.")
    subtitle = " · ".join(part for part in (short_label, formula if not mixture else "") if part)
    return {"type": "sample", "id": data.get("id"), "updated_at": str(data.get("updated_at") or ""),
            "title": sample_title(data), "subtitle": subtitle, "url": url or "",
            "image": _image(image, notes), "sections": [section for section in sections if section],
            "notes": notes}


# -- reaction -----------------------------------------------------------------
# details/reactions/: schemeTab (ReactionDetailsScheme: material groups, conditions,
# temperature/pH/vessel/duration, type, role, description, purification, "Additional
# information for publication and purification details" = observation) and
# propertiesTab (dangerous products, TLC-Control).

MATERIAL_GROUPS = (("starting_materials", "Starting materials"), ("reactants", "Reactants"),
                   ("solvents", "Solvents"), ("products", "Products"),
                   ("purification_solvents", "Purification solvents"))


def _list_text(value: Any) -> str:
    """purification / dangerous_products: a list (ELN 4) or text (ELN 3.1)."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return _text(value)


def _material_name(data: dict) -> str:
    molecule = data.get("molecule") if isinstance(data.get("molecule"), dict) else {}
    hash_ = data.get("molecule_name_hash") if isinstance(data.get("molecule_name_hash"), dict) else {}
    for candidate in (data.get("name"), data.get("external_label"), hash_.get("label"), data.get("showed_name"),
                      molecule.get("iupac_name")):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _material_kind(data: dict) -> str:
    """Sample.js defaultAmountType: the target amount until a real one is set."""
    return "target" if _number(data.get("target_amount_value")) and not _number(data.get("real_amount_value")) \
        else "real"


def _material_row(data: dict, product: bool) -> dict[str, str]:
    kind = _material_kind(data)
    value, unit = _stored_amount(data, kind)
    cells = {"Label": _text(data.get("short_label")), "Name": _material_name(data),
             "Ref": "yes" if data.get("reference") else "", "T/R": "", "Mass": "", "Volume": "",
             "Amount": "", "Equiv.": "", "Yield": ""}
    if value:
        cells["T/R"] = "T" if kind == "target" else "R"
        metrics = str(data.get("metrics") or "mmmm")
        metrics = metrics if len(metrics) > 3 else "mmmm"
        amounts = derived_amounts(data, value, unit)
        for key, column, prefix in (("g", "Mass", metrics[0]), ("l", "Volume", metrics[3]),
                                    ("mol", "Amount", metrics[2])):
            number, shown = _scaled(amounts[key], key, prefix, PRECISION[key])
            cells[column] = f"{number} {shown}" if number else ""
    equivalent = _number(data.get("equivalent"))
    if equivalent:
        if product:  # the stored equivalent of a product is its yield (a fraction)
            cells["Yield"] = f"{fmt_number(equivalent * 100, 0)} %"
        else:
            cells["Equiv."] = fmt_number(equivalent, 4)
    return cells


def _materials_table(title: str, materials: Any, product: bool) -> dict | None:
    if not isinstance(materials, list):
        return None
    items = [item for item in materials if isinstance(item, dict)]
    rows = [_material_row(item, product) for item in items]
    columns = ["Label", "Name", "Ref", "T/R", "Mass", "Volume", "Amount", "Yield" if product else "Equiv."]
    columns = [column for column in columns if column in ("Label", "Name") or any(row[column] for row in rows)]
    targets = [{"type": "sample", "id": item["id"]} if isinstance(item.get("id"), int) else None for item in items]
    return table(title, columns, [[row[column] for column in columns] for row in rows], targets)


def _sbmm_table(data: dict) -> dict | None:
    items = [item for item in data.get("reactant_sbmm_samples") or [] if isinstance(item, dict)]
    rows = [[_text(item.get("short_label")), _text(item.get("name"))] for item in items]
    targets = [{"type": "sequence_based_macromolecule_sample", "id": item["id"]}
               if isinstance(item.get("id"), int) else None for item in items]
    return table("Macromolecule reactants", ["Label", "Name"], rows, targets)


def _temperature(data: dict) -> tuple[str, str]:
    """Reaction.js temperature_display(_with_unit): the user's text, else the
    lowest ~ highest value of the chart; a unit only for numbers."""
    temperature = data.get("temperature") if isinstance(data.get("temperature"), dict) else {}
    unit = str(temperature.get("valueUnit") or "\u00b0C")
    user = str(temperature.get("userText") or "").strip()
    if user:
        numeric = all(_number(part.strip()) is not None for part in user.split("~"))
        return user, unit if numeric else ""  # "reflux" has no unit
    values = [_number(point.get("value")) for point in temperature.get("data") or [] if isinstance(point, dict)]
    values = [value for value in values if value is not None]
    if not values:
        return "", ""
    low, high = min(values), max(values)
    return (fmt_number(low) if low == high else f"{fmt_number(low)} ~ {fmt_number(high)}"), unit


def _duration(data: dict) -> str:
    duration = _text(data.get("duration"))
    if duration:
        return duration
    display = data.get("durationDisplay") if isinstance(data.get("durationDisplay"), dict) else {}
    value = _text(display.get("dispValue"))
    return f"{value} {display.get('dispUnit') or ''}".strip() if value else ""


def _ph(data: dict) -> str:
    value = fmt_number(data.get("ph_value"), 2)
    if not value:
        return ""
    operator = str(data.get("ph_operator") or "=")
    return value if operator == "=" else f"{operator} {value}"


def _vessel(data: dict) -> tuple[str, str]:
    vessel = data.get("vessel_size") if isinstance(data.get("vessel_size"), dict) else {}
    amount = fmt_number(vessel.get("amount"), 4)
    return (amount, str(vessel.get("unit") or "ml")) if amount and _number(vessel.get("amount")) else ("", "")


def reaction_title(data: dict) -> str:
    return str(data.get("name") or "").strip() or str(data.get("short_label") or "").strip()


def reaction_view(data: dict, images: dict | None = None, url: str = "") -> dict:
    """The view model of a reaction (see the module docstring); the scheme is
    `images["scheme"]` (IMAGE_REFS). The variations are only counted."""
    notes: list[str] = []
    short_label = str(data.get("short_label") or "").strip()
    status = str(data.get("status") or "").strip()
    temperature, temperature_unit = _temperature(data)
    vessel, vessel_unit = _vessel(data)
    rf_value = data.get("rf_value")
    sections = [_materials_table(title, data.get(role), role == "products") for role, title in MATERIAL_GROUPS
                if role != "purification_solvents"]
    sections += [
        _sbmm_table(data),
        text("Conditions", str(data.get("conditions") or "")),
        fields("Reaction", [
            ("Status", status),
            ("Type (Name Reaction)", data.get("rxno")),
            ("Role", data.get("role")),
            ("Temperature", temperature, temperature_unit),
            ("Time (duration)", _duration(data)),
            ("Start", data.get("timestamp_start")),
            ("Stop", data.get("timestamp_stop")),
            ("pH", _ph(data)),
            ("Vessel size", vessel, vessel_unit),
            ("Labels", _user_labels(data)),
        ]),
        text("Description", data.get("description")),
        fields("Purification", [("Purification", _list_text(data.get("purification")))]),
        _materials_table("Purification solvents", data.get("purification_solvents"), False),
        text("Additional information for publication and purification details", data.get("observation")),
        fields("Safety", [("Dangerous products", _list_text(data.get("dangerous_products")))]),
        fields("TLC-Control", [
            ("Solvents (parts)", data.get("tlc_solvents")),
            ("Rf-Value", "" if _number(rf_value) == 0 else rf_value),  # a new reaction stores 0.0
        ]),
        text("TLC-Description", data.get("tlc_description")),
    ]
    variations = data.get("variations")
    if isinstance(variations, list) and variations:
        notes.append(f"The reaction has {len(variations)} variation{'s' if len(variations) != 1 else ''}; "
                     "they are not shown yet.")
    subtitle = " \u00b7 ".join(part for part in (short_label, status) if part)
    return {"type": "reaction", "id": data.get("id"), "updated_at": str(data.get("updated_at") or ""),
            "title": reaction_title(data), "subtitle": subtitle, "url": url or "",
            "image": _image((images or {}).get("scheme"), notes, IMAGE_NAMES["reaction"]),
            "sections": [section for section in sections if section], "notes": notes}


def reaction_images(data: dict) -> dict:
    """The scheme the ELN stored (`reaction_svg_file`)."""
    name = data.get("reaction_svg_file")
    return {"scheme": f"/images/reactions/{name}"} if name else {}


ADAPTERS["reaction"] = lambda data, images, url: reaction_view(data, images, url)
IMAGE_REFS["reaction"] = reaction_images


def sample_images(data: dict) -> dict:
    """The sample's own drawing, else its molecule's (the key "image")."""
    if data.get("sample_svg_file"):
        return {"image": f"/images/samples/{data['sample_svg_file']}"}
    molecule = data.get("molecule") if isinstance(data.get("molecule"), dict) else {}
    return {"image": f"/images/molecules/{molecule['molecule_svg_file']}"} if molecule.get("molecule_svg_file") else {}


ADAPTERS["sample"] = lambda data, images, url: sample_view(data, images.get("image"), url)
IMAGE_REFS["sample"] = sample_images

# The other built-in types (research plan, wellplate, screen, cell line, device description,
# SBMM sample) register themselves into ADAPTERS/IMAGE_REFS, element_generic.py into the two hooks
# above; last, as they import this module.
import element_types  # noqa: E402  (it registers itself; may still be loading here)
import element_generic  # noqa: E402  (so does this one: GENERIC_ADAPTER, SEGMENT_SECTIONS)

del element_types, element_generic
