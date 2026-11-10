from functools import partial
import math
import os
import re
from pathlib import Path

from . import config, exports, media, subtitles, pdf_export
from .intelligence import Intelligence
from .report_prompts import VERSION
from .storage import atomic_bytes, atomic_json, atomic_text, fingerprint, read_checkpoint, replace_file
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
        required = exports.ARTIFACTS[mode]
        if mode == 'subtitles' and job.get('subtitle_source_language') == 'en':
            required = [*required, exports.ENGLISH_SUBTITLES]
        if all(name in artifacts and not artifacts[name].get('pending') and (output/name).stat().st_size == artifacts[name]['size']
               for name,_,_ in required):
            ready.add(mode)
            store.update_output(job_id, mode, status='completed', progress=100, message='已完成', error=None)
    modes -= ready
    if not modes:
        return
    tracks = {}
    source = job["source"]
    metadata = {"title": job["title"]}
    media_path = None
    details = None
    has_video = False
    errors = {}
    progress("inspect", 3, "检查素材与已有字幕")

    if source["type"] == "url":
        metadata_path = folder / "source_info.json"
        saved_metadata = read_checkpoint(metadata_path, lambda value: isinstance(value, dict))
        if saved_metadata is not None:
            for lang in ("zh", "en", "original"):
                if (folder / f"platform.{lang}.srt").exists():
                    cues = subtitles.parse((folder / f"platform.{lang}.srt").read_text(encoding="utf-8"))
                    if cues:
                        tracks[lang] = cues
        metadata = saved_metadata if saved_metadata is not None else metadata
        if saved_metadata is None or ('zh' not in tracks and metadata.get('caption_scan') != media.CAPTION_SCAN_VERSION):
            try:
                metadata, tracks = yield Step('inspect', partial(media.inspect_url, source['url'], folder, check, warn,
                                                                resource_id=source.get('resource_id')))
            except (Cancelled, ServiceStopping):
                raise
            except Exception:
                if saved_metadata is None:
                    raise
                warn('平台字幕重新检查失败，继续使用本地素材或字幕')
        check_duration(metadata.get('duration'))
        title = metadata.get("title") or job["title"]
        folder = store.source_title(job_id, title)
        output = folder / "output"
        output.mkdir(exist_ok=True)
        intelligence.checkpoint_dir = folder / "subtitle-batches"
    else:
        media_path = folder / source["filename"]

    if source['type'] == 'url' and 'download' in modes and not tracks.get('zh'):
        # This video also supplies embedded subtitles, ASR audio and frames.
        try:
            progress('download', 8, '等待最高画质视频下载')
            media_path = yield Step('download', partial(_best_video, source, folder, check,
                lambda p: progress('download', 8 + p * .14, f'最高画质视频下载 {p}%', p)))
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            errors['download'] = config.redact(str(exc))[:1200]
            store.update_output(job_id, 'download', status='failed', message='处理失败', error=errors['download'])
            modes.remove('download')
            if not modes:
                raise OutputFailure(errors)

    if source["type"] == "url":
        if "download" in ready:
            media_path = _existing_best_video(folder) or output / "video.mp4"
        if not tracks and media_path is None:
            need_video = "illustrated" in modes
            progress("download", 8, "下载分析所需的视频" if need_video else "没有可用字幕，下载音频用于本地转写")
            media_path = yield Step('download', partial(media.download, source['url'], folder,
                best=False, audio=not need_video, check=check, resource_id=source.get('resource_id'),
                progress=lambda p: progress('download', 8 + p * .14, f'素材下载 {p}%', p)))
    else:
        if source.get("subtitle"):
            uploaded = subtitles.parse((folder / source["subtitle"]).read_text(encoding="utf-8-sig"))
            if uploaded:
                tracks[subtitles.language(uploaded)] = uploaded
    if media_path:
        details = yield Step('inspect', partial(media.probe, media_path))
        metadata["duration"] = float(details.get("format", {}).get("duration") or metadata.get("duration") or 0)
        has_video = any(s.get("codec_type") == "video" and not s.get('disposition', {}).get('attached_pic')
                        for s in details.get("streams", []))
        if 'download' in modes and not has_video:
            errors['download'] = '该素材没有视频画面'
            store.update_output(job_id, 'download', status='failed', message='处理失败', error=errors['download'])
            modes.remove('download')
            if not modes:
                raise OutputFailure(errors)
        if source['type'] == 'upload' and not has_video and modes == {'illustrated'}:
            raise ValueError('该素材没有视频画面，请改选文字汇报、思维导图或字幕文件')
        if not tracks.get('zh') and media_path != output / 'video.mp4':
            embedded = yield Step('media', partial(media.embedded_subtitles, media_path, folder, check, warn, details.get('streams', [])))
            tracks = embedded | tracks
    check_duration(metadata.get('duration'))
    from_asr = not tracks
    if from_asr:
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
        tracks[subtitles.language_code(lang)] = speech_data["segments"]
        transcript_source = "Whisper 本地转写"
        store.update(job_id, whisper_model=speech_data.get("model", "small"), whisper_device=speech_data.get("device", "cpu"))
    else:
        transcript_source = "素材自带字幕"
    language = next((lang for lang in ('zh', 'en', 'original') if tracks.get(lang)), None)
    if language is None:
        raise ValueError("字幕为空，无法继续处理")
    raw = tracks[language]
    metadata["duration"] = max(float(metadata.get("duration") or 0), max(s["end"] for s in raw))
    check_duration(metadata['duration'])
    atomic_text(folder / "subtitles.original.srt", subtitles.srt(raw))
    store.update(job_id, transcript_source=transcript_source, subtitle_source_language=language, duration=metadata["duration"])
    progress("transcript", 45, f"已取得字幕：{transcript_source}")
    model_cache = folder / 'model-checkpoints' / fingerprint(VERSION, intelligence.model_identity())[:24]
    zh = raw
    if from_asr and options.get('clean_subtitles', True):
        if os.getenv('ASR_CLEANUP_MODE', 'local').strip().lower() == 'model':
            zh = yield Step('model', partial(_transform_subtitles, zh, intelligence, model_cache, progress,
                                             clean=True, original=language != 'zh'))
        else:
            progress('clean', 46, '本地清理转写字幕', 0)
            zh = subtitles.clean_transcript(zh)
            if not zh:
                raise ValueError('字幕为空或仅含语气词，原始字幕已保留')
            progress('clean', 50, '本地清理转写字幕', 100)
    english = zh if language == 'en' else None
    if language != 'zh':
        zh = yield Step('model', partial(_transform_subtitles, zh, intelligence, model_cache, progress))
    zh = [dict(cue, text=subtitles.simplified(cue['text'])) for cue in zh]
    atomic_text(folder / 'subtitles.zh.srt', subtitles.srt(zh))
    english_caption = None
    if english is not None and 'download' in modes:
        english_caption = folder / 'subtitles.en.srt'
        atomic_text(english_caption, subtitles.srt(english))

    if 'subtitles' in modes:
        if english is not None:
            exports.write_text(output / exports.ENGLISH_SUBTITLES[0], subtitles.timestamped_text(english))
        exports.write_text(output / 'subtitles.zh.txt', subtitles.timestamped_text(zh))
        exports.publish(store, job_id)
        store.update_output(job_id, 'subtitles', status='completed', progress=100, message='已完成', error=None)
    pending_outputs = modes - {'subtitles'}
    if pending_outputs:
        output_errors = yield Step('outputs', partial(_deliver_outputs, store, job_id, folder, output, pending_outputs,
            options, intelligence, zh, english_caption, metadata, media_path if has_video else None, details, source, check, warn, model_cache))
        errors.update(output_errors)
    progress("export", 99, "校验生成文件与资源路径")
    provenance = {"title": metadata.get("title"), "source": source.get("url") or source.get("original_name"),
                  "transcript_source": transcript_source, "language": "zh-CN",
                  "whisper_model": store.get(job_id).get("whisper_model"),
                  "warnings": store.get(job_id)["warnings"]}
    atomic_json(folder / 'source.json', provenance)
    exports.publish(store, job_id)
    if errors:
        raise OutputFailure(errors)


def _transform_subtitles(cues, intelligence, model_cache, progress, *, clean=False, original=False):
    if clean:
        stage, label, start, span = 'clean', '转写字幕清洗', 46, 4
        name = 'cleaned.original' if original else 'cleaned.zh'
        options = {'clean': True}
    else:
        stage, label, start, span = 'translate', '翻译中文字幕', 50, 5
        name = 'translated.zh'
        options = {'language': 'zh'}
    progress(stage, start, label, 0)
    result = cached(model_cache / f'{name}-{fingerprint(cues)[:24]}.json',
                    lambda: intelligence.transform(cues, **options,
                        progress=lambda p: progress(stage, start + p * span, f'{label} {round(p * 100)}%', p * 100)),
                    valid_cues)
    if not result:
        raise ValueError('字幕为空或仅含语气词，原始字幕已保留')
    progress(stage, start + span, label, 100)
    return result


def _deliver_outputs(store, job_id, folder, output, modes, options, intelligence, zh, english_caption, metadata, media_path,
                     details, source, check, warn, model_cache):

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .output_progress import OutputProgress
    tracker = OutputProgress(store, job_id, modes)
    branches = {}
    with ThreadPoolExecutor(max_workers=3) as parallel:
        shared_video = None
        if source['type'] == 'url' and 'illustrated' in modes and media_path is None:
            download_progress = tracker.for_branch('report', {'illustrated'})
            progress = lambda p: download_progress('download', 60 + p * .04, f'配图素材下载 {p}%', p)
            if 'download' in modes:
                shared_video = parallel.submit(_best_video, source, folder, check, progress)
            else:
                shared_video = parallel.submit(media.download, source['url'], folder, best=False, audio=False, check=check,
                                               resource_id=source.get('resource_id'), progress=progress)
        report_modes = modes - {'download'}
        if report_modes:
            cache_dir = model_cache / f'report-{fingerprint(zh, options, metadata)[:24]}'
            future = parallel.submit(_report_outputs, store, job_id, folder, output, report_modes, options,
                intelligence, zh, metadata, media_path, check, warn, tracker.for_branch("report"), tracker,
                cache_dir, shared_video)
            branches[future] = report_modes
        if "download" in modes:
            future = parallel.submit(_video_output, store, job_id, folder, output, source, media_path, details,
                                     check, tracker.for_branch("video"), shared_video, english_caption)
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
                states = store.get(job_id)['output_states']
                for mode in branches[future]:
                    if states[mode]['status'] == 'completed':
                        continue
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
    plan = dict(plan, title=subtitles.simplified(plan['title']), chapters=[
        dict(chapter, title=subtitles.simplified(chapter['title']))
        for chapter in plan['chapters']
    ])
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
        body = exports.grounded_body(subtitles.simplified(body), evidence)
        body = re.sub(r"(?m)^#{1,2}\s+[^\n]*\n?", "", body).strip()
        body = re.sub(r"\n+\s*[（(]注[:：]\s*(?:已删除|已核查|已修订|已去除|已移除)[\s\S]*?[）)]\s*$", "", body).strip()
        start = subtitles.stamp(chapter['start']).split(',')[0]
        end = subtitles.stamp(chapter['end']).split(',')[0]
        heading = f"## {chapter['title']}\n\n*{start} — {end}*\n\n"
        sections.append((chapter, heading, body))
        fraction = (index + 1) / len(chapters)
        progress('report', 63 + fraction * 25, f'已完成 {index + 1}/{len(chapters)} 章正文', fraction * 100)

    report_modes = modes & {'text', 'illustrated'}

    def summarize():
        summary_progress = tracker.for_branch('report', report_modes - {'illustrated'})
        summary_progress('overview', 89, '提炼技术结论与关键数据')
        overview = cached(cache_dir / f'overview-{fingerprint(sections)[:24]}.json',
                          lambda: intelligence.overview([(chapter, body) for chapter, _, body in sections]),
                          valid_overview)
        title = re.sub(r"[<>\n]", "", str(plan['title']))
        lead = f"# {title}\n\n**一句话总结：** {subtitles.simplified(overview['one_sentence'])}\n\n"
        if overview['key_conclusions']:
            lead += "## 核心结论\n\n" + "\n".join("- " + subtitles.simplified(v) for v in overview['key_conclusions']) + "\n\n"
        lead += "**目录**\n\n[TOC]\n\n"
        terms = ""
        if overview['data_terms']:
            terms = "\n\n## 关键数据与术语\n\n" + "\n".join("- " + subtitles.simplified(v) for v in overview['data_terms'])
        return lead, terms

    def mindmap():
        check()
        update_pdf = tracker.for_branch('report', {'mindmap'})
        update_pdf('mindmap', 91, '生成思维导图 PDF')
        outline = exports.report_outline(plan, [body for _, _, body in sections])
        exports.mindmap_data(folder / 'mindmap.json', outline)
        pdf_export.render(folder, check, progress=lambda p: update_pdf('mindmap', 91 + p * .02, '生成思维导图 PDF', p))

    def produce(mode, generate):
        try:
            generate()
            exports.publish(store, job_id)
            tracker.done(mode)
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            error = config.redact(str(exc))[:1200]
            tracker.failed(mode, error)
            return error

    errors = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        summary = pool.submit(summarize) if report_modes else None
        futures = {}
        if 'mindmap' in modes:
            futures[pool.submit(produce, 'mindmap', mindmap)] = 'mindmap'
        if 'illustrated' in modes:
            generate = partial(_illustrate, folder, output, sections, intelligence, media_path, metadata,
                               summary, check, warn, tracker.for_branch('report', {'illustrated'}), shared_video)
            futures[pool.submit(produce, 'illustrated', generate)] = 'illustrated'
        if 'text' in modes:
            try:
                lead, terms = summary.result()
                check()
                exports.report(output, 'text', lead + "\n\n".join(heading + body for _, heading, body in sections) + terms)
                exports.publish(store, job_id)
                tracker.done('text')
            except (Cancelled, ServiceStopping):
                raise
            except Exception as exc:
                errors['text'] = config.redact(str(exc))[:1200]
                tracker.failed('text', errors['text'])
        for future in as_completed(futures):
            error = future.result()
            if error is not None:
                errors[futures[future]] = error
    return errors


def _illustrate(folder, output, sections, intelligence, media_path, metadata, summary, check, warn, progress, shared_video):
    if shared_video is not None:
        media_path = shared_video.result()
    if media_path is None:
        raise ValueError('图文汇报需要视频画面')
    progress('illustrated_cover', 89, '准备汇报封面')
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
        if summary.done():
            summary.result()
        chapter_label = f'第 {index + 1}/{len(sections)} 章'
        chapter_progress = 89 + index / len(sections) * 3
        def frame_progress(percent):
            progress('illustrated_frames', chapter_progress, f'{chapter_label}抽取候选画面', percent)

        frame_progress(0)
        images = media.candidates(media_path, chapter, folder / f'frames-{VERSION}', index, used, check, progress=frame_progress)
        ranked = [dict(candidate=image, caption='原视频画面 · ' + chapter['title'], reason='清晰度与去重筛选') for image in images]
        if images and intelligence.config.get('use_vlm', True):
            progress('illustrated_review', chapter_progress, f'{chapter_label}按正文评选画面')
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
            caption = re.sub(r"[\[\]<>\n]", "", subtitles.simplified(str(choice.get('caption') or chapter['title'])))
            stamp = subtitles.stamp(selected['time']).split(',')[0]
            figure = f"\n\n![{caption}]({relative})\n\n*{caption} · 视频 {stamp}*\n\n"
            selections.append(dict(chapter=chapter['title'], image=relative, time=selected['time'],
                                   caption=caption, reason=choice.get('reason', ''), score=choice.get('score')))
        else:
            warn(f'第 {index + 1} 章没有清晰、相关且不重复的画面，未强行插图')
        opening, separator, remainder = body.partition("\n\n")
        illustrated.append(heading + opening + figure + (separator + remainder if separator else ''))
        completed = (index + 1) / len(sections)
        progress('illustrate', 89 + completed * 3, f'已完成 {index + 1}/{len(sections)} 章配图', completed * 100)
    atomic_json(folder / 'image-selections.json', selections)
    if not summary.done():
        progress('illustrated_summary', 92, '等待汇报摘要')
    lead, terms = summary.result()
    progress('illustrated_bundle', 92, '打包图文汇报')
    front = "![视频封面](image/cover.jpg)\n\n" if (output / 'image/cover.jpg').is_file() else ''
    exports.report(output, 'illustrated', lead + front + "\n\n".join(illustrated) + terms)
    exports.bundle(output)


def _video_output(store, job_id, folder, output, source, path, details, check, progress, shared_video, english_caption):
    if path is None and shared_video is not None:
        progress('download', 93, '等待共用视频下载')
        path = shared_video.result()
    if path is None:
        progress('download', 93, '等待最高画质视频下载')
        path = _best_video(source, folder, check,
            lambda p: progress('download', 93 + p * .02, f'最高画质视频下载 {p}%', p))
    details = details or media.probe(path)
    check_duration(details.get('format', {}).get('duration'))
    temporary = folder / 'video.partial.mp4'
    temporary.unlink(missing_ok=True)
    try:
        media.export_video(path, temporary, details, check, caption=folder / 'subtitles.zh.srt',
            english_caption=english_caption,
            progress=lambda p: progress('export_video', 96 + p * .02, f'封装视频字幕 {p}%', p))
        check()
        replace_file(temporary, output / 'video.mp4')
    finally:
        temporary.unlink(missing_ok=True)
    exports.publish(store, job_id)


def _existing_best_video(folder):
    for target in (folder / 'original', folder):
        checkpoint = read_checkpoint(target / 'download-complete.json', lambda value: isinstance(value, dict)) or {}
        name = checkpoint.get('filename')
        if (checkpoint.get('best') and not checkpoint.get('audio') and isinstance(name, str)
                and Path(name).name == name and (target / name).is_file()):
            return target / name
    return None


def _best_video(source, folder, check, progress):
    cached_video = _existing_best_video(folder)
    target = cached_video.parent if cached_video else folder / 'original'
    return media.download(source['url'], target, best=True, audio=False, check=check,
                          progress=progress, resource_id=source.get('resource_id'))
