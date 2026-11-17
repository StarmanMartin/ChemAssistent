"""View models of Chemotion elements for the Details panel (no GTK, stdlib
only, no chemotion_api): the raw server JSON of an element (`json_data`) in,
a plain dict out that the panel only has to lay out.

    {"type", "id", "updated_at", "title", "subtitle", "url", "image": {"mime", "data"} | None,
     "sections": [{"title", "kind": "fields", "rows": [{"label", "value", "unit"}]}
                  | {"title", "kind": "table", "columns": [...], "rows": [[...]]}
                  | {"title", "kind": "text", "text"}],
     "notes": [...]}

Every value is a string, already formatted; empty values and empty sections
are left out here, never in the panel. A type without an adapter gives the
stub `{"type", "id", "unsupported": True}`.

The sample follows the web client (ELN 4.0.1, details/samples/SampleDetails.js
and propertiesTab/SampleForm.js): its labels, grouping, order, units and the
derivation of the amounts (Sample.js `amount_g`/`amount_l`/`amount_mol`). The
JSON of ELN 3.1.x (chemotion_api v312) and 4.x is handled by the presence of
keys, no version switch.
"""
from __future__ import annotations

import json
import math
from typing import Any

SUPPORTED = ("sample",)  # grows with the next milestones
MAX_IMAGE_BYTES = 2_000_000
IMAGE_MIME = "image/svg+xml"
# Web client: MWPrecision (SampleDetails.js) and the numInput precisions of the amounts.
MW_DIGITS = 6
PRECISION = {"g": 4, "l": 5, "mol": 4}
# The metric prefix letters of Sample.metrics (utilities/metricPrefix.js): factor and symbol.
PREFIXES = {"m": (1e3, "m"), "u": (1e6, "µ"), "n": (1.0, "")}
IMAGE_TOO_LARGE = "The structure image is larger than 2 MB and is not shown."


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


def _fields(title: str, rows: list) -> dict | None:
    rows = [row for row in rows if row]
    return {"title": title, "kind": "fields", "rows": rows} if rows else None


def _table(title: str, columns: list[str], rows: list[list[str]]) -> dict | None:
    rows = [row for row in rows if any(cell for cell in row)]
    return {"title": title, "kind": "table", "columns": columns, "rows": rows} if rows else None


def stub(type_name: str, data: dict | None = None) -> dict:
    return {"type": type_name, "id": (data or {}).get("id"), "unsupported": True}


def view(type_name: str, data: dict, image: str | None = None, url: str = "") -> dict:
    """The view model of one element from its raw JSON. `image` = SVG text or
    None; a type without an adapter gives the stub."""
    if type_name not in SUPPORTED:
        return stub(type_name, data)
    if type_name == "sample":
        return sample_view(data, image, url)
    return stub(type_name, data)  # pragma: no cover - SUPPORTED and the branches above agree


def _image(svg: str | None, notes: list[str]) -> dict | None:
    if not svg:
        return None
    if len(svg.encode("utf-8")) > MAX_IMAGE_BYTES:
        notes.append(IMAGE_TOO_LARGE)
        return None
    return {"mime": IMAGE_MIME, "data": svg}


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


def sample_view(data: dict, image: str | None = None, url: str = "") -> dict:
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
    description = delta_text(data.get("description"))
    if description:
        sections.append({"title": "Description", "kind": "text", "text": description})
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
