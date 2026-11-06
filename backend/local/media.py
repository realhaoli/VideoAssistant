import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA, WHISPER_MODELS, WHISPER_DEVICES
from . import subtitles

ASR_LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
MODEL = None
MODEL_KEY = None
WHISPER_RUNTIME = {}
FRAME_SLOTS = threading.BoundedSemaphore(3)
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4v"}


def network_options():
    options = dict(quiet=True, no_warnings=True, noplaylist=True, socket_timeout=30)
    if shutil.which("node"):
        options["js_runtimes"] = {"node": {}}
    if os.getenv("YTDLP_COOKIES_FILE"):
        options["cookiefile"] = os.environ["YTDLP_COOKIES_FILE"]
    return options


def command(args, check=lambda: None, timeout=7200):
    """Bound subprocess runtime, drain stderr to disk and support cancellation."""
    import tempfile
    with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(args, stdout=out, stderr=err)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                check()
                if time.monotonic() - start > timeout:
                    raise TimeoutError("媒体处理超过时间限制")
                time.sleep(.2)
            if proc.returncode:
                err.seek(0)
                raise RuntimeError(err.read().decode(errors="replace")[-1800:])
            out.seek(0)
            return out.read().decode(errors="replace")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def probe(path):
    return json.loads(command(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=30))


def download_options(folder, best=False, audio=False):
    return dict(
        **network_options(),
        format="bestaudio/best" if audio else ("bv*+ba/b" if best else "bv*[height<=1080]+ba/b[height<=?1080]"),
        outtmpl=str(folder / "source.%(ext)s"), merge_output_format="mp4",
        format_sort=["res", "ext:mp4:m4a"],
        retries=3, fragment_retries=3, continuedl=True,
    )


def choose_subtitle(info, prefix):
    for kind in ("subtitles", "automatic_captions"):
        choices = info.get(kind) or {}
        keys = sorted((k for k in choices if k.lower().startswith(prefix)), key=lambda k: ("orig" not in k, len(k)))
        for key in keys:
            entries = sorted(choices[key], key=lambda e: {"srt": 0, "vtt": 1, "json3": 2}.get(e.get("ext"), 10))
            for entry in entries:
                if entry.get("ext") in {"srt", "vtt", "json3"} and entry.get("url"):
                    return entry
    return None


def inspect_url(url, folder, check, warning):
    from yt_dlp import YoutubeDL
    folder.mkdir(parents=True, exist_ok=True)
    tracks = {}
    with YoutubeDL(network_options()) as ydl:
        info = ydl.extract_info(url, download=False)
        if info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("请提交单个视频链接，暂不支持播放列表")
        check()
        for lang in ("zh", "en"):
            track = choose_subtitle(info, lang)
            if track:
                try:
                    with ydl.urlopen(track["url"]) as response:
                        text = response.read(10 * 1024 * 1024).decode("utf-8-sig")
                    parsed = subtitles.parse(text)
                    if parsed:
                        tracks[lang] = parsed
                        (folder / f"platform.{lang}.srt").write_text(subtitles.srt(parsed), encoding="utf-8")
                except Exception as exc:
                    warning(f"{lang} 平台字幕读取失败，将使用其他字幕或本地转写：{type(exc).__name__}")
    metadata = {k: info.get(k) for k in ("title", "duration", "thumbnail", "webpage_url", "uploader")}
    metadata["chapters"] = info.get("chapters") or []
    from .storage import atomic_json
    atomic_json(folder / "source_info.json", metadata)
    return metadata, tracks


def download(url, folder, *, best, audio, check, progress):
    from yt_dlp import YoutubeDL
    existing = [p for p in folder.glob("source.*") if p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0]
    if existing:
        return existing[0]
    opts = download_options(folder, best=best, audio=audio)

    def hook(event):
        check()
        if event["status"] == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            if total:
                progress(min(99, int(event.get("downloaded_bytes", 0) * 100 / total)))
    opts["progress_hooks"] = [hook]
    with YoutubeDL(opts) as ydl:
        ydl.download([url])
    check()
    files = [p for p in folder.glob("source.*") if p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0]
    if not files:
        raise RuntimeError("视频下载完成但未找到媒体文件")
    return max(files, key=lambda p: p.stat().st_size)


def cover(url, output):
    import requests
    from PIL import Image
    if not url:
        return False
    target = output / "cover.jpg"
    if target.exists():
        return True
    with requests.get(url, timeout=25, stream=True) as response:
        response.raise_for_status()
        payload = bytearray()
        for data in response.iter_content(65536):
            payload.extend(data)
            if len(payload) > 12 * 1024 * 1024:
                raise ValueError("封面图片过大")
    import io
    with Image.open(io.BytesIO(payload)) as image:
        image.convert("RGB").save(target, quality=90)
    return True


def embedded_subtitles(path, folder, check):
    tracks = {}
    for stream in probe(path).get("streams", []):
        if stream.get("codec_type") != "subtitle" or stream.get("codec_name") not in {"subrip", "ass", "webvtt", "mov_text", "text", "ssa"}:
            continue
        lang = stream.get("tags", {}).get("language", "")
        lang = "zh" if lang in {"zh", "zho", "chi", "zh-CN"} else "en" if lang in {"en", "eng"} else "original"
        if lang in tracks:
            continue
        target = folder / f"embedded.{lang}.srt"
        command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", f"0:{stream['index']}", str(target)], check)
        parsed = subtitles.parse(target.read_text(encoding="utf-8"))
        if parsed:
            tracks[lang] = parsed
    return tracks


def cuda_available():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def whisper_status(model_name="small"):
    local = DATA / "models" / f"faster-whisper-{model_name}"
    cached = (local / "model.bin").exists() or any((DATA / "models").glob(f"models--*--faster-whisper-{model_name}/snapshots/*/model.bin"))
    return {"gpu_available": cuda_available(), "whisper_downloaded": cached,
            "whisper_runtime": dict(WHISPER_RUNTIME)}


def _load_whisper(model_name, device):
    global MODEL, MODEL_KEY, WHISPER_RUNTIME
    from faster_whisper import WhisperModel
    compute_type = "float16" if device == "cuda" else "int8"
    key = (model_name, device, compute_type)
    with MODEL_LOCK:
        if MODEL is None or MODEL_KEY != key:
            import gc
            MODEL = None
            MODEL_KEY = None
            gc.collect()
            local = DATA / "models" / f"faster-whisper-{model_name}"
            model_path = str(local) if (local / "model.bin").exists() else model_name
            instance = WhisperModel(model_path, device=device, compute_type=compute_type,
                                    download_root=str(DATA / "models"))
            MODEL, MODEL_KEY = instance, key
        WHISPER_RUNTIME = {"model": model_name, "device": device, "compute_type": compute_type}
        return MODEL


def transcribe(path, folder, check, progress, *, settings=None, warning=lambda message: None):
    # Keep the model and its lazy inference iterator owned by one job.
    while not ASR_LOCK.acquire(timeout=.25):
        check()
    try:
        return _transcribe(path, folder, check, progress, settings=settings, warning=warning)
    finally:
        ASR_LOCK.release()


def _transcribe(path, folder, check, progress, *, settings=None, warning=lambda message: None):
    settings = settings or {}
    model_name = settings.get("whisper_model", "small")
    requested = settings.get("whisper_device", "auto")
    if model_name not in WHISPER_MODELS or requested not in WHISPER_DEVICES:
        raise ValueError("Whisper 模型或设备配置无效")
    audio = folder / "audio.wav"
    if not audio.exists():
        temporary_audio = folder / "audio.partial.wav"
        try:
            command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(temporary_audio)], check)
            temporary_audio.replace(audio)
        finally:
            temporary_audio.unlink(missing_ok=True)
    check()
    device = "cuda" if requested != "cpu" and cuda_available() else "cpu"
    if requested == "cuda" and device == "cpu":
        warning("未检测到可用 GPU，Whisper 已自动切换为 CPU")

    def recognize(actual_device):
        check()
        model = _load_whisper(model_name, actual_device)
        stream, info = model.transcribe(str(audio), beam_size=5, vad_filter=True, language=None, word_timestamps=True)
        result = []
        for segment in stream:
            check()
            text = segment.text.strip()
            if text:
                words = getattr(segment, "words", None)
                result.extend(word_cues(words) if words else [dict(start=segment.start, end=segment.end, text=text)])
            progress(min(99, int(segment.end / max(info.duration, 1) * 100)))
        return result, info

    try:
        result, info = recognize(device)
    except Exception:
        check()  # Cancellation is never converted into a CPU retry.
        if device != "cuda":
            raise
        warning("GPU 转写失败，Whisper 已自动切换为 CPU 重新转写")
        result, info = recognize("cpu")
        device = "cpu"
    if not result:
        raise ValueError("未识别到有效语音，请检查音轨；静音视频无法自动生成字幕")
    from .storage import atomic_json
    atomic_json(folder / "whisper-runtime.json", {"model": model_name, "device": device})
    return result, info.language


def word_cues(words):
    """Split long ASR segments on real aligned word times, not invented offsets."""
    cues, current, start, end = [], "", 0.0, 0.0
    for word in words:
        if not current:
            start = word.start
        current += word.word
        end = max(word.end, start + .05)
        if re.search(r"[。！？；.!?;]$", current.strip()) or len(current) >= 60 or end - start >= 8:
            cues.append(dict(start=start, end=end, text=current.strip()))
            current = ""
    if current.strip():
        cues.append(dict(start=start, end=end, text=current.strip()))
    return cues


def frame(path, timestamp, target, check):
    with FRAME_SLOTS:
        check()
        command(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", "-q:v", "2", str(target)], check, timeout=60)
    if not target.exists() or target.stat().st_size < 100:
        raise ValueError("该位置无法抽取视频画面")


def image_quality(path):
    from PIL import Image, ImageFilter, ImageStat
    with Image.open(path) as image:
        gray = image.convert("L").resize((320, 180))
        brightness = ImageStat.Stat(gray).mean[0]
        if brightness < 12 or brightness > 248:
            return 0.0
        return ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0]


def image_hash(path):
    from PIL import Image
    with Image.open(path) as image:
        values = list(image.convert("L").resize((17, 16)).getdata())
    return sum((values[y * 17 + x] > values[y * 17 + x + 1]) << (y * 16 + x) for y in range(16) for x in range(16))


def candidates(path, chapter, output, index, used, check):
    from concurrent.futures import ThreadPoolExecutor
    start, end = chapter["start"], chapter["end"]
    duration = max(.1, end - start)
    def extract(item):
        n, fraction = item
        check()
        timestamp = min(end - .08, start + duration * fraction)
        # Chapter boundaries can change after prompt edits. Include the actual
        # frame timestamp so a resumed task never relabels an old screenshot.
        target = output / "images" / f"chapter-{index + 1:02d}-{n + 1}-{round(timestamp * 1000)}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not target.exists():
                frame(path, timestamp, target, check)
            quality = image_quality(target)
            fingerprint = image_hash(target)
            duplicate = any((fingerprint ^ other).bit_count() < 12 for other in used)
            return dict(path=target, time=timestamp, quality=quality, hash=fingerprint, duplicate=duplicate)
        except (RuntimeError, ValueError):
            return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        images = [image for image in pool.map(extract, enumerate((.04, .17, .30, .43, .56, .69, .82, .94))) if image]
    ranked = sorted(images, key=lambda item: (not item["duplicate"], item["quality"]), reverse=True)
    diverse = []
    for image in ranked:
        if image["quality"] > .4 and not any((image["hash"] ^ old["hash"]).bit_count() < 12 for old in diverse):
            diverse.append(image)
    return diverse[:4]


def embed(path, zh, en, target, check):
    streams = probe(path).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    video_codec = "copy" if video.get("codec_name") in {"h264", "hevc", "av1", "vp9", "mpeg4"} else "libx264"
    audio_codec = "copy" if audio.get("codec_name") in {"aac", "mp3", "alac"} else "aac"
    # MP4 soft subtitles use mov_text. Preserve resolution and compatible streams.
    command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-i", str(zh), "-i", str(en),
             "-map", "0:v?", "-map", "0:a?", "-map", "1:0", "-map", "2:0", "-c:v", video_codec, "-c:a", audio_codec, "-c:s", "mov_text", "-movflags", "+faststart",
             "-metadata:s:s:0", "language=zho", "-metadata:s:s:0", "title=简体中文",
             "-metadata:s:s:1", "language=eng", "-metadata:s:s:1", "title=English",
             "-disposition:s:0", "default", "-disposition:s:1", "0", str(target)], check)
    streams = probe(target).get("streams", [])
    languages = {s.get("tags", {}).get("language") for s in streams if s.get("codec_type") == "subtitle"}
    if not {"zho", "eng"} <= languages:
        raise RuntimeError("中英字幕轨校验未通过")
