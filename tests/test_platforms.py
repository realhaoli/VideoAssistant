"""Portable storage and paths, including a non-UTF-8 system locale."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from local import config, paths, pipeline, media
from local.jobs import JobStore
from local.runtime import ffmpeg_paths, node_path
from local.storage import atomic_json


@pytest.mark.parametrize("title", ["CON", "nul.mp4", "AUX", "PRN.txt", "COM1", "LPT9", "COM¹", "CONIN$", "con .txt"])
def test_windows_device_names_become_real_directories(tmp_path, title):
    jobs = JobStore(tmp_path)
    folder = jobs._new_folder(title)
    (folder / "source.mp4").write_bytes(b"media")
    assert folder.name.startswith("_")
    assert (folder / "source.mp4").read_bytes() == b"media"


def test_relative_data_and_browser_paths_do_not_follow_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MEDIA_DATA_DIR", "workspace data/中文")
    # Track a missing original value too: configure_browser_path writes to env.
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "unused")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    expected = paths.ROOT / "workspace data" / "中文"
    assert paths.data_directory() == expected
    assert paths.configure_browser_path() == str(expected / "browser")
    monkeypatch.setenv("MEDIA_DATA_DIR", str(tmp_path / "custom data"))
    assert paths.data_directory() == tmp_path / "custom data"
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
    assert paths.configure_browser_path() == "0"


def test_chinese_settings_and_restarted_jobs_ignore_system_text_encoding(tmp_path, monkeypatch):
    read_text, write_text = Path.read_text, Path.write_text
    def legacy_read(path, encoding=None, errors=None):
        return read_text(path, encoding=encoding or "cp1252", errors=errors)
    def legacy_write(path, data, encoding=None, errors=None, newline=None):
        return write_text(path, data, encoding=encoding or "cp1252", errors=errors, newline=newline)
    monkeypatch.setattr(Path, "read_text", legacy_read)
    monkeypatch.setattr(Path, "write_text", legacy_write)
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "SETTINGS", tmp_path / "settings.json")
    atomic_json(config.SETTINGS, {"llm_model": "中文模型😀", "llm_api_key": "fixture"})
    assert config.load()["llm_model"] == "中文模型😀"
    config.save({"llm_model": "更新后的模型😀"})
    jobs = JobStore(tmp_path / "任务")
    job = jobs.create(modes=["text"], source={}, options={}, title="发布会😀", enqueue=False)
    jobs.update(job["id"], status="completed")
    restored = JobStore(jobs.root)
    restored.start()
    try:
        assert restored.get(job["id"])["title"] == "发布会😀"
        assert restored.settings(job["id"])["llm_model"] == "更新后的模型😀"
    finally:
        restored.stop()
    cache = tmp_path / "字幕.json"
    atomic_json(cache, {"text": "中文字幕😀"})
    assert pipeline.cached(cache, lambda: pytest.fail("must reuse checkpoint"))["text"] == "中文字幕😀"


def test_subprocess_accepts_paths_with_spaces_and_chinese(tmp_path):
    folder = tmp_path / "中文目录 with spaces"
    folder.mkdir()
    script = folder / "回显.py"
    script.write_text("import sys; print(sys.argv[1])", encoding="utf-8")
    assert media.command([sys.executable, "-X", "utf8", str(script), "中文参数 with spaces"]).strip() == "中文参数 with spaces"


def test_download_and_media_use_python_installed_tools_without_path(tmp_path, monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    monkeypatch.setenv("PATH", str(tmp_path))
    ffmpeg, ffprobe = ffmpeg_paths()
    assert "ffmpeg version" in media.command(["ffmpeg", "-version"])
    assert "ffprobe version" in media.command(["ffprobe", "-version"])
    assert media.command([node_path(), "--version"]).startswith("v24.")
    options = media.download_options(tmp_path)
    assert options["ffmpeg_location"] == ffmpeg
    assert options["js_runtimes"]["node"]["path"] == node_path()
    with YoutubeDL(options) as downloader:
        processor = FFmpegPostProcessor(downloader)
        assert processor.available and processor.probe_available
        assert Path(processor.executable) == Path(ffmpeg)
        assert Path(processor.probe_executable) == Path(ffprobe)


def test_launcher_can_be_called_from_another_working_directory(tmp_path):
    # --help needs no installed model, browser or running application.
    result = subprocess.run([sys.executable, str(paths.ROOT / "start-local.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert "--install-browser" in result.stdout and "--dev" in result.stdout


@pytest.mark.parametrize("development", [False, True], ids=["production", "development"])
def test_real_launcher_serves_from_other_directory_and_cleans_up(tmp_path, development):
    if not (paths.ROOT / "frontend/dist/index.html").is_file():
        pytest.skip("build frontend first")
    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    backend_port, frontend_port = free_port(), free_port()
    working = tmp_path / "其他工作目录 with spaces"
    working.mkdir()
    env = {**os.environ, "MEDIA_DATA_DIR": str(tmp_path / "运行数据"), "PYTHONUTF8": "1", "PATH": str(tmp_path)}
    command = [sys.executable, str(paths.ROOT / "start-local.py"), "--host", "127.0.0.1", "--port", str(backend_port)]
    if development:
        command += ["--dev", "--frontend-port", str(frontend_port)]
    port = frontend_port if development else backend_port
    # Ignore machine-wide HTTP proxy settings for this loopback-only check.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (tmp_path / "launcher.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=working, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with client.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        assert b'healthy' in response.read()
                    break
                except (urllib.error.URLError, TimeoutError):
                    assert process.poll() is None, (tmp_path / "launcher.log").read_text(encoding="utf-8")
                    assert time.monotonic() < deadline, "launcher never became ready"
                    time.sleep(.1)
            with client.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
                assert b"<html" in response.read()
            with client.open(f"http://127.0.0.1:{port}/api/v1/tasks", timeout=5) as response:
                assert response.read() == b"[]"
        finally:
            if os.name == "nt":
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
            process.wait(timeout=30)
    # The launcher's child services must release their listening ports.
    for port in ([backend_port, frontend_port] if development else [backend_port]):
        with socket.socket() as sock:
            sock.settimeout(1)
            assert sock.connect_ex(("127.0.0.1", port)) != 0
