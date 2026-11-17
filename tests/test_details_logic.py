import details_logic as d


def test_type_label():
    assert d.type_label("research_plan") == "Research plan"
    assert d.type_label("sample") == "Sample"


def test_linked_mention_sample():
    item = {"type": "sample", "id": 7, "collection": "A/B", "shared": False, "collection_id": 3}
    model = {"title": "Ethanol", "subtitle": "API-12 · C2H6O"}
    assert d.linked_mention(item, model) == 'sample API-12 "Ethanol" (sample id 7 in collection "A/B")'


def test_linked_mention_shared_and_model_fields():
    item = {"type": "research_plan", "id": 2, "collection": "X", "shared": True, "collection_id": 5}
    model = {"title": "Plan", "subtitle": "", "short_label": "", "name": "Plan"}
    assert d.linked_mention(item, model) == 'research plan "Plan" (research_plan id 2 in shared collection "X")'


def test_linked_mention_needs_collection():
    assert d.linked_mention({"type": "sample", "id": 1}, {"title": "x"}) == ""


def test_inherit_collection_roundtrip():
    origin = {"collection": "A", "shared": True, "collection_id": 9}
    assert d.linked_collection(d.inherit_collection(origin)) == {"path": "A", "shared": True, "id": 9}
