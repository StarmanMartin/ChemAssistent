"""element_views.py: the view models of elements (sample adapter against the
recorded JSON of ELN 4.0.1 and the built JSON of ELN 3.1.x), the formatting
helpers and, through the real helper, the details of a sample created on the
Chemotion test instance."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import element_views
from element_views import delta_text, fmt_number, sample_view, view

FIXTURES = Path(__file__).with_name("fixtures") / "elements"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def section(model: dict, title: str) -> dict:
    return next(s for s in model["sections"] if s["title"] == title)


def rows(model: dict, title: str) -> dict[str, tuple[str, str]]:
    return {r["label"]: (r["value"], r["unit"]) for r in section(model, title)["rows"]}


def titles(model: dict) -> list[str]:
    return [s["title"] for s in model["sections"]]


# -- helpers -------------------------------------------------------------------

def test_fmt_number():
    assert fmt_number(None) == "" and fmt_number("") == "" and fmt_number("  ") == ""
    assert fmt_number(12) == "12" and fmt_number("12") == "12"
    assert fmt_number(1.5) == "1.5" and fmt_number(1.0) == "1"
    assert fmt_number(122.12133999999999) == "122.1213"
    assert fmt_number(122.12133999999999, 6) == "122.12134"
    assert fmt_number("0.98") == "0.98" and fmt_number(0.00004) == "0"
    assert fmt_number(-0.00001) == "0"
    assert fmt_number("n/a") == "n/a"
    assert fmt_number(float("nan")) == "" and fmt_number(float("inf")) == ""
    assert fmt_number(True) == ""


def test_delta_text():
    assert delta_text(None) == "" and delta_text("  plain ") == "plain"
    delta = {"ops": [{"insert": "Hello "}, {"insert": {"image": "x.png"}}, {"insert": "world\n"},
                     {"insert": "Second", "attributes": {"bold": True}}, {"insert": "\n"}]}
    assert delta_text(delta) == "Hello world\nSecond"
    assert delta_text(json.dumps(delta)) == "Hello world\nSecond"
    assert delta_text('{"ops": broken') == '{"ops": broken'
    assert delta_text({"ops": []}) == "" and delta_text(5) == ""


# -- the sample, ELN 4.0.1 (recorded) ------------------------------------------

def test_sample_v401_header_and_order():
    data = load("sample_v401.json")
    model = view("sample", data, "<svg/>", "http://eln/mydb/collection/1/sample/325")
    assert model["type"] == "sample" and model["id"] == 325
    assert model["title"] == "Benzoic acid test" and model["subtitle"] == "TST-1 · C7H6O2"
    assert model["updated_at"] == data["updated_at"]
    assert model["url"].endswith("/sample/325")
    assert model["image"] == {"mime": "image/svg+xml", "data": "<svg/>"}
    assert titles(model) == ["Sample", "Molecule", "Amount (target)", "Purity and density", "Physical properties",
                             "Solvents", "Description", "Chemical identifiers", "Elemental composition"]
    json.dumps(model)  # plain data


def test_sample_v401_values_and_units():
    model = sample_view(load("sample_v401.json"), None, "")
    assert model["image"] is None and model["url"] == ""
    sample = rows(model, "Sample")
    assert sample["Molecule name"] == ("benzoic acid", "") and sample["Location"] == ("Shelf 3", "")
    assert sample["External label"] == ("EXT-1", "") and sample["Stereo abs"] == ("any", "")
    assert "Anhydrous" not in sample and "Decoupled" not in sample and "Inventory label" not in sample
    molecule = rows(model, "Molecule")
    assert molecule["Molecular mass"] == ("122.12134", "g/mol")
    assert molecule["Exact mass"] == ("122.036779", "g/mol")
    assert molecule["CAS"] == ("65-85-0", "") and molecule["Sum formula"] == ("C7H6O2", "")
    # 1.5 g, density 1.27 g/ml, purity 0.98, M = 122.12134: the web client's mg / ml / mmol
    assert rows(model, "Amount (target)") == {"Mass": ("1500", "mg"), "Volume": ("1.1811", "ml"),
                                              "Amount of substance": ("12.0372", "mmol")}
    assert rows(model, "Purity and density") == {"Purity/Concentration": ("0.98", ""), "Density": ("1.27", "g/ml")}
    physical = rows(model, "Physical properties")
    assert physical["Melting point"] == ("121 – 123", "°C")
    assert physical["Boiling point"] == ("249", "°C")  # lower = upper
    assert physical["Flash point"] == ("121", "°C") and physical["Physical state"] == ("Solid", "")
    assert physical["Moisture"] == ("0.5", "%") and physical["Particle size"] == ("20", "µm")
    assert physical["Soluble in"] == ("hot water", "")
    assert section(model, "Solvents") == {"title": "Solvents", "kind": "table", "columns": ["Label", "Ratio"],
                                          "rows": [["Water", "1"]]}
    assert section(model, "Description") == {"title": "Description", "kind": "text",
                                             "text": "Recrystallised from water.\nSecond line."}
    ident = rows(model, "Chemical identifiers")
    assert ident["InChIKey"][0] == "WPYMKLBDIGXBTP-UHFFFAOYSA-N" and ident["Canonical SMILES"][0] == "OC(=O)c1ccccc1"
    assert section(model, "Elemental composition")["rows"][0] == ["C", "68.85 %", ""]


def test_empty_values_and_sections_are_left_out():
    data = {"id": 1, "type": "sample", "name": "", "molecule": {}, "xref": {}, "solvent": [], "description": ""}
    model = sample_view(data, None, "")
    assert model["sections"] == [] or all(s["rows"] if "rows" in s else s["text"] for s in model["sections"])
    assert model["title"] == "" and model["subtitle"] == "" and model["notes"] == []
    data = {"id": 2, "name": "x", "short_label": "A-1", "stereo": {}, "location": "  ", "melting_point": "",
            "target_amount_value": 0, "density": 0, "molarity_value": 0.0, "purity": None}
    model = sample_view(data, None, "")
    assert titles(model) == ["Sample"]
    assert [r["label"] for r in section(model, "Sample")["rows"]] == ["Sample name", "Short label"]


def test_title_falls_back_to_the_molecule():
    data = load("sample_v401.json")
    data["name"] = ""
    assert sample_view(data, None, "")["title"] == "benzoic acid"  # showed_name
    data["showed_name"] = ""
    assert sample_view(data, None, "")["title"] == "benzoic acid"  # iupac_name


def test_volume_from_molarity_and_amounts_in_other_units():
    data = load("sample_v401.json")
    data.update(density=0.0, molarity_value=0.5, target_amount_value=0.2, target_amount_unit="l",
                real_amount_value=0.0, purity=1.0, metrics="mmmm")
    amount = rows(sample_view(data, None, ""), "Amount (target)")
    assert amount["Volume"] == ("200", "ml")
    assert amount["Amount of substance"] == ("100", "mmol")  # 0.2 l * 0.5 M
    assert amount["Mass"] == ("12212.134", "mg")
    data.update(density=1.27, molarity_value=0.0, target_amount_value=10.0, target_amount_unit="mol", purity=1.0)
    amount = rows(sample_view(data, None, ""), "Amount (target)")
    assert amount["Mass"][0].startswith("1221213") and amount["Amount of substance"] == ("10000", "mmol")


def test_metrics_choose_the_prefix_and_no_density_means_no_volume():
    data = load("sample_v401.json")
    data.update(metrics="unun", density=0.0, molarity_value=0.0)
    amount = rows(sample_view(data, None, ""), "Amount (target)")
    assert amount["Mass"] == ("1500000", "µg") and "Volume" not in amount
    assert amount["Amount of substance"][1] == "\u00b5mol"  # metrics[2] is the substance amount's prefix
    data.update(metrics="nnnn")
    assert rows(sample_view(data, None, ""), "Amount (target)")["Mass"] == ("1.5", "g")


def test_real_amount_has_its_own_section_and_decoupled_uses_own_mass():
    data = load("sample_v401.json")
    data.update(real_amount_value=0.75, real_amount_unit="g")
    model = sample_view(data, None, "")
    assert rows(model, "Amount (real)")["Mass"] == ("750", "mg") and "Amount (target)" in titles(model)
    data.update(decoupled=True, molecular_mass=100.0, sum_formula="X2")
    model = sample_view(data, None, "")
    assert rows(model, "Molecule")["Molecular mass"] == ("100", "g/mol") and "Exact mass" not in rows(model, "Molecule")
    assert rows(model, "Sample")["Decoupled"] == ("yes", "") and model["subtitle"] == "TST-1 · X2"


def test_ranges():
    from element_views import _range

    assert _range({"melting_point": "-Infinity..65.0"}, "melting_point") == "<65"
    assert _range({"melting_point": "300.0..Infinity"}, "melting_point") == "300"
    assert _range({"melting_point": "65.0...68.5"}, "melting_point") == "65 – 68.5"
    assert _range({"melting_point_lowerbound": 10, "melting_point_upperbound": 12}, "melting_point") == "10 – 12"
    assert _range({"melting_point": "Infinity..Infinity"}, "melting_point") == ""
    assert _range({"melting_point": None}, "melting_point") == ""
    assert _range({"xref": {"melting_point_label": ">300"}, "melting_point": "300..300"}, "melting_point") == ">300"
    model = sample_view({"id": 1, "xref": {"melting_point_decomposed": True}}, None, "")
    assert rows(model, "Physical properties")["Melting point"][0] == "Decomposed"


def test_image_limit_note():
    data = load("sample_v401.json")
    big = "<svg>" + "x" * element_views.MAX_IMAGE_BYTES + "</svg>"
    model = sample_view(data, big, "")
    assert model["image"] is None and model["notes"] == [element_views.IMAGE_TOO_LARGE]
    assert sample_view(data, "<svg>" + "x" * 1000 + "</svg>", "")["image"] is not None


def test_unsupported_types_give_the_stub():
    assert view("reaction", {"id": 9, "name": "R"}) == {"type": "reaction", "id": 9, "unsupported": True}
    assert view("my_generic", {}) == {"type": "my_generic", "id": None, "unsupported": True}
    assert "sample" in element_views.SUPPORTED


def test_mixture_and_polymer_use_the_stored_amount_only():
    data = load("sample_v401.json")
    data["sample_type"] = "Mixture"
    model = sample_view(data, None, "")
    assert rows(model, "Amount (target)") == {"Mass": ("1500", "mg")}
    assert model["notes"] and "Elemental composition" not in titles(model)
    assert model["subtitle"] == "TST-1"


# -- the sample, ELN 3.1.x (built from v312's empty sample) -----------------------

def test_sample_v312():
    data = load("sample_v312.json")
    model = sample_view(data, None, "")
    assert model["title"] == "Caffeine batch 2" and model["subtitle"] == "TST-2 · C8H10N4O2"
    sample = rows(model, "Sample")
    assert sample["Anhydrous"] == ("yes", "") and sample["Labels"] == ("Favourite", "")
    assert rows(model, "Molecule")["CAS"] == ("58-08-2", "")
    # target 0.5 g and real 0.48 g; molarity 0.1 M with purity 0.99
    assert rows(model, "Amount (target)")["Mass"] == ("500", "mg")
    real = rows(model, "Amount (real)")
    assert real["Mass"] == ("480", "mg") and real["Volume"][1] == "ml"
    assert rows(model, "Purity and density")["Molarity"] == ("0.1", "M")
    physical = rows(model, "Physical properties")
    assert physical["Melting point"] == ("234 – 236", "°C") and physical["Boiling point"] == ("<178", "°C")
    assert physical["Flash point"] == ("-", "°C")
    other = rows(model, "Other fields")
    assert other == {"lab_note": ("keep dry", "")}  # private notes stay out
    solvents = section(model, "Solvents")
    assert solvents["columns"] == ["Label", "Ratio", "Purity", "Vendor"]
    assert solvents["rows"] == [["Acetonitrile", "3", "1", "Sigma"], ["Water", "1", "", ""]]
    assert section(model, "Description")["text"] == "Bought from the shop.\nChecked by NMR."
    assert section(model, "Elemental composition")["rows"][0] == ["C", "49.48 %", "49.3 %"]
    assert "Chemical identifiers" in titles(model)


def test_old_solvent_formats_and_missing_keys_do_not_raise():
    data = load("sample_v312.json")
    data["solvent"] = {"label": "THF", "ratio": 1}
    assert section(sample_view(data, None, ""), "Solvents")["rows"] == [["THF", "1"]]
    data["solvent"] = ["Hexane"]
    assert section(sample_view(data, None, ""), "Solvents")["rows"] == [["Hexane", ""]]
    for key in ("molecule", "xref", "stereo", "user_labels", "elemental_compositions", "solvent"):
        broken = copy.deepcopy(load("sample_v312.json"))
        broken[key] = "garbage"
        sample_view(broken, None, "")
        broken[key] = None
        sample_view(broken, None, "")


# -- through the real helper -----------------------------------------------------

@pytest.mark.chemotion
def test_details_through_the_real_helper(chemotion, test_collection, instance, cleanup_samples):
    from explorer import ExplorerProxy

    collection = instance.get_root_collection().get_collection(test_collection)
    sample = collection.new_sample_smiles("c1ccccc1C(=O)O")
    sample.properties["name"] = "Details test"
    sample.properties["target_amount"] = {"value": 2.0, "unit": "g"}
    sample.properties["purity"] = 0.9
    sample.properties["density"] = 1.2
    sample.properties["melting_point_lowerbound"] = 121
    sample.properties["melting_point_upperbound"] = 123
    sample.properties["description"] = "Made for the test."
    sample.properties["solvent"].add_new_name("Water")
    sample.save()
    cleanup_samples.append(sample.id)

    login = SimpleNamespace(verify_tls=True, explorer_types=None, **chemotion)
    proxy = ExplorerProxy(load_config=lambda: login, save_setting=lambda k, v: None)
    try:
        model = proxy.details("sample", sample.id, collection.id)
        assert model["title"] == "Details test" and model["id"] == sample.id
        assert model["url"] == f"{chemotion['url']}/mydb/collection/{collection.id}/sample/{sample.id}"
        assert model["updated_at"]
        assert model["image"] and model["image"]["mime"] == "image/svg+xml" and "<svg" in model["image"]["data"]
        assert rows(model, "Amount (target)")["Mass"] == ("2000", "mg")
        assert rows(model, "Physical properties")["Melting point"] == ("121 – 123", "°C")
        assert section(model, "Solvents")["rows"][0][0] == "Water"
        assert section(model, "Description")["text"] == "Made for the test."
        assert proxy.details("sample", sample.id)["url"] == ""
        # a listing carries the cache key the panel uses
        node = next(n for n in proxy.collections() if n["label"] == "AIExamples")["children"][0]
        item = next(i for i in proxy.page(node, "sample")["items"] if i["id"] == sample.id)
        assert item["updated_at"]
        assert proxy.details("reaction", 1) == {"type": "reaction", "id": 1, "unsupported": True}
    finally:
        proxy.close()
