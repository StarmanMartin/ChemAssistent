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
from element_views import delta_markup, delta_text, fmt_number, sample_view, view

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
                                             "text": "Recrystallised from water.\nSecond line.",
                                             "markup": "Recrystallised from water.\nSecond line."}
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
    assert view("vessel", {"id": 9, "name": "R"}) == {"type": "vessel", "id": 9, "unsupported": True}
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
        assert proxy.details("vessel", 1) == {"type": "vessel", "id": 1, "unsupported": True}
    finally:
        proxy.close()


# -- shared builders -------------------------------------------------------------

def test_fields_table_text_and_image_section():
    assert element_views.fields("T", []) is None
    assert element_views.fields("T", [("A", "", "g"), ("B", None), ("C", 2.5, "g"), None]) == {
        "title": "T", "kind": "fields", "rows": [{"label": "C", "value": "2.5", "unit": "g"}]}
    assert element_views.table("T", ["a", "b"], [["", ""], ["x", 3]], [None, {"type": "sample", "id": 1}]) == {
        "title": "T", "kind": "table", "columns": ["a", "b"], "rows": [["x", "3"]],
        "targets": [{"type": "sample", "id": 1}]}
    assert "targets" not in element_views.table("T", ["a"], [["x"]], [None])
    assert element_views.table("T", ["a"], [[""]]) is None
    assert element_views.text("T", "") is None and element_views.text("T", {"ops": [{"insert": "\n"}]}) is None
    assert element_views.text("T", "a <b>") == {"title": "T", "kind": "text", "text": "a <b>",
                                               "markup": "a &lt;b&gt;"}
    image = {"mime": "image/svg+xml", "data": "<svg/>"}
    assert element_views.image_section("Scheme", image) == {"title": "Scheme", "kind": "image", "image": image}
    assert element_views.image_section("Scheme", None) is None
    assert element_views.image_section("Scheme", {"mime": "image/png", "data": ""}) is None


def pango_ok(markup: str) -> bool:
    from xml.etree import ElementTree

    try:
        ElementTree.fromstring(f"<span>{markup}</span>")
    except ElementTree.ParseError:
        return False
    return True


def test_delta_markup_inline_attributes_and_escaping():
    delta = {"ops": [{"insert": "a<b> & "}, {"insert": "bold", "attributes": {"bold": True}},
                     {"insert": " "}, {"insert": "both", "attributes": {"bold": True, "italic": True}},
                     {"insert": " "}, {"insert": "u", "attributes": {"underline": True, "strike": True}},
                     {"insert": "2", "attributes": {"script": "sub"}}, {"insert": "3", "attributes": {"script": "super"}},
                     {"insert": "x", "attributes": {"code": True}},
                     {"insert": "\n"}]}
    markup = delta_markup(delta)
    assert markup == ("a&lt;b&gt; &amp; <b>bold</b> <b><i>both</i></b> <u><s>u</s></u><sub>2</sub><sup>3</sup>"
                      "<tt>x</tt>")
    assert pango_ok(markup)
    assert delta_markup(json.dumps(delta)) == markup  # JSON text of a delta
    assert delta_markup("plain <text> ") == "plain &lt;text&gt;" and delta_markup(None) == ""
    assert delta_markup('{"ops": broken') == "{&quot;ops&quot;: broken".replace("&quot;", '"')
    assert delta_markup({"ops": []}) == "" and delta_markup(5) == "5" and delta_markup([1]) == ""


def test_delta_markup_links_only_for_safe_schemes():
    ok = delta_markup({"ops": [{"insert": "ELN", "attributes": {"link": 'https://x.org/?a=1&b="2"'}}, {"insert": "\n"}]})
    assert ok == '<a href="https://x.org/?a=1&amp;b=&quot;2&quot;">ELN</a>' and pango_ok(ok)
    bad = delta_markup({"ops": [{"insert": "x", "attributes": {"link": "javascript:alert(1)"}}, {"insert": "\n"}]})
    assert bad == "x"


def test_delta_markup_blocks():
    delta = {"ops": [
        {"insert": "Title"}, {"insert": "\n", "attributes": {"header": 1}},
        {"insert": "para one\n\npara two\n"},
        {"insert": "first"}, {"insert": "\n", "attributes": {"list": "bullet"}},
        {"insert": "nested"}, {"insert": "\n", "attributes": {"list": "bullet", "indent": 1}},
        {"insert": "one"}, {"insert": "\n", "attributes": {"list": "ordered"}},
        {"insert": "two"}, {"insert": "\n", "attributes": {"list": "ordered"}},
        {"insert": {"image": "x.png"}}, {"insert": "text\n"},
        {"insert": "again"}, {"insert": "\n", "attributes": {"list": "ordered"}},
        {"insert": "\n\n"}]}
    markup = delta_markup(delta)
    assert markup.split("\n") == ["<b>Title</b>", "para one", "", "para two", "• first", "    • nested",
                                  "1. one", "2. two", "text", "1. again"]
    assert pango_ok(markup)


def test_delta_markup_never_yields_broken_markup():
    nasty = {"ops": [{"insert": "<<&>>", "attributes": {"bold": True, "link": "mailto:a@b.c?x=<y>"}},
                     {"insert": "\n"}, {"insert": 7}, {"insert": None}, "bare", {"attributes": {}}]}
    assert pango_ok(delta_markup(nasty))


# -- reaction ------------------------------------------------------------------------

SCHEME = {"mime": "image/svg+xml", "data": "<svg>scheme</svg>"}


def test_reaction_v401_header_materials_and_order():
    model = view("reaction", load("reaction_v401.json"), {"scheme": SCHEME}, "https://eln/x")
    assert model["type"] == "reaction" and model["id"] == 51 and model["url"] == "https://eln/x"
    assert model["title"] == "Esterification M7" and model["subtitle"] == "API-R51 · Running"
    assert model["image"] == SCHEME and model["notes"] == []
    assert titles(model) == ["Starting materials", "Reactants", "Solvents", "Products", "Conditions", "Reaction",
                             "Description", "Purification",
                             "Additional information for publication and purification details", "Safety",
                             "TLC-Control"]
    start = section(model, "Starting materials")
    assert start["columns"] == ["Label", "Name", "Ref", "T/R", "Mass", "Amount", "Equiv."]
    assert start["rows"] == [["API-511-1", "Benzoic acid", "yes", "T", "1000 mg", "8.1886 mmol", "1"]]
    assert start["targets"] == [{"type": "sample", "id": 576}]
    reactant = section(model, "Reactants")
    assert reactant["rows"][0][:2] == ["reactant", "Ethanol"] and reactant["rows"][0][-1] == "2"
    assert reactant["rows"][0][3] == "754.4699 mg"  # the target amount is 16.377 mmol
    product = section(model, "Products")
    assert product["columns"][-1] == "Yield" and product["rows"][0][-3:] == ["600.698 mg", "4 mmol", "49 %"]
    assert product["rows"][0][2] == "R" and product["targets"] == [{"type": "sample", "id": 579}]
    solvent = section(model, "Solvents")
    assert solvent["columns"] == ["Label", "Name"] and solvent["rows"] == [["solvent", "Toluene"]]
    assert section(model, "Conditions")["text"] == "Reflux\nInert atmosphere"
    reaction = rows(model, "Reaction")
    assert reaction["Status"] == ("Running", "") and reaction["Temperature"] == ("80", "°C")
    assert reaction["Time (duration)"] == ("2 Hour(s)", "") and reaction["Vessel size"] == ("50", "ml")
    assert reaction["Type (Name Reaction)"][0] == "namerxn:1.7.9" and reaction["Role"][0] == "parts"
    assert "pH" not in reaction and "Start" not in reaction
    assert rows(model, "Purification") == {"Purification": ("TLC, Filtration", "")}
    assert rows(model, "Safety") == {"Dangerous products": ("Mutagenic", "")}
    assert rows(model, "TLC-Control") == {"Solvents (parts)": ("EtOAc/Hex 1:4", ""), "Rf-Value": ("0.45", "")}
    assert section(model, "Description")["markup"] == "Stir for **2 h**."


def test_reaction_v312():
    model = view("reaction", load("reaction_v312.json"), {"scheme": SCHEME})
    assert model["title"] == "Esterification v312" and model["subtitle"] == "TST-R7 · Done"
    assert model["image"] == SCHEME
    assert titles(model) == ["Starting materials", "Reactants", "Solvents", "Products", "Conditions", "Reaction",
                             "Description", "Purification"]  # no dangerous products, TLC, observation
    reaction = rows(model, "Reaction")
    assert reaction["Temperature"] == ("25 ~ 80", "°C") and "Vessel size" not in reaction
    solvent = section(model, "Solvents")  # a solvent carries its amount in the old line
    assert solvent["rows"][0][1] == "Toluene" and "10 ml" in solvent["rows"][0]
    assert section(model, "Products")["rows"][0][-1] == "50 %"
    assert section(model, "Description")["markup"] == "Stirred for <b>2 h</b>."
    assert rows(model, "Purification") == {"Purification": ("TLC", "")}


def test_reaction_temperature_ph_and_purification_forms():
    data = load("reaction_v312.json")
    data["temperature"] = {"valueUnit": "°F", "userText": "reflux", "data": []}
    data.update(ph_value=7.25, ph_operator="<", purification=["TLC", "HPLC"], dangerous_products="Mutagenic",
                timestamp_start="2026-05-05 10:00", rf_value=0.5)
    reaction = rows(view("reaction", data), "Reaction")
    assert reaction["Temperature"] == ("reflux", "") and reaction["pH"] == ("< 7.25", "")
    assert reaction["Start"][0] == "2026-05-05 10:00"
    data["temperature"] = {"valueUnit": "°F", "userText": "21 ~ 25", "data": []}
    assert rows(view("reaction", data), "Reaction")["Temperature"] == ("21 ~ 25", "°F")
    data["temperature"] = {"valueUnit": "K", "userText": "", "data": [{"value": "300"}, {"value": "300"}]}
    assert rows(view("reaction", data), "Reaction")["Temperature"] == ("300", "K")
    model = view("reaction", data)
    assert rows(model, "Purification") == {"Purification": ("TLC, HPLC", "")}
    assert rows(model, "Safety") == {"Dangerous products": ("Mutagenic", "")}
    assert rows(model, "TLC-Control") == {"Rf-Value": ("0.5", "")}


def test_reaction_empty_and_odd_values_do_not_raise():
    model = view("reaction", {"id": 1, "short_label": "R-1", "temperature": None, "vessel_size": None,
                              "starting_materials": [{"id": "uuid", "short_label": "x"}, "junk", {}],
                              "products": None, "variations": [1, 2], "status": None})
    assert model["title"] == "R-1" and model["subtitle"] == "R-1" and model["image"] is None
    assert titles(model) == ["Starting materials"]
    assert "targets" not in section(model, "Starting materials")  # a new sample has no numeric id yet
    assert model["notes"] == ["The reaction has 2 variations; they are not shown yet."]
    empty = view("reaction", {"id": 2})
    assert empty["sections"] == [] and empty["title"] == ""


def test_images_reach_the_view_and_big_ones_are_left_out():
    data = load("reaction_v312.json")
    assert element_views.IMAGE_REFS["reaction"](data) == {"scheme": "/images/reactions/aa11.svg"}
    assert element_views.IMAGE_REFS["reaction"]({}) == {}
    big = {"mime": "image/svg+xml", "data": "x" * (element_views.MAX_IMAGE_BYTES + 1)}
    model = view("reaction", data, {"scheme": big})
    assert model["image"] is None and model["notes"] == ["The scheme image is larger than 2 MB and is not shown."]
    assert element_views.IMAGE_REFS["sample"]({"molecule": {"molecule_svg_file": "m.svg"}}) == {
        "image": "/images/molecules/m.svg"}
    assert element_views.IMAGE_REFS["sample"]({"sample_svg_file": "s.svg"}) == {"image": "/images/samples/s.svg"}
    assert view("sample", load("sample_v401.json"), {"image": SCHEME})["image"] == SCHEME


def test_supported_follows_the_adapters(monkeypatch):
    assert "reaction" in element_views.SUPPORTED and "vessel" not in element_views.SUPPORTED
    monkeypatch.setitem(element_views.ADAPTERS, "vessel", lambda data, images, url: {"type": "vessel"})
    assert "vessel" in element_views.SUPPORTED and view("vessel", {}) == {"type": "vessel"}


@pytest.mark.chemotion
def test_reaction_details_through_the_real_helper(chemotion, test_collection, instance, cleanup_samples):
    import requests
    from chemotion_api.elements.utils import QuantityUnit

    from explorer import ExplorerProxy

    collection = instance.get_root_collection().get_collection(test_collection)
    acid = collection.new_sample_smiles("OC(=O)c1ccccc1")
    acid.save()
    alcohol = collection.new_sample_smiles("CCO")
    alcohol.save()
    ester = collection.new_sample_smiles("CCOC(=O)c1ccccc1")
    ester.save()
    reaction = collection.new_reaction()
    reaction.properties["name"] = "Details test reaction"
    reaction.starting_materials.append(acid)
    reaction.reactants.append(alcohol)
    reaction.products.append(ester)
    reaction.solvents.append(collection.new_solvent("Toluene"))
    reaction.save()
    reaction = instance.get_reaction(reaction.id)
    reaction.reference_sample = reaction.starting_materials[0]
    reaction.starting_materials[0].set_amount(1.0, QuantityUnit.Gram)
    reaction.reactants[0].set_amount(2, QuantityUnit.Equivalent)
    reaction.products[0].set_amount(0.004, QuantityUnit.Mole)
    reaction.properties["temperature"].add_time_point(0, 30, 0, 80)
    reaction.properties["status"] = "Running"
    reaction.properties["purification"] = ["TLC"]
    reaction.properties["description"] = "Stir for 2 h."
    reaction.save(load_after_save=True)
    reaction = instance.get_reaction(reaction.id)
    material_ids = [m["id"] for role in ("starting_materials", "reactants", "products", "solvents")
                    for m in reaction.json_data[role]]
    cleanup_samples.extend([acid.id, alcohol.id, ester.id])

    login = SimpleNamespace(verify_tls=True, explorer_types=None, **chemotion)
    proxy = ExplorerProxy(load_config=lambda: login, save_setting=lambda k, v: None)
    try:
        model = proxy.details("reaction", reaction.id, collection.id)
        assert model["title"] == "Details test reaction" and model["id"] == reaction.id
        assert model["url"] == f"{chemotion['url']}/mydb/collection/{collection.id}/reaction/{reaction.id}"
        assert model["image"] and model["image"]["mime"] == "image/svg+xml" and "<svg" in model["image"]["data"]
        start = section(model, "Starting materials")
        assert start["rows"][0][1].lower() == "benzoic acid" and start["rows"][0][4] == "1000 mg"
        assert start["targets"][0]["id"] in material_ids
        assert section(model, "Products")["rows"][0][-1].endswith(" %")
        assert section(model, "Solvents")["rows"][0][1] == "Toluene"
        assert rows(model, "Reaction")["Temperature"] == ("80", "°C")
        assert section(model, "Description")["text"] == "Stir for 2 h."
        # the material's own sample opens through the same helper
        sample = proxy.details("sample", start["targets"][0]["id"], collection.id)
        assert sample["title"].lower() == "benzoic acid" and sample["type"] == "sample"
    finally:
        proxy.close()
        # reactions (and their material samples) go with the ELN's "delete selection"
        state = {"reaction": {"checkedAll": False, "checkedIds": [reaction.id], "uncheckedIds": []},
                 "sample": {"checkedAll": False, "checkedIds": material_ids, "uncheckedIds": []},
                 "currentCollection": {"id": collection.id}, "options": {"deleteSubsamples": True}, "selecteds": []}
        try:
            requests.delete(f"{instance.host_url}/api/v1/ui_state/", json=state, timeout=60,
                            headers={"Authorization": f"Bearer {instance.token}"}).raise_for_status()
        except Exception:  # the cleanup must not hide the test's own failure
            pass
