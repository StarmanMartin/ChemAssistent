"""The data of the Explorer tab (no GTK): the collection tree, the element
types and one page of elements of one type in one collection, all through
chemotion_api, plus the worker thread the tab runs them in.

The tab is read-only and the user starts every request, so this runs in the
app process (like the settings' "Test connection"), not through
chemotion_runner.py and a confirmation card.

Supported chemotion_api versions: the v4 line and v312 (ELN 3.x, the Windows
bundle). They differ in how a shared collection is listed: v312's
`ElementSet` has `collection_is_sync` (the request then names
`sync_collection_id`), v4 has no such parameter and lists every collection by
its id. `page()` passes it only where the signature has it.

A page is listed with chemotion_api's `ElementSet` (request, paging, totals
from `X-Total`), but its rows are kept as the listing's JSON (`_Row`) instead
of element objects: building those parses every element (a research plan
with an image fails in the listing, v312) and the Explorer needs only labels.
"""
from __future__ import annotations

import inspect
import queue
from dataclasses import replace
import threading
from typing import Any, Callable

PER_PAGE = 10
# Shown when nothing was chosen yet (config `explorer_types` empty).
DEFAULT_TYPES = ("sample", "reaction", "research_plan", "wellplate", "screen")
# chemotion_api's pseudo class for "any generic element": not a type of its own.
PSEUDO_TYPES = {"generic_element"}
SHARED_LABEL = "Shared with me"
ALL_LABEL = "All"

NOT_CONFIGURED = "Chemotion is not set up yet: enter URL, user and password in the settings."
LOGIN_FAILED = "Login to Chemotion failed: check user and password in the settings."


class ExplorerError(Exception):
    """A failure the tab shows as it is (the message is for the user)."""


class _Row:
    """One element of a listing: its JSON, nothing parsed (see the module
    docstring). Takes the arguments ElementSet gives an element class."""

    def __init__(self, segments=None, session=None, json_data: dict | None = None, **_ignored) -> None:
        self.json_data = json_data or {}

    def mark_partial(self) -> None:
        pass


_listing_class = None


def listing_class():
    """chemotion_api's ElementSet, keeping rows instead of elements."""
    global _listing_class
    if _listing_class is None:
        from chemotion_api.elements import ElementSet

        class Listing(ElementSet):
            def _get_element_class(self):
                return _Row

        _listing_class = Listing
    return _listing_class


def _uses_sync_ids(listing) -> bool:
    """v312: a shared collection is listed with collection_is_sync=True."""
    return "collection_is_sync" in inspect.signature(listing.__init__).parameters


def _is_unauthorized(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "status_code", None) == 401


def element_name(data: dict) -> str:
    """The name the ELN shows: a sample without a name shows its molecule."""
    name = data.get("name") or data.get("showed_name") or ""
    if not name and isinstance(data.get("molecule"), dict):
        name = data["molecule"].get("iupac_name") or ""
    return str(name).strip()


def type_label(name: str, types: dict[str, dict]) -> str:
    return types.get(name, {}).get("label") or name.replace("_", " ").capitalize()


class ExplorerSource:
    """Logs in on the first request with fresh credentials (`load_config`),
    again after a 401 or when URL, user, password or TLS setting changed."""

    def __init__(self, load_config: Callable[[], Any] | None = None,
                 instance_factory: Callable[..., Any] | None = None,
                 save_setting: Callable[[str, Any], None] | None = None) -> None:
        if load_config is None:
            from config import load_config
        if save_setting is None:
            from config import save_setting
        self._load_config = load_config
        self._save_setting = save_setting
        self._config = None
        self._instance_factory = instance_factory
        self._instance = None
        self._login_key: tuple | None = None
        self._root = None
        self._classes: dict[str, dict] | None = None

    # -- login ---------------------------------------------------------------

    def _new_instance(self, url: str, verify_tls: bool):
        if self._instance_factory is not None:
            return self._instance_factory(url, verify_ssl=verify_tls)
        from chemotion_api import Instance

        return Instance(url, verify_ssl=verify_tls)

    def _login(self, force: bool = False):
        try:
            config = self._load_config()
        except ValueError as exc:
            raise ExplorerError(f"{NOT_CONFIGURED} ({exc})") from None
        self._config = config
        key = (config.url, config.user, config.password, config.verify_tls)
        if self._instance is not None and key == self._login_key and not force:
            return self._instance
        self._instance = self._root = self._classes = None
        try:
            instance = self._new_instance(config.url, config.verify_tls)
            instance.test_connection().login(config.user, config.password)
        except PermissionError:
            raise ExplorerError(LOGIN_FAILED) from None
        except ExplorerError:
            raise
        except Exception as exc:
            raise ExplorerError(f"Cannot connect to Chemotion at {config.url}: {exc}") from None
        self._instance, self._login_key = instance, key
        return instance

    def _call(self, work: Callable[[Any], Any]):
        """`work(instance)`, logged in; once more after a fresh login when the
        session expired."""
        instance = self._login()
        try:
            return work(instance)
        except ExplorerError:
            raise
        except Exception as exc:
            if not _is_unauthorized(exc):
                raise ExplorerError(f"Chemotion answered with an error: {exc}") from None
        instance = self._login(force=True)
        try:
            return work(instance)
        except ExplorerError:
            raise
        except Exception as exc:
            raise ExplorerError(f"Chemotion answered with an error: {exc}") from None

    def _root_of(self, instance, reload: bool = False):
        if self._root is None or reload:
            self._root = instance.get_root_collection()
            self._classes = None
        return self._root

    # -- what the tab asks for ----------------------------------------------

    def collections(self) -> list[dict]:
        """The tree: "All", the own collections, then "Shared with me" (only
        when something is shared). Nodes are
        {id, label, path, shared, children}; "Shared with me" has id None."""

        def work(instance):
            root = self._root_of(instance, reload=True)
            nodes = [{"id": root.id, "label": ALL_LABEL, "path": ALL_LABEL, "shared": False, "children": []}]
            nodes += [_node(child, "", False) for child in root.children]
            shared = [_node(child, "", True) for child in root.sync_root.children]
            if shared:
                nodes.append({"id": None, "label": SHARED_LABEL, "path": SHARED_LABEL, "shared": True,
                              "children": shared})
            return nodes

        return self._call(work)

    def _all_classes(self, instance) -> dict[str, dict]:
        if self._classes is None:
            self._classes = dict(self._root_of(instance)._element_manager.all_classes)
        return self._classes

    def types(self) -> list[dict]:
        """[{name, label, generic, shown}]: the ELN's types in its order
        ("place"), then the generic ones by label; inactive ones left out.
        `shown`: chosen in the config (explorer_types), else DEFAULT_TYPES."""

        def work(instance):
            found = []
            for name, klass in self._all_classes(instance).items():
                if name in PSEUDO_TYPES or klass.get("is_active") is False:
                    continue
                generic = bool(klass.get("is_generic"))
                place = klass.get("place")
                found.append((generic, place if isinstance(place, (int, float)) else 1000,
                              str(klass.get("label") or name).lower(), name, klass))
            found.sort(key=lambda entry: entry[:3])
            chosen = getattr(self._config, "explorer_types", None)
            shown = DEFAULT_TYPES if chosen is None else chosen
            return [{"name": name, "label": klass.get("label") or name, "generic": generic, "shown": name in shown}
                    for generic, _place, _label, name, klass in found]

        return self._call(work)

    def save_types(self, shown: list[str], known: list[str]) -> None:
        """Save the choice: `shown` of the `known` types of this server; saved
        names of types this server does not have stay as they were."""
        chosen = getattr(self._config, "explorer_types", None)
        others = [name for name in (DEFAULT_TYPES if chosen is None else chosen) if name not in known]
        try:
            self._save_setting("explorer_types", list(shown) + others)
        except OSError as exc:
            raise ExplorerError(f"The choice of types was not saved: {exc}") from None
        if self._config is not None:
            try:
                self._config = replace(self._config, explorer_types=tuple(shown) + tuple(others))
            except TypeError:  # not a dataclass (tests)
                self._config.explorer_types = tuple(shown) + tuple(others)

    def page(self, collection: dict, type_name: str, page: int = 1, per_page: int = PER_PAGE) -> dict:
        """One page of one type in one collection:
        {items: [{id, type, short_label, name, url, tooltip}], total, more, page}."""
        if collection.get("id") is None:
            return {"items": [], "total": 0, "more": False, "page": page}

        def work(instance):
            classes = self._all_classes(instance)
            klass = classes.get(type_name)
            if klass is None:
                raise ExplorerError(f"This Chemotion has no element type '{type_name}'.")
            root = self._root_of(instance)
            listing_type = listing_class()
            sync_ids = _uses_sync_ids(listing_type)
            options = {"collection_is_sync": True} if sync_ids and collection.get("shared") else {}
            listing = listing_type(root._session, klass, collection["id"], **options)
            listing._page = page  # ElementSet.page = n refuses pages beyond the unloaded max_page
            listing.load_elements(per_page=per_page)
            rows = list(list.__iter__(listing))  # the loaded page, not every page
            total = listing._length
            if total is None:
                more = page < listing.max_page
                total = (page - 1) * per_page + len(rows) + (1 if more else 0)
            else:
                more = page * per_page < total
            # 3.x shows shared collections under /mydb/scollection/<id>.
            route = "scollection" if sync_ids and collection.get("shared") else "collection"
            host = instance.host_url.rstrip("/")
            label = type_label(type_name, classes)
            items = []
            for row in rows:
                data = row.json_data
                item_id = data.get("id")
                name = element_name(data)
                tooltip = [f"{label} {data.get('short_label') or ''}".strip(), f"id {item_id}"]
                if name:
                    tooltip.insert(1, name)
                formula = data.get("sum_formula") or (data.get("molecule") or {}).get("sum_formular")
                if formula:
                    tooltip.append(f"Formula {formula}")
                if data.get("updated_at"):
                    tooltip.append(f"Updated {data['updated_at']}")
                items.append({"id": item_id, "type": type_name, "short_label": data.get("short_label") or "",
                              "name": name, "url": f"{host}/mydb/{route}/{collection['id']}/{type_name}/{item_id}",
                              "tooltip": "\n".join(tooltip)})
            return {"items": items, "total": total, "more": more, "page": page}

        return self._call(work)


def _node(collection, parent_path: str, shared: bool) -> dict:
    label = str(collection.label or "")
    path = f"{parent_path}/{label}" if parent_path else label
    return {"id": collection.id, "label": label, "path": path, "shared": shared,
            "children": [_node(child, path, shared) for child in collection.children]}


class ExplorerWorker:
    """Runs the source's requests one after another in one thread (one
    chemotion_api session). `deliver(fn)` brings a finished request's callback
    to the caller's thread (the tab: GLib.idle_add); answers that started
    before the last `reset()` are dropped there."""

    def __init__(self, source: ExplorerSource, deliver: Callable[[Callable[[], None]], Any]) -> None:
        self.source = source
        self._deliver = deliver
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.generation = 0
        self.closed = False

    def request(self, method: str, args: tuple, on_done: Callable[[Any], None],
                on_error: Callable[[str], None]) -> None:
        """Queues `source.<method>(*args)`."""
        with self._lock:
            if self.closed:
                return
            self._queue.put((self.generation, method, args, on_done, on_error))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="explorer", daemon=True)
                self._thread.start()

    def reset(self) -> None:
        """Drops the queued requests; answers of running ones are dropped too."""
        with self._lock:
            self.generation += 1
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def close(self) -> None:
        self.reset()
        with self._lock:
            self.closed = True
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            generation, method, args, on_done, on_error = job
            if generation != self.generation:
                continue
            try:
                result = getattr(self.source, method)(*args)
            except ExplorerError as exc:
                self._send(generation, on_error, str(exc))
            except Exception as exc:  # a bug here must not end the thread
                self._send(generation, on_error, f"{type(exc).__name__}: {exc}")
            else:
                self._send(generation, on_done, result)

    def _send(self, generation: int, callback: Callable[[Any], None], value: Any) -> None:
        def call() -> bool:
            if generation == self.generation and not self.closed:
                callback(value)
            return False  # GLib.idle_add: once

        self._deliver(call)
