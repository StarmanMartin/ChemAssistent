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

        instance.test_connection = test_connection
        instance.login = login
        instance.get_root_collection = get_root_collection
        return instance

    return factory


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
                    "url": "https://eln.example/mydb/collection/10/sample/1",
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


def test_icons_are_in_adwaita():
    """The Windows bundle ships only the Adwaita and hicolor icon themes.
    The names are read from the source: importing GTK 3 here may clash with
    a GTK 4 another test loaded."""
    import re
    from pathlib import Path

    adwaita = Path("/usr/share/icons/Adwaita")
    if not adwaita.is_dir():
        pytest.skip("no Adwaita icon theme here")
    source = (Path(explorer.__file__).parent / "explorer_view.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"([a-z0-9-]+-symbolic)"', source))
    assert len(names) > 10
    missing = [name for name in sorted(names) if not any(adwaita.rglob(f"{name}.*"))]
    assert missing == []
