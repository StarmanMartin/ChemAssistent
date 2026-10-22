"""Stand-in for python-magic in the Windows build.

chemotion_api uses python-magic only to tell the MIME type of a file it
uploads. python-magic needs libmagic, which has no maintained Windows build,
so the Windows build ships this module instead (build.ps1 installs
chemotion_api without python-magic): the type comes from the file name.
"""
from __future__ import annotations

import mimetypes
import os


UNKNOWN_MIME = "application/octet-stream"


class Magic:
    def __init__(self, mime: bool = False, **_options) -> None:
        self.mime = mime

    def from_file(self, filename: str | os.PathLike) -> str:
        guessed, _encoding = mimetypes.guess_type(os.fspath(filename))
        if self.mime:
            return guessed or UNKNOWN_MIME
        return guessed or "data"

    def from_buffer(self, _buffer: bytes) -> str:
        return UNKNOWN_MIME if self.mime else "data"


def from_file(filename: str | os.PathLike, mime: bool = False) -> str:
    return Magic(mime=mime).from_file(filename)


def from_buffer(buffer: bytes, mime: bool = False) -> str:
    return Magic(mime=mime).from_buffer(buffer)
