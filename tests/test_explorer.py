"""explorer.py: the data of the Explorer tab and its worker thread.

A fake chemotion_api (instance, collections, ElementSet) covers both API
lines: v312 lists a shared collection with collection_is_sync=True, v4 has
no such parameter. The last test runs against the Chemotion test instance.
"""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

import element_views
import explorer
from explorer import ExplorerError, ExplorerSource, ExplorerWorker

CLASSES = {
    "reaction": {"name": "reaction", "label": "Reaction", "is_generic": False, "is_active": True, "place": 2},
    "sample": {"name": "sample", "label": "Sample", "is_generic": False, "is_active": True, "place": 1},
    "wrk": {"name": "wrk", "label": "Workflow", "is_generic": True, "is_active": True, "place": 100},
    "alpha": {"name": "alpha", "label": "Alpha", "is_generic": True, "is_active": True, "place": 100},
    "old": {"name": "old", "label": "Old", "is_generic": True, "is_active": False, "place": 100},
    "generic_element": {"name": "generic_element", "label": "generic_element", "is_generic": True},
}


class FakeCollection:
    def __init__(self, id, label, children=()):
        self.id, self.label, self.children = id, label, list(children)


class FakeServer:
    """What the fake instances see: elements per (collection id, type),
    recorded listings, and failures to inject."""

    def __init__(self):
        self.elements: dict[tuple[int, str], list[dict]] = {}
        self.listings: list[dict] = []
        self.logins: list[tuple] = []
        self.unauthorized = 0  # the next n listings answer 401
        self.shared = [FakeCollection(50, "From Bob", [FakeCollection(51, "Spectra")])]
        self.login_error: Exception | None = None
        self.sample_json: dict[int, dict] = {}
        self.images: dict[str, bytes | int] = {}  # path -> content, or the HTTP status that fails
        self.requested: list[str] = []
        self.content_types: dict[str, str] = {}  # path -> Content-Type of its answer
        self.reaction_json: dict[int, dict] = {}


class Unauthorized(Exception):
    status_code = 401


def fake_instance_factory(server: FakeServer):
    def factory(url, verify_ssl=True):
        instance = SimpleNamespace(host_url=url + "/")

        def test_connection():
            return instance

        def login(user, password):
            if server.login_error:
                raise server.login_error
            server.logins.append((url, user, password, verify_ssl))
            return instance

        def get_root_collection():
            root = FakeCollection(9, "root", [
                FakeCollection(10, "Project", [FakeCollection(12, "Step 1")]),
                FakeCollection(11, "Other"),
            ])
            root.sync_root = FakeCollection(None, "sync_root", server.shared)
            root._element_manager = SimpleNamespace(all_classes=dict(CLASSES))
            root._session = SimpleNamespace(server=server)
            return root

        def get_sample(sample_id):
            session = SimpleNamespace(get=lambda path: fake_response(server, path))
            data = server.sample_json[sample_id]
            return SimpleNamespace(
                json_data=data, _session=session,
                load_image=lambda: fake_response(server, f"/images/samples/{data['sample_svg_file']}"))

        def get_reaction(reaction_id):
            session = SimpleNamespace(get=lambda path: fake_response(server, path))
            return SimpleNamespace(json_data=server.reaction_json[reaction_id], _session=session)

        instance.get_sample = get_sample
        instance.get_reaction = get_reaction
        instance.test_connection = test_connection
        instance.login = login
        instance.get_root_collection = get_root_collection
        return instance

    return factory


def fake_response(server: FakeServer, path: str):
    server.requested.append(path)
    found = server.images.get(path, 404)
    if isinstance(found, int):
        return SimpleNamespace(status_code=found, content=b"", text="", headers={})
    headers = {"Content-Type": server.content_types[path]} if path in server.content_types else {}
    return SimpleNamespace(status_code=200, content=found, text="", headers=headers)


def make_listing(sync_parameter: bool):
    """A fake ElementSet of the v312 line (sync_parameter) or of v4."""

    class Listing(list):
        def __init__(self, session, element_type, collection_id=None, fixed_length=False):
            super().__init__()
            self.server = session.server
            self.element_type, self.collection_id, self.sync = element_type, collection_id, False
            self._page, self.max_page, self._length = 1, 1, None

        def load_elements(self, per_page=None, **_):
            server = self.server
            server.listings.append({"collection": self.collection_id, "type": self.element_type["name"],
                                    "page": self._page, "per_page": per_page, "sync": self.sync})
            if server.unauthorized:
                server.unauthorized -= 1
                raise Unauthorized("401 -> unauthorized")
            rows = server.elements.get((self.collection_id, self.element_type["name"]), [])
            start = (self._page - 1) * per_page
            self.max_page = max(1, -(-len(rows) // per_page))
            self._length = len(rows)
            self.clear()
            for data in rows[start:start + per_page]:
                element = explorer._Row(None, None, json_data=data)
                element.mark_partial()
                self.append(element)

    if not sync_parameter:
        return Listing

    class SyncListing(Listing):  # v312: the extra keyword argument
        def __init__(self, session, element_type, collection_id=None, collection_is_sync=False,
                     fixed_length=False):
            super().__init__(session, element_type, collection_id, fixed_length)
            self.sync = collection_is_sync

    return SyncListing


@pytest.fixture
def server(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(explorer, "_listing_class", make_listing(sync_parameter=False))
    return server


def config(**changes):
    values = {"url": "https://eln.example", "user": "ann", "password": "pw", "verify_tls": True}
    values.update(changes)
    return SimpleNamespace(**values)


def source_for(server, conf=None):
    current = {"config": conf or config()}
    source = ExplorerSource(lambda: current["config"], fake_instance_factory(server))
    return source, current


def samples(count, prefix="S"):
    return [{"id": n, "short_label": f"{prefix}-{n}", "name": f"Sample {n}", "updated_at": "2026-11-12",
             "molecule": {"sum_formular": "C2H6O"}} for n in range(1, count + 1)]


def test_collection_tree(server):
    source, _ = source_for(server)
    nodes = source.collections()
    assert [node["label"] for node in nodes] == ["All", "Project", "Other", "Shared with me"]
    assert nodes[0] == {"id": 9, "label": "All", "path": "All", "shared": False,
                        "url": "https://eln.example/mydb/collection/9", "children": []}
    assert nodes[1]["children"][0] == {"id": 12, "label": "Step 1", "path": "Project/Step 1", "shared": False,
                                       "url": "https://eln.example/mydb/collection/12", "children": []}
    shared = nodes[3]
    assert shared["id"] is None and shared["shared"] and shared["url"] is None
    assert shared["children"][0]["children"][0]["path"] == "From Bob/Spectra"
    assert shared["children"][0]["shared"]
    assert shared["children"][0]["url"] == "https://eln.example/mydb/collection/50"  # v4

    explorer._listing_class = make_listing(sync_parameter=True)  # v312: the web client's own route
    assert source.collections()[3]["children"][0]["url"] == "https://eln.example/mydb/scollection/50"

    server.shared = []
    assert [node["label"] for node in source.collections()] == ["All", "Project", "Other"]


def test_types_in_the_elns_order(server):
    source, _ = source_for(server)
    assert [(t["name"], t["label"], t["generic"]) for t in source.types()] == [
        ("sample", "Sample", False), ("reaction", "Reaction", False),
        ("alpha", "Alpha", True), ("wrk", "Workflow", True)]


def test_shown_types_and_saving_the_choice(server):
    source, current = source_for(server)
    assert [t["name"] for t in source.types() if t["shown"]] == ["sample", "reaction"]  # DEFAULT_TYPES

    current["config"] = config(explorer_types=("wrk", "elsewhere"))
    assert [t["name"] for t in source.types() if t["shown"]] == ["wrk"]

    saved = []
    source._save_setting = lambda key, value: saved.append((key, value))
    source.save_types(["sample", "alpha"], ["sample", "reaction", "alpha", "wrk"])
    # "elsewhere" is no type of this server: kept for the server that has it.
    assert saved == [("explorer_types", ["sample", "alpha", "elsewhere"])]
    assert source._config.explorer_types == ("sample", "alpha", "elsewhere")

    def refuse(key, value):
        raise OSError("the settings cannot be saved now: locked")

    source._save_setting = refuse
    with pytest.raises(ExplorerError, match="not saved: the settings cannot be saved now"):
        source.save_types([], ["sample"])


def test_pages_of_ten_with_more(server):
    server.elements[(10, "sample")] = samples(12)
    source, _ = source_for(server)
    collection = {"id": 10, "label": "Project", "shared": False}

    first = source.page(collection, "sample")
    assert (first["total"], first["more"], first["page"], len(first["items"])) == (12, True, 1, 10)
    item = first["items"][0]
    assert item == {"id": 1, "type": "sample", "short_label": "S-1", "name": "Sample 1",
                    "updated_at": "2026-11-12", "url": "https://eln.example/mydb/collection/10/sample/1",
                    "tooltip": "Sample S-1\nSample 1\nid 1\nFormula C2H6O\nUpdated 2026-11-12"}

    second = source.page(collection, "sample", 2)
    assert (second["total"], second["more"], [i["id"] for i in second["items"]]) == (12, False, [11, 12])
    assert [(l["page"], l["per_page"]) for l in server.listings] == [(1, 10), (2, 10)]

    empty = source.page(collection, "reaction")
    assert (empty["items"], empty["total"], empty["more"]) == ([], 0, False)
    assert source.page({"id": None, "label": "Shared with me"}, "sample")["items"] == []
    with pytest.raises(ExplorerError, match="no element type 'nope'"):
        source.page(collection, "nope")


def test_page_without_total_uses_the_page_count(server, monkeypatch):
    listing = make_listing(sync_parameter=False)

    class NoTotal(listing):
        def load_elements(self, per_page=None, **options):
            super().load_elements(per_page, **options)
            self._length = None

    monkeypatch.setattr(explorer, "_listing_class", NoTotal)
    server.elements[(10, "sample")] = samples(12)
    source, _ = source_for(server)
    assert source.page({"id": 10}, "sample")["more"]
    assert not source.page({"id": 10}, "sample", 2)["more"]


def test_mention_text():
    collection = {"id": 10, "path": "Project/Step \"1\"", "shared": False}
    item = {"id": 345, "type": "sample", "short_label": "API-12", "name": "Benzaldehyde"}
    assert explorer.mention_text("Sample", item, collection) == (
        'sample API-12 "Benzaldehyde" (sample id 345 in collection "Project/Step \\"1\\"")')
    plan = {"id": 1, "type": "research_plan", "short_label": "", "name": ""}
    assert explorer.mention_text("Research Plan", plan, {"id": 51, "path": "Bob/Spectra", "shared": True}) == (
        'research plan (research_plan id 1 in shared collection "Bob/Spectra")')
    assert explorer.mention_text("", None, collection) == 'collection "Project/Step \\"1\\"" (id 10)'


def test_element_names_fall_back_to_the_molecule():
    assert explorer.element_name({"name": "", "molecule": {"iupac_name": "ethanol"}}) == "ethanol"
    assert explorer.element_name({"name": None, "showed_name": "EtOH"}) == "EtOH"
    assert explorer.element_name({}) == ""


@pytest.mark.parametrize("sync_parameter", [True, False])
def test_shared_collections_per_api_line(server, monkeypatch, sync_parameter):
    """v312 needs collection_is_sync=True (and the web client's scollection
    route); v4 lists a shared collection like any other."""
    monkeypatch.setattr(explorer, "_listing_class", make_listing(sync_parameter))
    server.elements[(51, "sample")] = samples(1)
    source, _ = source_for(server)
    own = source.page({"id": 10, "shared": False}, "sample")
    shared = source.page({"id": 51, "shared": True}, "sample")
    assert [l["sync"] for l in server.listings] == [False, sync_parameter]
    route = "scollection" if sync_parameter else "collection"
    assert shared["items"][0]["url"] == f"https://eln.example/mydb/{route}/51/sample/1"
    assert own["items"] == []


def test_logs_in_again_after_401_and_on_config_changes(server):
    server.elements[(10, "sample")] = samples(3)
    source, current = source_for(server)
    source.page({"id": 10}, "sample")
    source.page({"id": 10}, "sample")
    assert len(server.logins) == 1

    server.unauthorized = 1
    assert len(source.page({"id": 10}, "sample")["items"]) == 3
    assert len(server.logins) == 2

    server.unauthorized = 2
    with pytest.raises(ExplorerError, match="401"):
        source.page({"id": 10}, "sample")

    current["config"] = config(user="bob")
    source.types()
    assert server.logins[-1] == ("https://eln.example", "bob", "pw", True)
    current["config"] = config(user="bob", verify_tls=False)
    source.types()
    assert server.logins[-1][3] is False


def test_errors_are_readable(server):
    def missing():
        raise ValueError("Chemotion URL is missing")

    with pytest.raises(ExplorerError, match="not set up yet.*URL is missing"):
        ExplorerSource(missing, fake_instance_factory(server)).collections()

    server.login_error = PermissionError("bad password")
    source, _ = source_for(server)
    with pytest.raises(ExplorerError, match="Login to Chemotion failed"):
        source.collections()

    server.login_error = OSError("connection refused")
    with pytest.raises(ExplorerError, match="Cannot connect to Chemotion at https://eln.example: connection refused"):
        source.collections()


class StubSource:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def slow(self, value):
        self.started.set()
        self.release.wait(5)
        return value

    def echo(self, value):
        return value

    def fail(self):
        raise ExplorerError("no connection")

    def crash(self):
        raise KeyError("bug")


def test_worker_delivers_in_order_and_drops_stale_answers():
    delivered: queue.Queue = queue.Queue()
    source = StubSource()
    worker = ExplorerWorker(source, delivered.put)
    results, errors = [], []

    worker.request("echo", (1,), results.append, errors.append)
    worker.request("fail", (), results.append, errors.append)
    worker.request("crash", (), results.append, errors.append)
    for _ in range(3):
        assert delivered.get(timeout=5)() is False  # a GLib.idle_add callback runs once
    assert results == [1]
    assert errors == ["no connection", "KeyError: 'bug'"]

    worker.request("slow", ("old",), results.append, errors.append)
    assert source.started.wait(5)
    worker.request("echo", ("queued",), results.append, errors.append)
    worker.reset()  # e.g. Refresh: the running answer and the queued request are dropped
    worker.request("echo", ("new",), results.append, errors.append)
    source.release.set()
    delivered.get(timeout=5)()  # "old", stale
    delivered.get(timeout=5)()  # "new"
    assert results == [1, "new"]

    worker.request("echo", ("late",), results.append, errors.append)
    late = delivered.get(timeout=5)
    worker.close()  # the window closed before the answer reached the main loop
    late()
    worker.request("echo", ("after close",), results.append, errors.append)
    assert results == [1, "new"]
    worker._thread.join(5)
    assert not worker._thread.is_alive()


@pytest.mark.chemotion
def test_pages_against_the_test_instance(chemotion, test_collection, cleanup_samples):
    """12 new samples in the test collection come back in pages of 10."""
    from chemotion_api import Instance

    instance = Instance(chemotion["url"]).test_connection().login(chemotion["user"], chemotion["password"])
    collection = instance.get_root_collection().get_collection(test_collection)
    for n in range(12):
        sample = collection.new_sample_smiles("CCO")
        sample.save()
        cleanup_samples.append(sample.id)

    source = ExplorerSource(lambda: SimpleNamespace(verify_tls=True, **chemotion))
    nodes = source.collections()

    def find(nodes, path):
        for node in nodes:
            if node["path"] == path:
                return node
            found = find(node["children"], path)
            if found:
                return found

    node = find(nodes, test_collection.strip("/"))
    assert node and not node["shared"]
    assert "sample" in [t["name"] for t in source.types()]

    seen, page = [], 1
    while True:
        result = source.page(node, "sample", page)
        assert len(result["items"]) <= explorer.PER_PAGE
        assert result["total"] >= 12
        seen += [item["id"] for item in result["items"]]
        if not result["more"]:
            break
        assert len(result["items"]) == explorer.PER_PAGE
        page += 1
    assert page >= 2
    assert set(cleanup_samples) <= set(seen)
    assert len(seen) == result["total"]
    item = next(item for item in source.page(node, "sample")["items"] if item["id"] in cleanup_samples)
    assert item["name"] == "ethanol"  # no name: the ELN shows the molecule
    assert item["url"].endswith(f"/mydb/collection/{node['id']}/sample/{item['id']}")


def test_detail_rows_leave_out_empty_values():
    item = {"type_label": "Sample", "short_label": "API-1", "name": "", "collection": "A/B", "shared": False,
            "id": 7}
    assert explorer.detail_rows(item) == [("Type", "Sample"), ("Short label", "API-1"), ("Collection", "A/B"),
                                          ("ID", "7")]
    item.update(shared=True, name="Water")
    assert ("Shared", "yes") in explorer.detail_rows(item) and ("Name", "Water") in explorer.detail_rows(item)
    assert explorer.detail_rows({}) == []
    assert explorer.detail_rows({"id": 0}) == [("ID", "0")]


def test_describe_has_the_detail_fields():
    """ExplorerPanel.describe() (GTK: in a subprocess) fills the Details fields."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    code = """
import json, gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
import explorer_view as v
panel = v.ExplorerPanel.__new__(v.ExplorerPanel)
panel.types = [{"name": "sample", "label": "Sample"}]
store = Gtk.TreeStore(str, str, str, str, object)
panel.store = store
node = {"id": 3, "label": "B", "path": "A/B", "shared": True, "url": "u/c"}
parent = store.append(None, ["", "", None, v.COLLECTION, node])
element = {"id": 9, "type": "sample", "short_label": "API-9", "name": "Water", "url": "u/s",
           "updated_at": "2026-11-17T10:00:00Z"}
child = store.append(parent, ["", "", None, v.ELEMENT, element])
print(json.dumps([panel.describe(parent), panel.describe(child)]))
"""
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=Path(explorer.__file__).parent)
    if done.returncode:
        pytest.skip("no GTK here: " + done.stderr[-200:])
    collection, element = json.loads(done.stdout.strip().splitlines()[-1])
    assert (collection["kind"], collection["id"], collection["collection"], collection["shared"]) == \
        ("collection", 3, "A/B", True)
    assert element["kind"] == "element" and element["type_label"] == "Sample"
    assert (element["short_label"], element["name"], element["collection"], element["shared"]) == \
        ("API-9", "Water", "A/B", True)
    assert element["label"] == "API-9 Water" and element["url"] == "u/s"
    # The Details panel's detail view: request args and cache key.
    assert (element["collection_id"], element["updated_at"]) == (3, "2026-11-17T10:00:00Z")


def test_icons_are_in_adwaita():
    """The Windows bundle ships only the Adwaita and hicolor icon themes.
    The names are read from the source: importing GTK 3 here may clash with
    a GTK 4 another test loaded."""
    import re
    from pathlib import Path

    adwaita = Path("/usr/share/icons/Adwaita")
    if not adwaita.is_dir():
        pytest.skip("no Adwaita icon theme here")
    names = set()
    for module in ("explorer_view.py", "details_view.py", "dock.py"):  # dock.py: the docking layout
        path = Path(explorer.__file__).parent / module
        if path.exists():
            names |= set(re.findall(r'"([a-z0-9-]+-symbolic)"', path.read_text(encoding="utf-8")))
    assert len(names) > 10
    missing = [name for name in sorted(names) if not any(adwaita.rglob(f"{name}.*"))]
    assert missing == []


def test_the_selection_goes_with_the_message_and_back_out_of_the_history():
    """workspace.selection_note() is appended to the typed text (app's _send());
    app.replay_items() shows it as a note again (GTK: in a subprocess)."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    import workspace

    pytest.importorskip("gi")
    selected = ['sample API-12 "Benzaldehyde" (sample id 345 in collection "A/B")', 'collection "A" (id 3)']
    note = workspace.selection_note(selected)
    assert note == ("\n\nSelected in the Explorer:\n"
                    '- sample API-12 "Benzaldehyde" (sample id 345 in collection "A/B")\n- collection "A" (id 3)')
    assert workspace.selection_note([]) == ""

    import agent

    messages = [
        {"role": "user", "content": "Compare these" + note + agent.ATTACHMENT_NOTE + "- /tmp/x.csv (text file, 1 kB)"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "Thanks"},
    ]
    code = "import json, sys, app\nprint(json.dumps(app.replay_items(json.loads(sys.stdin.read()))))\n"
    root = Path(explorer.__file__).resolve().parent
    result = subprocess.run([sys.executable, "-c", code], input=json.dumps(messages), capture_output=True,
                            text=True, cwd=root, timeout=60)
    assert result.returncode == 0, result.stderr
    items = json.loads(result.stdout)
    assert items[0] == ["user", "Compare these", ["/tmp/x.csv"], selected]
    assert items[2] == ["user", "Thanks", [], []]


def test_only_the_desktop_prompt_explains_the_selection(workspace):
    import workspace as workspace_module

    assert 'A message may end with "Selected in the Explorer:"' in workspace_module.system_prompt()
    assert "Selected in the Explorer" not in workspace_module.system_prompt(headless=True)


def test_worker_error_text_has_a_release_attribute():
    class Source:
        def types(self):
            raise ExplorerError("Install v401.1.1", release="v401.1.1")

        def collections(self):
            raise RuntimeError("bug")

    got, done = [], threading.Event()
    worker = ExplorerWorker(Source(), lambda fn: fn())
    worker.request("types", (), lambda r: None, lambda t: got.append(t))
    worker.request("collections", (), lambda r: None, lambda t: (got.append(t), done.set()))
    assert done.wait(5)
    assert got == ["Install v401.1.1", "RuntimeError: bug"]
    assert [t.release for t in got] == ["v401.1.1", ""]
    worker.close()


# -- details -------------------------------------------------------------------

SAMPLE = {"id": 5, "name": "Ethanol", "short_label": "S-5", "updated_at": "2026-11-12", "sample_type": "Micromolecule",
          "molecule": {"sum_formular": "C2H6O", "molecule_svg_file": "mol.svg", "iupac_name": "ethanol"}}


def test_page_items_carry_updated_at(server):
    source, _ = source_for(server)
    server.elements[(10, "sample")] = samples(2)
    items = source.page({"id": 10, "shared": False}, "sample")["items"]
    assert [item["updated_at"] for item in items] == ["2026-11-12"] * 2


def test_details_of_a_sample_with_the_molecule_image(server):
    source, _ = source_for(server)
    server.sample_json[5] = dict(SAMPLE, sample_svg_file=None)
    server.images["/images/molecules/mol.svg"] = b"<svg>mol</svg>"
    model = source.details("sample", 5, 10)
    assert model["title"] == "Ethanol" and model["subtitle"] == "S-5 \u00b7 C2H6O"
    assert model["url"] == "https://eln.example/mydb/collection/10/sample/5"
    assert model["image"] == {"mime": "image/svg+xml", "data": "<svg>mol</svg>"}
    assert server.requested == ["/images/molecules/mol.svg"]  # reading only
    assert source.details("sample", 5)["url"] == ""


def test_details_prefers_the_samples_own_drawing(server):
    source, _ = source_for(server)
    server.sample_json[5] = dict(SAMPLE, sample_svg_file="own.svg")
    server.images["/images/samples/own.svg"] = b"<svg>own</svg>"
    assert source.details("sample", 5)["image"]["data"] == "<svg>own</svg>"


def test_details_without_an_image_still_work(server):
    source, _ = source_for(server)
    server.sample_json[5] = dict(SAMPLE, sample_svg_file=None)
    model = source.details("sample", 5)  # 404 for the molecule file
    assert model["image"] is None and model["notes"] == ["The structure image could not be loaded."]
    assert model["title"] == "Ethanol"
    server.images["/images/molecules/mol.svg"] = b"x" * (element_views.MAX_IMAGE_BYTES + 1)
    model = source.details("sample", 5)
    assert model["image"] is None and model["notes"] == [element_views.IMAGE_TOO_LARGE]
    server.sample_json[6] = dict(SAMPLE, id=6, molecule={})
    assert source.details("sample", 6)["image"] is None


REACTION = {"id": 3, "name": "Esterification", "short_label": "R-3", "status": "Done", "reaction_svg_file": "r.svg",
            "starting_materials": [{"id": 31, "short_label": "S-31", "name": "Acid", "target_amount_value": 1.0,
                                    "target_amount_unit": "g", "equivalent": 1.0, "reference": True,
                                    "molecule": {"molecular_weight": 122.0}}]}


def test_details_of_a_reaction_loads_the_scheme_and_links_the_materials(server):
    source, _ = source_for(server)
    server.reaction_json[3] = dict(REACTION)
    server.images["/images/reactions/r.svg"] = b"<svg>scheme</svg>"
    model = source.details("reaction", 3, 10)
    assert model["title"] == "Esterification" and model["subtitle"] == "R-3 \u00b7 Done"
    assert model["url"] == "https://eln.example/mydb/collection/10/reaction/3"
    assert model["image"] == {"mime": "image/svg+xml", "data": "<svg>scheme</svg>"}
    starting = next(s for s in model["sections"] if s["title"] == "Starting materials")
    assert starting["targets"] == [{"type": "sample", "id": 31}]
    assert server.requested == ["/images/reactions/r.svg"]  # reading only


def test_details_images_other_than_svg_are_base64_and_failures_become_notes(server):
    source, _ = source_for(server)
    server.reaction_json[3] = dict(REACTION, reaction_svg_file="r.png")
    server.images["/images/reactions/r.png"] = b"\x89PNG\r\n"
    server.content_types["/images/reactions/r.png"] = "image/png; charset=binary"
    assert source.details("reaction", 3)["image"] == {"mime": "image/png", "data": "iVBORw0K", "encoding": "base64"}
    server.images["/images/reactions/r.png"] = b"GIF"  # no known type and a name without an extension
    server.reaction_json[3] = dict(REACTION, reaction_svg_file="r")
    server.images["/images/reactions/r"] = b"data"
    model = source.details("reaction", 3)
    assert model["image"] is None and model["notes"] == ["The scheme image could not be loaded."]
    server.reaction_json[3] = dict(REACTION, reaction_svg_file="gone.svg")
    assert source.details("reaction", 3)["notes"] == ["The scheme image could not be loaded."]
    server.images["/images/reactions/gone.svg"] = b"x" * (element_views.MAX_IMAGE_BYTES + 1)
    assert source.details("reaction", 3)["notes"] == ["The scheme image is larger than 2 MB and is not shown."]


def test_a_type_the_installed_api_has_no_getter_for_is_the_stub(server, monkeypatch):
    source, _ = source_for(server)
    monkeypatch.setitem(element_views.ADAPTERS, "vessel", lambda data, images, url: {"type": "vessel"})
    assert source.details("vessel", 3, 10) == {"type": "vessel", "id": 3, "unsupported": True}


def test_details_of_an_unsupported_type_is_the_stub_without_a_login(server):
    source, _ = source_for(server)
    assert source.details("vessel", 3) == {"type": "vessel", "id": 3, "unsupported": True}
    assert server.logins == []


def test_details_errors_are_readable_and_a_401_logs_in_again(server):
    source, _ = source_for(server)
    with pytest.raises(ExplorerError, match="Chemotion answered with an error"):
        source.details("sample", 404)  # not on the server
    server.sample_json[5] = dict(SAMPLE, sample_svg_file=None)
    assert source.details("sample", 5)["title"] == "Ethanol"
