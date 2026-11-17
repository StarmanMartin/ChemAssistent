"""GTK-free logic of the Details panel (details_view.py)."""
from __future__ import annotations

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
