"""Atomic per-task files and local resource locks."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading

_guard = threading.Lock()
_locks = {}


def open_shared_read(path):
    """Keep downloads readable across atomic replacement, including on Windows."""
    if os.name != "nt":
        return Path(path).open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    # GENERIC_READ; FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE;
    # OPEN_EXISTING; FILE_ATTRIBUTE_NORMAL.
    handle = create(str(Path(path).resolve()), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    # open_osfhandle transfers ownership of the handle to the file descriptor.
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def path_lock(path):
    with _guard:
        return _locks.setdefault(str(Path(path).resolve()), threading.RLock())


def atomic_bytes(path, content, *, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if private:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                else:
                    os.chmod(name, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def atomic_text(path, content, *, private=False):
    atomic_bytes(path, content.encode("utf-8"), private=private)


def atomic_json(path, value, *, private=False):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2), private=private)
