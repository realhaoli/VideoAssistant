"""Locate native executables installed with the project's Python dependencies."""
from functools import cache
from importlib.util import find_spec
import os
from pathlib import Path


@cache
def ffmpeg_paths():
    try:
        # Require the platform wheel so first use can extract locally.
        if find_spec("portable_ffmpeg_full") is None:
            raise ModuleNotFoundError("portable-ffmpeg-full")
        from portable_ffmpeg import get_ffmpeg
        ffmpeg, ffprobe = get_ffmpeg()
        if not all(path.is_file() for path in (ffmpeg, ffprobe)):
            raise FileNotFoundError("FFmpeg or FFprobe")
        return str(ffmpeg), str(ffprobe)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("无法加载 FFmpeg / FFprobe，请在项目虚拟环境重新安装 backend/requirements.txt") from exc


@cache
def node_path():
    spec = find_spec("nodejs_wheel")
    if spec is not None and spec.origin:
        package = Path(spec.origin).parent
        executable = package / ("node.exe" if os.name == "nt" else "bin/node")
        if executable.is_file():
            return str(executable)
    raise RuntimeError("无法加载 Node.js，请在项目虚拟环境重新安装 backend/requirements.txt")
