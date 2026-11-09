import json
from functools import partial
import math
import os
import re
from pathlib import Path

from . import config, exports, media, subtitles, pdf_export
from .intelligence import Intelligence
from .report_prompts import VERSION
from .storage import atomic_bytes, atomic_json, atomic_text, fingerprint, read_checkpoint
from .jobs import Cancelled, ServiceStopping, OutputFailure
from .stages import Step, drive


def cached(path, generate, valid=lambda value: isinstance(value, (dict, list))):
    value = read_checkpoint(path, valid)
    if value is not None:
        return value
    value = generate()
    atomic_json(path, value)
    return value


def valid_cues(value):
    return isinstance(value, list) and all(
        isinstance(cue, dict) and isinstance(cue.get('text'), str)
        and all(isinstance(cue.get(key), (float, int)) and math.isfinite(cue[key]) for key in ('start', 'end'))
        and 0 <= cue['start'] < cue['end'] for cue in value)


def valid_overview(value):
    return (isinstance(value, dict) and isinstance(value.get('one_sentence'), str)
            and all(isinstance(value.get(key), list) and all(isinstance(item, str) for item in value[key])
                    for key in ('key_conclusions', 'data_terms')))


def check_duration(duration):
    duration = float(duration or 0)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('素材时长无效')
    maximum = int(os.getenv('MAX_DURATION_HOURS', '24')) * 3600
    if maximum and duration > maximum:
        raise ValueError('素材时长超过 MAX_DURATION_HOURS 限制，请调整部署配置后重试')


def run(store, job_id):
    return drive(steps(store, job_id, inline=True))


def steps(store, job_id, *, inline=False):
    job = store.get(job_id)
    folder = store.directory(job_id)
    output = folder / "output"
    output.mkdir(exist_ok=True)
    check = lambda: store.check(job_id)
    warn = lambda message: store.warn(job_id, message)
    def progress(stage, percent, message, stage_percent=None):
        store.progress(job_id, stage, percent, message, stage_progress=stage_percent)
    modes, options = set(job["modes"]), job["options"]
    exports.normalize_outputs(folder, modes)
    intelligence = Intelligence(store.settings(job_id), check, checkpoint_dir=folder / "subtitle-batches", warning=warn)
    exports.publish(store, job_id)
    ready = set()
    artifacts = {entry['name']:entry for entry in store.get(job_id)['artifacts']}
    for mode in modes:
        if all(name in artifacts and not artifacts[name].get('pending') and (output/name).stat().st_size == artifacts[name]['size']
               for name,_,_ in exports.ARTIFACTS[mode]):
            ready.add(mode)
            store.update_output(job_id, mode, status='completed', progress=100, message='已完成', error=None)
    modes -= ready
    if not modes:
        return
    tracks = {}
    source = job["source"]
    metadata = {"title": job["title"]}
    media_path = None
    has_video = False
    progress("inspect", 3, "检查素材与已有字幕")

    if source["type"] == "url":
        metadata_path = folder / "source_info.json"
        saved_metadata = read_checkpoint(metadata_path, lambda value: isinstance(value, dict))
        if saved_metadata is not None:
            metadata = saved_metadata
            for lang in ("zh", "en"):
                if (folder / f"platform.{lang}.srt").exists():
                    tracks[lang] = subtitles.parse((folder / f"platform.{lang}.srt").read_text(encoding="utf-8"))
        else:
            metadata, tracks = yield Step('inspect', partial(media.inspect_url, source['url'], folder, check, warn))
        check_duration(metadata.get('duration'))
        title = metadata.get("title") or job["title"]
        folder = store.source_title(job_id, title)
        output = folder / "output"
        output.mkdir(exist_ok=True)
        intelligence.checkpoint_dir = folder / "subtitle-batches"
        has_reports = bool(modes & {"illustrated", "text", "mindmap"})
        need_video = "illustrated" in modes or ("download" in modes and not has_reports)
        if not tracks:
            progress("download", 8, "下载最高画质视频" if "download" in modes and not has_reports else "下载分析所需的视频" if need_video else "没有可用字幕，下载音频用于本地转写", 0)
            media_path = yield Step('download', partial(media.download, source['url'], folder,
                best='download' in modes and need_video, audio=not need_video, check=check,
                progress=lambda p: progress('download', 8 + p * .14, f'素材下载 {p}%', p)))
    else:
        media_path = folder / source["filename"]
        if source.get("subtitle"):
            uploaded = subtitles.parse((folder / source["subtitle"]).read_text(encoding="utf-8-sig"))
            if uploaded:
                tracks[subtitles.language(uploaded)] = uploaded
    if media_path:
        details = yield Step('inspect', partial(media.probe, media_path))
        metadata["duration"] = float(details.get("format", {}).get("duration") or metadata.get("duration") or 0)
        has_video = any(s.get("codec_type") == "video" for s in details.get("streams", []))
        if source['type'] == 'upload' and not has_video and modes <= {'download', 'illustrated'}:
            raise ValueError('该素材没有视频画面，请改选文字汇报、思维导图或字幕文件')
        if not tracks:
            tracks = yield Step('inspect', partial(media.embedded_subtitles, media_path, folder, check))
    check_duration(metadata.get('duration'))
    if not tracks:
        if media_path is None:
            raise ValueError("未找到可供转写的音视频")
        if not metadata.get('duration'):
            raise ValueError('无法确认素材时长，请重新导出后再转写，或上传配套字幕')
        model_name = intelligence.config.get("whisper_model", "small")
        progress("transcribe", 22, "读取本地转写结果" if (folder / "whisper.json").exists() else "准备 Whisper 本地转写")

        speech_path = folder / 'whisper.json'
        speech_data = read_checkpoint(speech_path, lambda value: isinstance(value, dict)
            and value.get('model', 'small') == model_name and isinstance(value.get('language'), str)
            and valid_cues(value.get('segments')) and bool(value['segments']))
        if speech_data is None:
            def prepare(phase, percent):
                if phase == "audio":
                    progress("audio", 22 + (percent or 0) * .02, "提取转写音轨", percent)
                elif phase == "model":
                    progress("whisper_model", 24, "加载 Whisper 模型（首次使用可能需要下载）")
                elif phase == "waiting":
                    progress("transcribe_wait", 22, "等待 Whisper 转写资源")
            args = (media_path, folder, check, lambda p: progress('transcribe', 24 + p * .2, f'Whisper 转写 {p}%', p))
            kwargs = dict(settings=intelligence.config, warning=warn, phase=prepare, duration=metadata.get('duration'))
            if inline:
                segments, lang = media.transcribe(*args, **kwargs)
            else:
                segments, lang = yield from media.transcribe_steps(*args, **kwargs)
            runtime_path = folder / "whisper-runtime.json"
            runtime = read_checkpoint(runtime_path, lambda value: isinstance(value, dict)) or {}
            speech_data = {"segments": segments, "language": lang, "model": model_name, "device": runtime.get("device", "cpu")}
            atomic_json(speech_path, speech_data)
        progress("transcribe", 44, "Whisper 转写完成", 100)
        lang = speech_data["language"]
        tracks["zh" if lang.startswith("zh") else "en" if lang.startswith("en") else "original"] = speech_data["segments"]
        transcript_source = "Whisper 本地转写"
        store.update(job_id, whisper_model=speech_data.get("model", "small"), whisper_device=speech_data.get("device", "cpu"))
    else:
        transcript_source = "素材自带字幕"
    raw = tracks.get("zh") or tracks.get("en") or tracks.get("original")
    if not raw:
        raise ValueError("字幕为空，无法继续处理")
    metadata["duration"] = max(float(metadata.get("duration") or 0), max(s["end"] for s in raw))
    check_duration(metadata['duration'])
    (folder / "subtitles.original.srt").write_text(subtitles.srt(raw), encoding="utf-8")
    store.update(job_id, transcript_source=transcript_source, duration=metadata["duration"])
    progress("transcript", 45, f"已取得字幕：{transcript_source}")
    model_cache = folder / 'model-checkpoints' / fingerprint(VERSION, intelligence.model_identity())[:24]
    zh = tracks.get("zh")
    if zh is None:
        progress("translate", 46, "翻译中文字幕", 0)
        zh = yield Step('model', partial(cached, model_cache / f'translated.zh-{fingerprint(raw)[:24]}.json',
            lambda: intelligence.transform(raw, language='zh', progress=lambda p: progress('translate', 46 + p * 2, f'翻译中文字幕 {round(p * 100)}%', p * 100)), valid_cues))
        progress("translate", 48, "翻译中文字幕", 100)
    if options.get("clean_subtitles", True):
        progress("clean", 49, "字幕清洗", 0)
        zh = yield Step('model', partial(cached, model_cache / f'cleaned.zh-{fingerprint(zh)[:24]}.json',
            lambda: intelligence.transform(zh, clean=True, progress=lambda p: progress('clean', 49 + p * 6, f'字幕清洗 {round(p * 100)}%', p * 100)), valid_cues))
        progress("clean", 55, "字幕清洗", 100)
    if not zh:
        raise ValueError("字幕仅含语气词，没有可生成汇报的有效内容；原始字幕已保留")
    (folder / "subtitles.zh.srt").write_text(subtitles.srt(zh), encoding="utf-8")
    if "subtitles" in modes:
        text = "\n\n".join(f"[{subtitles.stamp(s['start'])} --> {subtitles.stamp(s['end'])}]\n{s['text']}" for s in zh)
        exports.write_text(output / "subtitles.zh.txt", text)
        exports.publish(store, job_id)

    errors = {}
    if modes - {'subtitles'}:
        errors = yield Step('outputs', partial(_deliver_outputs, store, job_id, folder, output, modes, options,
                                               intelligence, zh, metadata, media_path if has_video else None,
                                               tracks, source, check, warn, model_cache))
    else:
        store.update_output(job_id, 'subtitles', status='completed', progress=100, message='已完成', error=None)

    progress("export", 99, "校验生成文件与资源路径")
    provenance = {"title": metadata.get("title"), "source": source.get("url") or source.get("original_name"),
                  "transcript_source": transcript_source, "language": "zh-CN", "whisper_model": store.get(job_id).get("whisper_model"),
                  "warnings": store.get(job_id)["warnings"]}
    atomic_json(folder / 'source.json', provenance)
    exports.publish(store, job_id)
    if errors:
        raise OutputFailure(errors)


def _deliver_outputs(store, job_id, folder, output, modes, options, intelligence, zh, metadata, media_path, tracks,
                     source, check, warn, model_cache):

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .output_progress import OutputProgress
    tracker = OutputProgress(store, job_id, modes)
    if "subtitles" in modes:
        tracker.done("subtitles")
    branches = {}
    with ThreadPoolExecutor(max_workers=3) as parallel:
        shared_video = None
        if source['type'] == 'url' and 'illustrated' in modes and media_path is None:
            best = 'download' in modes
            target = folder / 'original' if best else folder
            download_progress = tracker.for_branch('report', {'illustrated'})
            shared_video = parallel.submit(media.download, source['url'], target, best=best, audio=False, check=check,
                progress=lambda p: download_progress('download', 60 + p * .04, f'配图素材下载 {p}%', p))
        report_modes = modes & {"illustrated", "text", "mindmap"}
        if report_modes:
            future = parallel.submit(_report_outputs, store, job_id, folder, output, modes, options,
                intelligence, zh, metadata, media_path, check, warn, tracker.for_branch("report"), tracker,
                model_cache / f'report-{fingerprint(zh, options, metadata)[:24]}', shared_video)
            branches[future] = report_modes
        if "download" in modes:
            future = parallel.submit(_video_output, store, job_id, folder, output, modes, intelligence,
                zh, tracks, source, media_path, check, tracker.for_branch("video"), model_cache, shared_video)
            branches[future] = {"download"}
        errors = {}
        for future in as_completed(branches):
            try:
                branch_errors = future.result() or {}
                errors.update(branch_errors)
                for mode in branches[future]:
                    if mode in branch_errors:
                        tracker.failed(mode, branch_errors[mode])
                    else:
                        tracker.done(mode)
            except (Cancelled, ServiceStopping):
                raise
            except Exception as exc:
                for mode in branches[future]:
                    errors[mode] = config.redact(str(exc))[:1200]
                    tracker.failed(mode, errors[mode])
            exports.publish(store, job_id)

    return errors


def _report_outputs(store, job_id, folder, output, modes, options, intelligence, zh, metadata, media_path,
                    check, warn, progress, tracker, cache_dir, shared_video):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    progress("chapters", 60, "按完整内容组织中文汇报章节")
    plan = intelligence.plan(zh, metadata["duration"], options, metadata, cache_dir / f"plan-{VERSION}.json",
                             lambda p: progress("chapters", 60 + p * 3, "组织汇报章节", p * 100))
    store.update(job_id, report_title=plan["title"])
    atomic_json(folder / 'chapters.json', plan)
    chapters = plan['chapters']
    sections = []
    progress('report', 63, f'已完成 0/{len(chapters)} 章正文', 0)
    for index, chapter in enumerate(chapters):
        check()
        draft_path = cache_dir / f'draft-{index}-{fingerprint(chapter)[:24]}.md'
        try:
            body = draft_path.read_text(encoding='utf-8')
        except (FileNotFoundError, UnicodeDecodeError):
            body = intelligence.draft(chapter, zh, options)
            body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
            atomic_text(draft_path, body)
        evidence = "\n".join(s['text'] for s in zh if s['end'] > chapter['start'] and s['start'] < chapter['end'])
        body = exports.grounded_body(body, evidence)
        body = re.sub(r"(?m)^#{1,2}\s+[^\n]*\n?", "", body).strip()
        body = re.sub(r"\n+\s*[（(]注[:：]\s*(?:已删除|已核查|已修订|已去除|已移除)[\s\S]*?[）)]\s*$", "", body).strip()
        start = subtitles.stamp(chapter['start']).split(',')[0]
        end = subtitles.stamp(chapter['end']).split(',')[0]
        heading = f"## {chapter['title']}\n\n*{start} — {end}*\n\n"
        sections.append((chapter, heading, body))
        fraction = (index + 1) / len(chapters)
        progress('report', 63 + fraction * 25, f'已完成 {index + 1}/{len(chapters)} 章正文', fraction * 100)

    errors = {}
    report_modes = modes & {'text', 'illustrated'}
    lead, terms = '', ''
    if report_modes:
        try:
            progress('overview', 89, '提炼技术结论与关键数据')
            overview = cached(cache_dir / f'overview-{fingerprint(sections)[:24]}.json',
                              lambda: intelligence.overview([(chapter, body) for chapter, _, body in sections]),
                              valid_overview)
            title = re.sub(r"[<>\n]", "", str(plan['title']))
            lead = f"# {title}\n\n**一句话总结：** {overview['one_sentence']}\n\n"
            if overview['key_conclusions']:
                lead += "## 核心结论\n\n" + "\n".join("- " + v for v in overview['key_conclusions']) + "\n\n"
            lead += "**目录**\n\n[TOC]\n\n"
            if overview['data_terms']:
                terms = "\n\n## 关键数据与术语\n\n" + "\n".join("- " + v for v in overview['data_terms'])
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            errors.update({mode: config.redact(str(exc))[:1200] for mode in report_modes})

    if 'text' in modes and 'text' not in errors:
        try:
            check()
            exports.report(output, 'text', lead + "\n\n".join(heading + body for _, heading, body in sections) + terms)
            exports.publish(store, job_id)
            tracker.done('text')
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            errors['text'] = config.redact(str(exc))[:1200]

    def mindmap():
        check()
        outline = exports.report_outline(plan, [body for _, _, body in sections])
        exports.mindmap_data(folder / 'mindmap.json', outline)
        tracker.for_branch('report', {'mindmap'})('mindmap', 91, '生成思维导图 PDF')
        pdf_export.render(folder, check)

    jobs = {}
    if 'mindmap' in modes:
        jobs['mindmap'] = mindmap
    if 'illustrated' in modes and 'illustrated' not in errors:
        jobs['illustrated'] = partial(_illustrate, folder, output, sections, intelligence, media_path, metadata,
                                      lead, terms, check, warn, tracker.for_branch('report', {'illustrated'}), shared_video)
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {pool.submit(generate): mode for mode, generate in jobs.items()}
            for future in as_completed(futures):
                mode = futures[future]
                try:
                    future.result()
                    exports.publish(store, job_id)
                    tracker.done(mode)
                except (Cancelled, ServiceStopping):
                    raise
                except Exception as exc:
                    errors[mode] = config.redact(str(exc))[:1200]
    return errors


def _illustrate(folder, output, sections, intelligence, media_path, metadata, lead, terms, check, warn, progress, shared_video):
    if shared_video is not None:
        media_path = shared_video.result()
    if media_path is None:
        raise ValueError('图文汇报需要视频画面')
    cover = folder / 'cover.jpg'
    if not cover.is_file() and metadata.get('thumbnail'):
        try:
            media.cover(metadata['thumbnail'], folder)
        except Exception as exc:
            check()
            warn(f'封面下载失败：{type(exc).__name__}')
    if not cover.is_file() and media_path:
        try:
            media.frame(media_path, min(1.0, metadata['duration'] / 2), cover, check)
        except Exception:
            check()
    if cover.is_file():
        atomic_bytes(output / 'image' / 'cover.jpg', cover.read_bytes())

    used, selections, illustrated = [], [], []
    for index, (chapter, heading, body) in enumerate(sections):
        check()
        progress('illustrate', 89 + index / len(sections) * 3,
                 f'已完成 {index}/{len(sections)} 章配图', index / len(sections) * 100)
        images = media.candidates(media_path, chapter, folder / f'frames-{VERSION}', index, used, check)
        ranked = [dict(candidate=image, caption='原视频画面 · ' + chapter['title'], reason='清晰度与去重筛选') for image in images]
        if images and intelligence.config.get('use_vlm', True):
            try:
                ranked = intelligence.select(chapter, images, body)
            except Exception as exc:
                check()
                warn(f'第 {index + 1} 章视觉评选不可用，采用清晰度选帧：{type(exc).__name__}')
        choice = next((entry for entry in ranked if not any(
            (entry['candidate']['hash'] ^ other).bit_count() < 12 for other in used)), None)
        figure = ''
        if choice:
            selected = choice['candidate']
            used.append(selected['hash'])
            relative = f'image/chapter-{index + 1:02d}.jpg'
            atomic_bytes(output / relative, Path(selected['path']).read_bytes())
            caption = re.sub(r"[\[\]<>\n]", "", str(choice.get('caption') or chapter['title']))
            stamp = subtitles.stamp(selected['time']).split(',')[0]
            figure = f"\n\n![{caption}]({relative})\n\n*{caption} · 视频 {stamp}*\n\n"
            selections.append(dict(chapter=chapter['title'], image=relative, time=selected['time'],
                                   caption=caption, reason=choice.get('reason', ''), score=choice.get('score')))
        else:
            warn(f'第 {index + 1} 章没有清晰、相关且不重复的画面，未强行插图')
        opening, separator, remainder = body.partition("\n\n")
        illustrated.append(heading + opening + figure + (separator + remainder if separator else ''))
    atomic_json(folder / 'image-selections.json', selections)
    front = "![视频封面](image/cover.jpg)\n\n" if (output / 'image/cover.jpg').is_file() else ''
    exports.report(output, 'illustrated', lead + front + "\n\n".join(illustrated) + terms)
    exports.bundle(output)


def _video_output(store, job_id, folder, output, modes, intelligence, zh, tracks, source, media_path, check, progress, model_cache, shared_video):
    progress("bilingual", 93, "准备中英双字幕轨")
    if shared_video is not None:
        progress('download', 93, '等待共用视频下载')
        media_path = shared_video.result()
    elif source["type"] == "url" and (media_path is None or not _best_video_ready(media_path, folder)):
        progress("download", 93, "下载最高画质视频", 0)
        original = folder / "original"
        original.mkdir(exist_ok=True)
        media_path = media.download(source["url"], original, best=True, audio=False, check=check,
                                    progress=lambda p: progress("download", 93 + p * .01, f"最高画质视频下载 {p}%", p))
    if media_path is None:
        raise ValueError('该素材没有视频画面，无法生成带双字幕轨的视频')
    en = tracks.get("en")
    if en is None:
        progress("translate_en", 94, "翻译英文字幕", 0)
        en = cached(model_cache / f"translated.en-{fingerprint(zh)[:24]}.json", lambda: intelligence.transform(zh, language="en",
                    progress=lambda p: progress("translate_en", 94 + p, f"翻译英文字幕 {round(p * 100)}%", p * 100)), valid_cues)
        progress("translate_en", 95, "翻译英文字幕", 100)
    (folder / "subtitles.en.srt").write_text(subtitles.srt(en), encoding="utf-8")
    final_video = output / "video.mp4"
    if not final_video.exists():
        progress("embed", 95, "封装 MP4 视频与可切换中英字幕轨", 0)
        temporary = folder / "video.partial.mp4"
        temporary.unlink(missing_ok=True)
        try:
            media.embed(media_path, folder / "subtitles.zh.srt", folder / "subtitles.en.srt", temporary, check,
                        progress=lambda p: progress("embed", 95 + p * .03, f"视频封装 {p}%", p))
            temporary.replace(final_video)
        finally:
            temporary.unlink(missing_ok=True)
        progress("embed", 98, "视频封装完成", 100)
    exports.publish(store, job_id)


def _best_video_ready(path, folder):
    marker = Path(path).parent / "download-complete.json"
    if not marker.is_file():
        return False
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value.get("best") and not value.get("audio") and value.get("filename") == Path(path).name and value.get('size', Path(path).stat().st_size) == Path(path).stat().st_size
    except (OSError, ValueError, AttributeError):
        return False
