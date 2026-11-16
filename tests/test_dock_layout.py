"""dock_layout.py: normalizing saved layouts and size arithmetic."""
import math

import pytest

import dock_layout as dl

PANELS = {"conversation": "bottom", "results": "right", "explorer": "left", "details": "center"}


def zones(**kw):
    out = {zone: [] for zone in dl.ZONES}
    out.update(kw)
    return out


def test_default_layout_shape():
    layout = dl.default_layout(PANELS)
    assert layout == {
        "zones": {"left": ["explorer"], "center": ["details"], "right": ["results"],
                  "bottom": ["conversation"]},
        "current": {}, "hidden": [], "sizes": {"left": 0.22, "right": 0.32, "bottom": 0.40}}
    assert dl.normalize_layout(None, PANELS) == layout


@pytest.mark.parametrize("garbage", [None, "x", 5, 1.5, True, [], ["zones"], {}, {"zones": 3},
                                     {"zones": []}, {"zones": {"left": "explorer"}},
                                     {"zones": {"left": [None, 3, [], {}]}}, b"zones", object()])
def test_garbage_gives_default(garbage):
    assert dl.normalize_layout(garbage, PANELS) == dl.default_layout(PANELS)


def test_garbage_fields_inside_a_dict():
    saved = {"zones": zones(left=[["a"], {"a": 1}, 7]), "current": ["x"], "hidden": "left",
             "sizes": [1, 2]}
    assert dl.normalize_layout(saved, PANELS) == dl.default_layout(PANELS)


def test_saved_placement_is_kept_and_missing_panels_appended():
    saved = {"zones": zones(right=["explorer", "results"], left=["details"])}
    layout = dl.normalize_layout(saved, PANELS)
    assert layout["zones"] == zones(left=["details"], right=["explorer", "results"],
                                    bottom=["conversation"])


def test_missing_panel_goes_to_the_end_of_its_default_zone():
    saved = {"zones": zones(right=["explorer"])}
    layout = dl.normalize_layout(saved, PANELS)
    assert layout["zones"]["right"] == ["explorer", "results"]
    assert layout["zones"]["left"] == []
    assert layout["zones"]["center"] == ["details"]


def test_duplicates_first_valid_place_wins():
    saved = {"zones": zones(left=["results", "results"], center=["results", "explorer"],
                            right=["explorer"])}
    layout = dl.normalize_layout(saved, PANELS)
    assert layout["zones"]["left"] == ["results"]
    assert layout["zones"]["center"] == ["explorer", "details"]  # details was missing
    assert layout["zones"]["right"] == []
    assert sum(len(v) for v in layout["zones"].values()) == 4


def test_unknown_ids_and_zones_dropped():
    saved = {"zones": {"left": ["nope", "explorer"], "top": ["details"], "": ["results"]},
             "current": {"top": "details"}, "hidden": ["top", "left"]}
    layout = dl.normalize_layout(saved, PANELS)
    assert layout["zones"] == zones(left=["explorer"], center=["details"], right=["results"],
                                    bottom=["conversation"])
    assert layout["current"] == {}
    assert layout["hidden"] == ["left"]


def test_current_validated():
    saved = {"zones": zones(left=["explorer", "details"]),
             "current": {"left": "details", "center": "details", "right": "nope",
                         "bottom": 5, "top": "x"}}
    layout = dl.normalize_layout(saved, PANELS)
    assert layout["current"] == {"left": "details"}


def test_hidden_order_duplicates_and_types():
    saved = {"hidden": ["bottom", "left", "left", 3, None, ["x"]]}
    assert dl.normalize_layout(saved, PANELS)["hidden"] == ["left", "bottom"]


def test_all_hidden_is_emptied():
    saved = {"hidden": list(dl.ZONES)}
    assert dl.normalize_layout(saved, PANELS)["hidden"] == []


def test_all_zones_with_panels_hidden_is_emptied_but_empty_zone_may_stay_hidden():
    only_two = {"explorer": "left", "details": "center"}
    assert dl.normalize_layout({"hidden": ["left", "center"]}, only_two)["hidden"] == []
    assert dl.normalize_layout({"hidden": ["left", "right"]}, only_two)["hidden"] == ["left", "right"]


def test_no_panels():
    layout = dl.normalize_layout({"hidden": ["left"]}, {})
    assert layout["zones"] == zones()
    assert layout["hidden"] == []


def test_sizes_clamped_and_defaults():
    saved = {"sizes": {"left": 0.01, "right": 5, "bottom": 0.5, "extra": 0.3}}
    sizes = dl.normalize_layout(saved, PANELS)["sizes"]
    assert sizes == {"left": dl.MIN_SIZE, "right": dl.MAX_SIZE, "bottom": 0.5}


@pytest.mark.parametrize("bad", [True, False, "0.5", None, [0.5], {}, math.nan])
def test_bad_sizes_use_default(bad):
    sizes = dl.normalize_layout({"sizes": {"left": bad, "right": bad, "bottom": bad}}, PANELS)["sizes"]
    assert sizes == dl.DEFAULT_SIZES


def test_infinite_and_int_sizes():
    sizes = dl.normalize_layout({"sizes": {"left": math.inf, "right": -math.inf, "bottom": 0}}, PANELS)["sizes"]
    assert sizes == {"left": dl.MAX_SIZE, "right": dl.MIN_SIZE, "bottom": dl.MIN_SIZE}


def test_output_is_new_and_idempotent():
    saved = {"zones": zones(left=["explorer"]), "hidden": ["bottom"]}
    once = dl.normalize_layout(saved, PANELS)
    assert dl.normalize_layout(once, PANELS) == once
    once["zones"]["left"].append("x")
    assert saved["zones"]["left"] == ["explorer"]
    assert dl.default_layout(PANELS)["zones"]["left"] == ["explorer"]


def test_default_zone_unknown_falls_back_to_center():
    layout = dl.normalize_layout(None, {"a": "nowhere", "b": "left"})
    assert layout["zones"]["center"] == ["a"]
    assert layout["zones"]["left"] == ["b"]


def test_positions_from_fractions():
    pos = dl.positions_from_fractions({"left": 0.2, "right": 0.3, "bottom": 0.4}, 1000, 800)
    assert pos == {"outer": 480, "top": 200, "inner": 500}


def test_positions_use_defaults_for_missing_and_clamp():
    pos = dl.positions_from_fractions({}, 1000, 1000)
    assert pos == {"outer": 600, "top": 220, "inner": 460}
    pos = dl.positions_from_fractions({"left": 0.0, "right": 9, "bottom": 9}, 1000, 1000)
    assert pos["top"] == 80 and pos["outer"] == 150


def test_fractions_from_positions():
    sizes = dl.fractions_from_positions({"outer": 480, "top": 200, "inner": 500}, 1000, 800)
    assert sizes == pytest.approx({"left": 0.2, "right": 0.3, "bottom": 0.4})


def test_fractions_clamped_and_degenerate():
    sizes = dl.fractions_from_positions({"outer": 0, "top": 0, "inner": 5000}, 1000, 1000)
    assert sizes == {"left": dl.MIN_SIZE, "right": dl.MIN_SIZE, "bottom": dl.MAX_SIZE}
    assert dl.fractions_from_positions({"outer": 1, "top": 1, "inner": 1}, 0, 0) == dl.DEFAULT_SIZES


@pytest.mark.parametrize("size", [(1920, 1080), (1366, 768), (777, 555)])
@pytest.mark.parametrize("sizes", [dl.DEFAULT_SIZES, {"left": 0.1, "right": 0.5, "bottom": 0.25},
                                   {"left": 0.5, "right": 0.1, "bottom": 0.7}])
def test_round_trip(size, sizes):
    width, height = size
    back = dl.fractions_from_positions(dl.positions_from_fractions(sizes, width, height), width, height)
    assert back == pytest.approx(sizes, abs=1.0 / min(width, height))


def _shown(*zones):
    return {zone: zone in zones for zone in dl.ZONES}


@pytest.mark.parametrize("zones, expected", [
    (("left", "center", "right", "bottom"), {"left", "right", "bottom"}),
    (("center", "right", "bottom"), {"right", "bottom"}),
    (("left", "center", "bottom"), {"left", "bottom"}),
    (("left", "bottom"), {"bottom"}),
    (("left", "right"), {"left"}),
    (("center", "right"), {"right"}),
    (("bottom",), set()),
    (("left",), set()),
    ((), set()),
])
def test_measurable_sizes(zones, expected):
    assert dl.measurable_sizes(_shown(*zones)) == expected
