import json
import errno
import hashlib
import os
import re
import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore
from typing import List, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Response, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from yt_dlp.utils import DownloadError, UnsupportedError

from local import config
from local.jobs import ACTIVE, Cancelled, TaskCapacityError, store
from local.media import MEDIA_EXTS, preview_link
from local.storage import atomic_bytes, path_lock, replace_file
from local.uploads import PDF_UPLOAD_LIMIT, UPLOAD_FILES, upload_form
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as MultipartFile
from urllib.parse import quote
import mimetypes

router = APIRouter(prefix="/api/v1", tags=["Local tasks"])


secured = APIRouter()
link_checks = BoundedSemaphore(2)


class Options(BaseModel):
    clean_subtitles: bool = True
    scenario: str = "auto"
    instructions: str = Field("", max_length=2500)


class LinkCheck(BaseModel):
    url: str = Field(..., max_length=4000)


class LinkTask(LinkCheck):
    modes: List[str]
    options: Options = Field(default_factory=Options)
    resource_id: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")


def get_job(job_id):
    try:
        return store.get(job_id)
    except KeyError:
        raise HTTPException(404, "任务不存在")


def job_directory(job_id):
    try:
        return store.directory(job_id)
    except KeyError:
        raise HTTPException(404, "任务不存在")


def resource_lease(job_id, filename):
    try:
        return store.open_resource(job_id, filename)
    except (FileNotFoundError, IsADirectoryError, KeyError):
        raise HTTPException(404, "文件不存在")


@secured.get("/settings")
def settings(whisper_model: str | None = None):
    from local.media import whisper_status
    from local.runtime import ffmpeg_paths
    values = config.public()
    model = whisper_model or values["whisper_model"]
    if model not in config.WHISPER_MODELS:
        raise HTTPException(400, "不支持的 Whisper 模型")
    try:
        ffmpeg_paths()
        ffmpeg_available = True
    except RuntimeError:
        ffmpeg_available = False
    return values | {"ffmpeg_available": ffmpeg_available, **whisper_status(model)}


class ModelSettings(BaseModel):
    llm_api_format: str = "openai"
    llm_base_url: str = Field("", max_length=500)
    llm_model: str = Field("", max_length=200)
    llm_api_key: str = Field("", max_length=1000)
    vlm_base_url: str = Field("", max_length=500)
    vlm_api_format: str = ""
    vlm_model: str = Field("", max_length=200)
    vlm_api_key: str = Field("", max_length=1000)
    use_vlm: bool = True
    use_subtitle_model: bool = False
    subtitle_api_format: str = "openai"
    subtitle_base_url: str = Field("", max_length=500)
    subtitle_model: str = Field("", max_length=200)
    subtitle_api_key: str = Field("", max_length=1000)
    subtitle_context_tokens: int = Field(32768, ge=2048, le=config.MAX_MODEL_TOKENS)
    subtitle_max_output_tokens: int = Field(8192, ge=256, le=config.MAX_MODEL_TOKENS)
    asr_cleanup_mode: str = "local"
    whisper_model: str = "small"
    whisper_device: str = "auto"
    llm_context_tokens: int = Field(32768, ge=2048, le=config.MAX_MODEL_TOKENS)
    vlm_context_tokens: int = Field(32768, ge=2048, le=config.MAX_MODEL_TOKENS)
    llm_max_output_tokens: int = Field(8192, ge=256, le=config.MAX_MODEL_TOKENS)
    vlm_max_output_tokens: int = Field(8192, ge=256, le=config.MAX_MODEL_TOKENS)


ModelRole = Literal["llm", "subtitle", "vlm"]


@secured.put("/settings")
def settings_save(body: ModelSettings, detect_for: Literal["llm", "subtitle", "vlm", "all"] | None = None):
    values = body.model_dump(exclude_unset=True)
    try:
        if detect_for is None:
            return config.save(values)
        previous = config.load()
        candidate = config.merge_settings(previous, values)
        if detect_for == "all":
            roles = [role for role in MODEL_LABELS
                     if _model_enabled(candidate, role) and candidate[f"{role}_model"]]
            for role in roles:
                _require_key_for_changed_endpoint(previous, candidate, values, role)
            with ThreadPoolExecutor(max_workers=len(MODEL_LABELS)) as pool:
                detected = dict(zip(roles, pool.map(lambda role: _discover_capacity(candidate, role), roles)))
            for capacity in detected.values():
                values.update(capacity)
            return _save_checked_settings(previous, values) | {"detected_capacities": detected}
        check = None
        role = "llm" if detect_for == "subtitle" and not candidate["use_subtitle_model"] else detect_for
        if _model_enabled(candidate, role) and candidate[f"{role}_model"]:
            _require_key_for_changed_endpoint(previous, candidate, values, role)
            try:
                check = _check_model(candidate, role)
                values.update(check["detected"])
            except HTTPException as exc:
                if exc.status_code != 502:
                    raise
                check = {"ok": False, "message": exc.detail, "role": role}
        return _save_checked_settings(previous, values) | {"model_check": check}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ModelTestRequest(BaseModel):
    role: ModelRole
    draft: ModelSettings | None = None


MODEL_LABELS = {"llm": "大语言模型", "subtitle": "字幕模型", "vlm": "多模态模型"}
MODEL_SWITCHES = {"subtitle": "use_subtitle_model", "vlm": "use_vlm"}


def _save_checked_settings(previous, values):
    with config.LOCK:
        if config.load() != previous:
            raise HTTPException(409, "模型设置已更新，请重新打开设置后保存")
        return config.save(values)


def _model_enabled(settings, role):
    return role == "llm" or settings[MODEL_SWITCHES[role]]


def _model_values(settings, role):
    return config.subtitle_model_settings(settings) if role == "subtitle" else settings


def _require_key_for_changed_endpoint(previous, candidate, values, role):
    from local.model_client import base_url, connection

    if values.get(f"{role}_api_key"):
        return
    if role == "subtitle":
        previous = {**previous, "use_subtitle_model": True}
    saved = connection(_model_values(previous, role), role == "vlm")
    tested = connection(_model_values(candidate, role), role == "vlm")
    saved_endpoint = (saved["format"], base_url(saved["url"], saved["format"]))
    tested_endpoint = (tested["format"], base_url(tested["url"], tested["format"]))
    if saved["key"] and tested["key"] and saved_endpoint != tested_endpoint:
        raise HTTPException(400, "更改接口地址或格式后，请填写该接口的 API Key")


def _discover_capacity(settings, role):
    from local.model_client import discover_model_limits

    limits = discover_model_limits(_model_values(settings, role), visual=role == "vlm")
    return {f"{role}_{field}": value for field, value in limits.items()}


def _check_model(settings, role):
    if not _model_enabled(settings, role):
        raise HTTPException(400, f"请先启用{MODEL_LABELS[role]}")
    detected = _discover_capacity(settings, role)
    _test_model_connection({**settings, **detected}, role)
    capacity = {key: detected.get(key, settings[key])
                for key in (f"{role}_context_tokens", f"{role}_max_output_tokens")}
    return {"ok": True, "message": f"{MODEL_LABELS[role]}连接成功", "role": role,
            "detected": detected, "capacity": capacity}


def _test_model_connection(settings, role):
    from local.intelligence import Intelligence
    from local.model_client import connection

    values = _model_values(settings, role)
    engine = Intelligence(values, lambda: None)
    try:
        if role == "vlm":
            from tempfile import TemporaryDirectory
            from PIL import Image

            with TemporaryDirectory(prefix="videoassistant-model-test-") as directory:
                image = Path(directory) / "sample.png"
                Image.new("RGB", (224, 224), "#409eff").save(image)
                engine.chat("请用一句话描述图片的颜色。", visual=[image], max_tokens=512, retry_tokens=8192)
        else:
            engine.chat("请只回复：连接成功", max_tokens=512, retry_tokens=8192)
    except Exception as exc:
        message = config.redact(str(exc))
        key = connection(values, role == "vlm")["key"]
        if key:
            message = message.replace(key, "[已隐藏]")
        raise HTTPException(502, f"{MODEL_LABELS[role]}测试失败，{message[:560]}") from exc


@secured.post("/settings/test")
def settings_test(body: ModelTestRequest | None = None):
    from local.model_client import connection

    settings = config.load()
    if body is None:
        roles = []
        if not settings["use_subtitle_model"] or settings["llm_model"]:
            roles.append("llm")
        if settings["use_subtitle_model"]:
            roles.append("subtitle")
        for role in roles:
            _test_model_connection(settings, role)
        return {"ok": True, "message": "、".join(MODEL_LABELS[role] for role in roles) + "连接成功"}

    role = body.role
    if body.draft is not None:
        draft = {key: value for key, value in body.draft.model_dump(exclude_unset=True).items()
                 if key.startswith(role + "_") or key == MODEL_SWITCHES.get(role)}
        try:
            candidate = config.merge_settings(settings, draft)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _require_key_for_changed_endpoint(settings, candidate, draft, role)
        return _check_model(candidate, role)
    result = _check_model(settings, role)
    if result["detected"]:
        with config.LOCK:
            current = config.load()
            if (not _model_enabled(current, role) or
                    connection(_model_values(current, role), role == "vlm") !=
                    connection(_model_values(settings, role), role == "vlm") or
                    any(current[key] != settings[key] for key in result["detected"])):
                raise HTTPException(409, "模型设置已更新，请重新测试")
            config.save(result["detected"])
    return result


@secured.get("/tasks")
def list_tasks(request: Request, limit: int | None = Query(None, ge=1, le=100), offset: int = Query(0, ge=0)):
    if limit is None:
        return store.list()
    return conditional_json(request, store.summary_page(offset, limit))


def conditional_json(request, value):
    response = JSONResponse(value)
    tag = '"' + hashlib.sha256(response.body).hexdigest() + '"'
    headers = {'ETag': tag, 'Cache-Control': 'private, no-cache'}
    if request.headers.get('if-none-match') == tag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return response


@secured.get("/workspace")
def workspace():
    return store.history_status()


@secured.get("/tasks/{job_id}")
def task(job_id: str, request: Request):
    job = get_job(job_id)
    job['needs_upload'] = store.needs_upload(job)
    return conditional_json(request, job)


class RenameTask(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@secured.patch("/tasks/{job_id}")
def rename(job_id: str, body: RenameTask):
    with store.lock:
        get_job(job_id)
        try:
            return store.rename(job_id, body.title)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


def parse_link(raw_url):
    try:
        parsed = urlparse(raw_url.strip())
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return parsed
    except ValueError:
        pass
    raise HTTPException(400, "请输入完整的 http(s) 视频链接")


@secured.post("/tasks/url/inspect")
def inspect_link(body: LinkCheck):
    parse_link(body.url)
    if not link_checks.acquire(blocking=False):
        raise HTTPException(429, "链接检查繁忙，请稍后重试")
    try:
        return preview_link(body.url.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (DownloadError, OSError) as exc:
        if isinstance(exc.__context__, UnsupportedError):
            raise HTTPException(422, "此页面没有可识别的音视频，请打开具体的视频或回放页面并提交该链接") from exc
        raise HTTPException(422, "无法读取链接中的音视频，请检查链接、网络或平台访问权限") from exc
    finally:
        link_checks.release()


@secured.post("/tasks/url", status_code=202)
def create_link(body: LinkTask):
    parsed = parse_link(body.url)
    source = {"type": "url", "url": body.url.strip()}
    if body.resource_id:
        source["resource_id"] = body.resource_id
    try:
        return store.create(modes=body.modes, source=source, options=body.options.model_dump(), title=parsed.hostname)
    except TaskCapacityError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@secured.post("/tasks/upload", status_code=202)
async def create_upload(request: Request):
    try:
        with store.upload_slot() as release_slot:
            async with upload_form(request, max_files=UPLOAD_FILES, max_fields=2) as form:
                spool = getattr(request.state, 'upload_spool', None)
                if spool is not None:
                    spool.resize(0)
                file, subtitle = form.get('file'), form.get('subtitle')
                modes, options = form.get('modes'), form.get('options', '{}')
                if (not isinstance(file, MultipartFile) or (subtitle is not None and not isinstance(subtitle, MultipartFile))
                        or not isinstance(modes, str) or not isinstance(options, str)):
                    raise HTTPException(400, '请上传媒体文件并选择输出功能')
                return await run_in_threadpool(_save_upload, request, file, modes, options, subtitle, release_slot)
    except TaskCapacityError as exc:
        raise HTTPException(429, str(exc)) from exc


def _save_upload(request, file, modes, options, subtitle, release_slot):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in MEDIA_EXTS:
        raise HTTPException(400, "不支持的媒体格式")
    try:
        chosen = json.loads(modes)
        if not isinstance(chosen, list):
            raise ValueError("功能选项格式错误")
        opts = Options(**json.loads(options)).model_dump()
        source = {"type": "upload", "filename": "source" + suffix, "original_name": Path(file.filename).name}
        if subtitle:
            sub_suffix = Path(subtitle.filename or "").suffix.lower()
            if sub_suffix not in {".srt", ".vtt"}:
                raise ValueError("配套字幕仅支持 SRT / VTT")
            source["subtitle"] = "uploaded" + sub_suffix
        with store.lock:
            release_slot()
            job = store.create(modes=chosen, source=source, options=opts, title=Path(file.filename).stem[:200],
                               enqueue=False, upload_pending=True)
    except TaskCapacityError:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise HTTPException(507, "可用磁盘空间不足，请释放空间后重新上传") from exc
        raise
    folder = store.directory(job["id"])
    staged = []
    published = []
    committed = False
    disk = getattr(request.state, 'upload_disk', None)
    try:
        for upload, name, limit in [(file, source["filename"], int(os.getenv("MAX_UPLOAD_MB", "4096")) * 1024 ** 2)] + ([(subtitle, source["subtitle"], 16 * 1024 ** 2)] if subtitle else []):
            if upload.size is not None and not 0 < upload.size <= limit:
                raise HTTPException(413 if upload.size > limit else 400, "上传文件超过大小限制" if upload.size > limit else "上传文件为空")
            size = 0
            temporary = folder / f'.{name}.upload'
            staged.append((temporary, folder / name))
            with temporary.open("xb") as dest:
                while chunk := upload.file.read(1024 * 1024):
                    store.check(job["id"])
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "上传文件超过大小限制")
                    dest.write(chunk)
                    if disk:
                        disk.consume(len(chunk))
                dest.flush()
                os.fsync(dest.fileno())
            if size == 0:
                raise ValueError("上传文件为空")
        with store.lock:
            store.check(job["id"])
            for temporary, target in staged:
                replace_file(temporary, target)
                published.append(target)
            store.update(job["id"], status="queued", upload_complete=True)
            committed = True
            store.queue.put(job["id"])
        return store.get(job["id"])
    except BaseException as exc:
        if not committed:
            for path in [temporary for temporary, _ in staged] + published:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        store.record_state(job["id"], status="cancelled" if isinstance(exc, Cancelled) else "failed",
                             upload_complete=committed, error=str(exc),
                             message="上传已取消，请重新上传" if isinstance(exc, Cancelled) else "上传失败，请重新上传")
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            raise HTTPException(507, "可用磁盘空间不足，请释放空间后重新上传") from exc
        raise HTTPException(400, "上传失败：" + str(exc))


@secured.post("/tasks/{job_id}/cancel")
def cancel(job_id: str):
    with store.lock:
        get_job(job_id)
        return store.cancel(job_id)


class RetryTask(BaseModel):
    use_current_model: bool = False


@secured.post("/tasks/{job_id}/retry")
def retry(job_id: str, body: RetryTask | None = None):
    with store.lock:
        get_job(job_id)
        try:
            return store.retry(job_id, use_current_model=bool(body and body.use_current_model))
        except TaskCapacityError as exc:
            raise HTTPException(429, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc))


@secured.delete("/tasks/{job_id}")
def remove(job_id: str):
    try:
        store.remove(job_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(404, "任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@secured.get("/tasks/{job_id}/files/{filename:path}")
def download(job_id: str, filename: str, request: Request, inline: bool = False):
    with store.lock:
        job = get_job(job_id)
        try:
            path = store.resource_path(job_id, filename)
        except FileNotFoundError as exc:
            raise HTTPException(404, "文件不存在") from exc
        if not path.is_file():
            raise HTTPException(404, "文件不存在")
        allowed = {entry["name"] for entry in job["artifacts"]} | {entry.get("download_name") for entry in job["artifacts"]}
        resource = filename.startswith("image/") and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and "illustrated" in job["modes"]
        if filename not in allowed and not resource:
            raise HTTPException(404, "文件不可下载")
        lease = resource_lease(job_id, filename)
    headers = {"X-Content-Type-Options": "nosniff"}
    if path.suffix == ".html":
        headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox allow-same-origin"
        if filename == "mindmap.html":
            headers["Content-Security-Policy"] = "default-src 'none'; img-src data: blob:; connect-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; sandbox allow-scripts allow-downloads"
    size = os.fstat(lease.stream.fileno()).st_size
    start, end, status = 0, size - 1, 200
    if request.headers.get("range"):
        match = re.fullmatch(r"bytes=([0-9]{0,19})-([0-9]{0,19})", request.headers["range"])
        if not match or not any(match.groups()):
            lease.close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start = int(match[1]) if match[1] else max(0, size - int(match[2]))
        end = min(int(match[2]) if match[2] and match[1] else size - 1, size - 1)
        if start >= size or start > end:
            lease.close()
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers.update({"Accept-Ranges": "bytes", "Content-Length": str(max(0, end - start + 1)),
                    "Content-Disposition": ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(path.name)})
    def chunks():
        try:
            lease.stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = lease.stream.read(min(1024 * 1024, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
        finally:
            lease.close()
    return StreamingResponse(chunks(), status_code=status, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                             headers=headers, background=BackgroundTask(lease.close))


def read_resource(job_id, filename):
    with resource_lease(job_id, filename) as stream:
        return stream.read().decode("utf-8")


def video_subtitle_streams(path):
    from local.media import TEXT_SUBTITLE_CODECS, probe
    try:
        return [stream for stream in probe(path).get('streams', [])
                if stream.get('codec_type') == 'subtitle' and stream.get('codec_name') in TEXT_SUBTITLE_CODECS]
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return []


@secured.get("/tasks/{job_id}/video-subtitles/{index}.vtt")
def video_subtitle(job_id: str, index: int):
    from local.media import command
    from local.resources import FFMPEG
    if not any(a['name'] == 'video.mp4' for a in get_job(job_id)['artifacts']):
        raise HTTPException(404, '视频尚未生成')
    with resource_lease(job_id, 'video.mp4'):
        path = job_directory(job_id) / 'output' / 'video.mp4'
        if not any(stream['index'] == index for stream in video_subtitle_streams(path)):
            raise HTTPException(404, '字幕轨不存在')
        stat = path.stat()
        cache = path.parent.parent / f'preview-{stat.st_size}-{stat.st_mtime_ns}-{index}.vtt'
        with path_lock(cache):
            if cache.is_file():
                content = cache.read_bytes()
            else:
                with FFMPEG.slot():
                    content = command(['ffmpeg', '-v', 'error', '-i', str(path), '-map', f'0:{index}',
                                       '-c:s', 'webvtt', '-f', 'webvtt', 'pipe:1'], timeout=60).encode('utf-8')
                atomic_bytes(cache, content)
    return Response(content, media_type='text/vtt', headers={'X-Content-Type-Options': 'nosniff'})


@secured.get("/tasks/{job_id}/preview/{filename}")
def preview(job_id: str, filename: str):
    from local import exports
    with store.lock:
        job = get_job(job_id)
        artifact = next((a for a in job["artifacts"] if a["name"] == filename), None)
        if not artifact:
            raise HTTPException(404, "文件不可预览")
        output = job_directory(job_id) / "output"
        path = (output / filename).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file():
            raise HTTPException(404, "文件尚未生成")
    file_url = f"/api/v1/tasks/{job_id}/files/{filename}?inline=true"
    if path.suffix in {".pdf", ".html"}:
        return RedirectResponse(file_url)
    if path.suffix == ".md":
        with resource_lease(job_id, filename) as stream:
            content = exports.report_html(stream.read().decode("utf-8"), output, artifact["label"], embedded=True)
    elif path.suffix == ".txt":
        content = exports.page(artifact["label"], '<pre class="transcript">' + html.escape(read_resource(job_id, filename)) + '</pre>')
    elif path.suffix == ".mp4":
        from local.subtitles import language_code
        with resource_lease(job_id, filename):
            streams = video_subtitle_streams(path)
        codes = [language_code(stream.get('tags', {}).get('language')) for stream in streams]
        default_index = codes.index('zh') if 'zh' in codes else 0
        tracks = []
        for order, (stream, code) in enumerate(zip(streams, codes)):
            label = {'zh': '中文', 'en': 'English'}.get(code, f'字幕 {order + 1}')
            default = ' default' if order == default_index else ''
            track_url = f"/api/v1/tasks/{job_id}/video-subtitles/{stream['index']}.vtt"
            tracks.append(f'<track kind="subtitles" label="{label}" srclang="{code if code != "original" else "und"}" src="{track_url}"{default}>')
        content = exports.page(artifact["label"], f'<h1>视频预览</h1><video controls preload="metadata" style="width:100%" src="{file_url}">{"".join(tracks)}</video>')
    else:
        raise HTTPException(404, "暂不支持该预览类型")
    return HTMLResponse(content, headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; img-src data:; media-src 'self'; style-src 'unsafe-inline'; sandbox allow-same-origin"})


@secured.get("/tasks/{job_id}/mindmap")
def mindmap_source(job_id: str):
    with store.lock:
        get_job(job_id)
        path = job_directory(job_id) / "mindmap.json"
        if not path.is_file():
            raise HTTPException(409, "思维导图尚未生成")
        return json.loads(path.read_text(encoding="utf-8"))


@secured.post("/tasks/{job_id}/mindmap/pdf")
async def save_mindmap_pdf(job_id: str, request: Request):
    get_job(job_id)
    async with upload_form(request, max_files=1, max_fields=0) as form:
        file = form.get('file')
        if not isinstance(file, MultipartFile):
            raise HTTPException(400, "请选择 PDF 文件")
        if file.size is not None and file.size > PDF_UPLOAD_LIMIT:
            raise HTTPException(413, "PDF 文件超过 25 MB")
        spool = getattr(request.state, 'upload_spool', None)
        if spool is not None:
            spool.resize(0)
        return await run_in_threadpool(_save_mindmap_pdf, job_id, file)


def _save_mindmap_pdf(job_id, file):
    from local import exports
    content = file.file.read(PDF_UPLOAD_LIMIT + 1)
    if len(content) > PDF_UPLOAD_LIMIT:
        raise HTTPException(413, "PDF 文件超过 25 MB")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
        raise HTTPException(400, "PDF 文件格式不正确")
    with store.lock:
        job = get_job(job_id)
        output = job_directory(job_id) / "output"
        if "mindmap" not in job["modes"] or not (output.parent / "mindmap.json").is_file():
            raise HTTPException(409, "思维导图尚未生成")
        if job["status"] in ACTIVE:
            raise HTTPException(409, "思维导图正在后台生成，请稍后重试")
        with path_lock(output):
            target = output / "mindmap.pdf"
            if not target.exists():
                atomic_bytes(target, content)
        exports.publish(store, job_id)
    return {"ok": True}


@secured.post("/tasks/{job_id}/mindmap/render")
def render_mindmap_pdf(job_id: str):
    from local import exports, pdf_export
    job = get_job(job_id)
    folder = job_directory(job_id)
    if "mindmap" not in job["modes"] or not (folder / "mindmap.json").is_file():
        raise HTTPException(409, "思维导图尚未生成")
    # A read lease keeps the task directory alive throughout a legacy export.
    # Newly submitted jobs produce their PDF inside the background pipeline.
    with store.lock:
        get_job(job_id)
        store.readers[job_id] = store.readers.get(job_id, 0) + 1
    try:
        pdf_export.render(folder, lambda: store.check(job_id))
        exports.publish(store, job_id)
    finally:
        with store.lock:
            store.readers[job_id] -= 1
    return {"ok": True}


router.include_router(secured)
