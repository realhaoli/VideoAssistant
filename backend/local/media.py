from contextlib import suppress
import json
from functools import partial
import hashlib
from itertools import islice
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA, WHISPER_MODELS, WHISPER_DEVICES
from . import subtitles
from .links import LinkDownloader, describe_resource
from .resources import ASR, DOWNLOADS, FFMPEG, FRAMES, ensure_disk, reserve_disk, setting
from .stages import Step, drive
from .storage import atomic_json, atomic_text
from .whisper_worker import CudaWhisper, CudaWhisperTimeout, probe_cuda

MODEL_LOCK = threading.Lock()
MODEL = None
MODEL_KEY = None
WHISPER_RUNTIME = {}
CUDA_WHISPER = CudaWhisper()
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a", ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4v"}
TEXT_SUBTITLE_CODECS = {"subrip", "ass", "webvtt", "mov_text", "text", "ssa"}
CAPTION_SCAN_VERSION = 1


def network_options():
    options = dict(quiet=True, no_warnings=True, noplaylist=True, socket_timeout=30)
    if shutil.which("node"):
        options["js_runtimes"] = {"node": {}}
    if os.getenv("YTDLP_COOKIES_FILE"):
        options["cookiefile"] = os.environ["YTDLP_COOKIES_FILE"]
    return options


def command(args, check=lambda: None, timeout=7200, *, on_poll=None):
    """Bound subprocess runtime, drain stderr to disk and support cancellation."""
    import tempfile
    with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(args, stdout=out, stderr=err)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                check()
                if on_poll:
                    on_poll()
                if time.monotonic() - start > timeout:
                    raise TimeoutError("媒体处理超过时间限制")
                time.sleep(.2)
            if proc.returncode:
                err.seek(0)
                raise RuntimeError(err.read().decode(errors="replace")[-1800:])
            if on_poll:
                on_poll()
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


def ffmpeg_command(args, check, *, duration=None, progress=None, timeout=None):
    with FFMPEG.slot(check):
        threads = str(setting('FFMPEG_THREADS', 2, high=16))
        args = [args[0], '-threads', threads, '-filter_threads', '1', *args[1:-1], '-threads', threads, args[-1]]
        return _ffmpeg_command(args, check, duration=duration, progress=progress,
                               timeout=timeout if timeout is not None else setting('FFMPEG_TIMEOUT_SECONDS',21600,low=60,high=172800))


def _ffmpeg_command(args, check, *, duration=None, progress=None, timeout=7200):
    """Read FFmpeg's processed media time; never infer work from elapsed time."""
    if progress is None or not duration or duration <= 0:
        return command(args, check, timeout=timeout)
    import tempfile
    with tempfile.TemporaryDirectory(prefix="videoassistant-progress-") as temporary:
        telemetry = Path(temporary) / "progress.txt"
        last = 0
        offset, pending = 0, ""
        progress(0)
        def poll():
            nonlocal last, offset, pending
            try:
                with telemetry.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read()
                    offset = stream.tell()
            except FileNotFoundError:
                return
            lines = (pending + chunk.decode("ascii", errors="replace")).split("\n")
            pending = lines.pop()
            values = [line.split("=", 1)[1].strip() for line in lines if line.startswith("out_time_us=")]
            values = [value for value in values if value.isdigit()]
            if values:
                percent = max(last, min(99, int(int(values[-1]) / (duration * 1_000_000) * 100)))
                if percent > last:
                    last = percent
                    progress(percent)
        result = command([args[0], "-nostats", "-progress", str(telemetry), *args[1:]], check,
                         timeout=timeout, on_poll=poll)
        progress(100)
        return result


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


def subtitle_candidates(info, language):
    formats = {"srt": 0, "vtt": 1, "json3": 2}
    for kind in ("subtitles", "automatic_captions"):
        choices = info.get(kind) or {}
        keys = sorted((k for k in choices if k.lower() not in {'danmaku', 'live_chat'} and subtitles.language_code(k) == language),
                      key=lambda k: ("orig" not in k.lower(), k.lower().startswith("ai-"), len(k)))
        for key in keys:
            entries = sorted(choices[key], key=lambda e: formats.get(e.get("ext"), 10))
            for entry in entries:
                if entry.get("ext") in formats and entry.get("url"):
                    yield entry


def preview_link(url):
    with LinkDownloader({**network_options(), "socket_timeout": 10, "extractor_retries": 0}, preview=True) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("未找到可处理的音视频资源")
    if "resources" not in info:
        return {"resources": [describe_resource(info)], "truncated": False}
    if not info["resources"]:
        raise ValueError("未找到可处理的音视频资源，请提供具体的视频或音频链接")
    return info


def inspect_url(url, folder, check, warning, *, resource_id=None):
    folder.mkdir(parents=True, exist_ok=True)
    tracks = {}
    with LinkDownloader(network_options(), resource_id=resource_id) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("未找到可处理的音视频资源")
        check()
        # Prefer Chinese regardless of origin, and try another usable track
        # when an advertised subtitle URL is empty or unavailable.
        for lang in ("zh", "en", "original"):
            seen = set()
            for track in islice(subtitle_candidates(info, lang), 8):
                check()
                if track["url"] in seen:
                    continue
                seen.add(track["url"])
                try:
                    with ydl.urlopen(track["url"]) as response:
                        data = response.read(10 * 1024 * 1024 + 1)
                    if len(data) > 10 * 1024 * 1024:
                        raise ValueError('平台字幕超过大小限制')
                    parsed = subtitles.parse(data.decode("utf-8-sig"))
                except Exception as exc:
                    warning(f"{lang} 平台字幕读取失败，尝试其他字幕：{type(exc).__name__}")
                    continue
                if parsed:
                    tracks[lang] = parsed
                    atomic_text(folder / f"platform.{lang}.srt", subtitles.srt(parsed))
                    break
            if tracks:
                break
    metadata = {k: info.get(k) for k in ("title", "duration", "thumbnail", "webpage_url", "uploader")}
    metadata["chapters"] = info.get("chapters") or []
    metadata["caption_scan"] = CAPTION_SCAN_VERSION
    atomic_json(folder / "source_info.json", metadata)
    return metadata, tracks


def download(url, folder, *, best, audio, check, progress, resource_id=None):
    with DOWNLOADS.slot(check):
        return _download(url, folder, best=best, audio=audio, check=check, progress=progress, resource_id=resource_id)


def _download(url, folder, *, best, audio, check, progress, resource_id):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    maximum = setting('MAX_DOWNLOAD_MB', 16384, high=1_048_576) * 1024**2
    def completed_files():
        # source.f137.mp4 and source.f140.m4a are yt-dlp components, not results.
        return [p for p in folder.glob('source.*') if p.stem == 'source' and p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0]
    def valid(path):
        if path.stat().st_size > maximum:
            raise ValueError('下载文件超过 MAX_DOWNLOAD_MB 限制，现有文件已保留')
        try:
            details = probe(path)
            return any(s.get('codec_type') == ('audio' if audio else 'video') for s in details.get('streams', []))
        except (RuntimeError, ValueError):
            return False
    for candidate in completed_files():
        if valid(candidate):
            progress(100)
            return candidate
        candidate.rename(candidate.with_name(f'source.invalid-{time.time_ns()}{candidate.suffix}'))
    ensure_disk(folder)
    opts = download_options(folder, best=best, audio=audio)
    with reserve_disk(folder) as disk:
        received = {}
        last_check = 0

        def hook(event):
            nonlocal last_check
            check()
            if event['status'] not in {'downloading', 'finished'}:
                return
            total = event.get('total_bytes') or event.get('total_bytes_estimate')
            downloaded = event.get('downloaded_bytes') or 0
            stream = event.get('filename') or event.get('tmpfilename') or 'source'
            new_stream = stream not in received
            received[stream] = max(received.get(stream, 0), downloaded)
            aggregate = sum(received.values())
            if aggregate + max(0, (total or downloaded) - downloaded) > maximum:
                raise ValueError('下载超过 MAX_DOWNLOAD_MB 限制，已保留可续传文件')
            remaining = max(0, (total or downloaded) - downloaded)
            if new_stream or event['status'] == 'finished' or aggregate - last_check >= 4 * 1024**2:
                disk.resize(max(remaining, 4 * 1024**2) if event['status'] == 'downloading' else 0)
                ensure_disk(folder)
                last_check = aggregate
            if total and event['status'] == 'downloading':
                progress(min(99, int(downloaded * 100 / total)))

        def postprocess(event):
            check()
            if event.get('status') == 'started':
                disk.resize(sum(p.stat().st_size for p in folder.glob('source.f*') if p.is_file()))
                ensure_disk(folder)
            elif event.get('status') == 'finished':
                disk.resize(0)

        def merge(inputs, destination, options):
            target = Path(destination)
            budget = sum(Path(name).stat().st_size for name in inputs)
            args = ['ffmpeg', '-y', '-v', 'error']
            for name in inputs:
                args.extend(['-i', 'file:' + str(name)])
            args.extend([*options, '-movflags', '+faststart', 'file:' + str(target)])

            def check_merge():
                check()
                written = target.stat().st_size if target.is_file() else 0
                disk.resize(max(budget - written, 4 * 1024**2))
                ensure_disk(folder)

            check_merge()
            try:
                ffmpeg_command(args, check_merge)
            except BaseException:
                with suppress(OSError):
                    target.unlink(missing_ok=True)
                raise

        opts['progress_hooks'] = [hook]
        opts['postprocessor_hooks'] = [postprocess]
        opts['postprocessor_args'] = {'ffmpeg':['-threads',str(setting('FFMPEG_THREADS',2,high=16))]}
        with LinkDownloader(opts, resource_id=resource_id) as ydl:
            ydl.merge_runner = merge
            ydl.download([url])
    check()
    files = [p for p in completed_files() if valid(p)]
    if not files:
        raise RuntimeError("视频下载完成但未找到媒体文件")
    result = max(files, key=lambda p: p.stat().st_size)
    atomic_json(folder/'download-complete.json', {'filename':result.name,'size':result.stat().st_size,'best':best,'audio':audio})
    progress(100)
    return result


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


def embedded_subtitles(path, folder, check, warning, streams):
    tracks = {}
    for stream in streams:
        if stream.get("codec_type") != "subtitle" or stream.get("codec_name") not in TEXT_SUBTITLE_CODECS:
            continue
        code = stream.get("tags", {}).get("language")
        lang = subtitles.language_code(code)
        target = folder / f"embedded.{stream['index']}.srt"
        try:
            ffmpeg_command(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-map", f"0:{stream['index']}", str(target)], check)
            parsed = subtitles.parse(target.read_text(encoding="utf-8"))
        except (RuntimeError, OSError, ValueError) as exc:
            warning(f"内嵌字幕读取失败，尝试其他字幕：{type(exc).__name__}")
            continue
        if parsed:
            if not code or code.lower() in {"und", "mul"}:
                lang = subtitles.language(parsed)
            tracks.setdefault(lang, parsed)
            if lang == "zh":
                break
    return tracks


def cuda_available():
    now = time.monotonic()
    return now >= CUDA_WHISPER.retry_after and probe_cuda(int(now // 60))


def whisper_status(model_name="small"):
    local = DATA / "models" / f"faster-whisper-{model_name}"
    cached = (local / "model.bin").exists() or any((DATA / "models").glob(f"models--*--faster-whisper-{model_name}/snapshots/*/model.bin"))
    return {"gpu_available": cuda_available(), "whisper_downloaded": cached,
            "whisper_runtime": dict(WHISPER_RUNTIME)}


def _load_whisper(model_name, device):
    global MODEL, MODEL_KEY, WHISPER_RUNTIME
    from faster_whisper import WhisperModel
    compute_type = "int8"
    if device == 'cuda':
        import ctranslate2
        requested = os.getenv('WHISPER_COMPUTE_TYPE', 'auto')
        supported = ctranslate2.get_supported_compute_types('cuda')
        compute_type = next((value for value in ('int8_float16','float16','int8_float32','int8','float32') if value in supported), None) if requested == 'auto' else requested
        if compute_type not in supported:
            raise ValueError('GPU 不支持配置的 Whisper 计算精度，将切换 CPU')
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
                                    download_root=str(DATA / "models"), cpu_threads=setting('WHISPER_CPU_THREADS', 4, high=32), num_workers=1)
            MODEL, MODEL_KEY = instance, key
        WHISPER_RUNTIME = {"model": model_name, "device": device, "compute_type": compute_type}
        return MODEL


def transcribe(path, folder, check, progress, *, settings=None, warning=lambda message: None, phase=None, duration=None):
    return drive(transcribe_steps(path, folder, check, progress, settings=settings, warning=warning,
                                  phase=phase, duration=duration))


def transcribe_steps(path, folder, check, progress, *, settings=None, warning=lambda message: None, phase=None, duration=None):
    # Keep the model and its lazy inference iterator owned by one job.
    if phase:
        phase("waiting", None)
    chunk_seconds = setting('WHISPER_CHUNK_SECONDS', 300, low=30, high=900)
    if duration and duration > chunk_seconds:
        return (yield from _chunk_steps(path, folder, check, progress, settings or {}, warning, phase, duration, chunk_seconds))
    return (yield Step('asr', partial(_serial_transcribe, path, folder, check, progress, settings=settings,
                                    warning=warning, phase=phase, duration=duration)))


def _serial_transcribe(path, folder, check, progress, **options):
    with ASR.slot(check):
        return _transcribe(path, folder, check, progress, **options)


def _transcribe_chunks(path, folder, check, progress, settings, warning, phase, duration, chunk_seconds):
    return drive(_chunk_steps(path, folder, check, progress, settings, warning, phase, duration, chunk_seconds))


def _chunk_steps(path, folder, check, progress, settings, warning, phase, duration, chunk_seconds):
    from .storage import atomic_json
    path, folder = Path(path), Path(folder)
    identity = hashlib.sha256()
    with path.open('rb') as stream:
        identity.update(stream.read(1024**2))
        stream.seek(max(0, path.stat().st_size - 1024**2))
        identity.update(stream.read(1024**2))
    identity.update(json.dumps([path.stat().st_size, duration, settings.get('whisper_model', 'small'), chunk_seconds, 'overlap-v1']).encode())
    root = folder / 'whisper-chunks' / identity.hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    result, languages, runtime = [], {}, {}
    actual_settings = dict(settings)
    progress(0)
    for index, start in enumerate(range(0, int(duration + .999), chunk_seconds)):
        check()
        end = min(duration, start + chunk_seconds)
        if end <= start:
            continue
        record = root / f'{index:05}.json'
        saved = None
        if record.is_file():
            try:
                saved = json.loads(record.read_text(encoding='utf-8'))
                if saved['start'] != start or saved['end'] != end or not isinstance(saved['segments'],list) or not isinstance(saved['language'],str):
                    raise ValueError('invalid chunk checkpoint')
            except (OSError, ValueError, KeyError, TypeError):
                saved = None
                warning(f'第 {index + 1} 个转写检查点无效，将只重做该分块')
        if saved is None:
            work = root / f'{index:05}'
            work.mkdir(exist_ok=True)
            window_start, window_end = max(0, start - 2), min(duration, end + 2)
            audio = work / 'audio.wav'
            def extract_audio():
                temporary = work / 'audio.partial.wav'
                try:
                    with reserve_disk(work, int((window_end - window_start) * 32000) + 4096):
                        ffmpeg_command(['ffmpeg', '-y', '-v', 'error', '-ss', str(window_start), '-i', str(path),
                                        '-t', str(window_end-window_start), '-vn', '-ac', '1', '-ar', '16000', str(temporary)], check,
                                       duration=window_end-window_start, progress=(lambda p: phase('audio', p)) if phase else None)
                        temporary.replace(audio)
                finally:
                    temporary.unlink(missing_ok=True)
            if not audio.is_file():
                if phase:
                    phase('audio', None)
                yield Step('media', extract_audio)
            if phase:
                phase('waiting', None)
            values, language = yield Step('asr', partial(_serial_transcribe, path, work, check,
                lambda p: progress(min(99, int((start + (end-start)*p/100) / duration * 100))),
                settings=actual_settings, warning=warning, phase=phase, duration=window_end-window_start, allow_empty=True))
            runtime = json.loads((work/'whisper-runtime.json').read_text(encoding='utf-8'))
            aligned = []
            for cue in values:
                middle = window_start + (cue['start'] + cue['end']) / 2
                if start <= middle < end or (end == duration and middle == end):
                    cue = dict(cue, start=max(start, window_start+cue['start']), end=min(end, window_start+cue['end']))
                    if cue['end'] > cue['start']:
                        aligned.append(cue)
            saved = {'segments':aligned, 'language':language, 'start':start, 'end':end, 'runtime':runtime}
            atomic_json(record, saved)
            if runtime.get('device') == 'cpu':
                actual_settings['whisper_device'] = 'cpu'
        (root / f'{index:05}' / 'audio.wav').unlink(missing_ok=True)
        result.extend(saved['segments'])
        languages[saved['language']] = languages.get(saved['language'], 0) + end-start
        runtime = saved.get('runtime', runtime)
        progress(min(99, int(end / duration * 100)))
    if not result:
        raise ValueError('未识别到有效语音，已保留分块检查点')
    atomic_json(folder/'whisper-runtime.json', runtime)
    progress(100)
    return subtitles.normalize(result), max(languages, key=languages.get)


def _transcribe(path, folder, check, progress, *, settings=None, warning=lambda message: None, phase=None, duration=None, allow_empty=False):
    global MODEL, MODEL_KEY, WHISPER_RUNTIME
    settings = settings or {}
    model_name = settings.get("whisper_model", "small")
    requested = settings.get("whisper_device", "auto")
    if model_name not in WHISPER_MODELS or requested not in WHISPER_DEVICES:
        raise ValueError("Whisper 模型或设备配置无效")
    audio = folder / "audio.wav"
    if not audio.exists():
        temporary_audio = folder / "audio.partial.wav"
        try:
            args = ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(temporary_audio)]
            if phase:
                phase("audio", 0 if duration else None)
                ffmpeg_command(args, check, duration=duration, progress=lambda p: phase("audio", p))
            else:
                command(args, check)
            temporary_audio.replace(audio)
        finally:
            temporary_audio.unlink(missing_ok=True)
    check()
    device = "cuda" if requested != "cpu" and cuda_available() else "cpu"
    if requested != "cpu" and device == "cpu" and time.monotonic() < CUDA_WHISPER.retry_after:
        warning("GPU 转写超时后暂不可用，Whisper 本次使用 CPU")
    elif requested == "cuda" and device == "cpu":
        warning("未检测到可用 GPU，Whisper 已自动切换为 CPU")

    if device == "cuda":
        if phase:
            phase("model", None)
        with MODEL_LOCK:
            MODEL = MODEL_KEY = None
        try:
            result, language, WHISPER_RUNTIME = CUDA_WHISPER.recognize(audio, model_name, check, progress)
        except Exception as exc:
            check()  # Cancellation is never converted into a CPU retry.
            reason = str(exc) if isinstance(exc, CudaWhisperTimeout) else "GPU 转写失败"
            warning(f"{reason}，Whisper 已自动切换为 CPU 重新转写")
            device = "cpu"
    if device == "cpu":
        with suppress(OSError):
            CUDA_WHISPER.close()
        if phase:
            phase("model", None)
        result, language = _recognize_whisper(audio, model_name, "cpu", check, progress)
    check()
    if not result and not allow_empty:
        raise ValueError("未识别到有效语音，请检查音轨；静音视频无法自动生成字幕")
    atomic_json(folder / "whisper-runtime.json", {"model": model_name, "device": device})
    progress(100)
    return result, language


def _recognize_whisper(audio, model_name, device, check, progress, ready=lambda: None):
    check()
    model = _load_whisper(model_name, device)
    ready()
    progress(0)
    stream, info = model.transcribe(str(audio), beam_size=5, vad_filter=True, language=None, word_timestamps=True)
    result = []
    last = 0
    for segment in stream:
        check()
        text = segment.text.strip()
        if text:
            words = getattr(segment, "words", None)
            result.extend(word_cues(words) if words else [dict(start=segment.start, end=segment.end, text=text)])
        percent = max(last, min(99, int(segment.end / max(info.duration, .001) * 100)))
        if percent > last:
            progress(percent)
            last = percent
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
    with FRAMES.slot(check):
        check()
        ffmpeg_command(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0, timestamp):.3f}", "-i", str(path),
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


def candidates(path, chapter, output, index, used, check, *, progress=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    start, end = chapter["start"], chapter["end"]
    duration = max(.1, end - start)
    def extract(item):
        n, timestamp = item
        check()
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
    latest = max(start, end - .08)
    samples = [min(latest, start + duration * fraction) for fraction in (.04, .17, .30, .43, .56, .69, .82, .94)]
    available = set(range(len(samples)))
    for anchor in chapter.get('visual_anchors', [])[:2]:
        timestamp = min(latest, anchor + .5)
        nearest = min(available, key=lambda index: (abs(samples[index] - timestamp), index))
        samples[nearest] = timestamp
        available.remove(nearest)
    samples.sort()
    images = [None] * len(samples)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(extract, (n, fraction)): n for n, fraction in enumerate(samples)}
        for completed, future in enumerate(as_completed(futures), 1):
            images[futures[future]] = future.result()
            if progress:
                progress(completed * 100 / len(samples))
    ranked = sorted((image for image in images if image), key=lambda item: (not item["duplicate"], item["quality"]), reverse=True)
    diverse = []
    for image in ranked:
        if image["quality"] > .4 and not any((image["hash"] ^ old["hash"]).bit_count() < 12 for old in diverse):
            diverse.append(image)
    return diverse[:4]


def export_video(path, target, details, check, *, caption, english_caption=None, progress):
    path, target = Path(path), Path(target)
    streams = details.get('streams', [])
    video = [stream for stream in streams if stream.get('codec_type') == 'video'
             and not stream.get('disposition', {}).get('attached_pic')]
    if not video:
        raise ValueError('该素材没有视频画面')
    size = path.stat().st_size
    args = ['ffmpeg', '-y', '-v', 'error', '-i', str(path), '-i', str(caption)]
    if english_caption is not None:
        args.extend(['-i', str(english_caption)])
    args.extend(['-map', '0:V', '-map', '0:a?', '-map', '1:s:0', '-c', 'copy', '-c:s', 'mov_text',
                 '-metadata:s:s:0', 'language=zho', '-disposition:s:0', 'default'])
    expected_languages = {'zho'}
    if english_caption is not None:
        args.extend(['-map', '2:s:0', '-metadata:s:s:1', 'language=eng', '-disposition:s:1', '0'])
        expected_languages.add('eng')
    for index, stream in enumerate(video):
        if stream.get('codec_name') not in {'h264', 'hevc', 'av1', 'vp9', 'mpeg4'}:
            args.extend([f'-c:v:{index}', 'libx264'])
    audio = [stream for stream in streams if stream.get('codec_type') == 'audio']
    for index, stream in enumerate(audio):
        if stream.get('codec_name') not in {'aac', 'mp3', 'alac'}:
            args.extend([f'-c:a:{index}', 'aac'])
    budget = int(size * 1.1)
    duration = float(details.get('format', {}).get('duration') or 0)
    with reserve_disk(target.parent, budget) as disk:
        def check_output():
            check()
            written = target.stat().st_size if target.is_file() else 0
            disk.resize(max(budget - written, 4 * 1024**2))
            ensure_disk(target.parent)

        if not duration:
            progress(0)
        ffmpeg_command([*args, '-movflags', '+faststart', str(target)], check_output,
                       duration=duration, progress=lambda percent: progress(min(99, percent)))
    exported = probe(target).get('streams', [])
    if not any(stream.get('codec_type') == 'video' for stream in exported):
        raise RuntimeError('导出的视频校验未通过')
    languages = {stream.get('tags', {}).get('language') for stream in exported if stream.get('codec_name') == 'mov_text'}
    if languages != expected_languages:
        raise RuntimeError('导出的视频字幕轨校验未通过')
    progress(100)
