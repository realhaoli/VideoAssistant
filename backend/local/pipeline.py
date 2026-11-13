from hashlib import sha256
from functools import partial
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import config, exports, media, subtitles, pdf_export
from .intelligence import Intelligence
from .output_progress import OutputProgress
from .report_prompts import VERSION
from .storage import atomic_bytes, atomic_json, atomic_text, fingerprint, read_checkpoint, replace_file
from .jobs import Cancelled, ServiceStopping, OutputFailure
from .stages import ParallelSteps, Step, drive


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
    modes, options = set(job["modes"]), job["options"]
    exports.recover_report_installs(folder)
    exports.normalize_outputs(folder, modes)
    model_settings = store.settings(job_id)
    intelligence = Intelligence(model_settings, check, checkpoint_dir=folder / "subtitle-batches", warning=warn)
    exports.publish(store, job_id)
    ready = set()
    artifacts = {entry['name']:entry for entry in store.get(job_id)['artifacts']}
    for mode in modes:
        if (folder / f'{mode}.summary-pending').exists():
            continue
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
    progress = OutputProgress(store, job_id, modes)
    cleanup_mode = model_settings.get('asr_cleanup_mode', config.default_asr_cleanup_mode())
    cleanup_enabled = options.get('clean_subtitles', True)
    if cleanup_mode != 'model':
        progress.weights['clean'] = 1
    url_modes = modes if source['type'] == 'url' else set()
    progress.configure(download=url_modes, audio=modes, whisper_model=modes, transcribe=modes,
                       clean=modes if cleanup_enabled else set(), translate=modes & {'subtitles', 'download'})
    progress("inspect", "检查素材与已有字幕")

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

    if tracks:
        progress.configure(audio=set(), whisper_model=set(), transcribe=set(), clean=set())
    early_download = source['type'] == 'url' and 'download' in modes and not tracks
    if early_download:
        # This video also supplies embedded subtitles, ASR audio and frames.
        try:
            progress('download', '等待最高画质视频下载')
            media_path = yield Step('download', partial(_best_video, source, folder, check,
                lambda p: progress('download', f'最高画质视频下载 {p}%', p)))
            progress.complete('download')
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            errors['download'] = config.redact(str(exc), settings=model_settings)[:1200]
            store.update_output(job_id, 'download', status='failed', message='处理失败', error=errors['download'])
            modes.remove('download')
            if not modes:
                raise OutputFailure(errors)

    if source["type"] == "url":
        if "download" in ready:
            media_path = _existing_best_video(folder) or output / "video.mp4"
        if not tracks and media_path is None:
            progress.reset('download')
            progress.configure(download=modes)
            need_video = "illustrated" in modes
            progress("download", "下载分析所需的视频" if need_video else "没有可用字幕，下载音频用于本地转写")
            media_path = yield Step('download', partial(media.download, source['url'], folder,
                best=False, audio=not need_video, check=check, resource_id=source.get('resource_id'),
                progress=lambda p: progress('download', f'素材下载 {p}%', p)))
            progress.complete('download')
        elif not early_download or media_path is None:
            late_modes = modes & {'download', 'illustrated'} if media_path is None else set()
            progress.reset('download')
            progress.configure(download=late_modes)
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
    clean_asr = from_asr and cleanup_enabled
    asr_modes = modes if from_asr else set()
    progress.configure(audio=asr_modes, whisper_model=asr_modes, transcribe=asr_modes,
                       clean=modes if clean_asr else set(),
                       translate=set() if tracks.get('zh') else modes & {'subtitles', 'download'})
    progress("inspect", "素材检查完成", 100, visible=set())
    if from_asr:
        if media_path is None:
            raise ValueError("未找到可供转写的音视频")
        if not metadata.get('duration'):
            raise ValueError('无法确认素材时长，请重新导出后再转写，或上传配套字幕')
        model_name = intelligence.config.get("whisper_model", "small")
        progress("transcribe", "读取本地转写结果" if (folder / "whisper.json").exists() else "准备 Whisper 本地转写")

        speech_path = folder / 'whisper.json'
        speech_data = read_checkpoint(speech_path, lambda value: isinstance(value, dict)
            and value.get('model', 'small') == model_name and isinstance(value.get('language'), str)
            and valid_cues(value.get('segments')) and bool(value['segments']))
        if speech_data is None:
            def prepare(phase, percent):
                if phase == "audio":
                    progress("audio", "提取转写音轨", percent)
                elif phase == "model":
                    progress("whisper_model", "加载 Whisper 模型（首次使用可能需要下载）")
                elif phase == "waiting":
                    progress("transcribe_wait", "等待 Whisper 转写资源", visible=modes)
            def transcribing(percent):
                progress.complete('audio', 'whisper_model')
                progress('transcribe', f'Whisper 转写 {percent}%', percent)

            args = (media_path, folder, check, transcribing)
            kwargs = dict(settings=intelligence.config, warning=warn, phase=prepare, duration=metadata.get('duration'))
            if inline:
                segments, lang = media.transcribe(*args, **kwargs)
            else:
                segments, lang = yield from media.transcribe_steps(*args, **kwargs)
            runtime_path = folder / "whisper-runtime.json"
            runtime = read_checkpoint(runtime_path, lambda value: isinstance(value, dict)) or {}
            speech_data = {"segments": segments, "language": lang, "model": model_name, "device": runtime.get("device", "cpu")}
            atomic_json(speech_path, speech_data)
        progress.complete('audio', 'whisper_model')
        progress("transcribe", "Whisper 转写完成", 100)
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
    caption_modes = modes & {'subtitles', 'download'}
    translate_modes = caption_modes if language != 'zh' else set()
    progress.configure(translate=translate_modes)
    progress("transcript", f"已取得字幕：{transcript_source}", 100)
    model_cache = folder / 'model-checkpoints' / fingerprint(VERSION, intelligence.model_identity())[:24]
    subtitle_engine, subtitle_cache = intelligence, model_cache
    if model_settings.get('use_subtitle_model') and (translate_modes or clean_asr and cleanup_mode == 'model'):
        subtitle_engine = Intelligence(config.subtitle_model_settings(model_settings), check,
                                       checkpoint_dir=intelligence.checkpoint_dir, warning=warn)
        subtitle_cache = folder / 'model-checkpoints' / fingerprint(VERSION, subtitle_engine.model_identity())[:24]
    cues = raw
    if clean_asr:
        if cleanup_mode == 'model':
            cues = yield Step('model', partial(_transform_subtitles, cues, subtitle_engine, subtitle_cache, progress,
                                               clean=True, original=language != 'zh'))
        else:
            progress('clean', '本地清理转写字幕', 0)
            cues = subtitles.clean_transcript(cues)
            if not cues:
                raise ValueError('字幕为空或仅含语气词，原始字幕已保留')
            progress('clean', '本地清理转写字幕', 100)
    if language == 'zh':
        cues = [dict(cue, text=subtitles.simplified(cue['text'])) for cue in cues]

    for mode in modes:
        progress.queue(mode)
    errors.update((yield from _deliver_outputs(
        store, job_id, folder, output, modes, options, intelligence, cues, language, metadata,
        media_path if has_video else None, details, source, check, warn, model_cache, progress,
        subtitle_engine, subtitle_cache)))
    progress("export", "校验生成文件与资源路径", visible=set())
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
        stage, label = 'clean', '转写字幕清洗'
        name = 'cleaned.original' if original else 'cleaned.zh'
        options = {'clean': True}
    else:
        stage, label = 'translate', '翻译中文字幕'
        name = 'translated.zh'
        options = {'language': 'zh'}
    progress(stage, label, 0)
    result = cached(model_cache / f'{name}-{fingerprint(cues)[:24]}.json',
                    lambda: intelligence.transform(cues, **options,
                        progress=lambda p: progress(stage, f'{label} {round(p * 100)}%', p * 100)),
                    valid_cues)
    if not result:
        raise ValueError('字幕为空或仅含语气词，原始字幕已保留')
    progress(stage, label, 100)
    return result


def _deliver_outputs(store, job_id, folder, output, modes, options, intelligence, cues, language, metadata, media_path,
                     details, source, check, warn, model_cache, tracker, subtitle_engine, subtitle_cache):
    from concurrent.futures import Future, wait
    work = []
    shared_video = None
    if source['type'] == 'url' and modes & {'illustrated', 'download'} and media_path is None:
        material = Future()
        def transfer_progress(percent):
            with store.lock:
                states = store.output_states(job_id)
                visible = modes & {'download'} if states.get('download', {}).get('stage') != 'translate' else set()
                if states.get('illustrated', {}).get('stage') == 'waiting_video':
                    visible |= modes & {'illustrated'}
                message = '下载最高画质视频' if 'download' in modes else '下载配图素材'
                tracker('waiting_video', message, percent, phase='download', visible=visible)

        def fetch():
            try:
                check()
                transfer_progress(None)
                if 'download' in modes:
                    path = _best_video(source, folder, check, transfer_progress)
                else:
                    path = media.download(source['url'], folder, best=False, audio=False, check=check,
                                          resource_id=source.get('resource_id'), progress=transfer_progress)
                transfer_progress(100)
                material.set_result(path)
            except Exception as exc:
                material.set_exception(exc)
            return {}

        def shared_video():
            while not material.done():
                check()
                wait((material,), timeout=.25)
            return material.result()

        work.append(Step('download', fetch))

    def produce(selected, generate):
        try:
            errors = generate() or {}
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            states = store.output_states(job_id)
            errors = {mode: config.redact(str(exc), settings=intelligence.config)[:1200] for mode in selected
                      if states[mode]['status'] != 'completed'}
        for mode in selected:
            if mode in errors:
                tracker.failed(mode, errors[mode])
            else:
                tracker.done(mode)
        exports.publish(store, job_id)
        return errors

    report_modes = modes & {'text', 'illustrated', 'mindmap'}
    if report_modes:
        cache_dir = model_cache / f'report-{fingerprint(cues, options, metadata)[:24]}'
        if language != 'zh':
            cache_dir = model_cache / f'report-source-{fingerprint(language, cues, options, metadata)[:24]}'
        report = partial(_report_outputs, store, job_id, folder, output, report_modes, options,
                         intelligence, cues, language, metadata, media_path, check, warn, tracker, cache_dir, shared_video)
        work.append(Step('outputs', partial(produce, report_modes, report)))

    caption_modes = modes & {'subtitles', 'download'}
    if caption_modes:
        caption_error = None
        english_caption = None

        def prepare_captions():
            nonlocal caption_error, english_caption
            try:
                zh = cues
                if language != 'zh':
                    zh = _transform_subtitles(cues, subtitle_engine, subtitle_cache, tracker)
                    zh = [dict(cue, text=subtitles.simplified(cue['text'])) for cue in zh]
                atomic_text(folder / 'subtitles.zh.srt', subtitles.srt(zh))
                if 'download' in modes and language == 'en':
                    english_caption = folder / 'subtitles.en.srt'
                    atomic_text(english_caption, subtitles.srt(cues))
            except (Cancelled, ServiceStopping):
                raise
            except Exception as exc:
                caption_error = config.redact(str(exc), settings=subtitle_engine.config)[:1200]
                for mode in caption_modes:
                    tracker.failed(mode, caption_error)
                return dict.fromkeys(caption_modes, caption_error)

            def write_subtitles():
                tracker('subtitles', '写入字幕文件')
                if language == 'en':
                    exports.write_text(output / exports.ENGLISH_SUBTITLES[0], subtitles.timestamped_text(cues))
                exports.write_text(output / 'subtitles.zh.txt', subtitles.timestamped_text(zh))
                tracker.complete('subtitles')

            return produce({'subtitles'}, write_subtitles) if 'subtitles' in modes else {}

        def video():
            if caption_error is not None:
                return {'download': caption_error}
            _video_output(store, job_id, folder, output, media_path, details,
                          check, tracker, shared_video, english_caption)

        caption_step = len(work)
        work.append(Step('media' if language == 'zh' else 'model', prepare_captions))
        if 'download' in modes:
            dependencies = (0, caption_step) if shared_video else caption_step
            work.append(Step('video_output', partial(produce, {'download'}, video), after=dependencies))
    if len(work) == 1:
        return (yield work[0])
    results = yield ParallelSteps(tuple(work))
    return {mode: message for result in results for mode, message in result.items()}


def _report_outputs(store, job_id, folder, output, modes, options, intelligence, cues, language, metadata, media_path,
                    check, warn, tracker, cache_dir, shared_video):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tracker("chapters", "按完整内容组织中文汇报章节")
    plan = intelligence.plan(cues, metadata["duration"], options, metadata, cache_dir / f"plan-{VERSION}.json",
                             lambda p: tracker("chapters", "规划章节并核对窗口边界", p * 100))
    tracker.complete("chapters")
    plan = dict(plan, title=subtitles.simplified(plan['title']), chapters=[
        dict(chapter, title=subtitles.simplified(chapter['title']))
        for chapter in plan['chapters']
    ])
    store.update(job_id, report_title=plan["title"])
    atomic_json(folder / 'chapters.json', plan)
    chapters = plan['chapters']
    sections, figure_evidence = [], {}
    tracker('report', f'已完成 0/{len(chapters)} 章正文', 0)
    for index, chapter in enumerate(chapters):
        check()
        if language == 'zh':
            draft_path = cache_dir / f'draft-{index}-{fingerprint(chapter)[:24]}.md'
            try:
                body = draft_path.read_text(encoding='utf-8')
            except (FileNotFoundError, UnicodeDecodeError):
                body = intelligence.draft(chapter, cues, options)
                body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
                atomic_text(draft_path, body)
            evidence = "\n".join(cue['text'] for cue in cues
                                 if cue['end'] > chapter['start'] and cue['start'] < chapter['end'])
            body = exports.grounded_body(subtitles.simplified(body), evidence)
        else:
            draft = intelligence.draft(chapter, cues, options, source_language=language)
            evidence = "\n".join(fact['quote'] for fact in draft['facts'])
            body = exports.grounded_body(subtitles.simplified(draft['body']), evidence, source_language=language)
            figure_evidence[index] = draft['facts']
        body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
        body = re.sub(r"(?m)^#{1,2}\s+[^\n]*\n?", "", body).strip()
        body = re.sub(r"\n+\s*[（(]注[:：]\s*(?:已删除|已核查|已修订|已去除|已移除)[\s\S]*?[）)]\s*$", "", body).strip()
        start = subtitles.stamp(chapter['start']).split(',')[0]
        end = subtitles.stamp(chapter['end']).split(',')[0]
        heading = f"## {chapter['title']}\n\n*{start} — {end}*\n\n"
        sections.append((chapter, heading, body))
        fraction = (index + 1) / len(chapters)
        tracker('report', f'已完成 {index + 1}/{len(chapters)} 章正文', fraction * 100)

    report_modes = modes & {'text', 'illustrated'}
    title = re.sub(r"[<>\n]", "", str(plan['title']))
    table_of_contents = "**目录**\n\n[TOC]\n\n"
    base_lead = f"# {title}\n\n" + table_of_contents

    def summarize():
        tracker('overview', '提炼技术结论与关键数据', visible=report_modes & {'text'})
        overview = cached(cache_dir / f'overview-{fingerprint(sections)[:24]}.json',
                          lambda: intelligence.overview([(chapter, body) for chapter, _, body in sections]),
                          valid_overview)
        tracker.complete('overview')
        lead = f"# {title}\n\n**一句话总结：** {subtitles.simplified(overview['one_sentence'])}\n\n"
        if overview['key_conclusions']:
            lead += "## 核心结论\n\n" + "\n".join("- " + subtitles.simplified(v) for v in overview['key_conclusions']) + "\n\n"
        lead += table_of_contents
        terms = ""
        if overview['data_terms']:
            terms = "\n\n## 关键数据与术语\n\n" + "\n".join("- " + subtitles.simplified(v) for v in overview['data_terms'])
        return lead, terms

    def mindmap():
        check()
        tracker('mindmap', '生成思维导图 PDF')
        outline = exports.report_outline(plan, [body for _, _, body in sections])
        exports.mindmap_data(folder / 'mindmap.json', outline)
        pdf_export.render(folder, check, progress=lambda p: tracker('mindmap', '生成思维导图 PDF', p))
        tracker.complete('mindmap')

    def produce(mode, generate):
        try:
            generate()
            exports.publish(store, job_id)
            tracker.done(mode)
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            error = config.redact(str(exc), settings=intelligence.config)[:1200]
            tracker.failed(mode, error)
            return error

    errors = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        summary = pool.submit(summarize) if report_modes else None

        def deliver_report(mode, body, *, cover=""):
            marker = folder / f'{mode}.summary-pending'

            def write(lead, terms=''):
                check()
                if cover:
                    lead = lead.removesuffix(table_of_contents) + cover + table_of_contents
                exports.install_report(output, mode, lead + body + terms, store.lock, check)
                exports.publish(store, job_id)

            if not summary.done() or summary.exception() is not None:
                required = [output / name for name in exports.report_files(mode)]
                if not marker.exists() or not all(path.is_file() for path in required):
                    atomic_text(marker, 'pending\n')
                    write(base_lead)
                tracker('overview', '正文可下载，正在生成汇报摘要', visible={mode})
            lead, terms = summary.result()
            write_phase = f'write_{mode}'
            tracker('write_report', '写入汇报文件', phase=write_phase)
            write(lead, terms)
            marker.unlink(missing_ok=True)
            tracker.complete(write_phase)

        futures = {}
        if 'mindmap' in modes:
            futures[pool.submit(produce, 'mindmap', mindmap)] = 'mindmap'
        if 'illustrated' in modes:
            def illustrated():
                body, cover = _illustrate(folder, output, sections, intelligence, media_path, metadata,
                                         check, warn, tracker, shared_video, cache_dir, cues, figure_evidence)
                deliver_report('illustrated', body, cover=cover)

            futures[pool.submit(produce, 'illustrated', illustrated)] = 'illustrated'
        if 'text' in modes:
            error = produce('text', lambda: deliver_report('text', "\n\n".join(
                exports.numbered_section(index, heading, body)
                for index, (_, heading, body) in enumerate(sections, 1))))
            if error is not None:
                errors['text'] = error
        for future in as_completed(futures):
            error = future.result()
            if error is not None:
                errors[futures[future]] = error
    return errors


VISUAL_TERMS = re.compile(
    r"价格|售价|定价|补贴|价格表|参数|实测|仪表|图表|曲线|表格|读数|"
    r"底盘|座舱|车身|车头|车尾|外观|内饰|实物|屏幕|界面|结构|示意图|演示|"
    r"发布现场|(?:功能|产品|车型|技术细节|驾驶)展示|"
    r"风扇|吸盘|吸附|倒挂|倒置|尾翼|扩散器|推进器|喷嘴|储气|方向盘|座椅|线控|"
    r"下压力|抓地力|加速测试|起飞|点火|翻滚|飞跃"
)
VISUAL_NUMBER = re.compile(
    r"\d+(?:[.,]\d+)*\s*(?:千瓦时|千瓦|英里|公里|千米|小时|分钟|秒|毫米|厘米|"
    r"万元|元|公斤|千克|吨|升|度|%|％|(?:kwh|kw|km|mph|kg|mm|nm|kpa|hz|gb|mb|kn|g|l)(?![a-z]))",
    re.IGNORECASE,
)
VISUAL_HEADING = re.compile(r"(?m)^#{3,6}[ \t]+(.+)")
NON_VISUAL_CONTEXT = re.compile(r"^(?:风险与限制|后续计划)")
FIGURE_SEARCH_RADII = (8, 24, 48)


def _evidence_terms(text):
    runs = re.findall(r"[\u3400-\u9fff]+|[a-z]{3,}|\d+(?:\.\d+)?", text.lower())
    terms = set()
    for run in runs:
        if "\u3400" <= run[0] <= "\u9fff":
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
        else:
            terms.add(run)
    return terms


def _illustration_targets(chapter, body, cues, check):
    """Attach a few report blocks to actual subtitle evidence in the chapter."""
    blocks = re.split(r"\n[ \t]*\n|\n(?=[ \t]*[-*+]\s+)", body.strip()) if body.strip() else []
    if not blocks:
        return blocks, []
    postings = defaultdict(list)
    cue_terms = []
    cue_visual = []
    for index, cue in enumerate(cues):
        if index % 128 == 0:
            check()
        terms = _evidence_terms(cue["text"])
        cue_terms.append(terms)
        cue_visual.append(set(VISUAL_TERMS.findall(cue["text"])))
        for term in terms:
            postings[term].append(index)

    targets = []
    heading = ""
    for position, paragraph in enumerate(blocks):
        headings = VISUAL_HEADING.findall(paragraph)
        if headings:
            heading = headings[-1]
        content = re.sub(r"(?m)^#{1,6}[ \t]+[^\n]+\n?", "", paragraph).strip()
        if not content:
            continue
        numeric = _evidence_terms(" ".join(match[0] for match in VISUAL_NUMBER.finditer(content)))
        numeric = {term for term in numeric if term[0].isdigit()}
        visual = set(VISUAL_TERMS.findall(content))
        if not (numeric or visual or "演示" in heading) or NON_VISUAL_CONTEXT.match(heading):
            continue
        check()
        terms = _evidence_terms(content)
        scores = Counter()
        for term in sorted(terms):
            hits = postings.get(term, ())
            if hits:
                weight = (3 if term[0].isdigit() else 1) / math.sqrt(len(hits))
                for index in hits:
                    scores[index] += weight
        if not scores:
            continue
        eligible = []
        for i in scores:
            shared = terms & cue_terms[i]
            exact_visual = visual & cue_visual[i]
            numeric_hit = numeric & cue_terms[i]
            if len(shared) >= 3 and (numeric_hit or visual or "演示" in heading):
                eligible.append(i)
            elif (exact_visual and len(shared) >= 2) or (numeric_hit and (visual or "cue_id" in cues[i])):
                eligible.append(i)
        if not eligible:
            continue
        index = max(eligible, key=lambda i: (
            len(visual & cue_visual[i]) * 2 + min(2, len(numeric & cue_terms[i])) * 2 + scores[i], -i))
        evidence = cues[index]
        strength = scores[index] / math.sqrt(max(1, len(terms)))
        concrete = bool(visual & cue_visual[index] or numeric & cue_terms[index])
        targets.append((not concrete, -strength, position, evidence))

    selected = []
    for _, _, position, evidence in sorted(targets, key=lambda row: row[:3]):
        if any(abs(evidence["start"] - other["start"]) < 6 for _, other in selected):
            continue
        selected.append((position, evidence))
        if len(selected) == 4:
            break
    return blocks, sorted(selected)


def _illustrate(folder, output, sections, intelligence, media_path, metadata, check, warn, progress, shared_video, cache_dir, source_cues, figure_evidence):
    if shared_video is not None:
        progress('waiting_video', '等待配图素材', phase='download', visible={'illustrated'})
        media_path = shared_video()
    if media_path is None:
        raise ValueError('图文汇报需要视频画面')
    progress('illustrated_cover', '准备汇报封面', phase='illustrate')
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
    media_stat = media_path.stat()
    visual_config = (intelligence.config.get('use_vlm', True), intelligence.model_identity(visual=True))
    media_identity = (media_path.name, media_stat.st_size, media_stat.st_mtime_ns)
    cue_start = 0
    for index, (chapter, heading, body) in enumerate(sections):
        check()
        while cue_start < len(source_cues) and source_cues[cue_start]["end"] <= chapter["start"]:
            cue_start += 1
        cue_end = cue_start
        while cue_end < len(source_cues) and source_cues[cue_end]["start"] < chapter["end"]:
            cue_end += 1
        cues = (figure_evidence[index] if index in figure_evidence else
                [cue for cue in source_cues[cue_start:cue_end] if cue["end"] > chapter["start"]])
        blocks, targets = _illustration_targets(chapter, body, cues, check)
        for part, (position, evidence) in enumerate(targets):
            sample = dict(chapter, start=max(0, chapter["start"] - FIGURE_SEARCH_RADII[-1]),
                          end=min(metadata["duration"], chapter["end"] + FIGURE_SEARCH_RADII[-1]),
                          evidence_time=max(chapter["start"], evidence["start"]),
                          evidence_text=evidence.get("quote", evidence["text"])[:500])
            if "cue_id" in evidence:
                sample["evidence_cue_id"] = evidence["cue_id"]
            paragraph = blocks[position]
            chapter_label = f'第 {index + 1}/{len(sections)} 章'
            if len(targets) > 1:
                chapter_label += f'第 {part + 1}/{len(targets)} 个配图要点'
            chapter_progress = (index + part / len(targets)) / len(sections)
            name = f'chapter-{index + 1:02d}' + (f'-{part + 1:02d}' if len(targets) > 1 else '')
            relative = f'image/{name}.jpg'
            cache_key = fingerprint('frame-choice-v5', sample, paragraph, position, used, visual_config, media_identity)[:24]
            selection = _chapter_figure(folder, output, media_path, sample, paragraph, intelligence, index, used, check,
                                        warn, progress, chapter_label, chapter_progress, relative,
                                        cache_dir / f'figure-{cache_key}.json')
            if selection:
                used.append(selection['hash'])
                entry = selection['entry']
                stamp = subtitles.stamp(entry['time']).split(',')[0]
                caption = entry['caption']
                blocks[position] += f"\n\n![{caption}]({entry['image']})\n\n*{caption} · 视频 {stamp}*"
                selections.append(entry)
        illustrated.append(exports.numbered_section(index + 1, heading, "\n\n".join(blocks)))
        completed = (index + 1) / len(sections)
        progress('illustrate', f'已完成 {index + 1}/{len(sections)} 章配图', completed * 100)
    atomic_json(folder / 'image-selections.json', selections)
    cover_markdown = "![视频封面](image/cover.jpg)\n\n" if cover.is_file() else ""
    return "\n\n".join(illustrated), cover_markdown


def _chapter_figure(folder, output, media_path, chapter, body, intelligence, index, used, check, warn,
                    progress, label, percent, relative, checkpoint):
    target = output / relative

    def image_variant(digest):
        return f'{relative.removesuffix(".jpg")}-{digest[:16]}.jpg'

    def valid(value):
        if not isinstance(value, dict) or 'entry' not in value:
            return False
        entry = value['entry']
        if entry is None:
            return True
        digest = value.get('digest')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or not isinstance(entry, dict):
            return False
        image = entry.get('image')
        if image not in (relative, image_variant(digest)):
            return False
        saved_image = output / image
        return (isinstance(entry.get('caption'), str)
                and isinstance(entry.get('time'), (int, float)) and math.isfinite(entry['time'])
                and type(value.get('hash')) is int and not media.duplicate_frame(value['hash'], used)
                and saved_image.is_file()
                and digest == sha256(saved_image.read_bytes()).hexdigest())

    saved = read_checkpoint(checkpoint, valid)
    if saved is not None:
        return saved['entry'] and saved

    if not intelligence.config.get('use_vlm', True):
        return None
    seen_hashes = list(used)
    seen_times = []
    choice = None
    previous_radius = None
    for attempt, radius in enumerate(FIGURE_SEARCH_RADII, 1):
        round_label = f'{label}第 {attempt}/{len(FIGURE_SEARCH_RADII)} 轮'

        def frame_progress(value):
            progress('illustrated_frames', f'{round_label}抽取候选画面', value, phase='illustrate', fraction=percent)

        sample = dict(chapter, search_radius=radius, previous_search_radius=previous_radius)
        previous_radius = radius
        frame_progress(0)
        candidates = media.candidates(media_path, sample, folder / f'frames-{VERSION}', index, seen_hashes, check,
                                      progress=frame_progress, exclude_times=seen_times)
        images = []
        for image in candidates:
            if not media.duplicate_frame(image['hash'], seen_hashes):
                images.append(image)
                seen_hashes.append(image['hash'])
                seen_times.append(image['time'])
        if not images:
            continue
        progress('illustrated_review', f'{round_label}按正文评选画面', phase='illustrate', fraction=percent)
        try:
            ranked = intelligence.select(sample, images, body)
        except (Cancelled, ServiceStopping):
            raise
        except Exception as exc:
            check()
            warn(f'{label}视觉评选不可用，已省略配图：{type(exc).__name__}')
            return None
        if ranked:
            choice = ranked[0]
            break

    if choice is None:
        warn(f'{label}没有清晰、相关且不重复的画面，未强行插图')
        if seen_times:
            atomic_json(checkpoint, {'entry': None})
        return None
    selected = choice['candidate']
    content = Path(selected['path']).read_bytes()
    digest = sha256(content).hexdigest()
    if target.is_file() and sha256(target.read_bytes()).hexdigest() != digest:
        relative = image_variant(digest)
        target = output / relative
    if not target.is_file():
        atomic_bytes(target, content)
    caption = re.sub(r"[\[\]<>\n]", "", subtitles.simplified(str(choice.get('caption') or chapter['title'])))
    entry = dict(chapter=chapter['title'], image=relative, time=selected['time'], caption=caption,
                 reason=choice.get('reason', ''), score=choice.get('score'))
    if "evidence_time" in chapter:
        entry.update(evidence_time=chapter["evidence_time"], evidence_text=chapter["evidence_text"])
    if "evidence_cue_id" in chapter:
        entry["evidence_cue_id"] = chapter["evidence_cue_id"]
    result = {'entry': entry, 'hash': selected['hash'], 'digest': digest}
    atomic_json(checkpoint, result)
    return result


def _video_output(store, job_id, folder, output, path, details, check, progress, shared_video, english_caption):
    check()
    if path is None and shared_video is not None:
        path = shared_video()
    if path is None:
        raise ValueError('未找到可封装的视频')
    details = details or media.probe(path)
    check_duration(details.get('format', {}).get('duration'))
    temporary = folder / 'video.partial.mp4'
    temporary.unlink(missing_ok=True)
    try:
        progress('export_video', '封装视频字幕', 0)
        media.export_video(path, temporary, details, check, caption=folder / 'subtitles.zh.srt',
            english_caption=english_caption,
            progress=lambda p: progress('export_video', f'封装视频字幕 {p}%', p))
        check()
        replace_file(temporary, output / 'video.mp4')
        progress.complete('export_video')
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
