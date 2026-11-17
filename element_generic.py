"""View models of LabIMotion generic elements and generic segments (no GTK,
stdlib only, no chemotion_api): the raw server JSON in, the view model of
element_views.py out. No code per class: everything comes from the layers and
fields the element carries (`properties.layers`, as the ELN saved them) and the
`select_options` of its template.

The rendering follows LabIMotion's web client (chem-generic-ui `src/`:
`components/fields/GenProperties.js` for the field types, `GenPropertiesFields.js`,
`PropCalculate.js`, `GenPropSelectMulti.js`, `DateTimeRange.js`, `table/TableRecord.js`,
`layers/GenPropertiesLayer.js`, `utils/template/visibility-handler.js`; generic-ui-core's
`showProperties` and chem-units' unit table) read only:

- one "fields" section per layer (layer label as title), layers by `position` (then
  `wf_position`), fields by `position`; layers without fields, layers hidden by their
  conditions (`cond_fields`/`cond_operator`) and empty values are left out;
- a field with conditions is shown only when `show_properties()` says so (and the fields it
  depends on are shown themselves); the label of the matching condition replaces the field's
  label like in the web client;
- text, textarea, integer, number, date, datetime, ontology-select: the value; checkbox:
  Yes/No; select / select-multi: the option labels of `select_options`; system-defined: value
  and the unit label (chem-units' table, `UNITS`); input-group: its sub-fields as one row;
  datetime-range: start - stop (duration); formula-field / text-formula: the computed
  value; upload: file names; table: a "table" section; drag sample / element / reaction: a
  one-row "table" section with a target (the panel opens it), molecule: a row; unknown field
  types: the raw value as text. wf-next and dummy fields (controls without a value) are left out.

`generic_view()` is the adapter for any type without an adapter of its own,
`segment_sections()` gives the generic segments (`data["segments"]`, each with
`klass_label` and `properties`) of any element; element_views.view() appends them to every view. Both register themselves into
element_views (`GENERIC_ADAPTER`, `SEGMENT_SECTIONS`; either module may be imported first).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

import element_views as ev

# chem-units' unit table (system field -> {unit key: label}; the first unit of a system is its
# default, shown when a field has no `value_system`). Generated from chem-units' `si.fields`.
UNITS: dict[str, dict[str, str]] = {
    'acceleration': {'mm_s2': 'mm/s²'},
    'agitation': {'rpm': 'rpm'},
    'amount_enzyme': {'u': 'U', 'mu': 'mU', 'kat': 'kat', 'mkat': 'mkat', 'µkat': 'µkat', 'nkat': 'nkat'},
    'amount_substance': {'mol': 'mol', 'mmol': 'mmol', 'umol': 'µmol', 'nmol': 'nmol', 'pmol': 'pmol'},
    'areal_density': {'kg_m2': 'kg m⁻²', 't_m2': 't m⁻²', 'kg_cm2': 'kg cm⁻²', 'g_m2': 'g m⁻²'},
    'molarity': {'mol_l': 'mol/L', 'mmol_l': 'mmol/L', 'umol_l': 'µmol/L', 'nmol_l': 'nmol/L', 'pmol_l': 'pmol/L'},
    'chem_distances': {'angstrom': 'Å'},
    'concentration': {'ng_l': 'ng/L', 'mg_l': 'mg/L', 'g_l': 'g/L'},
    'conductivity': {'s_m': 'S/m', 'ms_cm': 'mS/cm'},
    'currency': {'eur': 'EUR', 'chf': 'CHF', 'gbp': 'GBP', 'usd': 'USD', 'jpy': 'JPY'},
    'current': {'A': 'A', 'mA': 'mA', 'uA': 'µA', 'nA': 'nA'},
    'c_rate': {'c_h': '1/h'},
    'degree': {'degree': '°'},
    'density': {'g_cm3': 'g/cm³', 'kg_l': 'kg/l'},
    'dimensionless_concentration': {'ppm': 'ppm', 'ppb': 'ppb'},
    'duration': {'d': 'd', 'h': 'h', 'min': 'm', 's': 's', 'y': 'y'},
    'elastic_modulus': {'m_pa': 'MPa', 'k_pa': 'kPa', 'pa': 'Pa'},
    'electric_charge_c': {'ec_c': 'C'},
    'electric_charge_mol': {'ec_mol': 'C/mol'},
    'electric_field': {'v_m': 'V/m'},
    'electrical_capacity': {'mah': 'mAh', 'ah': 'Ah', 'as': 'As', 'c': 'C'},
    'electrical_potential': {'v_re': 'V vs. RE', 'mv_re': 'mV vs. RE', 'mv_ce': 'mV vs. CE', 'v_ce': 'V vs. CE'},
    'energy': {'eV': 'eV', 'keV': 'keV', 'j': 'J', 'k_j': 'kJ', 'kWh': 'kWh'},
    'free_energy': {'kj_mol': 'kJ/mol'},
    'enzyme_activity': {'u_l': 'U/L', 'u_ml': 'U/mL', 'u_g': 'U/g', 'u_mg': 'U/mg'},
    'faraday': {'faraday': 'Fd'},
    'flow_rate': {'l_m': 'L/min', 'ml_min': 'mL/min', 'ul_min': 'µL/min', 'ml_h': 'mL/h'},
    'frequency': {'hz': 'Hz', 'khz': 'kHz', 'mhz': 'MHz'},
    'heating_rate': {
        'k_min': 'K/min', 'k_s': 'K/s', 'k_h': 'K/h', 'degc_min': '°C min⁻¹', 'degc_s': '°C s⁻¹',
        'degc_h': '°C h⁻¹',
    },
    'length': {'mm': 'mm', 'cm': 'cm', 'm': 'm'},
    'magnetic_flux_density': {'T': 'T'},
    'mass': {'g': 'g', 'mg': 'mg', 'ug': 'µg', 'kg': 'kg'},
    'mass_flow': {'g_min': 'g/min', 'kg_h': 'kg/h', 'kg_s': 'kg/s', 'g_s': 'g/s', 'mg_s': 'mg/s'},
    'mass_fraction': {'wt_p': 'wt.%'},
    'mass_loading': {'g_cm2': 'g cm⁻²', 'mg_cm2': 'mg cm⁻²'},
    'mass_molecule': {'dalton': 'D', 'kilo_dalton': 'kD'},
    'molar_conductivity': {'s_cm2_mol': 'S cm² mol⁻¹'},
    'molar_entropy': {'j_mol_k': 'J/(mol*K)'},
    'molecular_weight': {'g_mol': 'g/mol'},
    'percentage': {'p': '%'},
    'power': {'watt': 'W'},
    'pressure': {
        'atm': 'atm', 'pa': 'Pa', 'kpa': 'kPa', 'mpa': 'MPa', 'hpa': 'hPa', 'torr': 'Torr', 'bar': 'bar',
        'mbar': 'mbar',
    },
    'process_rate': {'1_s': '1/s', '1_min': '1/min', '1_h': '1/h'},
    'rate_constant': {'1_m_s': '1/(M*s)'},
    'reaction_rate': {'mol_lmin': 'mol L⁻¹ min⁻¹', 'mol_lsec': 'mol L⁻¹ s⁻¹'},
    'resolution': {'px': 'px', 'mp': 'MP'},
    'scan_rate': {'mv_s': 'mV/s', 'v_s': 'V/s'},
    'space_time_yield': {
        'kg_m3_d': 'kg/(m³*d)', 'kg_m3_h': 'kg/(m³*h)', 'g_l_h': 'g/(L*h)', 'mg_ml_h': 'mg/(mL*h)',
        'g_l_min': 'g/(L*min)', 'g_m3_s': 'g/(m³*s)',
    },
    'specific_current': {'ma_g': 'mA/g', 'a_g': 'A/g', 'ma_cm2': 'mA/cm²', 'a_cm2': 'A/cm²', 'a_mm2': 'A/mm²'},
    'specific_electrical_capacity': {'mah_g': 'mAh/g', 'ah_g': 'Ah/g'},
    'specific_surface_area': {'m2_g': 'm²/g'},
    'specific_volume': {'cm3_g': 'cm³/g'},
    'speed': {'cm_s': 'cm/s', 'mm_s': 'mm/s', 'um_m': 'µm/min', 'nm_m': 'nm/min', 'cm_h': 'cm/h', 'mm_h': 'mm/h'},
    'subatomic_length': {'um': 'µm', 'nm': 'nm', 'pm': 'pm'},
    'surface': {'a_2': 'A²', 'um_2': 'µm²', 'mm_2': 'mm²', 'cm_2': 'cm²'},
    'temperature': {'C': '°C', 'F': '°F', 'K': 'K'},
    'tensile_force': {'n': 'N', 'kn': 'kN', 'mn': 'mN'},
    'tensile_stress': {'n_cm2': 'N cm⁻²', 'n_mm2': 'N mm⁻²'},
    'turnover_number': {'1_s': '1/s', '1_m': '1/min'},
    'viscosity': {'pas': 'Pas', 'mpas': 'mPas'},
    'kinematic_viscosity': {'m2_s': 'm²/s'},
    'voltage': {'mv': 'mV', 'v': 'V', 'kv': 'kV'},
    'volumes': {'l': 'l', 'ml': 'ml', 'ul': 'µl', 'nl': 'nl'},
    'volume_fraction': {'vol_p': 'vol.%'},
    'volumes_metric': {'cm3': 'cm³', 'mm3': 'mm³', 'm3': 'm³'},
}


SEPARATOR_TAG = "@@"  # Constants.SEPARATOR_TAG: text-formula's "<table field>@@<column>"
DURATION_LABELS = {"d": "day(s)", "h": "hour(s)", "min": "min(s)", "s": "sec(s)"}  # DateTimeRange.js
MAX_RAW = 500  # an unknown field's non-text value is clipped to this many characters
FORMULA_FUNCTIONS = {
    "abs": abs, "sqrt": math.sqrt, "exp": math.exp, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "round": round, "floor": math.floor,
    "ceil": math.ceil, "min": min, "max": max, "pow": pow,
}
FORMULA_CONSTANTS = {"pi": math.pi, "e": math.e}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _s(value: Any) -> str:
    """A scalar as display text (numbers without trailing zeros, booleans Yes/No, None "")."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return ev.fmt_number(value, ev.MW_DIGITS)
    if isinstance(value, str):
        return value.strip()
    return ""


def _raw(value: Any) -> str:
    """Any value as text, for field types this code does not know."""
    text = _s(value)
    if text or value is None or isinstance(value, (str, int, float, bool)):
        return text
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= MAX_RAW else text[:MAX_RAW] + "..."


def is_generic(data: Any) -> bool:
    """True for the JSON of a generic element: it carries its layers."""
    return isinstance(data, dict) and isinstance(_dict(data.get("properties")).get("layers"), dict)


# -- units ----------------------------------------------------------------------

def unit_label(system: Any, key: Any) -> str:
    """The label chem-units' genUnit() gives for a unit key of a unit system (the system's
    first unit when the key is empty; an unknown key is shown as it is)."""
    units = UNITS.get(str(system or ""), {})
    key = str(key or "")
    if not key:
        return next(iter(units.values()), "")
    if key in units:
        return units[key]
    for other in UNITS.values():
        if key in other:
            return other[key]
    return key


# -- conditions -----------------------------------------------------------------

def _truthy_text(value: Any) -> str:
    return ("" if value is None else str(value)).strip()


def _condition_met(target: dict, cond: dict) -> bool:
    """generic-ui-core's sr/lr/fr/pr/vr: does the target field hold the condition's value?"""
    value = cond.get("value")
    kind = target.get("type")
    if kind == "checkbox":
        current = target.get("value") or False
        if isinstance(value, bool):
            return current is value
        text = ("false" if not value else str(value)).strip().lower()
        return (text in ("false", "no", "f", "0") and current is False) or \
               (text in ("true", "yes", "t", "1") and current is True)
    if kind in ("select", "text", "ontology-select"):
        return _truthy_text(target.get("value")) == _truthy_text(value)
    if kind == "select-multi":
        wanted = _truthy_text(value)
        chosen = [_truthy_text(sub.get("value")) for sub in _list(target.get("sub_fields")) if isinstance(sub, dict)]
        return wanted in chosen if wanted else not chosen
    return False


def show_properties(item: dict, layers: dict) -> tuple[bool, str]:
    """generic-ui-core's showProperties: (shown, label of the matching condition). An item
    (field or layer) without `cond_fields` is shown; conditions on the same field form one
    group (any of them may match); `cond_operator` 1 = any group matches (default), 0 = none,
    9 = all."""
    conds = _list(item.get("cond_fields"))
    if not conds:
        return True, ""
    operator = item.get("cond_operator")
    operator = 1 if operator is None else operator
    groups: dict[tuple, list[dict]] = {}
    for cond in conds:
        cond = _dict(cond)
        if not cond.get("field"):
            return True, ""
        groups.setdefault((cond.get("layer"), cond.get("field")), []).append(cond)
    matched = 0
    for group in groups.values():
        hit = None
        for cond in group:
            layer = _dict(layers.get(cond.get("layer")))
            target = next((f for f in _list(layer.get("fields"))
                           if isinstance(f, dict) and f.get("field") == cond.get("field")), {})
            if _condition_met(target, cond):
                hit = cond
                break
        if hit is not None:
            matched += 1
            if operator == 1:
                return True, str(hit.get("label") or "")
    if operator == 0:
        return matched == 0, ""
    if operator == 9:
        return matched == len(groups), ""
    return False, ""


def _visible(item: dict, own_layer: str | None, layers: dict, seen: frozenset = frozenset()) -> tuple[bool, str]:
    """visibility-handler.js: the item's own conditions hold and every field it depends on is
    shown itself (a cycle hides it)."""
    if own_layer is not None:  # a field; layers have no key of their own to guard against
        mark = (own_layer, item.get("field"))
        if mark in seen:
            return False, str(item.get("label") or "")
        seen = seen | {mark}
    shown, label = show_properties(item, layers)
    if not shown:
        return False, label
    for cond in _list(item.get("cond_fields")):
        cond = _dict(cond)
        layer = _dict(layers.get(cond.get("layer")))
        target = next((f for f in _list(layer.get("fields"))
                       if isinstance(f, dict) and f.get("field") == cond.get("field")), None)
        if target is not None and not _visible(target, str(cond.get("layer")), layers, seen)[0]:
            return False, label
    return True, label


# -- formulas -------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:(\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|[-+*/^%(),]))")


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise ValueError("bad formula")
        number, name, op = match.groups()
        out.append(("num", number) if number else ("name", name) if name else ("op", op))
        pos = match.end()
    return out


class _Formula:
    """A small arithmetic evaluator for formula-field (mathjs' evaluate in the web client):
    + - * / % ^ ** (power), parentheses, a few functions; a name is the value of the layer's
    integer / system-defined field of that name, or 0."""

    def __init__(self, text: str, values: dict[str, float]) -> None:
        self.tokens = _tokens(text)
        self.values = values
        self.pos = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, op: str) -> bool:
        token = self._peek()
        if token == ("op", op):
            self.pos += 1
            return True
        return False

    def evaluate(self) -> float:
        value = self._expr()
        if self._peek() is not None:
            raise ValueError("bad formula")
        return value

    def _expr(self) -> float:
        value = self._term()
        while True:
            if self._take("+"):
                value += self._term()
            elif self._take("-"):
                value -= self._term()
            else:
                return value

    def _term(self) -> float:
        value = self._unary()
        while True:
            if self._take("*"):
                value *= self._unary()
            elif self._take("/"):
                divisor = self._unary()
                value = value / divisor if divisor else math.inf
            elif self._take("%"):
                divisor = self._unary()
                value = math.fmod(value, divisor) if divisor else math.nan
            else:
                return value

    def _unary(self) -> float:
        if self._take("-"):
            return -self._unary()
        if self._take("+"):
            return self._unary()
        return self._power()

    def _power(self) -> float:
        base = self._atom()
        if self._take("^") or self._take("**"):
            return math.pow(base, self._unary())
        return base

    def _atom(self) -> float:
        token = self._peek()
        if token is None:
            raise ValueError("bad formula")
        self.pos += 1
        kind, text = token
        if kind == "num":
            return float(text)
        if kind == "name":
            if self._take("("):
                args = [self._expr()]
                while self._take(","):
                    args.append(self._expr())
                if not self._take(")") or text not in FORMULA_FUNCTIONS:
                    raise ValueError("bad formula")
                return float(FORMULA_FUNCTIONS[text](*args))
            return self.values.get(text, FORMULA_CONSTANTS.get(text, 0.0))
        if text == "(":
            value = self._expr()
            if not self._take(")"):
                raise ValueError("bad formula")
            return value
        raise ValueError("bad formula")


def formula_value(field: dict, layer: dict) -> str:
    """PropCalculate.js: the formula evaluated with the integer / system-defined fields of the
    layer, rounded to `decimal` places (default 5). "" when it cannot be computed."""
    formula = str(field.get("formula") or "")
    if not formula.strip():
        return ""
    values: dict[str, float] = {}
    for other in _list(layer.get("fields")):
        if isinstance(other, dict) and other.get("type") in ("integer", "system-defined"):
            try:
                values[str(other.get("field"))] = float(other.get("value") or 0)
            except (TypeError, ValueError):
                values[str(other.get("field"))] = 0.0
    try:
        digits = int(float(field.get("decimal"))) or 5
    except (TypeError, ValueError):
        digits = 5
    try:
        result = _Formula(formula, values).evaluate()
    except (ValueError, OverflowError, ZeroDivisionError, TypeError):
        return ""
    if math.isnan(result) or math.isinf(result):
        return ""
    return ev.fmt_number(round(result, digits), digits)


def text_formula_value(field: dict, layers: dict) -> str:
    """GenTextFormula: the values of the referenced fields (or a table's column) joined, every
    non-empty value followed by its separator (as the web client does, also the last)."""
    parts: list[str] = []
    for sub in _list(field.get("text_sub_fields")):
        sub = _dict(sub)
        name = str(sub.get("field") or "")
        if not name:
            continue
        separator = "" if sub.get("separator") is None else str(sub.get("separator"))
        fields = _list(_dict(layers.get(sub.get("layer"))).get("fields"))
        if f"[{SEPARATOR_TAG}]" in name:
            pair = name.split(f"[{SEPARATOR_TAG}]")
            if len(pair) != 2:
                continue
            table = next((f for f in fields if isinstance(f, dict) and f.get("field") == pair[0]
                          and f.get("type") == "table"), None)
            for row in _list(_dict(table).get("sub_values")):
                cell = _dict(row).get(pair[1])
                if cell not in (None, ""):
                    parts += [_cell_text(cell), separator]
        else:
            target = next((f for f in fields if isinstance(f, dict) and f.get("field") == name), None)
            if target and target.get("value") not in (None, "", False, 0):
                parts += [_raw(target.get("value")), separator]
    return "".join(parts).strip()


# -- field values ---------------------------------------------------------------

def _options(select_options: dict, system: Any) -> list[dict]:
    return [o for o in _list(_dict(select_options.get(str(system or ""))).get("options")) if isinstance(o, dict)]


def _option_label(select_options: dict, system: Any, key: Any) -> str:
    text = _s(key)
    for option in _options(select_options, system):
        if _s(option.get("key")) == text:
            return _s(option.get("label")) or text
    return text


def _quantity(value: Any, system: Any, key: Any) -> tuple[str, str]:
    """(value, unit label) of a system-defined value; ("", "") when there is no value."""
    text = _s(value)
    return (text, unit_label(system, key)) if text else ("", "")


def _cell_text(cell: Any) -> str:
    """A table cell or sub-value as text (a drag value shows its label)."""
    if isinstance(cell, dict):
        inner = cell.get("value")
        if isinstance(inner, dict):
            return _drag_label(inner)
        text, unit = _quantity(inner, None, cell.get("value_system"))
        return " ".join(p for p in (text, unit if "value_system" in cell else "") if p)
    return _s(cell)


def _drag_label(value: dict) -> str:
    return _s(value.get("el_label")) or _s(value.get("el_name")) or _s(value.get("el_short_label"))


def _drag_target(value: dict) -> dict | None:
    """The `{type, id}` the panel can open for a dropped element (None for a molecule)."""
    element_id = value.get("el_id")
    if not isinstance(element_id, int) or isinstance(element_id, bool):
        return None
    kind = str(value.get("el_type") or "")
    if kind == "element":
        kind = str(value.get("el_klass") or "")
    if not kind or kind == "molecule":
        return None
    return {"type": kind, "id": element_id}


def _file_names(value: Any) -> str:
    names = []
    for item in _list(_dict(value).get("files")):
        item = _dict(item)
        name = _s(item.get("filename"))
        label = _s(item.get("label"))
        if name:
            names.append(f"{name} ({label})" if label and label != name else name)
    return ", ".join(names)


def _group_text(field: dict, select_options: dict) -> str:
    """input-group: the labels, numbers, texts and quantities of its sub-fields in order;
    "" unless one of them holds a value."""
    parts, filled = [], False
    for sub in _list(field.get("sub_fields")):
        sub = _dict(sub)
        kind = sub.get("type")
        if kind == "label":
            parts.append(_s(sub.get("value")))
        elif kind == "system-defined":
            text, unit = _quantity(sub.get("value"), sub.get("option_layers"), sub.get("value_system"))
            filled = filled or bool(text)
            parts.append(" ".join(p for p in (text, unit) if p))
        elif kind == "select":
            text = _option_label(select_options, sub.get("option_layers"), sub.get("value"))
            filled = filled or bool(text)
            parts.append(text)
        else:
            text = _raw(sub.get("value"))
            filled = filled or bool(text)
            parts.append(text)
    return " ".join(part for part in parts if part) if filled else ""


def _range_text(field: dict) -> str:
    subs = {_s(_dict(sub).get("col_name")): _dict(sub) for sub in _list(field.get("sub_fields"))}
    start, stop = _s(subs.get("timeStart", {}).get("value")), _s(subs.get("timeStop", {}).get("value"))
    duration = subs.get("duration", {})
    length = _s(duration.get("value"))
    text = f"{start} - {stop}" if start or stop else ""
    if length:
        unit = DURATION_LABELS.get(str(duration.get("value_system") or "d"), "")
        length = f"{length} {unit}".strip()
        text = f"{text} ({length})" if text else length
    return text


def _table_section(title: str, field: dict, select_options: dict) -> dict | None:
    """TableRecord.js: the columns are the sub-fields (`col_name`), the rows `sub_values`."""
    subs = [_dict(sub) for sub in _list(field.get("sub_fields"))]
    if not subs:
        return None
    columns = [_s(sub.get("col_name")) or f"Column {number}" for number, sub in enumerate(subs, start=1)]
    rows, targets = [], []
    for row in _list(field.get("sub_values")):
        row = _dict(row)
        cells, target = [], None
        for sub in subs:
            cell = row.get(sub.get("id"))
            kind = sub.get("type")
            if kind == "select":
                cells.append(_option_label(select_options, sub.get("option_layers"), cell))
            elif kind == "system-defined":
                value = _dict(cell)
                text, unit = _quantity(value.get("value", cell if not isinstance(cell, dict) else None),
                                       sub.get("option_layers"), value.get("value_system") or sub.get("value_system"))
                cells.append(" ".join(p for p in (text, unit) if p))
            else:
                cells.append(_cell_text(cell))
                inner = _dict(cell).get("value")
                if target is None and kind in ("drag_sample", "drag_element") and isinstance(inner, dict):
                    target = _drag_target(inner)
        rows.append(cells)
        targets.append(target)
    return ev.table(title, columns, rows, targets)


def field_rows(field: dict, layer: dict, layers: dict, select_options: dict, label: str, title: str) -> tuple[list, list]:
    """(fields-section rows, further sections) of one field; both may be empty."""
    kind = str(field.get("type") or "text")
    value = field.get("value")
    if kind in ("wf-next", "dummy", "label"):
        return [], []
    if kind == "table":
        return [], [_table_section(f"{title} · {label}", field, select_options)]
    if kind.startswith("drag_") or kind == "sys-reaction":
        value = _dict(value)
        text = _drag_label(value)
        target = _drag_target(value)
        if target and text:
            return [], [ev.table(f"{title} · {label}", ["Label", "Type"], [[text, target["type"].replace("_", " ")]],
                                 [target])]
        return [(label, text, "")], []
    if kind == "checkbox":
        return [(label, "Yes" if value is True else "No" if value is False else "", "")], []
    if kind == "select":
        return [(label, _option_label(select_options, field.get("option_layers"), value), "")], []
    if kind == "select-multi":
        chosen = [_option_label(select_options, field.get("option_layers"), _dict(sub).get("value"))
                  for sub in _list(field.get("sub_fields"))]
        return [(label, ", ".join(c for c in chosen if c), "")], []
    if kind == "system-defined":
        text, unit = _quantity(value, field.get("option_layers"), field.get("value_system"))
        return [(label, text, unit)], []
    if kind == "input-group":
        return [(label, _group_text(field, select_options), "")], []
    if kind == "datetime-range":
        return [(label, _range_text(field), "")], []
    if kind == "upload":
        return [(label, _file_names(value), "")], []
    if kind == "formula-field":
        computed = formula_value(field, layer)
        rows = [(label, computed, "")]
        adjusted = _s(value)
        if str(field.get("canAdjust")).lower() == "true" and adjusted and adjusted != computed:
            rows.append((f"{label} (adjusted)", adjusted, ""))
        return rows, []
    if kind == "text-formula":
        return [(label, text_formula_value(field, layers), "")], []
    if kind == "ontology-select":
        text = _s(value)
        name = text.split("|", 1)[1].strip() if "|" in text else ""  # absOlsTermLabel
        return [(label, name or text.split("|", 1)[0].strip(), "")], []
    return [(label, _raw(value), "")], []  # text, textarea, integer, number, date, datetime and unknown types


# -- layers ---------------------------------------------------------------------

def _by_position(items: list, key=lambda item: item) -> list:
    return [item for _, item in sorted(enumerate(items), key=lambda pair: (key(pair[1]), pair[0]))]


def _position(item: Any) -> float:
    value = _dict(item).get("position")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def ordered_layers(layers: dict) -> list[dict]:
    """The layers in the web client's order: `position`, then `wf_position`."""
    found = [dict(layer, key=layer.get("key") or key) for key, layer in layers.items() if isinstance(layer, dict)]
    return _by_position(found, lambda layer: (_position(layer), _dict(layer).get("wf_position") or 0))


def merged_select_options(*sources: Any) -> dict:
    """The `select_options` of the element, its segment or its class (the first source that has
    an option list for a name wins)."""
    merged: dict = {}
    for source in sources:
        for name, entry in _dict(_dict(source).get("select_options")).items():
            merged.setdefault(name, entry)
    return merged


def layer_sections(layer: dict, layers: dict, select_options: dict, prefix: str = "") -> list[dict]:
    """The sections of one visible layer: its fields, then a table section per table or linked
    element field. Nothing when the layer has no filled field."""
    label = _s(layer.get("label")) or _s(layer.get("key"))
    title = f"{prefix} · {label}" if prefix else label
    rows: list = []
    extra: list = []
    for field in _by_position(_list(layer.get("fields")), _position):
        if not isinstance(field, dict):
            continue
        shown, cond_label = _visible(field, str(layer.get("key")), layers)
        if not shown:
            continue
        name = cond_label or _s(field.get("label")) or _s(field.get("field"))
        new_rows, new_extra = field_rows(field, layer, layers, select_options, name, title)
        rows += new_rows
        extra += new_extra
    return [section for section in [ev.fields(title, rows)] + extra if section]


def properties_sections(properties: dict, select_options: dict, prefix: str = "") -> list[dict]:
    layers = _dict(properties.get("layers"))
    sections: list[dict] = []
    for layer in ordered_layers(layers):
        if not _visible(layer, None, layers)[0]:
            continue
        sections += layer_sections(layer, layers, select_options, prefix)
    return sections


# -- the views ------------------------------------------------------------------

def klass_of(data: dict, klass: dict | None = None) -> dict:
    return _dict(klass) or _dict(data.get("element_klass"))


def generic_view(data: dict, images: dict | None = None, url: str = "", klass: dict | None = None) -> dict:
    """The view model of a generic element. `klass` (the class definition from the server's
    `all_classes`) is optional: the element carries its `element_klass`."""
    properties = _dict(data.get("properties"))
    klass = klass_of(data, klass)
    options = merged_select_options(properties, _dict(klass).get("properties_release"), data.get("properties_release"))
    short_label = _s(data.get("short_label"))
    name = _s(data.get("name")) or short_label
    version = _s(properties.get("version")) or _s(klass.get("version"))
    subtitle = " · ".join(part for part in (_s(klass.get("label")), short_label if short_label != name else "",
                                            f"version {version}" if version else "") if part)
    return {"type": str(data.get("type") or klass.get("name") or ""), "id": data.get("id"),
            "updated_at": _s(data.get("updated_at")), "title": name, "subtitle": subtitle, "url": url or "",
            "image": None, "sections": properties_sections(properties, options),
            "notes": []}


def segment_sections(data: dict) -> list[dict]:
    """The generic segments of any element (`data["segments"]`) as sections titled
    "<segment label> · <layer label>"."""
    sections: list[dict] = []
    for segment in _list(data.get("segments")):
        segment = _dict(segment)
        properties = _dict(segment.get("properties"))
        if not _dict(properties.get("layers")):
            continue
        options = merged_select_options(properties, segment.get("properties_release"))
        sections += properties_sections(properties, options, _s(segment.get("klass_label")) or "Segment")
    return sections


# Registered here, not in element_views: either module may be imported first (element_types does the same).
ev.GENERIC_ADAPTER = generic_view
ev.SEGMENT_SECTIONS = segment_sections
