"""The data of the Explorer tab (no GTK): the collection tree, the element
types and one page of elements of one type in one collection, all through
chemotion_api, plus the worker thread the tab runs them in.

The tab is read-only and the user starts every request, so there is no
confirmation card. `ExplorerSource` runs inside the helper process
(explorer_helper.py), not in the app: a Python process keeps the chemotion_api
it imported first, so a newly installed release would not be used.
`ExplorerProxy` (app side, no chemotion_api, no GTK) talks to that helper.

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

import base64
import collections
import inspect
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import replace
from typing import Any, Callable

import element_views

LOG = logging.getLogger("chem-assistent")

PER_PAGE = 10
# One request to the helper (a login and a listing); then the helper is killed.
REQUEST_TIMEOUT_S = 120.0
# The helper's stderr lines kept for error messages.
STDERR_TAIL_LINES = 20
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Shown when nothing was chosen yet (config `explorer_types` empty).
DEFAULT_TYPES = ("sample", "reaction", "research_plan", "wellplate", "screen")
# chemotion_api's pseudo class for "any generic element": not a type of its own.
PSEUDO_TYPES = {"generic_element"}
SHARED_LABEL = "Shared with me"
ALL_LABEL = "All"

NOT_CONFIGURED = "Chemotion is not set up yet: enter URL, user and password in the settings."
LOGIN_FAILED = "Login to Chemotion failed: check user and password in the settings."


class ExplorerError(Exception):
    """A failure the tab shows as it is (the message is for the user).
    `release`: the chemotionapi release that fits the server, when the
    message says the installed one does not ("Change to <release>")."""

    def __init__(self, message: str = "", release: str = "") -> None:
        super().__init__(message)
        self.release = release


class ErrorText(str):
    """The error text ExplorerWorker passes to `on_error`: a str that also
    carries `.release` ("" by default)."""

    release: str = ""

    def __new__(cls, text: str, release: str = ""):
        obj = super().__new__(cls, text)
        obj.release = release
        return obj


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
        {id, label, path, shared, url, children}; "Shared with me" has id
        and url None."""

        def work(instance):
            root = self._root_of(instance, reload=True)
            host = instance.host_url.rstrip("/")
            shared_route = "scollection" if _uses_sync_ids(listing_class()) else "collection"

            def node(collection, parent_path: str, shared: bool) -> dict:
                label = str(collection.label or "")
                path = f"{parent_path}/{label}" if parent_path else label
                route = shared_route if shared else "collection"
                return {"id": collection.id, "label": label, "path": path, "shared": shared,
                        "url": f"{host}/mydb/{route}/{collection.id}",
                        "children": [node(child, path, shared) for child in collection.children]}

            nodes = [{"id": root.id, "label": ALL_LABEL, "path": ALL_LABEL, "shared": False,
                      "url": f"{host}/mydb/collection/{root.id}", "children": []}]
            nodes += [node(child, "", False) for child in root.children]
            shared = [node(child, "", True) for child in root.sync_root.children]
            if shared:
                nodes.append({"id": None, "label": SHARED_LABEL, "path": SHARED_LABEL, "shared": True,
                              "url": None, "children": shared})
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
        {items: [{id, type, short_label, name, updated_at, url, tooltip}], total, more, page}."""
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
                              "name": name, "updated_at": str(data.get("updated_at") or ""), "url": f"{host}/mydb/{route}/{collection['id']}/{type_name}/{item_id}",
                              "tooltip": "\n".join(tooltip)})
            return {"items": items, "total": total, "more": more, "page": page}

        return self._call(work)

    def details(self, type_name: str, element_id: int, collection_id: int | str | None = None) -> dict:
        """The view model (element_views.py) of one element, read only. A type
        with no adapter that the server does not mark `is_generic`, or one the
        installed chemotion_api has no getter for, gives the stub; a generic
        type (LabIMotion) is loaded with `get_generic_by_name()` and shown from
        its layers (element_generic.py). The url needs the collection the
        element is shown in (else it is "")."""

        def work(instance):
            generic = False
            if type_name in element_views.ADAPTERS:
                getter = getattr(instance, GETTERS.get(type_name, ""), None)
            else:
                klass = self._all_classes(instance).get(type_name)
                generic = bool(klass and klass.get("is_generic")) and hasattr(instance, "get_generic_by_name")
                if not generic:
                    return element_views.stub(type_name, {"id": element_id})
                getter = lambda element_id: instance.get_generic_by_name(type_name, element_id)  # noqa: E731
            if getter is None:
                return element_views.stub(type_name, {"id": element_id})
            host = instance.host_url.rstrip("/")
            url = ""
            if collection_id not in (None, ""):
                url = f"{host}/mydb/collection/{collection_id}/{type_name}/{element_id}"
            element = getter(element_id)
            data = element.json_data
            if generic:
                if not isinstance(data.get("element_klass"), dict):
                    data = dict(data, element_klass={k: v for k, v in klass.items() if k != "properties_template"})
                images, notes = {}, []
            else:
                images, notes = self._load_images(element, type_name, data)
            model = element_views.view(type_name, data, images, url)
            model["notes"] = list(model.get("notes") or []) + notes
            return model

        return self._call(work)

    @staticmethod
    def _load_images(element, type_name: str, data: dict) -> tuple[dict, list[str]]:
        """GET every image `IMAGE_REFS[type_name]` names (SVG as text, others
        base64): ({key: IMAGE}, notes). An image that is missing, too large
        or fails to load is left out with a note (the details are shown
        without it); a 401 is raised."""
        refs = element_views.IMAGE_REFS.get(type_name)
        images: dict[str, dict] = {}
        notes: list[str] = []
        for key, path in (refs(data) if refs else {}).items():
            name = element_views.IMAGE_NAMES.get(type_name, "") if key in ("image", "scheme") else key
            name = name or "element"
            try:
                response = element._session.get(path)
                if response.status_code != 200:
                    raise ConnectionError(f"{response.status_code}")
                content = response.content
                if len(content) > element_views.MAX_IMAGE_BYTES:
                    notes.append(element_views.image_too_large(name))
                    continue
                images[key] = _image_of(path, content, getattr(response, "headers", None))
            except Exception as exc:
                if _is_unauthorized(exc):
                    raise
                LOG.warning("Image %s not loaded: %s", path, exc)
                notes.append(f"The {name} image could not be loaded.")
        return images, notes


# The instance's getter per element type (a line without one has no view for it).
GETTERS = {"sample": "get_sample", "reaction": "get_reaction", "wellplate": "get_wellplate",
           "screen": "get_screen", "research_plan": "get_research_plan", "cell_line": "get_cell_line",
           "device_description": "get_device_description",
           "sequence_based_macromolecule_sample": "get_sbmm_sample"}
RASTER_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _image_of(path: str, content: bytes, headers: Any) -> dict:
    """An IMAGE dict (element_views.py) from a response; ValueError for
    anything that is no SVG, PNG, JPEG or GIF."""
    mime = ""
    try:
        mime = str((headers or {}).get("Content-Type") or "").split(";")[0].strip().lower()
    except Exception:
        pass
    extension = os.path.splitext(path.split("?")[0])[1].lower()
    if "svg" in mime or extension == ".svg":
        return {"mime": element_views.IMAGE_MIME, "data": content.decode("utf-8", errors="replace")}
    if mime not in RASTER_TYPES.values():
        mime = RASTER_TYPES.get(extension, "")
    if not mime:
        raise ValueError(f"unsupported image type for {path}")
    return {"mime": mime, "data": base64.b64encode(content).decode("ascii"), "encoding": "base64"}


def _kill_tree(process: subprocess.Popen) -> None:
    """Ends the helper and whatever it started."""
    if process.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                               creationflags=NO_WINDOW)
            else:
                os.killpg(process.pid, signal.SIGKILL)  # start_new_session: its own group
        except OSError:
            pass
        process.kill()
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream and stream.close()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class _Helper:
    """One running helper process with its reader threads."""

    def __init__(self, command: list[str], stamp: Any) -> None:
        self.stamp = stamp
        self.stderr_tail: collections.deque = collections.deque(maxlen=STDERR_TAIL_LINES)
        self.answers: queue.Queue = queue.Queue()  # parsed answers; None = stdout closed
        kwargs: dict[str, Any] = {"creationflags": NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        errors="replace", bufsize=1, **kwargs)
        threading.Thread(target=self._read_stdout, daemon=True, name="explorer-helper-out").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="explorer-helper-err").start()

    def _read_stdout(self) -> None:
        try:
            for line in self.process.stdout:
                try:
                    answer = json.loads(line)
                except ValueError:
                    self.stderr_tail.append(line.strip())  # stray output, not an answer
                    continue
                if isinstance(answer, dict):
                    self.answers.put(answer)
        except (OSError, ValueError):
            pass
        self.answers.put(None)

    def _read_stderr(self) -> None:
        try:
            for line in self.process.stderr:
                if line.strip():
                    self.stderr_tail.append(line.strip())
        except (OSError, ValueError):
            pass

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def last_error_line(self) -> str:
        return self.stderr_tail[-1] if self.stderr_tail else ""

    def stop(self) -> None:
        """Closes stdin (the helper then exits), kills it if it does not."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
        _kill_tree(self.process)


def _check_versions(config: Any, installed: str) -> tuple[str, str]:
    """(warning, release to install) when the server's ELN version does not
    fit the installed chemotionapi, else ("", ""); ("", "") also when the
    server's version cannot be read. The releases unknown (offline): the
    warning without a release."""
    import api_versions

    try:
        version = api_versions.server_eln_version(config.url, config.verify_tls)
    except api_versions.VersionError:
        return "", ""
    try:
        tags = api_versions.list_tags(timeout=20)
    except api_versions.VersionError:
        tags = None
    text = api_versions.check_installed(version, tags, api_versions.installed_tag())
    if not text:
        return "", ""
    release = ""
    if tags is not None:
        try:
            release = api_versions.release_for(version, tags).tag
        except api_versions.VersionError:
            pass
    return text, release


class ExplorerProxy:
    """The ExplorerSource's interface, answered by the helper process
    (explorer_helper.py) so that chemotion_api never loads in the app. A
    request goes to the helper with a fresh config (credentials on its stdin
    only). The helper restarts when it died, a request timed out, or the
    installed chemotionapi release changed (`release_stamp()`), so a newly
    installed release is used at once.

    When a request fails, the server's ELN version is compared with the
    installed release (once per URL and release): on a mismatch the
    ExplorerError carries that warning and `.release`, the one to install.
    Requests run one at a time (the ExplorerWorker's thread); close() may
    come from another thread."""

    def __init__(self, load_config: Callable[[], Any] | None = None,
                 save_setting: Callable[[str, Any], None] | None = None,
                 command: list[str] | None = None, timeout: float = REQUEST_TIMEOUT_S,
                 release_stamp: Callable[[], Any] | None = None,
                 version_check: Callable[[Any, str], tuple[str, str]] | None = None) -> None:
        self._load_config = load_config
        self._save_setting = save_setting
        self._command = command
        self.timeout = timeout
        self._release_stamp = release_stamp
        self._version_check = version_check or _check_versions
        self._config: Any = None
        self._helper: _Helper | None = None
        self._ids = 0
        self._checks: dict[tuple, tuple[str, str]] = {}
        self._lock = threading.Lock()

    # -- the helper ----------------------------------------------------------

    def _stamp(self) -> Any:
        if self._release_stamp is not None:
            return self._release_stamp()
        import api_versions

        return api_versions.installed_label()

    def _helper_for(self, stamp: Any) -> _Helper:
        helper = self._helper
        if helper is not None and (not helper.alive or helper.stamp != stamp):
            helper.stop()
            helper = self._helper = None
        if helper is None:
            command = self._command
            if command is None:
                import workspace

                command = workspace.command("explorer_helper.py", console=True)
            try:
                helper = self._helper = _Helper(command, stamp)
            except OSError as exc:
                raise ExplorerError(f"Could not start the Explorer's helper process: {exc}") from None
        return helper

    def _drop(self) -> None:
        helper, self._helper = self._helper, None
        if helper is not None:
            helper.stop()

    def close(self) -> None:
        """Ends the helper (the next request starts a new one)."""
        helper, self._helper = self._helper, None
        if helper is not None:
            threading.Thread(target=helper.stop, daemon=True).start()

    # -- requests -------------------------------------------------------------

    def _login(self) -> dict:
        if self._load_config is None:
            from config import load_config

            self._load_config = load_config
        try:
            config = self._load_config()
        except ValueError as exc:
            raise ExplorerError(f"{NOT_CONFIGURED} ({exc})") from None
        self._config = config
        types = getattr(config, "explorer_types", None)
        return {"url": config.url, "user": config.user, "password": config.password,
                "verify_tls": config.verify_tls, "explorer_types": None if types is None else list(types)}

    def _request(self, method: str, args: list) -> Any:
        with self._lock:
            login = self._login()
            stamp = self._stamp()
            try:
                result = self._exchange(method, args, login, stamp)
            except ExplorerError as exc:
                raise self._explained(exc, stamp) from None
            self._checks.pop((login["url"], stamp), None)
            return result

    def _exchange(self, method: str, args: list, login: dict, stamp: Any) -> Any:
        helper = self._helper_for(stamp)
        self._ids += 1
        request_id = self._ids
        line = json.dumps({"id": request_id, "method": method, "args": args, "config": login}, ensure_ascii=False)
        try:
            helper.process.stdin.write(line + "\n")
            helper.process.stdin.flush()
        except (OSError, ValueError):
            self._drop()
            raise ExplorerError(self._died(helper)) from None
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                answer = helper.answers.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self._drop()
                raise ExplorerError(f"Chemotion did not answer within {self.timeout:g} s.") from None
            if answer is None:
                self._drop()
                raise ExplorerError(self._died(helper))
            if answer.get("id") != request_id:
                continue  # an answer of an earlier, abandoned request
            if "error" in answer:
                text = str(answer["error"])
                if answer.get("kind") != "explorer":
                    LOG.warning("Explorer helper failed: %s", text)
                raise ExplorerError(text)
            return answer.get("result")

    @staticmethod
    def _died(helper: _Helper) -> str:
        try:
            helper.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        detail = helper.last_error_line()
        return "The Explorer's helper process ended unexpectedly" + (f": {detail}" if detail else ".")

    def _explained(self, exc: ExplorerError, stamp: Any) -> ExplorerError:
        """`exc`, or the version warning when the installed release does not fit the server."""
        text = str(exc)
        if text == LOGIN_FAILED or text.startswith(NOT_CONFIGURED) or self._config is None:
            return exc
        key = (self._config.url, stamp)
        if key not in self._checks:
            try:
                self._checks[key] = self._version_check(self._config, str(stamp))
            except Exception as check_error:  # the check must not hide the real error
                LOG.warning("Version check failed: %s", check_error)
                self._checks[key] = ("", "")
        warning, release = self._checks[key]
        return ExplorerError(warning, release) if warning else exc

    # -- what the tab asks for ----------------------------------------------

    def collections(self) -> list[dict]:
        return self._request("collections", [])

    def types(self) -> list[dict]:
        return self._request("types", [])

    def page(self, collection: dict, type_name: str, page: int = 1, per_page: int = PER_PAGE) -> dict:
        return self._request("page", [collection, type_name, page, per_page])

    def details(self, type_name: str, element_id: int, collection_id: int | str | None = None) -> dict:
        # Always the helper: only it knows whether the server's type is a generic one (it answers the stub otherwise).
        return self._request("details", [type_name, element_id, collection_id])

    def save_types(self, shown: list[str], known: list[str]) -> None:
        """As ExplorerSource.save_types(), in the app process."""
        if self._save_setting is None:
            from config import save_setting

            self._save_setting = save_setting
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


def mention_text(type_label: str, item: dict | None, collection: dict) -> str:
    """What "Mention in message" puts into the chat input: enough for the
    agent to find the element (type name and id) or the collection (path, id).
    `item` None: the collection itself."""
    where = "shared collection" if collection.get("shared") else "collection"
    path = json.dumps(collection.get("path") or "", ensure_ascii=False)
    if item is None:
        return f"{where} {path} (id {collection.get('id')})"
    words = [type_label.lower()]
    if item.get("short_label"):
        words.append(item["short_label"])
    if item.get("name"):
        words.append(json.dumps(item["name"], ensure_ascii=False))
    return f"{' '.join(words)} ({item['type']} id {item['id']} in {where} {path})"


def detail_rows(item: dict) -> list[tuple[str, str]]:
    """(label, value) rows of the Details panel for one describe() dict:
    Type, Short label, Name, Collection, Shared (only when true), ID. Empty
    values are left out."""
    rows = [("Type", item.get("type_label") or ""), ("Short label", item.get("short_label") or ""),
            ("Name", item.get("name") or ""), ("Collection", item.get("collection") or ""),
            ("Shared", "yes" if item.get("shared") else ""),
            ("ID", "" if item.get("id") is None else str(item["id"]))]
    return [(name, value) for name, value in rows if value]


class ExplorerWorker:
    """Runs the source's requests one after another in one thread (one
    chemotion_api session). `deliver(fn)` brings a finished request's callback
    to the caller's thread (the tab: GLib.idle_add); answers that started
    before the last `reset()` are dropped there."""

    def __init__(self, source: "ExplorerSource | ExplorerProxy", deliver: Callable[[Callable[[], None]], Any]) -> None:
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
        close = getattr(self.source, "close", None)
        if close is not None:
            close()  # ends the helper process

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
                self._send(generation, on_error, ErrorText(str(exc), getattr(exc, "release", "")))
            except Exception as exc:  # a bug here must not end the thread
                self._send(generation, on_error, ErrorText(f"{type(exc).__name__}: {exc}"))
            else:
                self._send(generation, on_done, result)

    def _send(self, generation: int, callback: Callable[[Any], None], value: Any) -> None:
        def call() -> bool:
            if generation == self.generation and not self.closed:
                callback(value)
            return False  # GLib.idle_add: once

        self._deliver(call)
