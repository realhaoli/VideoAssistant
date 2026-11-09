"""Process-wide, FIFO resource limits with cancellable waiting."""
from collections import deque
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
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


class DiskSpaceError(ValueError):
    pass


_disk_lock = threading.Lock()
_reserved = {}


def _disk_location(path):
    folder = Path(path).resolve()
    while not folder.exists():
        if folder == folder.parent:
            raise FileNotFoundError(folder)
        folder = folder.parent
    return folder, folder.stat().st_dev


def _check_disk(folder, required):
    reserve = setting("MIN_FREE_DISK_MB", 1024, low=0, high=1_048_576) * 1024**2
    if shutil.disk_usage(folder).free < reserve + required:
        raise DiskSpaceError("可用磁盘空间不足，请释放空间后重试")


def ensure_disk(path, additional=0):
    folder, device = _disk_location(path)
    with _disk_lock:
        _check_disk(folder, _reserved.get(device, 0) + max(0, additional))


class DiskReservation:
    def __init__(self, path):
        self.folder, self.device = _disk_location(path)
        self.remaining = 0

    def resize(self, remaining):
        """Reserve future writes. Bytes already written count in actual disk usage."""
        remaining = max(0, remaining)
        with _disk_lock:
            total = _reserved.get(self.device, 0) + remaining - self.remaining
            if remaining > self.remaining:
                _check_disk(self.folder, total)
            if total:
                _reserved[self.device] = total
            else:
                _reserved.pop(self.device, None)
            self.remaining = remaining

    def consume(self, written):
        self.resize(self.remaining - written)


@contextmanager
def reserve_disk(path, additional=0):
    reservation = DiskReservation(path)
    reservation.resize(additional)
    try:
        yield reservation
    finally:
        reservation.resize(0)
