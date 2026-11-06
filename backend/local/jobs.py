import copy
import json
import os
import queue
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from .config import JOBS, load, redact
from .storage import atomic_json, open_shared_read

MODES = {"illustrated", "text", "mindmap", "subtitles", "download"}
ACTIVE = {"uploading", "queued", "running", "cancelling"}


class Cancelled(Exception):
    pass


class ServiceStopping(Exception):
    pass


class ResourceLease:
    def __init__(self, store, job_id, stream):
        self.store, self.job_id, self.stream = store, job_id, stream
        self.closed = False

    def close(self):
        with self.store.lock:
            if self.closed:
                return
            self.closed = True
            self.stream.close()
            self.store.readers[self.job_id] -= 1

    def __enter__(self):
        return self.stream

    def __exit__(self, *args):
        self.close()


class JobStore:
    def __init__(self, root=JOBS, *, workers=None):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.queue = queue.Queue()
        self.jobs, self.paths, self.readers = {}, {}, {}
        self.cancelled = set()
        self.worker = None
        self.workers = []
        self.worker_count = max(1, min(8, int(workers or os.getenv("TASK_WORKERS", "2"))))
        self.stopping = threading.Event()
        self.owner = None

    @staticmethod
    def folder_name(title):
        value = "".join(c for c in str(title) if unicodedata.category(c) not in {"Cc", "Cf"})
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = re.sub(r"\s+", " ", value).strip(". ") or "未命名任务"
        device = value.split(".", 1)[0].rstrip(" ").upper()
        if device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device):
            value = "_" + value
        while len(value.encode("utf-8")) > 160:
            value = value[:-1]
        return value

    def _new_folder(self, title):
        base = self.folder_name(title)
        number = 1
        while True:
            path = self.root / (base if number == 1 else f"{base} ({number})")
            try:
                path.mkdir(parents=True, exist_ok=False)
                return path
            except FileExistsError:
                number += 1

    def _claim_service(self):
        self.owner = (self.root / ".service.lock").open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.owner.seek(0); self.owner.write("0"); self.owner.flush(); self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner.close(); self.owner = None
            raise RuntimeError("该任务目录已由另一个服务管理。请使用一个 Web 进程，通过 TASK_WORKERS 设置后台任务并发。")

    def start(self):
        with self.lock:
            if any(thread.is_alive() for thread in self.workers):
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self._claim_service()
            self.stopping.clear()
            for path in sorted(self.root.glob("*/task.json")):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    job_id = job["id"]
                    folder = path.parent
                    # Migrate old UUID-only directories once, before requests
                    # or workers start. Public IDs and URLs remain unchanged.
                    if folder.name == job_id:
                        target = self._new_folder(job["title"])
                        target.rmdir()
                        folder.rename(target)
                        folder = target
                    self.jobs[job_id], self.paths[job_id] = job, folder
                    self.readers[job_id] = 0
                    job["storage_folder"] = folder.name
                    if job["status"] == "uploading":
                        job.update(status="failed", message="上传未完成，请重新提交文件")
                    elif job["status"] == "cancelling":
                        job.update(status="cancelled", message="任务已取消")
                    elif job["status"] in {"running", "queued"}:
                        job.update(status="queued", message="服务恢复，等待继续处理")
                    self._persist(job)
                except (ValueError, KeyError, OSError):
                    continue
            # Clear stale in-memory queue entries before reconstructing it.
            self.queue = queue.Queue()
            for job in sorted(self.jobs.values(), key=lambda item: item["created_at"]):
                if job["status"] == "queued":
                    self.queue.put(job["id"])
            self.workers = [threading.Thread(target=self._work, name=f"media-worker-{i+1}", daemon=True) for i in range(self.worker_count)]
            self.worker = self.workers[0]
            for thread in self.workers:
                thread.start()

    def stop(self, timeout=15):
        self.stopping.set()
        for _ in self.workers:
            self.queue.put(None)
        deadline = time.monotonic() + timeout
        for thread in self.workers:
            thread.join(max(0, deadline - time.monotonic()))
        if not any(thread.is_alive() for thread in self.workers) and self.owner:
            self.owner.close(); self.owner = None

    def directory(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("任务不存在")
            return self.paths.get(job_id, self.root / job_id)

    def _persist(self, job):
        folder = self.directory(job["id"])
        atomic_json(folder / "task.json", job)

    def create(self, *, modes, source, options, title="新任务", enqueue=True):
        if not modes or not set(modes) <= MODES:
            raise ValueError("请选择有效的输出功能")
        job_id = uuid.uuid4().hex
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            folder = self._new_folder(title)
            job = dict(id=job_id, title=title, modes=list(dict.fromkeys(modes)), source=copy.deepcopy(source),
                       options=copy.deepcopy(options), status="queued" if enqueue else "uploading", progress=0, stage="queued", message="等待处理",
                       created_at=time.time(), updated_at=time.time(), artifacts=[], events=[], warnings=[], storage_folder=folder.name)
            self.jobs[job_id], self.paths[job_id], self.readers[job_id] = job, folder, 0
            # This private snapshot never appears in task API responses.
            atomic_json(folder / "model-settings.json", load(), private=True)
            self._persist(job)
            if enqueue:
                self.queue.put(job_id)
            return self.get(job_id)

    def settings(self, job_id):
        with self.lock:
            path = self.directory(job_id) / "model-settings.json"
            if not path.exists():
                atomic_json(path, load(), private=True)
            return json.loads(path.read_text(encoding="utf-8"))

    def source_title(self, job_id, title):
        with self.lock:
            job = self.jobs[job_id]
            folder = self.directory(job_id)
            # Resolve URL titles before downloading or publishing results. A
            # directory with published resources is never moved under readers.
            if not job.get("source_title_resolved") and not job["artifacts"] and not self.readers.get(job_id):
                if self.folder_name(title) != folder.name:
                    target = self._new_folder(title)
                    target.rmdir(); folder.rename(target)
                    self.paths[job_id] = target
                    job["storage_folder"] = target.name
            self.update(job_id, title=title, source_title_resolved=True)
            return self.directory(job_id)

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("任务不存在")
            return copy.deepcopy(self.jobs[job_id])

    def list(self):
        with self.lock:
            return [copy.deepcopy(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)]

    def update(self, job_id, **fields):
        with self.lock:
            job = self.jobs[job_id]
            if "title" in fields and job.get("custom_title") and "custom_title" not in fields:
                fields["title"] = job["custom_title"]
            job.update(fields, updated_at=time.time())
            self._persist(job)

    def rename(self, job_id, title):
        title = title.strip()
        if not title or len(title) > 200 or re.search(r"[\x00-\x1f\x7f]", title):
            raise ValueError("任务名称须为 1–200 个字符，且不能包含换行或控制字符")
        with self.lock:
            self.update(job_id, title=title, custom_title=title)
            return self.get(job_id)

    def progress(self, job_id, stage, percent, message):
        self.check(job_id)
        with self.lock:
            job = self.jobs[job_id]
            job.update(stage=stage, progress=max(job["progress"], min(percent, 99)), message=message, updated_at=time.time())
            event = dict(time=time.time(), stage=stage, message=message)
            previous = job["events"][-1] if job["events"] else None
            if previous and previous["stage"] == stage and re.sub(r"\d+%", "%", previous["message"]) == re.sub(r"\d+%", "%", message):
                job["events"][-1] = event
            elif not previous or previous["message"] != message:
                job["events"] = (job["events"] + [event])[-200:]
            self._persist(job)

    def warn(self, job_id, message):
        with self.lock:
            warnings = self.jobs[job_id]["warnings"]
            message = redact(message)
            if message not in warnings:
                warnings.append(message)
                self._persist(self.jobs[job_id])

    def check(self, job_id):
        if job_id in self.cancelled:
            raise Cancelled("任务已取消")
        if self.stopping.is_set():
            raise ServiceStopping("服务停止，检查点已保留")

    def cancel(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] == "cancelling":
                return job
            if job["status"] in ACTIVE:
                self.cancelled.add(job_id)
                self.update(job_id, status="cancelling" if job["status"] in {"running", "uploading"} else "cancelled", message="正在取消")
            return self.get(job_id)

    def retry(self, job_id):
        with self.lock:
            if self.get(job_id)["status"] in ACTIVE:
                raise ValueError("任务仍在处理中")
            self.cancelled.discard(job_id)
            self.update(job_id, status="queued", stage="queued", progress=0, message="等待继续处理", error=None)
            self.queue.put(job_id)
            return self.get(job_id)

    def open_resource(self, job_id, relative):
        with self.lock:
            folder = (self.directory(job_id) / "output").resolve()
            path = (folder / relative).resolve()
            if not path.is_relative_to(folder):
                raise FileNotFoundError(relative)
            stream = open_shared_read(path)
            self.readers[job_id] = self.readers.get(job_id, 0) + 1
            return ResourceLease(self, job_id, stream)

    def remove(self, job_id):
        with self.lock:
            if self.get(job_id)["status"] in ACTIVE:
                raise ValueError("请先取消任务，等待取消完成后再删除")
            if self.readers.get(job_id, 0):
                raise ValueError("该任务的资源正在访问中，请下载结束后再删除")
            shutil.rmtree(self.directory(job_id))
            del self.jobs[job_id]
            self.paths.pop(job_id, None); self.readers.pop(job_id, None)

    def artifact(self, job_id, filename, label, kind="file"):
        with self.lock:
            path = self.directory(job_id) / "output" / filename
            if not path.is_file():
                raise FileNotFoundError(filename)
            entry = dict(name=filename, label=label, kind=kind, size=path.stat().st_size)
            job = self.jobs[job_id]
            job["artifacts"] = [a for a in job["artifacts"] if a["name"] != filename] + [entry]
            self._persist(job)

    def _work(self):
        from .pipeline import run
        while True:
            job_id = self.queue.get()
            try:
                if job_id is None:
                    return
                # Claim atomically so duplicate queue entries cannot run a job
                # twice under multiple workers.
                with self.lock:
                    if job_id not in self.jobs or self.jobs[job_id]["status"] != "queued":
                        continue
                    self.check(job_id)
                    self.update(job_id, status="running", message="开始处理")
                run(self, job_id)
                self.check(job_id)
                self.update(job_id, status="completed", progress=100, stage="completed", message="处理完成，可以下载")
            except ServiceStopping:
                if job_id in self.jobs:
                    self.update(job_id, status="queued", message="服务暂停，等待恢复处理")
                return
            except Cancelled:
                self.update(job_id, status="cancelled", message="任务已取消")
            except Exception as exc:
                self.update(job_id, status="failed", message="处理失败，可检查原因后重试", error=redact(f"{type(exc).__name__}: {exc}")[:1800])
            finally:
                self.queue.task_done()


store = JobStore()
