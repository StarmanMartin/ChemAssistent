"""element_types.py: the view models of research plan, wellplate, screen, cell
line, device description and SBMM sample against recorded JSON of ELN 4.0.1
(*_v401.json: read from the Chemotion test instance with chemotion_api, the
container/uniprot noise trimmed; the *_full / *_modified / *_fields / *_readouts
ones extend a recorded element by hand with values the instance lacked) and
built JSON of ELN 3.1.x (*_v312.json, after chemotion_api v312's empty_elements).
No GTK, no server."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import element_types
from element_types import ADAPTERS, IMAGE_REFS, row_label, well_position

FIXTURES = Path(__file__).with_name("fixtures") / "elements"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build(type_name: str, name: str, images: dict | None = None) -> dict:
    return ADAPTERS[type_name](load(name), images or {}, "http://eln/x")


def section(model: dict, title: str) -> dict:
    return next(s for s in model["sections"] if s["title"] == title)


def rows(model: dict, title: str) -> dict:
    return {r["label"]: (r["value"], r["unit"]) for r in section(model, title)["rows"]}


def titles(model: dict) -> list[str]:
    return [s["title"] for s in model["sections"]]


def every_string(model) -> list[str]:
    out = []
    if isinstance(model, dict):
        for value in model.values():
            out += every_string(value)
    elif isinstance(model, list):
        for value in model:
            out += every_string(value)
    elif isinstance(model, str):
        out.append(model)
    return out


ALL = [
    ("research_plan", "research_plan_v401.json"), ("research_plan", "research_plan_fields_v401.json"),
    ("research_plan", "research_plan_v312.json"),
    ("wellplate", "wellplate_v401.json"), ("wellplate", "wellplate_readouts_v401.json"),
    ("wellplate", "wellplate_v312.json"),
    ("screen", "screen_v401.json"), ("screen", "screen_v312.json"),
    ("cell_line", "cell_line_v401.json"), ("cell_line", "cell_line_full_v401.json"),
    ("device_description", "device_description_v401.json"),
    ("device_description", "device_description_full_v401.json"),
    ("sequence_based_macromolecule_sample", "sbmm_v401.json"),
    ("sequence_based_macromolecule_sample", "sbmm_modified_v401.json"),
]


def test_registry():
    assert set(ADAPTERS) == {"research_plan", "wellplate", "screen", "cell_line", "device_description",
                             "sequence_based_macromolecule_sample"}
    assert set(IMAGE_REFS) <= set(ADAPTERS)


@pytest.mark.parametrize("type_name,name", ALL)
def test_model_shape_and_no_empty_values(type_name, name):
    model = build(type_name, name)
    assert model["type"] == type_name and model["title"] and model["url"] == "http://eln/x"
    assert not model.get("unsupported")
    assert model["sections"]
    for sec in model["sections"]:
        assert sec["kind"] in ("fields", "table", "text", "image")
        if sec["kind"] == "fields":
            assert sec["rows"] and all(r["value"] for r in sec["rows"])
        if sec["kind"] == "table":
            assert sec["rows"] and all(len(r) == len(sec["columns"]) for r in sec["rows"])
            if "targets" in sec:
                assert len(sec["targets"]) == len(sec["rows"])
    assert all(isinstance(s, str) for s in every_string(model))
    json.dumps(model)  # plain data


@pytest.mark.parametrize("type_name", sorted(ADAPTERS))
def test_adapters_survive_garbage(type_name):
    for data in ({}, {"id": 1}, {"id": 1, "body": "x", "wells": None, "wellplates": 3, "research_plans": [None],
                                 "cellline_material": [], "sequence_based_macromolecule": "x",
                                 "operators": ["a"], "readout_titles": 4}):
        model = ADAPTERS[type_name](data, {}, "")
        assert model["type"] == type_name


# -- research plan --------------------------------------------------------------

def test_research_plan_body_in_order():
    data = load("research_plan_v401.json")
    refs = IMAGE_REFS["research_plan"](data)
    (key, path), = refs.items()
    assert path == "/api/v1/attachments/image/-1?identifier=7a90991f-6016-4f2e-bd10-7d6e94906504"
    image = {"mime": "image/png", "data": "AAAA", "encoding": "base64"}
    model = ADAPTERS["research_plan"](data, {key: image}, "")
    assert [s["kind"] for s in model["sections"]] == ["text", "image", "text", "table"]
    assert model["sections"][0]["text"] == "Main Header" and model["sections"][2]["text"] == "Super Text"
    assert model["sections"][1]["image"] == image and "kadi.png" in model["sections"][1]["title"]
    table = model["sections"][3]
    assert table["columns"] == ["Name", "Age", "Is Developer"]
    assert table["rows"] == [["Mr. X", "30", ""], ["Mr. Y", "40", ""]]
    assert model["title"] == "New Research Plan" and model["subtitle"] == "API-RP7" and model["notes"] == []


def test_research_plan_without_loaded_image_says_so():
    model = build("research_plan", "research_plan_v401.json")
    assert [s["kind"] for s in model["sections"]] == ["text", "text", "table"]
    assert "could not be loaded" in model["notes"][0]


def test_research_plan_fields_targets_and_image_paths():
    data = load("research_plan_fields_v401.json")
    refs = IMAGE_REFS["research_plan"](data)
    assert refs == {"body:c1f5": "/images/research_plans/0f3a9c1d.svg",   # ketcher field
                    "body:c1f6": "/images/research_plans/1234.png"}       # legacy image (public name with a dot)
    model = ADAPTERS["research_plan"](data, {"body:c1f5": {"mime": "image/svg+xml", "data": "<svg/>"}}, "")
    sample = section(model, "Sample")
    assert sample["targets"] == [{"type": "sample", "id": 325}]
    assert section(model, "Reaction")["targets"] == [{"type": "reaction", "id": 41}]
    assert section(model, "Structure")["kind"] == "image"
    assert section(model, "Wellplates")["targets"] == [{"type": "wellplate", "id": 14}]
    assert any("future_field" in n for n in model["notes"])
    assert any("Image: old.png" in n for n in model["notes"])  # its image was not loaded


def test_research_plan_richtext_markup_when_the_helper_gives_it():
    data = {"id": 1, "name": "x", "body": [{"id": "a", "type": "richtext", "value": {"ops": [
        {"insert": "A & B <c>\n"}]}}]}
    sec = ADAPTERS["research_plan"](data, {}, "")["sections"][0]
    assert sec["text"] == "A & B <c>"
    if "markup" in sec:
        assert "&amp;" in sec["markup"] and "<c>" not in sec["markup"]


def test_research_plan_v312():
    model = build("research_plan", "research_plan_v312.json")
    assert model["sections"][0]["text"] == "Hello"


# -- wellplate ------------------------------------------------------------------

def test_row_labels_and_positions():
    assert [row_label(i) for i in (0, 1, 26, 27, 28, 52, 53, 104)] == ["", "A", "Z", "AA", "AB", "AZ", "BA", "CZ"]
    assert well_position({"position": {"x": 2, "y": 2}}) == "B2"
    assert well_position({"position": {"x": 12, "y": 8}}) == "H12"
    assert well_position({}) == "n/a" and well_position({"position": {"x": 0, "y": 1}}) == "n/a"


def test_wellplate_lists_only_filled_wells():
    model = build("wellplate", "wellplate_v401.json")
    assert rows(model, "Wellplate")["Size"] == ("96 (12x8)", "")
    wells = next(s for s in model["sections"] if s["title"].startswith("Wells"))
    assert wells["title"] == "Wells (1 of 4 filled)"
    assert wells["columns"][:5] == ["Position", "Name", "External label", "Sum formula", "Molarity (M)"]
    assert wells["rows"] == [["B2", "API-259-2", "", "BH3", ""]]
    assert wells["targets"] == [{"type": "sample", "id": 324}]


def test_wellplate_readouts_and_additive():
    model = build("wellplate", "wellplate_readouts_v401.json")
    assert rows(model, "Wellplate")["Readouts"] == ("Readout1, Readout2", "")
    wells = section(model, "Wells (4 of 4 filled)")
    assert wells["columns"][-3:] == ["Additive", "Readout1", "Readout2"]
    assert wells["rows"][2][-3:] == ["DMSO", "0.12 mg", "12 GW"]
    assert wells["targets"] == [None] * 4


def test_wellplate_without_filled_wells_has_no_wells_table():
    data = load("wellplate_v401.json")
    data["wells"] = [w for w in data["wells"] if not w["sample"]]
    model = ADAPTERS["wellplate"](data, {}, "")
    assert not any(t.startswith("Wells") for t in titles(model))


def test_wellplate_v312_size_and_decoupled_sample():
    model = build("wellplate", "wellplate_v312.json")
    assert rows(model, "Wellplate")["Size"] == ("96", "")
    wells = section(model, "Wells (1 of 2 filled)")
    assert wells["rows"] == [["A1", "S-9", "ext", "C2H6O", "0.5", "7.2 pH"]]


def test_wellplate_caps_the_rows():
    data = load("wellplate_v401.json")
    well = data["wells"][0]
    data["wells"] = [dict(well, position={"x": 1 + i % 50, "y": 1 + i // 50}) for i in range(element_types.MAX_TABLE_ROWS + 30)]
    model = ADAPTERS["wellplate"](data, {}, "")
    wells = next(s for s in model["sections"] if s["title"].startswith("Wells"))
    assert len(wells["rows"]) == len(wells["targets"]) == element_types.MAX_TABLE_ROWS
    assert "first 200" in model["notes"][0]


# -- screen ---------------------------------------------------------------------

def test_screen():
    model = build("screen", "screen_v401.json")
    assert model["title"] == "API test screen"
    assert rows(model, "Screen") == {"Collaborator": ("Dr. Example", ""), "Requirements": ("none", ""),
                                     "Conditions": ("37 °C", ""), "Result": ("All wells grew", "")}
    plates = section(model, "Wellplates")
    assert plates["rows"] == [["Plate 1-Split", "Control plate"], ["Readout plate", ""]]
    assert plates["targets"] == [{"type": "wellplate", "id": 16}, {"type": "wellplate", "id": 17}]
    assert section(model, "Research plans")["targets"] == [{"type": "research_plan", "id": 8}]
    assert "Description" not in titles(model)  # empty


def test_screen_v312():
    model = build("screen", "screen_v312.json")
    assert rows(model, "Screen") == {"Conditions": ("rt", "")}


# -- cell line ------------------------------------------------------------------

def test_cell_line_recorded_and_full():
    model = build("cell_line", "cell_line_v401.json")
    assert rows(model, "Common properties")["Organism"] == ("Homo sapiens", "")
    assert rows(model, "Common properties")["Tissue"] == ("cervix", "")
    assert "Disease" not in rows(model, "Common properties")
    assert rows(model, "Sample specific properties")["Amount"] == ("1000", "cells")
    assert rows(model, "Sample specific properties")["Passage"] == ("4", "")
    full = build("cell_line", "cell_line_full_v401.json")
    assert rows(full, "Common properties")["Biosafety level"] == ("Biosafety level 2", "")
    assert rows(full, "Common properties")["Opt. growth temperature"] == ("37", "")
    assert rows(full, "Sample specific properties")["Contamination"] == ("none", "")
    assert full["subtitle"] == "API-kind-c89a2c · ATCC"


# -- device description ---------------------------------------------------------

def test_device_description():
    model = build("device_description", "device_description_v401.json")
    assert model["title"].startswith("API test device") and model["subtitle"] == "API-Dev2"
    assert rows(model, "General description")["Manufacturer name"] == ("Acme", "")
    assert rows(model, "General description")["Tags"] == ("nmr", "")
    assert "Operators" not in titles(model) and "Maintenance" not in titles(model)
    assert "Physical descriptions" not in titles(model)


def test_device_description_full():
    model = build("device_description", "device_description_full_v401.json")
    assert rows(model, "Physical descriptions")["Weight"] == ("450", "kg")
    assert section(model, "Operators")["rows"] == [["A. Operator", "123", "a@example.org", "technical", ""]]
    assert section(model, "Planned maintenance")["rows"][0][:5] == ["2026-12-01", "inspection", "Yearly", "planned", "120.5"]
    assert rows(model, "Maintenance")["Scheduling"] == ("yearly", "")
    assert rows(model, "Version specific information")["Identifier (DOI)"] == ("10.1234/abc", "")
    assert titles(model).index("General description") < titles(model).index("Version specific information") \
        < titles(model).index("Operators") < titles(model).index("Maintenance")
    assert any("ontologies" in n for n in model["notes"])


# -- SBMM sample ----------------------------------------------------------------

def test_sbmm_recorded_reference_protein():
    model = build("sequence_based_macromolecule_sample", "sbmm_v401.json")
    protein = rows(model, "Protein")
    assert protein["UniProt number"] == ("P69905", "")
    assert protein["Derivation"][0].startswith("Protein used as described")
    assert protein["Sequence mass"] == ("15.258", "kD") and protein["Organism"] == ("Homo sapiens", "")
    assert section(model, "Sequence")["text"].startswith("MVLSPADKTN")
    assert "Modifications" not in titles(model)
    assert rows(model, "Sample characteristics")["Short label"] == ("API-sbmmS1", "")


def test_sbmm_modifications_and_amounts():
    model = build("sequence_based_macromolecule_sample", "sbmm_modified_v401.json")
    mods = rows(model, "Modifications")
    assert mods == {"N-terminal": ("Acetyl", ""), "Phosphorylation": ("Ser: Ser12", ""),
                    "Acetylation": ("Lysin No: 2", "")}
    chars = rows(model, "Sample characteristics")
    assert chars["Concentration"] == ("2.5", "ng/L") and chars["Activity in U/L"] == ("100", "U/L")
    assert chars["Function or application"] == ("Enzyme", "") and chars["Obtained by"] == ("Purchased", "")
    assert "Molarity" not in chars  # no value: no bare unit either
