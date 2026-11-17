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


ELEMENT = 'sample API-12 "Ethanol" (sample id 345 in collection "A/B")'


def test_mentions_are_references_only():
    analysis = {"id": 12, "name": "NMR 1"}
    assert d.analysis_mention({"text": ELEMENT}, analysis) == f'analysis "NMR 1" (analysis id 12) of {ELEMENT}'
    assert d.file_mention(ELEMENT, analysis, {"id": 34, "name": "x.jdx"}) == f'file "x.jdx" (attachment id 34) of {ELEMENT}'
    assert d.analysis_mention({}, analysis) == 'analysis "NMR 1" (analysis id 12)'


def test_human_size_and_confirmation():
    assert d.human_size(0) == "0 B" and d.human_size(1234) == "1.2 kB"
    assert d.human_size(52_000_000) == "52 MB" and d.human_size(None) == ""
    assert not d.needs_confirmation(d.MAX_DOWNLOAD_BYTES) and d.needs_confirmation(d.MAX_DOWNLOAD_BYTES + 1)
    assert not d.needs_confirmation(None)


def test_sanitize_name():
    assert "/" not in d.sanitize_name("../../etc/passwd") and not d.sanitize_name("../x").startswith(".")
    assert d.sanitize_name("..") == "file" and d.sanitize_name("", "f") == "f"
    assert d.sanitize_name("a\\b:c.txt") == "a_b_c.txt"
    long = d.sanitize_name("x" * 300 + ".jdx")
    assert len(long) <= d.MAX_NAME_CHARS and long.endswith(".jdx")


def test_unique_path(tmp_path):
    assert d.unique_path(tmp_path, "a.txt") == tmp_path / "a.txt"
    (tmp_path / "a.txt").write_text("x")
    assert d.unique_path(tmp_path, "a.txt") == tmp_path / "a (2).txt"
    (tmp_path / "a (2).txt").write_text("x")
    assert d.unique_path(tmp_path, "a.txt") == tmp_path / "a (3).txt"


def test_jupyter_and_open_paths(tmp_path):
    item = {"type": "sample", "id": 345, "short_label": "API-12"}
    analysis, file = {"id": 12, "name": "NMR/1"}, {"id": 34, "name": "../x.jdx"}
    path = d.jupyter_path(tmp_path, item, analysis, file)
    assert path == tmp_path / "chemotion" / "API-12" / "NMR_1" / "_x.jdx"
    assert d.relative_to_exchange(tmp_path, path) == "chemotion/API-12/NMR_1/_x.jdx"
    assert d.jupyter_path(tmp_path, {"type": "reaction", "id": 5}, analysis, file).parts[-3] == "reaction-5"
    assert d.open_path(tmp_path, file) == tmp_path / "34" / "_x.jdx"
