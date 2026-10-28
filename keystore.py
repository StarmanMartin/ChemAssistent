"""The system keyring, where config.py keeps the app's secrets.

Linux: the Secret Service (GNOME Keyring, KWallet) through libsecret
(gir1.2-secret-1), Windows: generic credentials of the Credential Manager
(advapi32 via ctypes). Without either, available() is False and config.py
keeps the secrets in config.json (0600) as before. Nothing is loaded before
the first call: config.py imports this module, and hotkey.py runs on every
key press. Standard library plus PyGObject on Linux.
"""
from __future__ import annotations

import logging
import sys
import threading
import time

LOG = logging.getLogger("chem-assistent")

# libsecret schema; every secret is one item with the attribute key=<name>.
SCHEMA = "edu.kit.chem-assistent"
# Credential Manager target names: ChemAssistent/<name>.
TARGET_PREFIX = "ChemAssistent/"
# Long enough to type the password when the keyring asks to be unlocked.
TIMEOUT_S = 120
# Connecting to the Secret Service and finding its default keyring.
PROBE_TIMEOUT_S = 10
# After that failed, look again at most this often (the user service may
# start before the keyring daemon is on the bus).
PROBE_RETRY_S = 60


class KeystoreError(Exception):
    """The keyring is missing or did not store or return a secret."""


class SecretService:
    """The freedesktop Secret Service through libsecret."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Gio, GLib, Secret

        self.Gio, self.GLib, self.Secret = Gio, GLib, Secret
        self.schema = Secret.Schema.new(SCHEMA, Secret.SchemaFlags.NONE,
                                        {"key": Secret.SchemaAttributeType.STRING})
        self.service = self._call(Secret.Service.get_sync, Secret.ServiceFlags.OPEN_SESSION,
                                  timeout=PROBE_TIMEOUT_S)
        # A Secret Service without a default keyring (a bare gnome-keyring-daemon,
        # e.g. under WSL) answers lookups with nothing, and every store fails
        # only after ~15 s.
        if self._call(Secret.Collection.for_alias_sync, self.service, "default", Secret.CollectionFlags.NONE,
                      timeout=PROBE_TIMEOUT_S) is None:
            raise KeystoreError("the Secret Service has no default keyring")

    def _call(self, function, *args, timeout: float = TIMEOUT_S):
        """`function(*args, cancellable)`, cancelled after `timeout` seconds."""
        cancellable = self.Gio.Cancellable()
        timer = threading.Timer(timeout, cancellable.cancel)
        timer.daemon = True
        timer.start()
        try:
            return function(*args, cancellable)
        except self.GLib.Error as exc:
            if cancellable.is_cancelled():
                raise KeystoreError(f"the keyring did not answer within {timeout:g} s") from exc
            raise KeystoreError(exc.message) from exc
        finally:
            timer.cancel()

    def read(self, key: str) -> str:
        return self._call(self.Secret.password_lookup_sync, self.schema, {"key": key}) or ""

    def read_all(self, keys) -> dict[str, str]:
        # One D-Bus search for every item of the schema (and one unlock prompt).
        flags = self.Secret.SearchFlags.ALL | self.Secret.SearchFlags.UNLOCK | self.Secret.SearchFlags.LOAD_SECRETS
        items = self._call(self.service.search_sync, self.schema, {}, flags)
        found, locked = {}, set()
        for item in items or []:
            key = item.get_attributes().get("key")
            if key not in keys:
                continue
            secret = None if item.get_locked() else item.get_secret()
            if secret is None:
                locked.add(key)
            else:
                found[key] = secret.get_text() or ""
        # A dismissed unlock prompt is no error for libsecret: the items come
        # back locked and without their secrets. Reading that as "no secrets"
        # would let a save delete them. (A locked copy in another keyring does
        # not count once the key was read.)
        if locked - found.keys():
            raise KeystoreError("the keyring is still locked")
        return found

    def write(self, key: str, value: str, label: str) -> None:
        if not self._call(self.Secret.password_store_sync, self.schema, {"key": key},
                          self.Secret.COLLECTION_DEFAULT, label, value):
            raise KeystoreError(f"the keyring did not store {key}")

    def delete(self, key: str) -> None:
        self._call(self.Secret.password_clear_sync, self.schema, {"key": key})


class CredentialManager:
    """Generic credentials of the Windows Credential Manager."""

    CRED_TYPE_GENERIC = 1
    # Roams with a roaming profile, like config.json in %APPDATA% (without
    # one, e.g. a local or Microsoft account, Windows keeps it locally). Where
    # a policy refuses it, this computer only.
    PERSIST = (3, 2)  # CRED_PERSIST_ENTERPRISE, CRED_PERSIST_LOCAL_MACHINE
    ERROR_NOT_FOUND = 1168
    # CRED_MAX_CREDENTIAL_BLOB_SIZE
    MAX_BLOB = 5 * 512

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        self.ctypes, self.CREDENTIAL = ctypes, CREDENTIAL
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.cred_read = advapi32.CredReadW
        self.cred_read.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.cred_read.restype = wintypes.BOOL
        self.cred_write = advapi32.CredWriteW
        self.cred_write.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.cred_write.restype = wintypes.BOOL
        self.cred_delete = advapi32.CredDeleteW
        self.cred_delete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.cred_delete.restype = wintypes.BOOL
        self.cred_free = advapi32.CredFree
        self.cred_free.argtypes = [ctypes.c_void_p]
        self.cred_free.restype = None

    def _error(self, action: str, key: str) -> KeystoreError:
        error = self.ctypes.get_last_error()
        return KeystoreError(f"could not {action} {key}: {self.ctypes.FormatError(error).strip()} ({error})")

    def read(self, key: str) -> str:
        ctypes = self.ctypes
        credential = ctypes.POINTER(self.CREDENTIAL)()
        if not self.cred_read(TARGET_PREFIX + key, self.CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
            if ctypes.get_last_error() == self.ERROR_NOT_FOUND:
                return ""
            raise self._error("read", key)
        try:
            blob = ctypes.string_at(credential.contents.CredentialBlob, credential.contents.CredentialBlobSize)
        finally:
            self.cred_free(credential)
        # UTF-16 like the passwords cmdkey and the Windows UI store.
        return blob.decode("utf-16-le", errors="replace")

    def read_all(self, keys) -> dict[str, str]:
        # Local calls without D-Bus: one per key is cheap.
        return {key: value for key in keys if (value := self.read(key))}

    def write(self, key: str, value: str, label: str) -> None:
        ctypes = self.ctypes
        blob = value.encode("utf-16-le")
        if len(blob) > self.MAX_BLOB:
            raise KeystoreError(f"{key} is too long for the Credential Manager")
        buffer = ctypes.create_string_buffer(blob, len(blob))
        for persist in self.PERSIST:
            credential = self.CREDENTIAL(
                Type=self.CRED_TYPE_GENERIC, TargetName=TARGET_PREFIX + key, Comment=label,
                CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
                Persist=persist, UserName=key,
            )
            if self.cred_write(ctypes.byref(credential), 0):
                return
        # E.g. ERROR_NO_SUCH_LOGON_SESSION: a network logon (SSH) has no credential set.
        raise self._error("store", key)

    def delete(self, key: str) -> None:
        if not self.cred_delete(TARGET_PREFIX + key, self.CRED_TYPE_GENERIC, 0):
            if self.ctypes.get_last_error() != self.ERROR_NOT_FOUND:
                raise self._error("delete", key)


_lock = threading.Lock()
# The backend once backend() found one; False: there is none (_problem says
# why), until PROBE_RETRY_S after _failed_at.
_store: object = None
_problem = ""
_failed_at = 0.0


def backend():
    """The keyring of this system, or None (found once per process; after a
    failure looked for again every PROBE_RETRY_S)."""
    global _store, _problem, _failed_at
    with _lock:
        if _store is None or (_store is False and time.monotonic() - _failed_at >= PROBE_RETRY_S):
            try:
                _store = CredentialManager() if sys.platform == "win32" else SecretService()
            except Exception as exc:  # no typelib, no D-Bus, no default keyring, no advapi32
                _store, _problem, _failed_at = False, str(exc) or type(exc).__name__, time.monotonic()
                LOG.info("No system keyring (%s); secrets stay in config.json.", _problem)
        return _store or None


def available() -> bool:
    return backend() is not None


def problem() -> str:
    """Why there is no keyring ("" when there is one)."""
    return "" if available() else _problem


def _require():
    store = backend()
    if store is None:
        raise KeystoreError(f"no system keyring: {_problem}")
    return store


def read(key: str) -> str:
    """The secret `key`, "" when the keyring has none. Raises KeystoreError."""
    return _require().read(key)


def read_all(keys) -> dict[str, str]:
    """The secrets among `keys` that the keyring has. Raises KeystoreError."""
    return _require().read_all(keys)


def write(key: str, value: str, label: str) -> None:
    """Store `value` under `key` ("" deletes it). Raises KeystoreError."""
    store = _require()
    if value:
        store.write(key, value, label)
    else:
        store.delete(key)


def delete(key: str) -> None:
    _require().delete(key)
