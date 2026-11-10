import copy
from concurrent.futures import ThreadPoolExecutor
import errno
from itertools import count
import json
import logging
import math
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
from .resources import DOWNLOADS, FFMPEG, MODELS, DiskSpaceError

MODES = {"illustrated", "text", "mindmap", "subtitles", "download"}
ACTIVE = {"uploading", "queued", "running", "cancelling"}
LOGGER = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


class ServiceStopping(Exception):
    pass


class OutputFailure(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__('；'.join(f'{mode}: {message}' for mode,message in errors.items()))


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
        self.history_recovered = 0
        self.history_unreadable = 0
        self._pending_writes = set()
        self._stage_pools = {}
        self._active_stages = {}
        self._stage_count = 0
        self._stages_idle = threading.Condition(self.lock)

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

    def _folder_candidates(self, title):
        base = self.folder_name(title)
        for number in count(1):
            yield self.root / (base if number == 1 else f"{base} ({number})")

    def _new_folder(self, title):
        for path in self._folder_candidates(title):
            try:
                path.mkdir(parents=True, exist_ok=False)
                return path
            except FileExistsError:
                continue

    def _rename_folder_if_possible(self, folder, title):
        """Try a display-friendly directory name while holding the store lock."""
        try:
            for target in self._folder_candidates(title):
                if target == folder:
                    return folder
                try:
                    target.lstat()
                except FileNotFoundError:
                    # Choose an unused name without creating an empty task
                    # directory that would be left behind if the move fails.
                    folder.rename(target)
                    return target
        except OSError as exc:
            if not folder.is_dir():
                raise
            LOGGER.warning("任务目录改名失败，继续使用原目录 %s：%s", folder, exc)
            return folder

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
            self.jobs, self.paths, self.readers = {}, {}, {}
            self._pending_writes.clear()
            self._active_stages.clear()
            self.history_recovered = self.history_unreadable = 0
            for directory in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.')):
                job = None
                for filename in ("task.json", "task.backup.json"):
                    try:
                        job = self._read_record(directory / filename)
                        if filename == "task.backup.json":
                            self.history_recovered += 1
                            LOGGER.warning("已从备份恢复任务记录：%s", directory)
                        break
                    except (ValueError, KeyError, TypeError, OSError) as exc:
                        LOGGER.warning("无法读取任务记录 %s：%s", directory / filename, type(exc).__name__)
                if job is None:
                    self.history_unreadable += 1
                    continue
                try:
                    job_id = job["id"]
                    folder = directory
                    # Migrate old UUID-only directories once, before requests
                    # or workers start. Public IDs and URLs remain unchanged.
                    if folder.name == job_id:
                        folder = self._rename_folder_if_possible(folder, job["title"])
                    self.jobs[job_id], self.paths[job_id] = job, folder
                    self.readers[job_id] = 0
                    job["storage_folder"] = folder.name
                    if job["status"] == "uploading":
                        job.update(status="failed", stage_progress=None, message="上传未完成，请重新提交文件")
                        if job["source"].get("type") == "upload":
                            job["upload_complete"] = False
                    elif job["status"] == "cancelling":
                        job.update(status="cancelled", stage_progress=None, message="任务已取消")
                        if job["source"].get("type") == "upload" and job.get("stage") == "queued":
                            job.setdefault("upload_complete", False)
                    elif job["status"] in {"running", "queued"}:
                        job.update(status="queued", stage="queued", stage_progress=None, stage_started_at=time.time(),
                                   message="服务恢复，等待继续处理")
                    self._persist(job)
                except OSError:
                    LOGGER.exception("任务已读取，但无法保存恢复状态：%s", directory)
            # Clear stale in-memory queue entries before reconstructing it.
            self.queue = queue.Queue()
            for job in sorted(self.jobs.values(), key=lambda item: item["created_at"]):
                if job["status"] == "queued":
                    self.queue.put(job["id"])
            capacities = {'inspect': self.worker_count, 'download': DOWNLOADS.capacity, 'media': FFMPEG.capacity, 'asr': 1,
                          'model': MODELS.capacity, 'outputs': self.worker_count}
            self._stage_pools = {name: ThreadPoolExecutor(count, thread_name_prefix=f'media-{name}')
                                 for name, count in capacities.items()}
            self.workers = [threading.Thread(target=self._work, name=f"media-worker-{i+1}", daemon=True) for i in range(self.worker_count)]
            self.worker = self.workers[0]
            for thread in self.workers:
                thread.start()

    @staticmethod
    def _read_record(path):
        job = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(job, dict):
            raise ValueError("任务记录不是对象")
        for key in ("id", "title", "status"):
            if not isinstance(job.get(key), str) or not job[key]:
                raise ValueError("任务记录缺少有效字段：" + key)
        if not isinstance(job.get("created_at"), (int, float)) or not math.isfinite(job["created_at"]):
            raise ValueError("任务记录缺少有效创建时间")
        for key, expected in (("modes", list), ("source", dict), ("options", dict)):
            if not isinstance(job.get(key), expected):
                raise ValueError("任务记录字段无效：" + key)
        for key in ("artifacts", "events", "warnings"):
            if not isinstance(job.get(key, []), list):
                raise ValueError("任务记录字段无效：" + key)
            job.setdefault(key, [])
        progress = job.get("progress", 100 if job["status"] == "completed" else 0)
        job["progress"] = max(0, min(100, progress)) if isinstance(progress, (int, float)) and math.isfinite(progress) else 0
        stage_progress = job.get("stage_progress")
        job["stage_progress"] = max(0, min(100, stage_progress)) if isinstance(stage_progress, (int, float)) and math.isfinite(stage_progress) else None
        job.setdefault("message", "已恢复历史任务")
        job.setdefault("updated_at", job["created_at"])
        return job

    def history_status(self):
        with self.lock:
            return {"task_count": len(self.jobs), "recovered_tasks": self.history_recovered,
                    "unreadable_tasks": self.history_unreadable}

    def update_output(self, job_id, mode, **fields):
        with self.lock:
            job = self.jobs[job_id]
            state = job.setdefault('output_states', {}).setdefault(mode, {})
            if fields.get('status') == 'running' and (state.get('status') != 'running' or fields.get('stage') != state.get('stage')):
                fields['stage_started_at'] = time.time()
            state.update(fields)
            self._persist(job)

    def stop(self, timeout=15):
        with self.lock:
            self.stopping.set()
        for pool in self._stage_pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        for _ in self.workers:
            self.queue.put(None)
        deadline = time.monotonic() + timeout
        for thread in self.workers:
            thread.join(max(0, deadline - time.monotonic()))
        with self._stages_idle:
            self._stages_idle.wait_for(lambda: self._stage_count == 0, max(0, deadline - time.monotonic()))
            if not self._stage_count and not any(thread.is_alive() for thread in self.workers) and self.owner:
                self.owner.close(); self.owner = None

    def directory(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("任务不存在")
            return self.paths.get(job_id, self.root / job_id)

    def _persist(self, job):
        folder = self.directory(job["id"])
        atomic_json(folder / "task.json", job)
        self._pending_writes.discard(job["id"])
        try:
            atomic_json(folder / "task.backup.json", job)
        except OSError:
            LOGGER.exception("任务记录已保存，但无法更新备份：%s", folder)

    def create(self, *, modes, source, options, title="新任务", enqueue=True, upload_pending=False):
        if not modes or not set(modes) <= MODES:
            raise ValueError("请选择有效的输出功能")
        job_id = uuid.uuid4().hex
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            folder = self._new_folder(title)
            job = dict(id=job_id, title=title, modes=list(dict.fromkeys(modes)), source=copy.deepcopy(source),
                       options=copy.deepcopy(options), status="queued" if enqueue else "uploading", progress=0, stage="queued", stage_progress=None, message="等待处理",
                       created_at=time.time(), updated_at=time.time(), artifacts=[], events=[], warnings=[], storage_folder=folder.name)
            job['output_states'] = {mode:{'status':'pending','progress':0,'message':'等待处理'} for mode in job['modes']}
            if upload_pending:
                job["upload_complete"] = False
            self.jobs[job_id], self.paths[job_id], self.readers[job_id] = job, folder, 0
            # This private snapshot never appears in task API responses.
            try:
                atomic_json(folder / "model-settings.json", load(), private=True)
                self._persist(job)
            except OSError:
                self.jobs.pop(job_id); self.paths.pop(job_id); self.readers.pop(job_id)
                shutil.rmtree(folder, ignore_errors=True)
                raise
            if enqueue:
                self.queue.put(job_id)
            return self.get(job_id)

    def settings(self, job_id):
        with self.lock:
            revision = self.jobs[job_id].get('model_settings_revision')
            if revision is not None:
                if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{32}', revision):
                    raise ValueError('任务模型配置版本无效')
                path = self.directory(job_id) / f'model-settings-{revision}.json'
                return json.loads(path.read_text(encoding='utf-8'))
            path = self.directory(job_id) / 'model-settings.json'
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
                folder = self._rename_folder_if_possible(folder, title)
                self.paths[job_id] = folder
            self.update(job_id, title=title, source_title_resolved=True, storage_folder=folder.name)
            return folder

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError("任务不存在")
            return copy.deepcopy(self.jobs[job_id])

    def list(self):
        with self.lock:
            return [copy.deepcopy(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)]

    def summary_page(self, offset, limit):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda job: (job['created_at'], job['id']), reverse=True)
            count = len(jobs)
            offset = min(offset, (max(0, count - 1) // limit) * limit)

            def summary(job):
                return {key: copy.deepcopy(job[key]) for key in
                        ('id', 'title', 'modes', 'status', 'progress', 'created_at', 'updated_at') if key in job}

            return self.history_status() | {
                'offset': offset,
                'items': [summary(job) for job in jobs[offset:offset + limit]],
                'recent': [summary(job) for job in jobs[:6]],
                'active_count': sum(job['status'] in ACTIVE for job in jobs),
                'completed_count': sum(job['status'] == 'completed' for job in jobs),
            }

    def update(self, job_id, **fields):
        with self.lock:
            job = self.jobs[job_id]
            if ("message" in fields or "stage" in fields) and "stage_progress" not in fields:
                fields["stage_progress"] = None
            if "title" in fields and job.get("custom_title") and "custom_title" not in fields:
                fields["title"] = job["custom_title"]
            now = time.time()
            if any(key in fields and fields[key] != job.get(key) for key in ('stage', 'status')):
                fields['stage_started_at'] = now
            job.update(fields, updated_at=now)
            self._persist(job)

    def rename(self, job_id, title):
        title = title.strip()
        if not title or len(title) > 200 or re.search(r"[\x00-\x1f\x7f]", title):
            raise ValueError("任务名称须为 1–200 个字符，且不能包含换行或控制字符")
        with self.lock:
            self.update(job_id, title=title, custom_title=title)
            return self.get(job_id)

    def progress(self, job_id, stage, percent, message, *, stage_progress=None):
        self.check(job_id)
        with self.lock:
            job = self.jobs[job_id]
            stage_progress = max(0, min(100, stage_progress)) if stage_progress is not None else None
            total = max(job["progress"], min(percent, 99))
            if job.get("stage") == stage and job["progress"] == total and job.get("stage_progress") == stage_progress and job.get("message") == message:
                return
            now = time.time()
            if job.get('stage') != stage or not job.get('stage_started_at'):
                job['stage_started_at'] = now
            job.update(stage=stage, progress=total, stage_progress=stage_progress, message=message, updated_at=now)
            event = dict(time=now, stage=stage, message=message, stage_progress=stage_progress)
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
                waiting = self._active_stages.get(job_id)
                if waiting is not None:
                    waiting.cancel()
            return self.get(job_id)

    def retry(self, job_id, *, use_current_model=False):
        with self.lock:
            previous = self.get(job_id)
            if previous["status"] in ACTIVE:
                raise ValueError("任务仍在处理中")
            if self.needs_upload(previous):
                raise ValueError("上传未完成，请重新上传素材")
            fields = {}
            snapshot = None
            if use_current_model:
                settings = self.settings(job_id)
                settings.update({key: value for key, value in load().items()
                                 if key.startswith(('llm_', 'vlm_')) or key == 'use_vlm'})
                revision = uuid.uuid4().hex
                snapshot = self.directory(job_id) / f'model-settings-{revision}.json'
                atomic_json(snapshot, settings, private=True)
                fields['model_settings_revision'] = revision
            self.cancelled.discard(job_id)
            for value in self.jobs[job_id].get('output_states',{}).values():
                if value.get('status') != 'completed':
                    value.update(status='pending', progress=0, message='等待继续处理', error=None)
            try:
                self.update(job_id, status="queued", stage="queued", progress=0, message="等待继续处理", error=None, **fields)
            except OSError:
                self.jobs[job_id] = previous
                if snapshot is not None:
                    snapshot.unlink(missing_ok=True)
                raise
            self.queue.put(job_id)
            return self.get(job_id)

    @staticmethod
    def needs_upload(job):
        if job.get("upload_complete") is not None:
            return not job["upload_complete"]
        return job["source"].get("type") == "upload" and job.get("message", "").startswith(
            ("上传未完成", "上传失败", "上传已取消"))

    def record_state(self, job_id, **fields):
        with self.lock:
            try:
                self.update(job_id, **fields)
            except OSError:
                self._pending_writes.add(job_id)
                LOGGER.exception("任务状态暂存在内存，等待磁盘恢复：%s", job_id)

    def _flush_pending(self):
        with self.lock:
            for job_id in tuple(self._pending_writes):
                if job_id not in self.jobs:
                    self._pending_writes.discard(job_id)
                    continue
                try:
                    self._persist(self.jobs[job_id])
                except OSError:
                    break

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
        from .pipeline import steps
        while True:
            try:
                item = self.queue.get(timeout=1)
            except queue.Empty:
                self._flush_pending()
                continue
            if item is None:
                self.queue.task_done()
                return
            handed_off = False
            iterator = None
            job_id = item if isinstance(item, str) else item[0]
            try:
                self._flush_pending()
                if isinstance(item, str):
                    future = None
                    with self.lock:
                        if self.stopping.is_set() or self.jobs.get(job_id, {}).get('status') != 'queued':
                            continue
                        self.check(job_id)
                        self.update(job_id, status='running', message='开始处理')
                    iterator = iter(steps(self, job_id))
                else:
                    job_id, iterator, future = item
                self.check(job_id)
                if future is None:
                    step = next(iterator)
                else:
                    try:
                        result = future.result()
                    except Exception as exc:
                        step = iterator.throw(exc)
                    else:
                        step = iterator.send(result)
                with self.lock:
                    self.check(job_id)
                    self._stage_count += 1
                    try:
                        running = self._stage_pools[step.queue].submit(self._run_step, job_id, step.run)
                    except BaseException:
                        self._stage_count -= 1
                        raise
                    self._active_stages[job_id] = running
                    running.add_done_callback(lambda done, task=job_id, continuation=iterator:
                                              self._stage_done(task, continuation, done))
                    handed_off = True
            except StopIteration:
                self._finish(job_id)
            except Exception as exc:
                if iterator is not None:
                    iterator.close()
                self._finish(job_id, exc)
            finally:
                if not handed_off:
                    self.queue.task_done()

    def _run_step(self, job_id, run):
        self.check(job_id)
        return run()

    def _stage_done(self, job_id, iterator, future):
        with self._stages_idle:
            try:
                self._active_stages.pop(job_id, None)
                if self.stopping.is_set():
                    iterator.close()
                    self._finish(job_id, ServiceStopping())
                else:
                    self.queue.put((job_id, iterator, future))
            finally:
                self._stage_count -= 1
                self._stages_idle.notify_all()
                self.queue.task_done()

    def _finish(self, job_id, exc=None):
        with self.lock:
            if exc is None:
                try:
                    self.check(job_id)
                except (Cancelled, ServiceStopping) as error:
                    exc = error
            if isinstance(exc, Cancelled) or job_id in self.cancelled:
                self._finish_open_outputs(job_id, 'cancelled')
                self.record_state(job_id, status='cancelled', message='任务已取消')
            elif isinstance(exc, ServiceStopping):
                self.record_state(job_id, status='queued', message='服务暂停，等待恢复处理')
            elif exc is None:
                self.record_state(job_id, status='completed', progress=100, stage='completed', stage_progress=100,
                                  message='处理完成，可以下载')
            else:
                self._finish_open_outputs(job_id, 'failed')
                completed = any(value.get('status') == 'completed' for value in self.jobs[job_id].get('output_states', {}).values())
                disk_full = isinstance(exc, DiskSpaceError) or getattr(exc, 'errno', None) == errno.ENOSPC
                message = ('磁盘空间不足，请释放空间后继续处理' if disk_full else
                           '部分产物失败，已完成文件可下载，可继续处理失败项' if completed else '处理失败，可检查原因后重试')
                try:
                    error = redact(f'{type(exc).__name__}: {exc}')[:1800]
                except (OSError, ValueError):
                    error = type(exc).__name__
                self.record_state(job_id, status='partial' if completed else 'failed', message=message, error=error)

    def _finish_open_outputs(self, job_id, status):
        with self.lock:
            for value in self.jobs[job_id].get('output_states',{}).values():
                if value.get('status') not in {'completed','failed'}:
                    value.update(status=status, message='已取消' if status == 'cancelled' else '处理失败')


store = JobStore()
