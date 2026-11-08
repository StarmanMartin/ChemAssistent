"""PDF text and page images for the agent's read-only tools (no GTK).

The model reads a paper with read_file/grep (agent.py), which get the text of
a PDF from pdf_lines(), and looks at a scheme or figure with view_page, which
gets a PNG from render_page() (or an attached picture from load_image()).

PDFs need pypdfium2 (a wheel that bundles pdfium; requirements.txt, the
Windows bundle). It is imported on first use, so this module and agent.py
import without it (the headless package, where it is the optional extra
"pdf"); every function then raises DocumentError(MISSING_PDF_SUPPORT).
pdfium is not thread-safe: every call into it holds `_lock`. PNGs are
written with zlib (no Pillow: it is not a dependency and not in the Windows
bundle); Pillow is only used, when importable, to shrink or crop pictures.
"""
from __future__ import annotations

import dataclasses
import io
import struct
import threading
import zlib
from pathlib import Path

# The longer side of a rendered page or region, in pixels: what vision models
# read well (OpenAI and Anthropic scale larger images down to about this).
IMAGE_MAX_SIDE = 1600
# A tiny page is not blown up beyond this scale (pixels per PDF point).
MAX_SCALE = 6.0
# One image as sent to the model (the data URL is a third larger).
MAX_IMAGE_BYTES = 4_000_000
# Pages whose text is extracted; a longer PDF says where it stops.
MAX_PDF_PAGES = 400
# Parsed PDFs kept in memory (read_file pages through a paper call by call).
TEXT_CACHE_SIZE = 4
MISSING_PDF_SUPPORT = ("PDF support is not installed (the Python package pypdfium2 is missing; "
                       "pip install pypdfium2).")
# view_page's `region`: (left, top, right, bottom) as fractions of the page.
REGIONS = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "top": (0.0, 0.0, 1.0, 0.5),
    "bottom": (0.0, 0.5, 1.0, 1.0),
    "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 1.0, 1.0),
    "top-left": (0.0, 0.0, 0.5, 0.5),
    "top-right": (0.5, 0.0, 1.0, 0.5),
    "bottom-left": (0.0, 0.5, 0.5, 1.0),
    "bottom-right": (0.5, 0.5, 1.0, 1.0),
}
# The picture formats vision APIs take (OpenAI: PNG, JPEG, WEBP, non-animated GIF).
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

_lock = threading.RLock()
_text_cache: dict[tuple, list[str]] = {}


class DocumentError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Image:
    data: bytes
    mime: str
    width: int
    height: int

    def data_url(self) -> str:
        import base64

        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


# --- file types ---------------------------------------------------------------
def _head(path: Path, size: int = 1024) -> bytes:
    with Path(path).open("rb") as handle:
        return handle.read(size)


def is_pdf(path: Path) -> bool:
    """pdfium, like most readers, accepts the header anywhere in the first kB."""
    try:
        return b"%PDF-" in _head(path)
    except OSError:
        return False


def image_type(path: Path) -> str | None:
    """The MIME type of a PNG/JPEG/WEBP/GIF file by its first bytes, else None."""
    try:
        head = _head(path, 16)
    except OSError:
        return None
    return _image_type(head)


def _image_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the header of a PNG/JPEG/WEBP/GIF, else None."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            return None
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                length = struct.unpack(">H", data[index + 2:index + 4])[0]
                # SOF0-15 except DHT (C4), JPG (C8) and DAC (CC).
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[index + 5:index + 9])
                    return width, height
                index += 2 + length
    except struct.error:
        return None
    return None


# --- PDF ----------------------------------------------------------------------
def _pdfium():
    try:
        import pypdfium2
    except ImportError:
        raise DocumentError(MISSING_PDF_SUPPORT) from None
    return pypdfium2


def _open(path: Path):
    """The document; the caller holds `_lock` and closes it."""
    pdfium = _pdfium()
    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise DocumentError(f"{path.name} is encrypted (password protected); it cannot be read.") from None
        raise DocumentError(f"{path.name} cannot be read as a PDF ({exc}).") from None


def page_count(path: Path) -> int:
    with _lock:
        document = _open(Path(path))
        try:
            return len(document)
        finally:
            document.close()


def pdf_text(path: Path) -> tuple[list[str], int]:
    """The text of each page (at most MAX_PDF_PAGES) and the page count.
    Cached by path, size and mtime: read_file pages through it call by call."""
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    with _lock:
        if key in _text_cache:
            pages = _text_cache.pop(key)
            _text_cache[key] = pages  # most recently used last
            return pages[1:], int(pages[0])
        document = _open(path)
        try:
            total = len(document)
            texts = []
            for index in range(min(total, MAX_PDF_PAGES)):
                page = document[index]
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_bounded()
                finally:
                    textpage.close()
                    page.close()
                texts.append(text.replace("\r\n", "\n").replace("\r", "\n").rstrip())
        finally:
            document.close()
        _text_cache[key] = [str(total)] + texts
        while len(_text_cache) > TEXT_CACHE_SIZE:
            _text_cache.pop(next(iter(_text_cache)))
        return texts, total


def pdf_lines(path: Path) -> list[str]:
    """The text of a PDF as lines, each page after a "--- page n of m ---"
    line (read_file numbers and pages these; grep searches them)."""
    texts, total = pdf_text(path)
    lines: list[str] = []
    for number, text in enumerate(texts, 1):
        lines.append(f"--- page {number} of {total} ---")
        if text.strip():
            lines.extend(text.split("\n"))
        else:
            lines.append("(no text on this page: a scan or a figure; view_page shows it)")
    if total > len(texts):
        lines.append(f"--- pages {len(texts) + 1}-{total} not extracted (more than {MAX_PDF_PAGES} pages) ---")
    return lines


def render_page(path: Path, page: int, region: str = "full") -> Image:
    """Page `page` (1-based) or a part of it (REGIONS) as a PNG whose longer
    side is IMAGE_MAX_SIDE pixels (smaller if the PNG would exceed
    MAX_IMAGE_BYTES)."""
    path = Path(path)
    if region not in REGIONS:
        raise DocumentError(f"Unknown region {region!r}; use one of {', '.join(REGIONS)}.")
    with _lock:
        document = _open(path)
        try:
            total = len(document)
            if not 1 <= page <= total:
                raise DocumentError(f"{path.name} has {total} page{'s' if total != 1 else ''}; "
                                    f"page {page} does not exist.")
            handle = document[page - 1]
            try:
                width, height = handle.get_size()
                left, top, right, bottom = REGIONS[region]
                # pdfium's crop: points cut off at the left, bottom, right, top.
                crop = (left * width, (1 - bottom) * height, (1 - right) * width, top * height)
                shown = max((right - left) * width, (bottom - top) * height)
                scale = min(MAX_SCALE, IMAGE_MAX_SIDE / shown) if shown > 0 else 1.0
                for _ in range(6):
                    bitmap = handle.render(scale=scale, crop=crop, rev_byteorder=True)
                    try:
                        data = png_encode(bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer),
                                          bitmap.n_channels)
                        size = bitmap.width, bitmap.height
                    finally:
                        bitmap.close()
                    if len(data) <= MAX_IMAGE_BYTES:
                        return Image(data, "image/png", *size)
                    scale *= 0.75
                raise DocumentError(f"Page {page} of {path.name} is too large as an image.")
            finally:
                handle.close()
        finally:
            document.close()


def png_encode(width: int, height: int, stride: int, pixels: bytes, channels: int = 3) -> bytes:
    """A PNG of 8-bit RGB (3 channels) or RGBA (4) rows `stride` bytes apart."""
    if channels not in (3, 4):
        raise DocumentError(f"Cannot write a PNG with {channels} channels.")
    row = width * channels
    raw = b"".join(b"\0" + pixels[y * stride:y * stride + row] for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2 if channels == 3 else 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


# --- pictures -----------------------------------------------------------------
def _pillow():
    try:
        from PIL import Image as PillowImage
    except ImportError:
        return None
    return PillowImage


def load_image(path: Path, region: str = "full") -> Image:
    """An attached PNG/JPEG/WEBP/GIF as the model gets it: as it is when it
    fits, else (and for a region) shrunk/cropped with Pillow if installed."""
    path = Path(path)
    if region not in REGIONS:
        raise DocumentError(f"Unknown region {region!r}; use one of {', '.join(REGIONS)}.")
    data = path.read_bytes()
    mime = _image_type(data[:16])
    if mime is None:
        raise DocumentError(f"{path.name} is not a PNG, JPEG, WEBP or GIF picture.")
    size = image_size(data) or (0, 0)
    if region == "full" and len(data) <= MAX_IMAGE_BYTES and max(size) <= 2 * IMAGE_MAX_SIDE:
        return Image(data, mime, *size)
    pillow = _pillow()
    if pillow is None:
        if region != "full":
            raise DocumentError("Showing part of a picture needs Pillow, which is not installed; "
                                "view the whole picture (region \"full\").")
        if len(data) <= MAX_IMAGE_BYTES:
            return Image(data, mime, *size)
        raise DocumentError(f"{path.name} is too large to show ({len(data) / 1e6:.1f} MB) and Pillow, "
                            f"which could shrink it, is not installed.")
    try:
        with pillow.open(io.BytesIO(data)) as picture:
            picture.load()
            left, top, right, bottom = REGIONS[region]
            width, height = picture.size
            part = picture.crop((round(left * width), round(top * height), round(right * width),
                                 round(bottom * height)))
            part.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            if part.mode not in ("RGB", "RGBA", "L"):
                part = part.convert("RGBA" if "A" in part.getbands() else "RGB")
            out = io.BytesIO()
            if part.mode == "RGB" and mime == "image/jpeg":
                part.save(out, "JPEG", quality=90)
                kind = "image/jpeg"
            else:
                part.save(out, "PNG", optimize=True)
                kind = "image/png"
            return Image(out.getvalue(), kind, *part.size)
    except (OSError, ValueError) as exc:
        raise DocumentError(f"{path.name} cannot be read as a picture ({exc}).") from None


def describe(path: Path) -> str | None:
    """"PDF, 12 pages" / "PNG image, 1200×800" for describe_attachment(), or
    None for other files. Never raises (an attachment list must not fail)."""
    path = Path(path)
    if is_pdf(path):
        try:
            pages = page_count(path)
        except DocumentError as exc:
            return "PDF, unreadable" if str(exc) != MISSING_PDF_SUPPORT else "PDF"
        return f"PDF, {pages} page{'s' if pages != 1 else ''}"
    mime = image_type(path)
    if mime is None:
        return None
    try:
        size = image_size(_head(path, 65536))
    except OSError:
        size = None
    name = mime.split("/", 1)[1].upper()
    return f"{name} image, {size[0]}×{size[1]}" if size else f"{name} image"
