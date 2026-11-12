"""Isolate CUDA calls so a hung native runtime cannot hold the ASR queue."""
from contextlib import suppress
from functools import lru_cache
import multiprocessing
import subprocess
import sys
import time

from .resources import setting


@lru_cache(maxsize=1)
def probe_cuda(_interval):
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", "import ctranslate2; raise SystemExit(ctranslate2.get_cuda_device_count() == 0)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return process.wait(timeout=8) == 0
    except subprocess.TimeoutExpired:
        with suppress(OSError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        return False


class CudaWhisperTimeout(TimeoutError):
    pass


def _serve(pipe):
    from . import media

    with pipe:
        try:
            while True:
                audio, model_name = pipe.recv()
                try:
                    cues, language = media._recognize_whisper(
                        audio, model_name, "cuda", lambda: None,
                        lambda percent: pipe.send(("progress", percent)),
                        lambda: pipe.send(("ready", None)),
                    )
                except Exception as exc:
                    pipe.send(("error", str(exc)[:1200]))
                    return
                pipe.send(("result", (cues, language, media.WHISPER_RUNTIME)))
        except (EOFError, BrokenPipeError):
            return


class CudaWhisper:
    """One reusable GPU worker, called under the server's existing ASR slot."""

    def __init__(self):
        self.process = None
        self.pipe = None
        self.retry_after = 0

    def _start(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_serve, args=(child,), name="whisper-cuda", daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.process, self.pipe = process, parent

    def close(self):
        if self.process is None:
            return
        process = self.process
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None
        if process.is_alive():
            with suppress(ProcessLookupError):
                process.kill()
        process.join(timeout=3)
        if not process.is_alive():
            process.close()
            self.process = None

    def recognize(self, audio, model_name, check, progress):
        load_timeout = setting("WHISPER_GPU_LOAD_TIMEOUT_SECONDS", 1800, high=7200)
        stall_timeout = setting("WHISPER_GPU_STALL_TIMEOUT_SECONDS", 180, high=3600)
        deadline = time.monotonic() + load_timeout
        loading = True
        last_progress = -1
        try:
            check()
            if self.pipe is None or not self.process.is_alive():
                self.close()
                if self.process is not None:
                    raise RuntimeError("上一次 GPU 转写进程尚未退出")
                self._start()
            self.pipe.send((str(audio), model_name))
            while True:
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.retry_after = time.monotonic() + 300
                    phase = "加载" if loading else "转写"
                    timeout = load_timeout if loading else stall_timeout
                    raise CudaWhisperTimeout(f"GPU {phase}无进展超时（{timeout} 秒）")
                if not self.pipe.poll(min(.2, remaining)):
                    continue
                event, value = self.pipe.recv()
                if event == "ready":
                    loading = False
                    deadline = time.monotonic() + stall_timeout
                elif event == "progress" and value > last_progress:
                    last_progress = value
                    deadline = time.monotonic() + stall_timeout
                    progress(value)
                elif event == "result":
                    return value
                elif event == "error":
                    raise RuntimeError(value)
        except BaseException:
            with suppress(OSError):
                self.close()
            raise
