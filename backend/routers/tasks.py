import json
import os
import re
import html
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from local import config
from local.jobs import ACTIVE, Cancelled, store
from local.media import MEDIA_EXTS
from local.storage import atomic_bytes, path_lock
from starlette.background import BackgroundTask
from urllib.parse import quote
import mimetypes

router = APIRouter(prefix="/api/v1", tags=["Local tasks"])


secured = APIRouter()


class Options(BaseModel):
    clean_subtitles: bool = True
    scenario: str = "auto"
    instructions: str = Field("", max_length=2500)


class LinkTask(BaseModel):
    url: str = Field(..., max_length=4000)
    modes: List[str]
    options: Options = Field(default_factory=Options)


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
def settings():
    import shutil
    from local.media import whisper_status
    return config.public() | {"ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                              **whisper_status(config.load()["whisper_model"])}


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
    whisper_model: str = "small"
    whisper_device: str = "auto"
    llm_context_tokens: int = Field(32768, ge=2048, le=2_000_000)
    vlm_context_tokens: int = Field(32768, ge=2048, le=2_000_000)
    llm_max_output_tokens: int = Field(8192, ge=256, le=131072)
    vlm_max_output_tokens: int = Field(8192, ge=256, le=131072)


@secured.put("/settings")
def settings_save(body: ModelSettings):
    try:
        return config.save(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@secured.post("/settings/test")
def settings_test():
    from local.intelligence import Intelligence
    try:
        answer = Intelligence(config.load(), lambda: None).chat("请只回复：连接成功", max_tokens=32)
        return {"ok": True, "message": answer}
    except Exception as exc:
        raise HTTPException(502, config.redact(str(exc))[:600])


@secured.get("/tasks")
def list_tasks():
    return store.list()


@secured.get("/workspace")
def workspace():
    return store.history_status()


@secured.get("/tasks/{job_id}")
def task(job_id: str):
    return get_job(job_id)


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


@secured.post("/tasks/url", status_code=202)
def create_link(body: LinkTask):
    url = body.url.strip()
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
        raise HTTPException(400, "请输入完整的 http(s) 视频链接")
    try:
        return store.create(modes=body.modes, source={"type": "url", "url": url}, options=body.options.model_dump(), title=urlparse(url).hostname)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@secured.post("/tasks/upload", status_code=202)
async def create_upload(file: UploadFile = File(...), modes: str = Form(...), options: str = Form("{}"), subtitle: Optional[UploadFile] = File(None)):
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
        job = store.create(modes=chosen, source=source, options=opts, title=Path(file.filename).stem[:200], enqueue=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    try:
        for upload, name, limit in [(file, source["filename"], int(os.getenv("MAX_UPLOAD_MB", "4096")) * 1024 ** 2)] + ([(subtitle, source["subtitle"], 16 * 1024 ** 2)] if subtitle else []):
            size = 0
            with (store.directory(job["id"]) / name).open("wb") as dest:
                while chunk := await upload.read(1024 * 1024):
                    store.check(job["id"])
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("上传文件超过大小限制")
                    dest.write(chunk)
            if size == 0:
                raise ValueError("上传文件为空")
        with store.lock:
            store.check(job["id"])
            store.update(job["id"], status="queued")
            store.queue.put(job["id"])
        return store.get(job["id"])
    except BaseException as exc:
        store.update(job["id"], status="cancelled" if isinstance(exc, Cancelled) else "failed", error=str(exc), message="上传已取消" if isinstance(exc, Cancelled) else "上传失败，请重新上传")
        if not isinstance(exc, Exception):
            raise
        raise HTTPException(400, "上传失败：" + str(exc))
    finally:
        await file.close()
        if subtitle:
            await subtitle.close()


@secured.post("/tasks/{job_id}/cancel")
def cancel(job_id: str):
    with store.lock:
        get_job(job_id)
        return store.cancel(job_id)


@secured.post("/tasks/{job_id}/retry")
def retry(job_id: str):
    with store.lock:
        get_job(job_id)
        try:
            return store.retry(job_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc))


@secured.delete("/tasks/{job_id}")
def remove(job_id: str):
    with store.lock:
        get_job(job_id)
        try:
            store.remove(job_id)
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(409, str(exc))


@secured.get("/tasks/{job_id}/files/{filename:path}")
def download(job_id: str, filename: str, request: Request, inline: bool = False):
    job = get_job(job_id)
    base = (job_directory(job_id) / "output").resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "文件不存在")
    allowed = {entry["name"] for entry in job["artifacts"]} | {entry.get("download_name") for entry in job["artifacts"]}
    resource = filename.startswith("image/") and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and "illustrated" in job["modes"]
    if filename not in allowed and not resource:
        raise HTTPException(404, "文件不可下载")
    headers = {"X-Content-Type-Options": "nosniff"}
    if path.suffix == ".html":
        headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox allow-same-origin"
        if filename == "mindmap.html":
            headers["Content-Security-Policy"] = "default-src 'none'; img-src data: blob:; connect-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; sandbox allow-scripts allow-downloads"
    try:
        lease = resource_lease(job_id, filename)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404, "文件不存在")
    size = os.fstat(lease.stream.fileno()).st_size
    start, end, status = 0, size - 1, 200
    if request.headers.get("range"):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers["range"])
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


@secured.get("/tasks/{job_id}/preview/{filename}")
def preview(job_id: str, filename: str):
    from local import exports
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
        content = exports.page(artifact["label"], f'<h1>视频预览</h1><video controls preload="metadata" style="width:100%" src="{file_url}"></video>')
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
async def save_mindmap_pdf(job_id: str, file: UploadFile = File(...)):
    from local import exports
    job = get_job(job_id)
    output = job_directory(job_id) / "output"
    if not (output.parent / "mindmap.json").is_file() or "mindmap" not in job["modes"]:
        raise HTTPException(409, "思维导图尚未生成")
    content = await file.read(25 * 1024 * 1024 + 1)
    await file.close()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(413, "PDF 文件超过 25 MB")
    if not content.startswith(b"%PDF-") or b"%%EOF" not in content[-1024:]:
        raise HTTPException(400, "PDF 文件格式不正确")
    with store.lock, path_lock(output):
        if get_job(job_id)["status"] in ACTIVE:
            raise HTTPException(409, "思维导图正在后台生成，请稍后重试")
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
