"""element_views.analyses_section() (the "analyses" section of every element view) against the
recorded JSON of ELN 4.0.1, a built ELN 3.1.x shape and hand-made edge cases, plus the helper's
`preview` and `download` (a scripted connection, the proxy's timeout, and the real helper against the
Chemotion test instance)."""
from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import element_views
from element_views import analyses_section, analysis_preview_attachment, view
from explorer import DOWNLOAD_TIMEOUT_S, ExplorerError, ExplorerProxy, ExplorerSource, size_text

FIXTURES = Path(__file__).with_name("fixtures") / "elements"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def png(width: int = 4) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(width))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, width, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def container(*analyses: dict) -> dict:
    return {"container": {"id": 1, "container_type": "root", "children": [
        {"id": 2, "container_type": "analyses", "children": list(analyses)}]}}


def analysis(children=(), **meta) -> dict:
    return {"id": 3, "name": "A", "container_type": "analysis", "description": "", "extended_metadata": meta,
            "children": list(children)}


def dataset(attachments=(), **extra) -> dict:
    return dict({"id": 4, "name": "D", "container_type": "dataset", "extended_metadata": {},
                 "attachments": list(attachments)}, **extra)


def attachment(att_id: int, name: str = "x.txt", **fields) -> dict:
    return dict({"id": att_id, "filename": name, "content_type": "text/plain", "filesize": 10, "thumb": False,
                 "updated_at": "01.01.2024, 10:00:00 +0000"}, **fields)


# -- the section ----------------------------------------------------------------

def test_recorded_v401_container():
    data = load("analyses_sample_v401.json")
    section = analyses_section(data)
    assert section["title"] == "Analyses" and section["kind"] == "analyses"
    (ana,) = section["analyses"]
    assert (ana["name"], ana["kind"], ana["status"], ana["text"]) == ("NMR 1", "1H NMR", "Confirmed", "Looks fine")
    assert ana["preview"] is False and ana["preview_attachment"] is None
    csv, picture = ana["datasets"]
    assert (csv["name"], csv["instrument"], csv["rows"]) == ("data.csv", "", [])
    assert csv["files"] == [{"id": 34, "name": "data.csv", "size": 12, "mime": "text/csv", "thumb": False}]
    # no thumbnail yet, but the server says it can show an image
    assert picture["files"] == [{"id": 35, "name": "pic.png", "size": 73, "mime": "image/png", "thumb": True}]


def test_view_appends_the_section_to_every_element():
    data = load("analyses_sample_v401.json")
    sample = dict(load("sample_v401.json"), container=data["container"])
    model = view("sample", sample)
    assert model["sections"][-1]["kind"] == "analyses"
    assert all(s["kind"] != "analyses" for s in model["sections"][:-1])
    # a generic element (layers) and an element type with an adapter get it too
    generic = dict(load("generic_try_v401.json"), container=data["container"])
    assert view(generic.get("type", "x"), generic)["sections"][-1]["kind"] == "analyses"
    research = dict(load("research_plan_v401.json"), container=data["container"])
    assert view("research_plan", research)["sections"][-1]["kind"] == "analyses"


def test_no_analyses_no_section():
    assert analyses_section({}) is None and analyses_section({"container": None}) is None
    assert analyses_section(load("sample_v401.json")) is None
    assert "analyses" not in [s["kind"] for s in view("sample", load("sample_v401.json"))["sections"]]
    assert analyses_section(container()) is None


def test_v312_shape():
    section = analyses_section(load("analyses_sample_v312.json"))
    (ana,) = section["analyses"]
    assert ana["kind"] == "13C NMR" and ana["status"] == "Confirmed"
    # the description and the content (JSON text of a Quill delta) are both shown
    assert ana["text"] == "<plain>\n\nPeak A" and "<b>A</b>" in ana["markup"] and "&lt;plain&gt;" in ana["markup"]
    (raw,) = ana["datasets"]
    assert raw["instrument"] == "Bruker 400" and raw["rows"] == []
    assert [f["thumb"] for f in raw["files"]] == [False, True]
    assert raw["files"][0] == {"id": 11, "name": "fid.zip", "size": 2048, "mime": "application/zip", "thumb": False}
    assert ana["preview"] is True and ana["preview_attachment"] == 12


def test_order_deleted_and_defaults():
    deleted = dict(analysis(name="gone"), is_deleted=True)
    second = dict(analysis(), id=5, name="  ", extended_metadata={"kind": "free text"})
    first = dict(analysis(children=[dataset([attachment(1, is_deleted=True), attachment(2, "b.txt"),
                                             {"filename": "no id"}]),
                                    dict(dataset(), id=9, name="", is_deleted=True),
                                    dict(dataset(), id=10, name="")], status=None, content={"ops": [{"insert": "\n"}]}),
                 id=6, name="First")
    section = analyses_section(container(first, deleted, second))
    assert [a["name"] for a in section["analyses"]] == ["First", "Analysis"]
    one, two = section["analyses"]
    assert one["text"] == "" and one["status"] == "" and one["kind"] == ""
    assert [d["id"] for d in one["datasets"]] == [4]  # the empty dataset 10 and the deleted one are left out
    assert [f["id"] for f in one["datasets"][0]["files"]] == [2]
    assert two["kind"] == "free text"


def test_file_size_unknown_and_mime():
    data = container(analysis(children=[dataset([attachment(1, filesize=None, content_type=None),
                                                 attachment(2, filesize="12")])]))
    files = analyses_section(data)["analyses"][0]["datasets"][0]["files"]
    assert files[0]["size"] is None and files[0]["mime"] == "" and files[1]["size"] is None


def test_dataset_metadata_rows():
    layers = {"general": {"key": "general", "label": "General", "position": 1, "fields": [
        {"field": "frequency", "label": "Frequency", "type": "system-defined", "position": 1,
         "value": "400", "option_layers": "frequency", "value_system": "MHz"},
        {"field": "solvent", "label": "Solvent", "type": "text", "position": 2, "value": "CDCl3"},
        {"field": "empty", "label": "Empty", "type": "text", "position": 3, "value": ""}]}}
    meta = {"klass_label": "NMR", "properties": {"layers": layers}}
    rows = analyses_section(container(analysis(children=[dataset(dataset=meta)])))["analyses"][0]["datasets"][0]["rows"]
    assert [(r["label"], r["value"]) for r in rows] == [("Type", "NMR"), ("Frequency", "400"), ("Solvent", "CDCl3")]
    assert rows[1]["unit"] == "MHz"
    # several layers: the layer title is part of the label
    layers["second"] = {"key": "second", "label": "More", "position": 2,
                        "fields": [{"field": "a", "label": "A", "type": "text", "position": 1, "value": "x"}]}
    rows = analyses_section(container(analysis(children=[dataset(dataset=meta)])))["analyses"][0]["datasets"][0]["rows"]
    assert [r["label"] for r in rows][-2:] == ["General · Solvent", "More · A"]
    # broken metadata is ignored
    for broken in (None, "x", {"properties": None}, {"properties": {"layers": []}}, {"klass_label": None}):
        analyses_section(container(analysis(children=[dataset(dataset=broken)])))


def test_preview_attachment_choice():
    def pick(atts, **meta):
        return analysis_preview_attachment(analysis(children=[dataset(atts)], **meta))

    old = attachment(1, "a.png", thumb=True, updated_at="01.01.2024, 10:00:00 +0000")
    new = attachment(2, "b.png", thumb=True, updated_at="02.01.2024, 09:00:00 +0000")
    combined = attachment(3, "My_Combined.png", thumb=True, updated_at="01.01.2020, 10:00:00 +0000")
    assert pick([old, new]) == 2 and pick([new, old]) == 2          # the latest
    assert pick([old, new, combined]) == 3                          # "combined" wins
    assert pick([attachment(4, thumb=False)]) is None               # no thumbnail
    assert pick([old, new], preferred_thumbnail="1") == 1           # the preferred one
    assert pick([old, new], preferred_thumbnail=" 1 ") == 1
    assert pick([old, new], preferred_thumbnail="99") == 2          # gone: back to the default
    assert pick([dict(old, previewable=False), dict(new, previewable=False)]) == 2   # only when nothing else
    assert pick([dict(new, previewable=False), old]) == 1           # previewable ones first
    assert pick([dict(old, is_deleted=True)]) is None
    assert pick([dict(old, thumb=True, updated_at="garbage"), new]) == 2


def test_size_text():
    assert size_text(0) == "0 B" and size_text(999) == "999 B" and size_text(1500) == "1.5 kB"
    assert size_text(50_000_000) == "50.0 MB" and size_text(2_500_000_000) == "2.5 GB"
    assert element_views.MAX_PREVIEW_BYTES == 5_000_000 and element_views.MAX_DOWNLOAD_BYTES == 50_000_000


# -- preview and download with a scripted connection ----------------------------------

class Response:
    def __init__(self, status=200, content=b"", headers=None, payload=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._payload = payload
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), 4):
            yield self.content[start:start + 4]

    def close(self):
        self.closed = True


class Connection:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def get(self, path, **kwargs):
        self.requests.append((path, kwargs))
        answer = self.routes.get(path, Response(404))
        if isinstance(answer, list):
            answer = answer.pop(0)
        return answer


def source_with(routes) -> tuple[ExplorerSource, Connection]:
    connection = Connection(routes)
    instance = SimpleNamespace(_con=connection, host_url="http://eln.test",
                               test_connection=lambda: SimpleNamespace(login=lambda *a: None))
    config = SimpleNamespace(url="http://eln.test", user="u", password="p", verify_tls=True, explorer_types=None)
    return ExplorerSource(lambda: config, lambda url, verify_ssl=True: instance, lambda k, v: None), connection


def test_preview_of_an_attachment_thumbnail():
    thumb = png()
    source, connection = source_with({"/api/v1/attachments/thumbnail/12": Response(
        payload=base64.b64encode(thumb).decode())})
    image = source.preview("attachment", 12)
    assert image == {"mime": "image/png", "data": base64.b64encode(thumb).decode(), "encoding": "base64"}
    assert [p for p, _ in connection.requests] == ["/api/v1/attachments/thumbnail/12"]


def test_preview_falls_back_to_the_image_route():
    picture = png()
    source, connection = source_with({
        "/api/v1/attachments/thumbnail/5": Response(payload=""),
        "/api/v1/attachments/image/5": Response(content=picture, headers={"Content-Type": "image/png",
                                                                        "Content-Length": str(len(picture))})})
    assert source.preview("attachment", 5)["mime"] == "image/png"
    assert [p for p, _ in connection.requests][-1] == "/api/v1/attachments/image/5"
    # a PDF, a missing file and an unknown id are no preview
    source, _ = source_with({"/api/v1/attachments/image/6": Response(content=b"%PDF-1.4", headers={
        "Content-Type": "application/pdf"})})
    assert source.preview("attachment", 6) is None
    assert source.preview("attachment", 7) is None


def test_preview_size_limit(monkeypatch):
    monkeypatch.setattr(element_views, "MAX_PREVIEW_BYTES", 100)
    big = base64.b64encode(b"x" * 400).decode()
    source, _ = source_with({"/api/v1/attachments/thumbnail/1": Response(payload=big)})
    with pytest.raises(ExplorerError, match="larger than 0 MB|larger than"):
        source.preview("attachment", 1)
    source, _ = source_with({"/api/v1/attachments/thumbnail/2": Response(payload=None),
                             "/api/v1/attachments/image/2": Response(content=b"x", headers={"Content-Length": "5000"})})
    with pytest.raises(ExplorerError, match="larger than"):
        source.preview("attachment", 2)
    source, _ = source_with({"/api/v1/attachments/thumbnail/3": Response(payload=None),
                             "/api/v1/attachments/image/3": Response(content=b"x" * 500, headers={
                                 "Content-Type": "image/png"})})
    with pytest.raises(ExplorerError, match="larger than"):
        source.preview("attachment", 3)


def test_preview_message_names_the_real_limit():
    source, _ = source_with({"/api/v1/attachments/thumbnail/1": Response(
        payload=base64.b64encode(b"x" * (element_views.MAX_PREVIEW_BYTES + 10)).decode())})
    with pytest.raises(ExplorerError) as caught:
        source.preview("attachment", 1)
    assert str(caught.value) == "The preview is larger than 5 MB."


def test_analysis_preview_comes_from_the_last_details():
    source, connection = source_with({"/api/v1/attachments/thumbnail/12": Response(
        payload=base64.b64encode(png()).decode())})
    assert source.preview("analysis", 3) is None and connection.requests == []   # not seen yet
    source._remember_previews(view("sample", dict(load("analyses_sample_v312.json"))))
    assert source.preview("analysis", 3)["mime"] == "image/png"
    assert connection.requests[0][0] == "/api/v1/attachments/thumbnail/12"
    with pytest.raises(ExplorerError):
        source.preview("folder", 1)
    with pytest.raises(ExplorerError):
        source.preview("attachment", "abc")


def test_unauthorized_logs_in_again():
    thumb = base64.b64encode(png()).decode()
    source, connection = source_with({"/api/v1/attachments/thumbnail/1": [Response(401), Response(payload=thumb)]})
    assert source.preview("attachment", 1)["mime"] == "image/png"
    assert len(connection.requests) == 2


def test_download_writes_the_file(tmp_path):
    content = b"hello world, a file" * 10
    source, connection = source_with({"/api/v1/attachments/9": Response(content=content, headers={
        "Content-Length": str(len(content))})})
    target = tmp_path / "deep" / "er" / "x.bin"
    answer = source.download(9, str(target), None)
    assert answer == {"path": str(target), "size": len(content)}
    assert target.read_bytes() == content
    assert (target.stat().st_mode & 0o777) == 0o600
    assert not list(target.parent.glob("*.part"))
    assert connection.requests[0][1] == {"stream": True}


def test_download_over_the_limit_writes_nothing(tmp_path):
    source, _ = source_with({"/api/v1/attachments/9": Response(content=b"x" * 100, headers={"Content-Length": "100"})})
    target = tmp_path / "x.bin"
    with pytest.raises(ExplorerError, match=r"100 B.*limit of 50 B"):
        source.download(9, str(target), 50)
    assert not target.exists() and not list(tmp_path.iterdir())
    # no Content-Length: counted while streaming, the partial file removed
    source, _ = source_with({"/api/v1/attachments/9": Response(content=b"x" * 100)})
    with pytest.raises(ExplorerError, match="limit of 50 B"):
        source.download(9, str(target), 50)
    assert not list(tmp_path.iterdir())
    # at the limit it works
    assert source.download(9, str(target), 100)["size"] == 100


def test_download_errors_leave_nothing(tmp_path, monkeypatch):
    source, _ = source_with({})
    with pytest.raises(ExplorerError, match="404"):
        source.download(1, str(tmp_path / "a"), None)
    with pytest.raises(ExplorerError):
        source.download("abc", str(tmp_path / "a"), None)
    with pytest.raises(ExplorerError):
        source.download(1, "", None)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    source, _ = source_with({"/api/v1/attachments/1": Response(content=b"abc")})
    with pytest.raises(ExplorerError, match="Could not write"):
        source.download(1, str(blocker / "sub" / "x"), None)   # the parent is a file
    # a failure halfway removes the partial file
    class Broken(Response):
        def iter_content(self, chunk_size):
            yield b"abc"
            raise ConnectionError("cut")

    source, _ = source_with({"/api/v1/attachments/2": Broken()})
    with pytest.raises(ExplorerError, match="cut"):
        source.download(2, str(tmp_path / "half"), None)
    assert not (tmp_path / "half").exists() and not (tmp_path / "half.part").exists()


# -- the proxy -----------------------------------------------------------------------

def test_proxy_uses_the_long_timeout_for_downloads_only(monkeypatch):
    seen = []

    def exchange(self, method, args, login, stamp, timeout):
        seen.append((method, args, timeout))
        return {"path": "p", "size": 1}

    monkeypatch.setattr(ExplorerProxy, "_exchange", exchange)
    proxy = ExplorerProxy(load_config=lambda: SimpleNamespace(url="u", user="u", password="p", verify_tls=True,
                                                              explorer_types=None),
                          release_stamp=lambda: "v401", version_check=lambda c, s: ("", ""))
    assert DOWNLOAD_TIMEOUT_S > proxy.timeout
    proxy.preview("analysis", 3)
    proxy.download(34, "/tmp/x", 100)
    proxy.download(34, "/tmp/x", None)
    proxy.details("sample", 1)
    assert seen == [("preview", ["analysis", 3], proxy.timeout), ("download", [34, "/tmp/x", 100], DOWNLOAD_TIMEOUT_S),
                    ("download", [34, "/tmp/x", None], DOWNLOAD_TIMEOUT_S), ("details", ["sample", 1, None],
                                                                              proxy.timeout)]
    proxy2 = ExplorerProxy(load_config=proxy._load_config, release_stamp=lambda: "v", download_timeout=5,
                           version_check=lambda c, s: ("", ""))
    assert proxy2.download_timeout == 5


def test_helper_knows_the_methods():
    import explorer_helper

    assert "preview" in explorer_helper.METHODS and "download" in explorer_helper.METHODS


# -- the real helper against the test instance ---------------------------------------------

@pytest.mark.chemotion
def test_analyses_through_the_real_helper(chemotion, test_collection, instance, cleanup_samples, tmp_path):
    collection = instance.get_root_collection().get_collection(test_collection)
    sample = collection.new_sample_smiles("CCO")
    sample.properties["name"] = "Analyses test"
    sample.save()
    cleanup_samples.append(sample.id)
    csv, picture = tmp_path / "data.csv", tmp_path / "pic.png"
    csv.write_text("x,y\n1,2\n3,4\n")
    picture.write_bytes(png())
    sample = instance.get_sample(sample.id)   # a freshly created sample does not take analyses
    ana = sample.analyses.add_analyses("NMR 1")
    ana["kind"], ana["status"], ana["description"] = "CHMO:0000593 | 1H NMR", "Confirmed", "Looks fine"
    ana.add_dataset(str(csv))
    ana.add_dataset(str(picture))
    sample.save()

    login = SimpleNamespace(verify_tls=True, explorer_types=None, **chemotion)
    proxy = ExplorerProxy(load_config=lambda: login, save_setting=lambda k, v: None)
    try:
        model = proxy.details("sample", sample.id, collection.id)
        section = next(s for s in model["sections"] if s["kind"] == "analyses")
        (found,) = section["analyses"]
        assert (found["name"], found["kind"], found["status"], found["text"]) == \
               ("NMR 1", "1H NMR", "Confirmed", "Looks fine")
        files = {f["name"]: f for d in found["datasets"] for f in d["files"]}
        assert files["data.csv"]["size"] == csv.stat().st_size and files["data.csv"]["mime"] == "text/csv"
        assert files["pic.png"]["thumb"] is True and files["data.csv"]["thumb"] is False
        assert found["datasets"][0]["name"] == "data.csv"

        # the preview of the picture (the server made no thumbnail: the image route answers)
        image = proxy.preview("attachment", files["pic.png"]["id"])
        assert image["mime"] == "image/png" and base64.b64decode(image["data"]) == picture.read_bytes()
        assert proxy.preview("attachment", files["data.csv"]["id"]) is None
        assert proxy.preview("analysis", found["id"]) is None or found["preview"]

        # download: the content arrives, a limit below the size writes nothing
        target = tmp_path / "out" / "nested" / "data.csv"
        answer = proxy.download(files["data.csv"]["id"], str(target), element_views.MAX_DOWNLOAD_BYTES)
        assert answer == {"path": str(target), "size": csv.stat().st_size}
        assert target.read_text() == "x,y\n1,2\n3,4\n"
        refused = tmp_path / "refused.csv"
        with pytest.raises(ExplorerError, match=r"12 B.*limit of 5 B"):
            proxy.download(files["data.csv"]["id"], str(refused), 5)
        assert not refused.exists() and not list(tmp_path.glob("refused*"))
        with pytest.raises(ExplorerError):
            proxy.download(99999999, str(tmp_path / "none.bin"), None)
        assert not (tmp_path / "none.bin").exists()
    finally:
        proxy.close()
