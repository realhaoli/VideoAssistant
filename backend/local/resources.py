"""Process-wide, FIFO resource limits with cancellable waiting."""
from collections import deque
from contextlib import contextmanager
import os
import threading


def setting(name, default, low=1, high=64):
    return max(low, min(high, int(os.getenv(name, str(default)))))


class FairLimiter:
    def __init__(self, capacity):
        self.capacity = capacity
        self.active = 0
        self.waiters = deque()
        self.condition = threading.Condition()

    @contextmanager
    def slot(self, check=lambda: None):
        ticket = object()
        acquired = False
        with self.condition:
            self.waiters.append(ticket)
        try:
            while True:
                check()
                with self.condition:
                    if self.waiters[0] is ticket and self.active < self.capacity:
                        self.waiters.popleft()
                        self.active += 1
                        acquired = True
                        self.condition.notify_all()
                        break
                    self.condition.wait(.2)
            yield
        finally:
            with self.condition:
                if acquired:
                    self.active -= 1
                elif ticket in self.waiters:
                    self.waiters.remove(ticket)
                self.condition.notify_all()


ASR = FairLimiter(1)  # One cached model, including GPU/CPU fallback.
MODELS = FairLimiter(setting("MODEL_CONCURRENCY", 2, high=8))
DOWNLOADS = FairLimiter(setting("DOWNLOAD_CONCURRENCY", 1, high=4))
FFMPEG = FairLimiter(setting("FFMPEG_CONCURRENCY", 1, high=4))
FRAMES = FairLimiter(setting("FRAME_CONCURRENCY", 2, high=4))
PDF = FairLimiter(1)


def ensure_disk(path, additional=0):
    import shutil
    from pathlib import Path
    folder = Path(path)
    while not folder.exists():
        folder = folder.parent
    reserve = setting("MIN_FREE_DISK_MB", 1024, low=0, high=1_048_576) * 1024**2
    if shutil.disk_usage(folder).free < reserve + max(0, additional):
        raise ValueError("可用磁盘空间不足，已保留现有文件；请释放空间后继续任务")
