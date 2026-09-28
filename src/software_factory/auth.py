"""User-scoped TypeSafe credentials; loaded lazily and never exported to children."""

from __future__ import annotations

import base64
import getpass
import json
import os
import stat
import sys
import uuid
import warnings
from contextlib import contextmanager
from pathlib import Path

from .core import FactoryError

KEY_ENV = "TYPESAFE_API_KEY"
DISABLED_ENV = "SOFTWARE_FACTORY_AUTH_DISABLED"
FILENAME = "credentials.json"
MAX_KEY_BYTES = 4096
MAX_STORE_BYTES = 32768
WINDOWS = os.name == "nt"
DIR_FD = os.name == "posix"


class CredentialError(FactoryError):
    """Only static messages: never include key bytes or underlying exception text."""


def _error(message):
    return CredentialError(message, 2)


def _key(value):
    if not isinstance(value, str):
        raise _error("TypeSafe API key must be nonempty printable ASCII without spaces")
    value = value.strip()
    # Size first: an oversized value is reported as too long whatever else is wrong with it.
    if len(value) > MAX_KEY_BYTES:
        raise _error("API key input exceeds the size limit")
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise _error("TypeSafe API key must be nonempty printable ASCII without spaces")
    return value


def _override():
    """The environment key when it overrides the store; blank or unset means no override."""
    value = os.environ.get(KEY_ENV)
    return value if value is not None and value.strip() else None


def _directory_unchecked():
    if WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if not base.is_absolute():
        raise _error("Credential configuration directory must be an absolute path")
    directory = base / "software-factory"
    resolved = directory.resolve()
    if any((parent / ".git").exists() for parent in (resolved, *resolved.parents)):
        raise _error(
            "Credential storage must be outside Git repositories; set an external XDG_CONFIG_HOME or APPDATA"
        )
    return directory


def _directory():
    try:
        return _directory_unchecked()
    except (OSError, RuntimeError, ValueError):
        raise _error("Cannot resolve the private credential directory") from None


def _check(info, *, directory=False, size=True):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise _error("Credential storage must not use symlinks or reparse points")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise _error("Credential storage has an unexpected file type")
    if not WINDOWS and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise _error("Credential storage must be owned by you: directory mode 700, file mode 600")
    # Atomic rotation can make stat or fstat observe an unlinked old inode.
    # Zero links is safe; the descriptor is independently checked after opening.
    links_ok = info.st_nlink in (0, 1)
    if not directory and (not links_ok or (size and info.st_size > MAX_STORE_BYTES)):
        raise _error("Credential file must have one link and fit the storage size limit")


@contextmanager
def _store(*, create=False):
    """Open the checked store directory; yields None when it does not exist.

    Only opening the directory is reported as a store access problem. Errors raised by the
    caller's body propagate unchanged, so a read or write failure is not relabelled here.
    """
    directory = _directory()
    descriptor = None
    try:
        if create:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            _check(directory.lstat(), directory=True)
        except FileNotFoundError:
            store = None
        else:
            if DIR_FD:
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                _check(os.fstat(descriptor), directory=True)
            store = (directory, descriptor)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise _error(
            "Cannot access the private credential store; check its ownership and permissions"
        ) from None
    try:
        yield store
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _path(store, name):
    directory, descriptor = store
    return (name, {"dir_fd": descriptor}) if descriptor is not None else (directory / name, {})


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _stat_file(store, *, size=True):
    """Safety-check the credential file; False when it does not exist."""
    path, options = _path(store, FILENAME)
    try:
        _check(os.stat(path, follow_symlinks=False, **options), size=size)
    except FileNotFoundError:
        return False
    except OSError:
        raise _error("Cannot access the credential file; check its ownership and permissions") from None
    return True


def _read(store):
    if store is None or not _stat_file(store):
        return None
    path, options = _path(store, FILENAME)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), **options)
        with os.fdopen(descriptor, "rb") as handle:
            _check(os.fstat(handle.fileno()))
            data = handle.read(MAX_STORE_BYTES + 1)
    except OSError:
        raise _error("Cannot read the credential file; check its ownership and permissions") from None
    if len(data) > MAX_STORE_BYTES:
        raise _error("Credential file exceeds the storage size limit")
    try:
        value = json.loads(data, object_pairs_hook=_unique_object)
        expected = "windows-dpapi" if WINDOWS else "private-file"
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "provider", "storage", "secret"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["provider"] != "typesafe"
            or value["storage"] != expected
            or not isinstance(value["secret"], str)
        ):
            raise ValueError()
        secret = value["secret"]
        if WINDOWS:
            secret = _dpapi(base64.b64decode(secret, validate=True), protect=False).decode("ascii")
        return _key(secret)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error("Credential store is invalid; restore it or move it aside before auth login") from None


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Windows user-bound encryption; never open an OS prompt during agent calls."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    try:
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        # CRYPTPROTECT_UI_FORBIDDEN, without machine-wide encryption.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise _error("Windows credential protection is unavailable for this user")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel.LocalFree(result.data)
    except (OSError, AttributeError):
        raise _error("Windows credential protection is unavailable for this user") from None


def get_typesafe_key():
    """A nonblank environment override, then the user store. No network and no environment writes.

    A blank TYPESAFE_API_KEY (unset in effect, as some shells and CI templates export it) is not
    an override: the saved key is used.
    """
    if os.environ.get(DISABLED_ENV) == "1":
        return None
    override = _override()
    if override is not None:
        return _key(override)
    with _store() as store:
        return _read(store)


def request_credential(get_api_key=None):
    """Preserve advisory callers' structured failure behavior without exposing storage details."""
    if os.environ.get(DISABLED_ENV) == "1":
        return None, "credential_disabled"
    try:
        value = (get_api_key or get_typesafe_key)()
        if not isinstance(value, str) or not value.strip():
            return None, "credential_missing"
        return _key(value), None
    except (CredentialError, OSError):
        return None, "credential_unavailable"


def save_typesafe_key(value):
    if os.environ.get(DISABLED_ENV) == "1":
        raise _error("Credential access is disabled in this process")
    value = _key(value)
    secret = (
        base64.b64encode(_dpapi(value.encode("ascii"), protect=True)).decode("ascii") if WINDOWS else value
    )
    record = {
        "schema_version": 1,
        "provider": "typesafe",
        "storage": "windows-dpapi" if WINDOWS else "private-file",
        "secret": secret,
    }
    payload = (json.dumps(record, indent=2) + "\n").encode()
    with _store(create=True) as store:
        _read(store)  # Never overwrite an unsafe or unrelated existing file.
        temporary = ".credentials-" + uuid.uuid4().hex
        path, options = _path(store, temporary)
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600, **options
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                directory, directory_fd = store
                if directory_fd is not None:
                    os.replace(temporary, FILENAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    os.fsync(directory_fd)
                else:
                    os.replace(directory / temporary, directory / FILENAME)
            finally:
                try:
                    os.unlink(path, **options)
                except FileNotFoundError:
                    pass
        except OSError:
            raise _error("Cannot write the credential file; the previous key, if any, is unchanged") from None
    return {
        "provider": "typesafe",
        "saved": True,
        "storage": record["storage"],
        "path": str(_directory() / FILENAME),
        "environment_override_present": _override() is not None,
        "provider_validation": "not_checked",
    }


def auth_status():
    source = None
    try:
        if os.environ.get(DISABLED_ENV) == "1":
            raise _error("Credential access is disabled in this process")
        key = get_typesafe_key()
        if key is not None:
            source = "environment" if _override() is not None else "user_store"
        return {
            "provider": "typesafe",
            "configured": key is not None,
            "source": source,
            "path": str(_directory() / FILENAME) if source != "environment" else None,
            "provider_validation": "not_checked",
            "environment_override_present": _override() is not None,
            "_exit_code": 0 if key is not None else 2,
        }
    except CredentialError as exc:
        return {
            "provider": "typesafe",
            "configured": False,
            "source": None,
            "error": str(exc),
            "provider_validation": "not_checked",
            "_exit_code": 2,
        }


def logout():
    """Remove the saved key file, even when its content is invalid.

    The file must still pass the safety checks (no symlink or reparse point, a regular file
    with one link, owned by you with mode 600); only its size and content are not required
    to be valid, so a corrupt store can be cleared without moving it aside by hand.
    """
    if os.environ.get(DISABLED_ENV) == "1":
        raise _error("Credential access is disabled in this process")
    removed = False
    with _store() as store:
        if store is not None and _stat_file(store, size=False):
            path, options = _path(store, FILENAME)
            try:
                os.unlink(path, **options)
                if store[1] is not None:
                    os.fsync(store[1])
            except FileNotFoundError:
                pass
            except OSError:
                raise _error(
                    "Cannot remove the credential file; check its ownership and permissions"
                ) from None
            removed = True
    return {
        "provider": "typesafe",
        "removed": removed,
        "environment_override_present": _override() is not None,
        "remote_key_revoked": False,
    }


def login(*, stdin=False):
    if os.environ.get(DISABLED_ENV) == "1":
        raise _error("Credential access is disabled in this process")
    if stdin:
        if sys.stdin.isatty():
            raise _error("Use auth login without --stdin for hidden interactive input")
        try:
            value = sys.stdin.read(MAX_KEY_BYTES + 2)
            if len(value) > MAX_KEY_BYTES + 1:
                raise _error("API key input exceeds the size limit")
        except (OSError, UnicodeError):
            raise _error("Cannot read API key from standard input") from None
    else:
        if not sys.stdin.isatty():
            raise _error("Run software-factory auth login in a terminal, or pipe a key with --stdin")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("TypeSafe API key (hidden): ")
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            raise _error("Credential login canceled; no key saved") from None
    return save_typesafe_key(value)


def argument_error(_message):
    # argparse normally echoes invalid argv, which may contain a misplaced key.
    raise _error("Invalid auth arguments; run software-factory auth --help. Keys use hidden input or --stdin")


def add_parser(sub):
    parser = sub.add_parser("auth", help="Save, inspect, or remove your TypeSafe API key")
    parser.error = argument_error
    commands = parser.add_subparsers(dest="auth_command", required=True, metavar="ACTION")
    summaries = {
        "login": "Save a key for this user with hidden input; no provider request",
        "status": "Check local credential availability without revealing the key",
        "logout": "Remove the saved key without revoking it at the provider",
    }
    for name in ("login", "status", "logout"):
        child = commands.add_parser(name, help=summaries[name], description=summaries[name])
        child.error = argument_error
        child.add_argument(
            "provider",
            nargs="?",
            choices=("typesafe",),
            default="typesafe",
            help="Credential provider (only typesafe; default)",
        )
        if name == "login":
            child.add_argument("--stdin", action="store_true", help="Read the key from piped standard input")
            child.set_defaults(handler=lambda a: login(stdin=a.stdin))
        elif name == "status":
            child.set_defaults(handler=lambda a: auth_status())
        else:
            child.set_defaults(handler=lambda a: logout())
