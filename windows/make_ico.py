"""Render an SVG into a Windows .ico (PNG images, 16 to 256 px) with GdkPixbuf.

Usage: python make_ico.py <icon.svg> <icon.ico>
Runs in the build environment of build.ps1 (gvsbuild's GTK and PyGObject).
"""
from __future__ import annotations

import struct
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # type: ignore  # noqa: E402


SIZES = (16, 24, 32, 48, 64, 256)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    images = []
    for size in SIZES:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(argv[1], size, size)
        ok, data = pixbuf.save_to_bufferv("png", [], [])
        if not ok:
            raise RuntimeError(f"Could not render {argv[1]} at {size} px")
        images.append((size, bytes(data)))
    # ICONDIR, one ICONDIRENTRY per image, then the PNG data (Windows Vista+).
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, data in images:
        side = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    with open(argv[2], "wb") as handle:
        handle.write(header + entries + payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
