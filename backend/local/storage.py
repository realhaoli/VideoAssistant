"""Atomic per-task files and local resource locks."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

_guard = threading.Lock()
_locks = {}


def _windows_files():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    return kernel


def open_shared_read(path):
    """Keep downloads readable across atomic replacement, including on Windows."""
    if os.name != "nt":
        return Path(path).open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = _windows_files()
    # GENERIC_READ; FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE;
    # OPEN_EXISTING; FILE_ATTRIBUTE_NORMAL.
    handle = kernel.CreateFileW(str(Path(path).resolve()), 0x80000000, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    # open_osfhandle transfers ownership of the handle to the file descriptor.
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def replace_file(source, target):
    try:
        os.replace(source, target)
        return
    except PermissionError:
        if os.name != "nt":
            raise
    # MoveFileEx (used by os.replace) rejects open destinations even when
    # readers share delete access. Windows 10+ supports an atomic POSIX rename
    # through FileRenameInfoEx; existing downloads retain the old file handle.
    import ctypes
    from ctypes import wintypes

    class RenameInfo(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                    ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 1)]

    kernel = _windows_files()
    handle = kernel.CreateFileW(str(Path(source).resolve()), 0x10000, 0x7, None, 3, 0x80, None)  # DELETE access
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        name = str(Path(target).resolve()).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(name))
        info = RenameInfo.from_buffer(buffer)
        info.Flags = 0x1 | 0x2  # REPLACE_IF_EXISTS | POSIX_SEMANTICS
        info.FileNameLength = len(name)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, name, len(name))
        if not kernel.SetFileInformationByHandle(handle, 22, buffer, len(buffer)):  # FileRenameInfoEx
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


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
        replace_file(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def atomic_text(path, content, *, private=False):
    atomic_bytes(path, content.encode("utf-8"), private=private)


def atomic_json(path, value, *, private=False):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2), private=private)


def read_checkpoint(path, valid):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if valid(value) else None
    except (FileNotFoundError, ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None


def fingerprint(*values):
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
