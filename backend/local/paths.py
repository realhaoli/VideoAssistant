"""Shared paths, independent of the shell's current working directory."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def data_directory():
    return (ROOT / Path(os.getenv("MEDIA_DATA_DIR", "data")).expanduser()).resolve()


def configure_browser_path():
    return os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(data_directory() / "browser"))
