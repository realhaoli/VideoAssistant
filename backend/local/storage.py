"""Atomic per-task files and local resource locks."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading

_guard = threading.Lock()
_locks = {}


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
