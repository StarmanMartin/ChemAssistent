"""documents.py: PDF text, page images and attached pictures."""
from __future__ import annotations

import struct
import sys
import zlib

import pytest

import documents
from documents import DocumentError
from pdf_maker import make_pdf


@pytest.fixture
def paper(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(make_pdf(["Synthesis of 8a\nScheme 1: the cyclisation", "", "Figure 2 (m/z 312)"]))
    return path


def png_pixels(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and the decompressed scanlines of a PNG from png_encode()."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    start = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[start - 8:start - 4])[0]
    return width, height, zlib.decompress(data[start:start + length])


def test_text_per_page_and_lines(paper):
    texts, total = documents.pdf_text(paper)
    assert total == 3
    assert texts[0] == "Synthesis of 8a\nScheme 1: the cyclisation"
    assert texts[1] == ""

    lines = documents.pdf_lines(paper)
    assert lines[0] == "--- page 1 of 3 ---"
    assert lines[1:3] == ["Synthesis of 8a", "Scheme 1: the cyclisation"]
    assert lines[3] == "--- page 2 of 3 ---"
    assert "view_page shows it" in lines[4]
    assert lines[-1] == "Figure 2 (m/z 312)"
    assert documents.page_count(paper) == 3


def test_long_pdf_says_where_the_text_stops(paper, monkeypatch):
    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    documents._text_cache.clear()
    lines = documents.pdf_lines(paper)
    assert lines[-1] == "--- pages 3-3 not extracted (more than 2 pages) ---"


def test_text_is_cached_until_the_file_changes(paper, monkeypatch):
    documents._text_cache.clear()
    documents.pdf_text(paper)
    opened = []
    real_open = documents._open
    monkeypatch.setattr(documents, "_open", lambda path: opened.append(path) or real_open(path))
    documents.pdf_text(paper)
    assert opened == []
    paper.write_bytes(make_pdf(["changed"]))
    assert documents.pdf_text(paper) == (["changed"], 1)
    assert len(opened) == 1


def test_render_page_and_regions(paper):
    image = documents.render_page(paper, 1)
    width, height, raw = png_pixels(image.data)
    assert (image.mime, image.width, image.height) == ("image/png", width, height)
    assert height == documents.IMAGE_MAX_SIDE  # US letter is portrait
    assert width == round(612 * documents.IMAGE_MAX_SIDE / 792) or abs(width - 612 * height / 792) < 2
    assert len(raw) == height * (1 + 3 * width)
    assert set(raw) - {0, 255}  # not blank: text and the line are drawn

    top = documents.render_page(paper, 1, "top")
    assert top.width == documents.IMAGE_MAX_SIDE and top.height < top.width  # half the page, at full width
    corner = documents.render_page(paper, 3, "bottom-right")
    assert max(corner.width, corner.height) == documents.IMAGE_MAX_SIDE


def test_render_page_errors(paper, tmp_path):
    with pytest.raises(DocumentError, match="has 3 pages; page 4 does not exist"):
        documents.render_page(paper, 4)
    with pytest.raises(DocumentError, match="page 0"):
        documents.render_page(paper, 0)
    with pytest.raises(DocumentError, match="Unknown region"):
        documents.render_page(paper, 1, "middle")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nnothing else")
    with pytest.raises(DocumentError, match="cannot be read as a PDF"):
        documents.page_count(broken)


def test_large_renders_shrink(paper, monkeypatch):
    limit = len(documents.render_page(paper, 1).data) - 1
    monkeypatch.setattr(documents, "MAX_IMAGE_BYTES", limit)
    image = documents.render_page(paper, 1)
    assert len(image.data) <= limit and image.height < documents.IMAGE_MAX_SIDE


def test_missing_pypdfium2(paper, monkeypatch):
    documents._text_cache.clear()
    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # import raises ImportError
    with pytest.raises(DocumentError, match="pypdfium2"):
        documents.pdf_text(paper)
    assert documents.describe(paper) == "PDF"


def test_png_encode_rgba_and_stride():
    pixels = bytes([255, 0, 0, 255, 0, 0, 255, 128]) + b"\x00" * 8  # 2 px per row, 8 bytes padding
    data = documents.png_encode(2, 1, 16, pixels, 4)
    width, height, raw = png_pixels(data)
    assert (width, height, raw) == (2, 1, b"\0" + pixels[:8])
    assert data[25] == 6  # colour type RGBA
    with pytest.raises(DocumentError):
        documents.png_encode(1, 1, 1, b"\0", 1)


def test_image_types_and_sizes():
    png = documents.png_encode(3, 2, 9, b"\x80" * 18)
    assert documents._image_type(png) == "image/png"
    assert documents.image_size(png) == (3, 2)
    assert documents.image_size(b"GIF89a" + struct.pack("<HH", 40, 30)) == (40, 30)
    # SOI, an APP0 segment, then SOF0 with height 200 and width 300.
    jpeg = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF"
            + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 200, 300) + b"\x00" * 6)
    assert documents._image_type(jpeg) == "image/jpeg"
    assert documents.image_size(jpeg) == (300, 200)
    webp = b"RIFF" + b"\0" * 4 + b"WEBPVP8X" + b"\0" * 8 + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
    assert documents._image_type(webp) == "image/webp"
    assert documents.image_size(webp) == (640, 480)
    assert documents._image_type(b"%PDF-1.4") is None


def test_load_image(tmp_path, monkeypatch):
    path = tmp_path / "scheme.png"
    path.write_bytes(documents.png_encode(40, 20, 120, b"\x10\x20\x30" * 800))
    image = documents.load_image(path)
    assert (image.mime, image.width, image.height, image.data) == ("image/png", 40, 20, path.read_bytes())
    assert image.data_url().startswith("data:image/png;base64,iVBOR")

    monkeypatch.setattr(documents, "_pillow", lambda: None)
    with pytest.raises(DocumentError, match="needs Pillow"):
        documents.load_image(path, "left")
    (tmp_path / "notes.txt").write_text("no picture", encoding="utf-8")
    with pytest.raises(DocumentError, match="not a PNG"):
        documents.load_image(tmp_path / "notes.txt")


def test_load_image_region_with_pillow(tmp_path):
    pytest.importorskip("PIL")
    path = tmp_path / "scheme.png"
    path.write_bytes(documents.png_encode(40, 20, 120, b"\x10\x20\x30" * 800))
    left = documents.load_image(path, "left")
    assert (left.width, left.height, left.mime) == (20, 20, "image/png")


def test_describe(paper, tmp_path):
    assert documents.describe(paper) == "PDF, 3 pages"
    picture = tmp_path / "a.png"
    picture.write_bytes(documents.png_encode(3, 2, 9, b"\x80" * 18))
    assert documents.describe(picture) == "PNG image, 3×2"
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 broken")
    assert documents.describe(tmp_path / "broken.pdf") == "PDF, unreadable"
    (tmp_path / "a.txt").write_text("text", encoding="utf-8")
    assert documents.describe(tmp_path / "a.txt") is None


def test_encrypted_pdf_message(paper, monkeypatch):
    import pypdfium2

    def refuse(path):
        raise pypdfium2.PdfiumError("Failed to load document (PDFium: Incorrect password error).")

    monkeypatch.setattr(pypdfium2, "PdfDocument", refuse)
    with pytest.raises(DocumentError, match="paper.pdf is encrypted"):
        documents.page_count(paper)


def test_windows_license_files_include_pep639_folders():
    """windows/licenses.py: pypdfium2 names its texts pdfium.txt etc. below
    dist-info/licenses/; the build must find them, or it fails."""
    from pathlib import PurePosixPath

    sys.path.insert(0, str(documents.Path(__file__).resolve().parent.parent / "windows"))
    try:
        import licenses
    finally:
        sys.path.pop(0)

    class Dist:
        files = [PurePosixPath(p) for p in (
            "pypdfium2/__init__.py",
            "pypdfium2-5.0.dist-info/METADATA",
            "pypdfium2-5.0.dist-info/licenses/LICENSES/Apache-2.0.txt",
            "pypdfium2-5.0.dist-info/licenses/BUILD_LICENSES/SHARED/Apache-2.0.txt",
            "pypdfium2-5.0.dist-info/licenses/BUILD_LICENSES/pdfium.txt",
            "other-1.0.dist-info/LICENSE",
            "other-1.0.dist-info/COPYING.md",
        )]

    names = [name for name, _ in licenses.license_files(Dist)]
    assert names == ["LICENSES/Apache-2.0.txt", "BUILD_LICENSES/SHARED/Apache-2.0.txt", "BUILD_LICENSES/pdfium.txt",
                     "LICENSE", "COPYING.md"]
    assert any(c.name == "PDFium" and "pdfium.dll" in c.dlls for c in licenses.COMPONENTS)
