"""GTK-free logic of the Details panel (details_view.py)."""
from __future__ import annotations

import json
import re
from pathlib import Path

if __package__:
    from .explorer import mention_text
else:
    from explorer import mention_text

SEPARATOR = " · "  # element_views joins the subtitle's parts with it


def type_label(type_name: str) -> str:
    """The type name for people: `research_plan` -> "Research plan"."""
    return str(type_name or "").replace("_", " ").strip().capitalize()


def linked_collection(origin: dict) -> dict:
    """The collection (path, shared, id) of the element a trail started from,
    taken from its describe() dict (or from the one a link item inherited it into)."""
    return {"path": origin.get("collection") or "", "shared": bool(origin.get("shared")),
            "id": origin.get("collection_id")}


def inherit_collection(origin: dict) -> dict:
    """The fields a linked item takes over from the view it was opened from."""
    return {"collection": origin.get("collection") or "", "shared": bool(origin.get("shared")),
            "collection_id": origin.get("collection_id")}


def linked_mention(item: dict, model: dict) -> str:
    """"Mention in message" text of a detail view opened from a link, from the
    view model (`short_label`/`name` if it has them, else the subtitle's first part
    and the title) and the collection the item inherited; "" when the item has no
    id or collection to name."""
    if item.get("id") is None or not item.get("type") or not item.get("collection"):
        return ""
    title = str(model.get("title") or "").strip()
    short = str(model.get("short_label") or "").strip()
    if not short and model.get("short_label") is None:
        short = str(model.get("subtitle") or "").split(SEPARATOR)[0].strip()
    name = str(model["name"]).strip() if model.get("name") is not None else title
    if name == short:
        name = ""
    entry = {"type": item["type"], "id": item["id"], "short_label": short, "name": name}
    return mention_text(type_label(item["type"]), entry, linked_collection(item))


# --- analyses and files (M9) ------------------------------------------------------
try:  # worker A's shared limits; the same values as a fallback
    from element_views import MAX_DOWNLOAD_BYTES, MAX_PREVIEW_BYTES  # type: ignore
except ImportError:  # pragma: no cover - before element_views has them
    MAX_PREVIEW_BYTES = 5_000_000
    MAX_DOWNLOAD_BYTES = 50_000_000

MAX_NAME_CHARS = 120
WINDOWS_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                              *(f"LPT{n}" for n in range(1, 10))})
JUPYTER_SUBFOLDER = "chemotion"


def element_text(element: dict | str) -> str:
    """The element's reference text: a describe() dict's `text` or a string."""
    return str(element.get("text") or "") if isinstance(element, dict) else str(element or "")


def analysis_mention(element: dict | str, analysis: dict) -> str:
    """"Mention in message" text of an analysis: a reference only, e.g.
    `analysis "NMR 1" (analysis id 12) of sample API-12 "Ethanol" (sample id 345 in collection "A/B")`."""
    of = element_text(element)
    text = f"analysis {json.dumps(str(analysis.get('name') or ''), ensure_ascii=False)} (analysis id {analysis.get('id')})"
    return f"{text} of {of}" if of else text


def file_mention(element: dict | str, analysis: dict, file: dict) -> str:
    """"Mention in message" text of a file: `file "x.jdx" (attachment id 34) of …`."""
    of = element_text(element)
    text = f"file {json.dumps(str(file.get('name') or ''), ensure_ascii=False)} (attachment id {file.get('id')})"
    return f"{text} of {of}" if of else text


def human_size(size) -> str:
    """1234 -> "1.2 kB", 52_000_000 -> "52 MB" (decimal units, like the limits)."""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "kB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        value /= 1000
    return ""


def needs_confirmation(size) -> bool:
    """True for a file above MAX_DOWNLOAD_BYTES (ask the user first)."""
    try:
        return int(size) > MAX_DOWNLOAD_BYTES
    except (TypeError, ValueError):
        return False


def sanitize_name(name: str, fallback: str = "file") -> str:
    """A safe file or folder name: no path separators, controls or reserved
    characters, no leading dots or trailing dots/spaces, at most MAX_NAME_CHARS
    (the extension kept)."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip()
    text = text.lstrip(". ").rstrip(". ")
    if len(text) > MAX_NAME_CHARS:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            text = stem[: MAX_NAME_CHARS - len(ext) - 1] + "." + ext
        else:
            text = text[:MAX_NAME_CHARS]
    # Windows device names (also with an extension: "nul.txt") are no files there.
    if text.split(".", 1)[0].strip().upper() in WINDOWS_RESERVED:
        text = "_" + text
    return text or fallback


def unique_path(folder: Path, name: str) -> Path:
    """`folder/name`, or `name (2).ext`, `name (3).ext` … when it exists."""
    path = folder / name
    if not path.exists():
        return path
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    number = 2
    while (folder / f"{stem} ({number}){ext}").exists():
        number += 1
    return folder / f"{stem} ({number}){ext}"


def element_folder_name(item: dict) -> str:
    """Folder name of an element below chemotion/: its short label, else `<type>-<id>`."""
    short = str(item.get("short_label") or "").strip()
    return sanitize_name(short or f"{item.get('type') or 'element'}-{item.get('id')}")


def jupyter_path(exchange: Path, item: dict, analysis: dict, file: dict) -> Path:
    """Where "Download to JupyterLab" writes:
    `<exchange>/chemotion/<element>/<analysis>/<file>` (sanitised, unique)."""
    folder = (Path(exchange) / JUPYTER_SUBFOLDER / element_folder_name(item)
              / sanitize_name(str(analysis.get("name") or ""), f"analysis-{analysis.get('id')}"))
    return unique_path(folder, sanitize_name(str(file.get("name") or ""), f"attachment-{file.get('id')}"))


def open_path(downloads: Path, file: dict) -> Path:
    """Where "Open" keeps a file: `<downloads>/<attachment id>/<file name>`."""
    return Path(downloads) / sanitize_name(str(file.get("id")), "0") / sanitize_name(str(file.get("name") or ""), "file")


def relative_to_exchange(exchange: Path, path: Path) -> str:
    try:
        return Path(path).relative_to(exchange).as_posix()
    except ValueError:
        return str(path)
