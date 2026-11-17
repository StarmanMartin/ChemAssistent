"""element_generic.py: the view of LabIMotion generic elements and segments. Recorded JSON
of ELN 4.0.1 (generic_try_v401 / generic_wrk_v401: elements created on the Chemotion test
instance with chemotion_api and trimmed; sample_segments_v401: sample_v401 with the segment
the instance has), a v312-style element built by hand from chemotionapi's recorded class
JSON (generic_class_v312 / generic_segment_class_v312, from its tests; generic_all_types_v312
fills every field type the class offers and adds an unknown type), small dicts for the
conditions and formulas, and the real helper loading generic elements and a sample with a
segment from the test instance. No GTK."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import element_generic
import element_views
from element_generic import (formula_value, generic_view, segment_sections, show_properties, text_formula_value,
                             unit_label)

FIXTURES = Path(__file__).with_name("fixtures") / "elements"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def section(model: dict, title: str) -> dict:
    return next(s for s in model["sections"] if s["title"] == title)


def rows(model: dict, title: str) -> dict[str, tuple[str, str]]:
    return {r["label"]: (r["value"], r["unit"]) for r in section(model, title)["rows"]}


def titles(model: dict) -> list[str]:
    return [s["title"] for s in model["sections"]]


def field(kind: str, name: str = "f", value=None, position: int = 1, **more) -> dict:
    return {"type": kind, "field": name, "label": name.upper(), "position": position, "value": value,
            "sub_fields": [], "text_sub_fields": [], **more}


def element(*layers: dict, options: dict | None = None, **more) -> dict:
    """A generic element with the given layers (each `{key, label, fields: [...], ...}`)."""
    props = {"layers": {layer["key"]: {"position": 10 * n, "wf_position": 0, **layer}
                        for n, layer in enumerate(layers, start=1)}}
    if options:
        props["select_options"] = options
    return {"id": 1, "type": "gen", "name": "Gen", "short_label": "G-1", "properties": props, **more}


def layer(key: str, *fields: dict, label: str | None = None, **more) -> dict:
    return {"key": key, "label": key.title() if label is None else label, "fields": list(fields), **more}


def one_row(kind: str, value=None, **more) -> tuple[str, str]:
    """The row a single field makes: (value, unit)."""
    model = generic_view(element(layer("l", field(kind, "f", value, **more))))
    return rows(model, "L")["F"] if model["sections"] else ("", "")


# -- registration ---------------------------------------------------------------

def test_hooks_are_registered():
    assert element_views.GENERIC_ADAPTER is generic_view
    assert element_views.SEGMENT_SECTIONS is segment_sections
    assert "gen" not in element_views.ADAPTERS  # a generic type has no adapter of its own


def test_the_modules_import_in_every_order():
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    for first in ("element_views", "element_types", "element_generic", "explorer"):
        code = (f"import {first}, element_views as ev; "
                "assert ev.GENERIC_ADAPTER and ev.SEGMENT_SECTIONS and 'sample' in ev.ADAPTERS "
                "and 'wellplate' in ev.ADAPTERS")
        done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
        assert done.returncode == 0, (first, done.stderr)


def test_a_type_without_an_adapter_and_without_layers_is_still_the_stub():
    assert element_views.view("vessel", {"id": 3, "name": "x"}) == {"type": "vessel", "id": 3, "unsupported": True}
    assert element_views.view("vessel", {"id": 3, "properties": {}}) == {"type": "vessel", "id": 3, "unsupported": True}


# -- recorded elements (ELN 4.0.1) --------------------------------------------------

def test_recorded_tryout_v401():
    model = element_views.view("try", load("generic_try_v401.json"), {}, "http://eln/x")
    assert model["title"] == "Recorded Tryout" and model["type"] == "try" and model["url"] == "http://eln/x"
    assert model["subtitle"] == "Tryout · API-T7 · version 1.0"
    assert titles(model) == ["Nr. 1", "Nr. 1 · Table"]
    assert rows(model, "Nr. 1") == {"Name": ("Recorded tryout", ""), "Group": ("TEST 12.5 note", "")}
    table = section(model, "Nr. 1 · Table")
    assert table["columns"] == ["Col 1", "Col 2"] and table["rows"] == [["a1", "b1"], ["a2", ""]]
    assert "targets" not in table and model["notes"] == [] and model["image"] is None


def test_recorded_workflow_v401_leaves_out_layers_without_fields_and_controls():
    model = element_views.view("wrk", load("generic_wrk_v401.json"))
    assert model["subtitle"].startswith("Workflow · ") and model["subtitle"].endswith("version 2.0")
    assert titles(model) == ["Nr 1"]  # "Fixed" has no fields, "Next" (wf-next) is a control, the rest is empty
    assert rows(model, "Nr 1") == {"name_one": ("first", "")}


def test_an_empty_element_has_no_sections():
    data = load("generic_try_v401.json")
    for key in ("one",):
        for entry in data["properties"]["layers"][key]["fields"]:
            entry.pop("value", None)
            if entry["type"] == "table":
                entry["sub_values"] = []
            if entry["type"] == "input-group":
                for sub in entry["sub_fields"]:
                    sub["value"] = "" if sub["type"] != "label" else sub["value"]
    model = element_views.view("try", data)
    assert model["sections"] == [] and model["title"] == "Recorded Tryout"


def test_segments_of_a_sample_are_appended():
    data = load("sample_segments_v401.json")
    plain = element_views.view("sample", load("sample_v401.json"))
    model = element_views.view("sample", data)
    assert titles(model) == titles(plain) + ["ApiTester · One"]
    assert rows(model, "ApiTester · One") == {"Text": ("segment text", "")}
    assert model["title"] == plain["title"]
    data["segments"] = []
    assert titles(element_views.view("sample", data)) == titles(plain)


# -- every field type (v312-style element) -----------------------------------------------

@pytest.fixture
def everything() -> dict:
    return element_views.view("rrr", load("generic_all_types_v312.json"), {}, "http://eln/x")


def test_the_class_json_of_chemotionapi_is_what_the_element_is_built_from():
    klass, built = load("generic_class_v312.json"), load("generic_all_types_v312.json")
    assert built["element_klass"] == klass
    assert [f["type"] for f in klass["properties_release"]["layers"]["aaa"]["fields"]] == [
        "checkbox", "datetime", "datetime-range", "drag_element", "drag_molecule", "drag_sample", "formula-field",
        "input-group", "integer", "select", "system-defined", "table", "text", "text-formula", "textarea", "upload"]
    assert load("generic_segment_class_v312.json")["label"] == "eee"


def test_all_field_types(everything):
    model = everything
    assert model["title"] == "Everything" and model["subtitle"] == "zzz · ttt7 · version 0.2"
    assert rows(model, "bbb") == {
        "a": ("Yes", ""),
        "b": ("20/11/2024 09:30:00", ""),
        "c": ("20/11/2024 09:30:00 - 20/11/2024 11:30:00 (2 hour(s))", ""),
        "e": ("ethanol", ""),  # a molecule has no view: a plain row
        "g": ("21", ""),  # i*3 with decimal 2
        "h": ("1 at foo 3.5 eV", ""),
        "i": ("7", ""),
        "j": ("Beta", ""),  # the label of the option, not its key
        "k": ("150", "rpm"),
        "m": ("alpha", ""),
        "n": ("alpha.t1.t2.", ""),
        "o": ("line one\nline two", ""),
        "p": ("spectrum.pdf (NMR), notes.txt", ""),
    }


def test_linked_elements_are_one_row_tables_with_a_target(everything):
    element_row = section(everything, "bbb · d")
    assert element_row["columns"] == ["Label", "Type"] and element_row["rows"] == [["ttt1 Other element", "rrr"]]
    assert element_row["targets"] == [{"type": "rrr", "id": 5}]  # an element opens as its own class
    sample = section(everything, "bbb · f")
    assert sample["rows"] == [["API-3 water", "sample"]] and sample["targets"] == [{"type": "sample", "id": 12}]


def test_a_table_field_is_a_table_section(everything):
    table = section(everything, "bbb · l")
    assert table["columns"] == ["Column 1", "Column 2", "Column 3", "Column 4", "Column 5"]  # the template has no names
    assert table["rows"] == [["ethanol", "API-3 water", "Alpha", "2.5 U/L", "t1"], ["", "", "Beta", "", "t2"]]
    assert table["targets"] == [{"type": "sample", "id": 12}, None]  # the empty third row is dropped


def test_unknown_types_show_their_raw_value_and_empty_values_nothing(everything):
    assert titles(everything) == ["bbb", "bbb · d", "bbb · f", "bbb · l", "Later", "eee · Nr 1"]
    assert rows(everything, "Later") == {"Widget": ('{"x": 1, "y": [2, 3]}', "")}  # the empty text field is left out


def test_the_segment_of_the_element_follows_through_view(everything):
    assert titles(everything)[-1] == "eee · Nr 1"
    assert segment_sections(load("generic_all_types_v312.json")) == [
        {"title": "eee · Nr 1", "kind": "fields", "rows": [{"label": "a", "value": "segment value", "unit": ""}]}]
    assert generic_view(load("generic_all_types_v312.json"))["sections"][-1]["title"] == "Later"  # no segments here


def test_every_section_is_plain_data():
    def strings(value):
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return [value]

    model = element_views.view("rrr", load("generic_all_types_v312.json"))
    json.dumps(model)
    assert all(isinstance(s, (str, int, type(None), bool)) for s in strings(model))


# -- single fields -------------------------------------------------------------------

@pytest.mark.parametrize("kind, value, expected", [
    ("text", "  hello ", ("hello", "")), ("textarea", "a\nb", ("a\nb", "")),
    ("integer", 7, ("7", "")), ("integer", "7", ("7", "")), ("number", 2.50, ("2.5", "")),
    ("date", "20/11/2024", ("20/11/2024", "")), ("datetime", "20/11/2024 09:30:00", ("20/11/2024 09:30:00", "")),
    ("checkbox", True, ("Yes", "")), ("checkbox", False, ("No", "")),
    ("ontology-select", "CHMO:0000593 | 1H nuclear magnetic resonance spectroscopy",
     ("1H nuclear magnetic resonance spectroscopy", "")), ("ontology-select", "CHMO:0000593", ("CHMO:0000593", "")),
    ("future", "x", ("x", "")), ("future", {"a": 1}, ('{"a": 1}', "")), ("future", 3, ("3", "")),
])
def test_single_field_values(kind, value, expected):
    assert one_row(kind, value) == expected


@pytest.mark.parametrize("kind, value", [
    ("text", None), ("text", ""), ("text", "   "), ("checkbox", None), ("integer", None), ("select", ""),
    ("table", None), ("upload", {"files": []}), ("upload", None), ("system-defined", ""), ("input-group", None),
    ("drag_sample", None), ("drag_sample", {}), ("datetime-range", None), ("wf-next", "abc"), ("dummy", "x"),
    ("formula-field", ""), ("text-formula", ""), ("select-multi", None), ("future", None),
])
def test_empty_values_are_left_out(kind, value):
    assert generic_view(element(layer("l", field(kind, "f", value))))["sections"] == []


def test_system_defined_units():
    assert one_row("system-defined", "25", value_system="C", option_layers="temperature") == ("25", "°C")
    assert one_row("system-defined", 3.5, option_layers="energy") == ("3.5", "eV")  # no unit: the system's first
    assert one_row("system-defined", "2", value_system="k_j", option_layers="energy") == ("2", "kJ")
    assert one_row("system-defined", "2", value_system="mystery") == ("2", "mystery")  # an unknown key as it is
    assert one_row("system-defined", "2", value_system="mm_s2") == ("2", "mm/s²")  # found without its system
    assert one_row("system-defined", "2") == ("2", "")
    assert unit_label("temperature", "K") == "K" and unit_label("nope", "") == "" and unit_label(None, "rpm") == "rpm"
    assert unit_label("molarity", "umol_l") == "µmol/L"


def test_select_and_select_multi_use_the_option_labels():
    options = {"cols": {"options": [{"key": "r", "label": "Red"}, {"key": "g", "label": "Green"}]}}
    model = generic_view(element(layer("l", field("select", "s", "g", option_layers="cols"),
                                       field("select-multi", "m", None, position=2, option_layers="cols",
                                             sub_fields=[{"id": "1", "type": "select", "value": "r"},
                                                         {"id": "2", "type": "select", "value": "g"},
                                                         {"id": "3", "type": "select", "value": "x"}]),
                                       field("select", "u", "zz", position=3, option_layers="gone")), options=options))
    assert rows(model, "L") == {"S": ("Green", ""), "M": ("Red, Green, x", ""), "U": ("zz", "")}


def test_select_options_come_from_the_element_the_class_or_the_release():
    base = element(layer("l", field("select", "s", "a", option_layers="o")))
    assert rows(generic_view(base), "L")["S"] == ("a", "")
    klass = {"label": "K", "properties_release": {"select_options": {"o": {"options": [{"key": "a", "label": "From class"}]}}}}
    assert rows(generic_view(base, klass=klass), "L")["S"] == ("From class", "")
    base["element_klass"] = klass
    assert rows(generic_view(base), "L")["S"] == ("From class", "")
    base["properties_release"] = {"select_options": {"o": {"options": [{"key": "a", "label": "From release"}]}}}
    assert rows(generic_view(base), "L")["S"] == ("From class", "")  # the class comes first
    base["properties"]["select_options"] = {"o": {"options": [{"key": "a", "label": "Own"}]}}
    assert rows(generic_view(base), "L")["S"] == ("Own", "")


def test_input_group_and_datetime_range():
    group = field("input-group", "g", None, sub_fields=[
        {"id": "1", "type": "label", "value": "T ="}, {"id": "2", "type": "number", "value": "20"},
        {"id": "3", "type": "system-defined", "value": "5", "value_system": "h", "option_layers": "duration"},
        {"id": "4", "type": "text", "value": ""}])
    assert generic_view(element(layer("l", group)))["sections"][0]["rows"][0]["value"] == "T = 20 5 h"
    group["sub_fields"] = [{"id": "1", "type": "label", "value": "T ="}, {"id": "2", "type": "number", "value": ""}]
    assert generic_view(element(layer("l", group)))["sections"] == []  # only a label is no value

    def range_of(start, stop, duration="", unit="d"):
        return one_row("datetime-range", None, sub_fields=[
            {"type": "text", "value": start, "col_name": "timeStart"}, {"type": "text", "value": stop, "col_name": "timeStop"},
            {"type": "text", "value": duration, "col_name": "duration", "value_system": unit}])[0]

    assert range_of("a", "b", "3", "min") == "a - b (3 min(s))"
    assert range_of("a", "") == "a -" and range_of("", "", "4") == "4 day(s)" and range_of("", "") == ""


def test_upload_lists_the_file_names():
    value = {"files": [{"filename": "a.pdf", "label": "A"}, {"filename": "b.txt", "label": "b.txt"}, {"label": "x"}]}
    assert one_row("upload", value)[0] == "a.pdf (A), b.txt"


def test_a_linked_element_without_a_target_is_a_row():
    row = {"el_id": 3, "el_type": "sample", "el_label": "S"}
    for kind in ("drag_sample", "drag_element", "sys-reaction"):
        model = generic_view(element(layer("l", field(kind, "f", {**row, "el_id": None}))))
        assert rows(model, "L") == {"F": ("S", "")}
    assert generic_view(element(layer("l", field("drag_element", "f", {**row, "el_type": "element"}))))[
        "sections"][0]["rows"] == [{"label": "F", "value": "S", "unit": ""}]  # an element without its class
    reaction = generic_view(element(layer("l", field("sys-reaction", "f", {"el_id": 8, "el_type": "reaction",
                                                                           "el_label": "R-1"}))))
    assert section(reaction, "L · F")["targets"] == [{"type": "reaction", "id": 8}]
    assert generic_view(element(layer("l", field("drag_molecule", "f", {"el_id": 8, "el_type": "molecule",
                                                                        "el_label": "water"}))))[
        "sections"][0]["rows"][0]["value"] == "water"


def test_a_table_with_units_selects_and_a_linked_sample_column():
    options = {"o": {"options": [{"key": "x", "label": "Ex"}]}}
    table = field("table", "t", None,
                  sub_fields=[{"id": "c1", "type": "text", "col_name": "Name"},
                              {"id": "c2", "type": "system-defined", "col_name": "Temp", "option_layers": "temperature",
                               "value_system": "C"},
                              {"id": "c3", "type": "select", "col_name": "Kind", "option_layers": "o"},
                              {"id": "c4", "type": "drag_sample", "col_name": "Sample"}],
                  sub_values=[{"id": "r", "c1": "one", "c2": {"value": "20", "value_system": "K"}, "c3": "x",
                               "c4": {"value": {"el_id": 7, "el_type": "sample", "el_label": "API-7"}}},
                              {"id": "s", "c1": "two", "c2": {"value": "", "value_system": "C"}}])
    model = generic_view(element(layer("l", table), options=options))
    assert section(model, "L · T") == {"title": "L · T", "kind": "table", "columns": ["Name", "Temp", "Kind", "Sample"],
                                       "rows": [["one", "20 K", "Ex", "API-7"], ["two", "", "", ""]],
                                       "targets": [{"type": "sample", "id": 7}, None]}


# -- conditions ------------------------------------------------------------------------

def cond(layer_key: str, name: str, value, label: str = "") -> dict:
    return {"id": "x", "layer": layer_key, "field": name, "value": value, "label": label}


def test_show_properties_operators():
    layers = {"l": {"fields": [field("select", "s", "a"), field("checkbox", "c", True),
                               field("text", "t", "hello")]}}
    a, b, c = cond("l", "s", "a"), cond("l", "s", "b"), cond("l", "t", "hello")
    assert show_properties({}, layers) == (True, "")
    assert show_properties({"cond_fields": [a]}, layers)[0] is True
    assert show_properties({"cond_fields": [b]}, layers)[0] is False
    assert show_properties({"cond_fields": [b, c]}, layers)[0] is True  # default operator 1: any group
    assert show_properties({"cond_fields": [b, c], "cond_operator": 9}, layers)[0] is False  # all groups
    assert show_properties({"cond_fields": [a, c], "cond_operator": 9}, layers)[0] is True
    assert show_properties({"cond_fields": [b], "cond_operator": 0}, layers)[0] is True  # none
    assert show_properties({"cond_fields": [a], "cond_operator": 0}, layers)[0] is False
    assert show_properties({"cond_fields": [a], "cond_operator": 5}, layers)[0] is False  # an unknown operator
    # conditions on the same field are one group: either value
    assert show_properties({"cond_fields": [b, a], "cond_operator": 9}, layers)[0] is True
    assert show_properties({"cond_fields": [cond("nope", "s", "a")]}, layers)[0] is False  # a field that is not there
    assert show_properties({"cond_fields": [{"layer": "l", "value": "a"}]}, layers) == (True, "")  # no field named


def test_checkbox_conditions_accept_the_words_of_the_template_editor():
    def met(stored, wanted):
        layers = {"l": {"fields": [field("checkbox", "c", stored)]}}
        return show_properties({"cond_fields": [cond("l", "c", wanted)]}, layers)[0]

    assert met(True, "true") and met(True, "Yes") and met(True, "1") and met(True, True) and not met(True, "false")
    assert met(False, "false") and met(None, "no") and met(False, False) and not met(False, "true")
    assert not met(None, "true") and not met("x", "true")


def test_select_multi_conditions():
    chosen = field("select-multi", "m", None, sub_fields=[{"value": "a"}, {"value": "b"}])
    layers = {"l": {"fields": [chosen]}}
    assert show_properties({"cond_fields": [cond("l", "m", "b")]}, layers)[0]
    assert not show_properties({"cond_fields": [cond("l", "m", "z")]}, layers)[0]
    assert not show_properties({"cond_fields": [cond("l", "m", "")]}, layers)[0]
    layers = {"l": {"fields": [field("select-multi", "m", None)]}}
    assert show_properties({"cond_fields": [cond("l", "m", "")]}, layers)[0]  # nothing chosen = the empty value


def test_conditional_fields_and_layers_in_a_view():
    layers = [
        layer("a", field("select", "kind", "b", position=1, option_layers="o"),
              field("text", "only_a", "A!", position=2, cond_fields=[cond("a", "kind", "a")]),
              field("text", "only_b", "B!", position=3, label="Beta only", cond_fields=[cond("a", "kind", "b", "Shown for b")]),
              field("text", "both", "shown", position=4, cond_operator=9,
                    cond_fields=[cond("a", "kind", "b"), cond("c", "flag", True)])),
        layer("b", field("text", "x", "hidden layer"), label="Hidden", cond_fields=[cond("a", "kind", "a")]),
        layer("c", field("checkbox", "flag", True), label="Shown", cond_fields=[cond("a", "kind", "b")]),
    ]
    model = generic_view(element(*layers, options={"o": {"options": [{"key": "b", "label": "Bee"}]}}))
    assert titles(model) == ["A", "Shown"]
    assert rows(model, "A") == {"KIND": ("Bee", ""), "Shown for b": ("B!", ""), "BOTH": ("shown", "")}
    assert rows(model, "Shown") == {"FLAG": ("Yes", "")}


def test_a_field_depending_on_a_hidden_field_is_hidden_and_cycles_do_not_loop():
    layers = [layer("a", field("select", "k", "x", position=1),
                    field("text", "gate", "open", position=2, cond_fields=[cond("a", "k", "other")]),
                    field("text", "child", "kid", position=3, cond_fields=[cond("a", "gate", "open")]),
                    field("text", "p", "p", position=4, cond_fields=[cond("a", "q", "q")]),
                    field("text", "q", "q", position=5, cond_fields=[cond("a", "p", "p")]))]
    model = generic_view(element(*layers))
    assert rows(model, "A") == {"K": ("x", "")}  # gate is hidden, so is its child; p and q wait for each other


# -- formulas ----------------------------------------------------------------------------

def formula(text: str, values: dict | None = None, **more) -> str:
    fields = [field("integer", name, number) for name, number in (values or {}).items()]
    return formula_value(field("formula-field", "g", None, formula=text, **more), {"fields": fields})


@pytest.mark.parametrize("text, values, expected", [
    ("i*3", {"i": 7}, "21"), ("a+b*2", {"a": 1, "b": 2}, "5"), ("(a+b)*2", {"a": 1, "b": 2}, "6"),
    ("a/3", {"a": 1}, "0.33333"), ("2**3", {}, "8"), ("2^3^2", {}, "512"), ("-a+4", {"a": 1}, "3"),
    ("sqrt(a)", {"a": 16}, "4"), ("missing+1", {}, "1"), ("10%4", {}, "2"), ("a*1.5e1", {"a": 2}, "30"),
    ("max(1,a)", {"a": 5}, "5"), ("pi", {}, "3.14159"),
])
def test_formula_field(text, values, expected):
    assert formula(text, values) == expected


def test_formula_field_decimals_and_failures():
    assert formula("1/3", decimal="2") == "0.33" and formula("1/3", decimal=3) == "0.333"
    assert formula("1/3", decimal="0") == "0.33333"  # `toNullOrInt(decimal) || 5`
    for broken in ("", "1/0", "(1", "1+", "foo(1)", "1 2", "$", "sqrt(-1)"):
        assert formula(broken) == "", broken
    system = field("system-defined", "e", "2.5", value_system="eV")
    assert formula_value(field("formula-field", "g", None, formula="e*2"), {"fields": [system]}) == "5"


def test_an_adjusted_formula_value_is_shown_too():
    model = generic_view(element(layer("l", field("integer", "i", 2), field("formula-field", "g", "9", position=2,
                                                                         formula="i*3", canAdjust="true"))))
    assert rows(model, "L") == {"I": ("2", ""), "G": ("6", ""), "G (adjusted)": ("9", "")}


def test_text_formula():
    layers = {"l": {"fields": [field("text", "a", "x"), field("text", "b", ""), field("integer", "n", 5),
                               field("table", "t", None, sub_values=[{"c": "1"}, {"c": ""}, {"c": "3"}])]}}

    def joined(*subs):
        return text_formula_value({"text_sub_fields": list(subs)}, layers)

    assert joined({"layer": "l", "field": "a", "separator": "-"}, {"layer": "l", "field": "b", "separator": "-"},
                  {"layer": "l", "field": "n", "separator": ""}) == "x-5"
    assert joined({"layer": "l", "field": "t[@@]c", "separator": "/"}) == "1/3/"  # the last one too
    assert joined({"layer": "l", "field": "a[@@]b[@@]c", "separator": "/"}) == ""
    assert joined({"layer": "nope", "field": "a", "separator": "/"}, {"layer": "l", "field": "", "separator": "/"}) == ""
    assert joined({"layer": "l", "field": "a"}) == "x"


# -- robustness ----------------------------------------------------------------------------

def test_layer_and_field_order():
    layers = [layer("late", field("text", "x", "x1"), label="Late", position=30),
              layer("first", field("text", "b", "b2", position=2), field("text", "a", "a1", position=1), label="First",
                    position=10),
              layer("wf2", field("text", "w", "w"), label="Wf two", position=20, wf_position=2),
              layer("wf1", field("text", "w", "w"), label="Wf one", position=20, wf_position=1)]
    data = element(*layers)
    for entry, position in zip(data["properties"]["layers"].values(), (30, 10, 20, 20)):
        entry["position"] = position
    model = generic_view(data)
    assert titles(model) == ["First", "Wf one", "Wf two", "Late"]
    assert [r["label"] for r in section(model, "First")["rows"]] == ["A", "B"]


def test_a_layer_without_label_uses_its_key_and_a_name_the_element_lacks():
    model = generic_view(element(layer("lay", field("text", "f", "v"), label="")))
    assert titles(model) == ["lay"]
    data = element(layer("l", field("text", "f", "v")))
    data["name"] = ""
    assert generic_view(data)["title"] == "G-1" and generic_view(data)["subtitle"] == ""


def test_garbage_never_fails():
    junk = [None, 3, "x", [], {}, {"layers": None}, {"layers": {"a": None, "b": {"fields": None}, "c": {"fields": [None, 3]}}},
            {"layers": {"a": {"key": "a", "fields": [{"type": None, "field": None, "value": {"x": [1]},
                                                    "cond_fields": [None, 3, {"layer": 1}], "sub_fields": "no"}]}}}]
    for properties in junk:
        for extra in ({}, {"segments": [None, 3, {"properties": properties}], "element_klass": [1], "user_labels": 3}):
            model = generic_view({"id": 1, "name": None, "properties": properties, **extra})
            json.dumps(model)
            assert isinstance(model["sections"], list)
            assert isinstance(segment_sections({"segments": extra.get("segments", properties)}), list)
    fields = [field("table", "t", None, sub_fields=[None, {"id": "a"}, {"type": "select"}], sub_values=[None, 3, {"a": {"x": 1}}]),
              field("input-group", "g", None, sub_fields=[3, None, {"type": "system-defined", "value": "1"}]),
              field("formula-field", "f", None, formula="1+"), field("text-formula", "x", None, text_sub_fields=[3, {}]),
              field("upload", "u", "text"), field("select-multi", "m", None, sub_fields=[None, 3]),
              field("datetime-range", "r", None, sub_fields=[None, 3]), field("drag_sample", "d", "text")]
    json.dumps(generic_view(element(layer("l", *fields))))


def test_a_huge_unknown_value_is_clipped():
    value = one_row("future", {"k": "x" * 5000})[0]
    assert len(value) <= element_generic.MAX_RAW + 3 and value.endswith("...")


# -- segments ----------------------------------------------------------------------------------

def test_segments_on_any_element():
    seg = lambda label, text, **more: {"id": 1, "klass_label": label, "properties": {"layers": {  # noqa: E731
        "one": {"key": "one", "label": "One", "position": 1, "fields": [field("text", "t", text)]}}, **more}}
    data = {"segments": [seg("Alpha", "first"), seg("Beta", ""), seg("", "no label"),
                         seg("Gamma", "", select_options={}), {"id": 5}]}
    sections = segment_sections(data)
    assert [s["title"] for s in sections] == ["Alpha · One", "Segment · One"]
    assert segment_sections({}) == [] and segment_sections({"segments": None}) == []
    chosen = seg("Sel", "", select_options={"o": {"options": [{"key": "k", "label": "Kay"}]}})
    chosen["properties"]["layers"]["one"]["fields"] = [field("select", "s", "k", option_layers="o")]
    assert segment_sections({"segments": [chosen]})[0]["rows"] == [{"label": "S", "value": "Kay", "unit": ""}]
    chosen["properties"].pop("select_options")
    chosen["properties_release"] = {"select_options": {"o": {"options": [{"key": "k", "label": "Release"}]}}}
    assert segment_sections({"segments": [chosen]})[0]["rows"][0]["value"] == "Release"


def test_segment_sections_reach_every_adapter(monkeypatch):
    extra = [{"title": "Seg · L", "kind": "fields", "rows": [{"label": "a", "value": "1", "unit": ""}]}]
    monkeypatch.setattr(element_views, "SEGMENT_SECTIONS", lambda data: extra if data.get("segments") else [])
    monkeypatch.setitem(element_views.ADAPTERS, "vessel", lambda data, images, url: {"type": "vessel", "sections": []})
    assert element_views.view("vessel", {"segments": [1]})["sections"] == extra
    assert element_views.view("vessel", {"segments": []})["sections"] == []


# -- the real helper on the test instance ------------------------------------------------

@pytest.mark.chemotion
def test_generic_element_and_segment_through_the_real_helper(chemotion, test_collection, instance):
    from explorer import ExplorerProxy

    collection = instance.get_root_collection().get_collection(test_collection)
    if "try" not in instance.get_root_collection()._element_manager.all_classes:
        pytest.skip("the test instance has no generic type 'try'")
    made = []
    login = SimpleNamespace(verify_tls=True, explorer_types=None, **chemotion)
    proxy = ExplorerProxy(load_config=lambda: login, save_setting=lambda k, v: None)
    try:
        generic = collection._create_new_element("try")
        generic.name = "Generic details test"
        generic.properties["Nr. 1"]["Name"] = "Hello"
        generic.properties["Nr. 1"]["Table"] = [{"Col 1": "x", "Col 2": "y"}]
        generic.save()
        made.append(generic)
        model = proxy.details("try", generic.id, collection.id)
        assert model["type"] == "try" and model["id"] == generic.id and model["title"] == "Generic details test"
        assert model["subtitle"].startswith("Tryout · ") and model["subtitle"].endswith("version 1.0")
        assert model["url"] == f"{chemotion['url']}/mydb/collection/{collection.id}/try/{generic.id}"
        assert rows(model, "Nr. 1")["Name"] == ("Hello", "")
        assert section(model, "Nr. 1 · Table")["rows"] == [["x", "y"]]
        with pytest.raises(Exception, match="(?i)error|type"):
            proxy.details("wrk", generic.id, collection.id)  # the id of an element of another type
        assert proxy.details("vessel", 1) == {"type": "vessel", "id": 1, "unsupported": True}

        sample = collection.new_sample_smiles("CCO")
        classes = instance.get_root_collection()._element_manager.all_classes
        segments = sample.segments
        if "ApiTester" not in segments.available():
            pytest.skip("the test instance has no generic segment for samples")
        segments.get("ApiTester")["One"]["Text"] = "segment text"
        sample.save()
        made.append(sample)
        assert "sample" in classes
        model = proxy.details("sample", sample.id, collection.id)
        assert rows(model, "ApiTester · One") == {"Text": ("segment text", "")}
        assert model["title"] == "ethanol" and titles(model)[-1] == "ApiTester · One"  # after the sample's own sections
    finally:
        proxy.close()
        for item in made:
            try:
                item.delete(delete_subsamples=True)
            except Exception:  # cleanup must not fail a passing test
                pass


def test_every_model_names_its_short_label_and_name_for_the_mention():
    """details_logic.linked_mention() must not take the class version for the short label."""
    import details_logic
    from element_views import view

    data = {"id": 7, "short_label": "TRY-7", "name": "Run 7", "element_klass": {"label": "Tryout", "version": "1.2"},
            "properties": {"layers": {"one": {"key": "one", "label": "One", "position": 1, "fields": [
                {"type": "text", "field": "f", "label": "F", "value": "x", "position": 1}]}}}}
    model = view("try", data)
    assert (model["short_label"], model["name"]) == ("TRY-7", "Run 7")
    item = {"type": "try", "id": 7, "collection": "A/B", "shared": False, "collection_id": 3}
    text = details_logic.linked_mention(item, model)
    assert "TRY-7" in text and "Run 7" in text and "1.2" not in text

