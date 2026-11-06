import json
import re
import shutil
from pathlib import Path

from . import config, exports, media, subtitles, pdf_export
from .intelligence import Intelligence
from .report_prompts import VERSION
from .storage import atomic_bytes, atomic_json, atomic_text


def cached(path, generate):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = generate()
    atomic_json(path, value)
    return value


def run(store, job_id):
    job = store.get(job_id)
    folder = store.directory(job_id)
    output = folder / "output"
    output.mkdir(exist_ok=True)
    check = lambda: store.check(job_id)
    warn = lambda message: store.warn(job_id, message)
    progress = lambda stage, percent, message: store.progress(job_id, stage, percent, message)
    modes, options = set(job["modes"]), job["options"]
    exports.normalize_outputs(folder, modes)
    intelligence = Intelligence(store.settings(job_id), check, checkpoint_dir=folder / "subtitle-batches", warning=warn)
    exports.publish(store, job_id)
    tracks = {}
    source = job["source"]
    metadata = {"title": job["title"]}
    media_path = None
    progress("inspect", 3, "检查素材与已有字幕")

    if source["type"] == "url":
        metadata_path = folder / "source_info.json"
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            for lang in ("zh", "en"):
                if (folder / f"platform.{lang}.srt").exists():
                    tracks[lang] = subtitles.parse((folder / f"platform.{lang}.srt").read_text(encoding="utf-8"))
        else:
            metadata, tracks = media.inspect_url(source["url"], folder, check, warn)
        title = metadata.get("title") or job["title"]
        folder = store.source_title(job_id, title)
        output = folder / "output"
        output.mkdir(exist_ok=True)
        intelligence.checkpoint_dir = folder / "subtitle-batches"
        if "illustrated" in modes and not (folder / "cover.jpg").exists():
            try:
                media.cover(metadata.get("thumbnail"), folder)
            except Exception as exc:
                warn(f"封面下载失败，不影响正文处理：{type(exc).__name__}")
        has_reports = bool(modes & {"illustrated", "text", "mindmap"})
        need_video = "illustrated" in modes or ("download" in modes and not has_reports)
        if need_video or not tracks or ("download" in modes and not has_reports):
            progress("download", 8, "下载最高画质视频" if "download" in modes and not has_reports else "下载分析所需的视频" if need_video else "没有可用字幕，下载音频用于本地转写")
            media_path = media.download(source["url"], folder, best="download" in modes and not has_reports, audio=not need_video,
                                        check=check, progress=lambda p: progress("download", 8 + p * .14, f"素材下载 {p}%"))
    else:
        media_path = folder / source["filename"]
        if source.get("subtitle"):
            uploaded = subtitles.parse((folder / source["subtitle"]).read_text(encoding="utf-8-sig"))
            if uploaded:
                tracks[subtitles.language(uploaded)] = uploaded
    if media_path:
        details = media.probe(media_path)
        metadata["duration"] = float(details.get("format", {}).get("duration") or metadata.get("duration") or 0)
        has_video = any(s.get("codec_type") == "video" for s in details.get("streams", []))
        if not has_video and "download" in modes and (source["type"] != "url" or need_video):
            raise ValueError("该素材没有视频画面，无法生成带双字幕轨的视频")
        if not has_video and "illustrated" in modes:
            raise ValueError("图文汇报需要视频画面；纯音频请改选文字汇报、思维导图或字幕文件")
        if not tracks:
            tracks = media.embedded_subtitles(media_path, folder, check)
    if "illustrated" in modes:
        cover = folder / "cover.jpg"
        if not cover.exists() and media_path:
            try:
                media.frame(media_path, min(1.0, metadata["duration"] / 2), cover, check)
            except Exception:
                check()
        if cover.exists():
            (output / "image").mkdir(exist_ok=True)
            atomic_bytes(output / "image" / "cover.jpg", cover.read_bytes())
    if not tracks:
        if media_path is None:
            raise ValueError("未找到可供转写的音视频")
        model_name = intelligence.config.get("whisper_model", "small")
        progress("transcribe", 24, "读取本地转写结果" if (folder / "whisper.json").exists() else "Whisper 本地转写")

        def speech():
            segments, lang = media.transcribe(media_path, folder, check,
                                               lambda p: progress("transcribe", 24 + p * .2, f"Whisper 转写 {p}%"), settings=intelligence.config, warning=warn)
            runtime_path = folder / "whisper-runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8")) if runtime_path.exists() else {}
            return {"segments": segments, "language": lang, "model": model_name, "device": runtime.get("device", "cpu")}
        speech_data = cached(folder / "whisper.json", speech)
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
    (folder / "subtitles.original.srt").write_text(subtitles.srt(raw), encoding="utf-8")
    store.update(job_id, transcript_source=transcript_source, duration=metadata["duration"])
    progress("transcript", 45, f"已取得字幕：{transcript_source}")
    zh = tracks.get("zh")
    if zh is None:
        progress("translate", 46, "将原始字幕翻译为简体中文，保留时间轴")
        zh = cached(folder / "translated.zh.json", lambda: intelligence.transform(raw, language="zh", progress=lambda p: progress("translate", 46 + p * 2, f"翻译中文字幕 {round(p * 100)}%")))
    if options.get("clean_subtitles", True):
        progress("clean", 49, "处理中文字幕")
        zh = cached(folder / "cleaned.zh.json", lambda: intelligence.transform(zh, clean=True, progress=lambda p: progress("clean", 49 + p * 6, f"字幕清洗 {round(p * 100)}%")))
    if not zh:
        raise ValueError("字幕仅含语气词，没有可生成汇报的有效内容；原始字幕已保留")
    (folder / "subtitles.zh.srt").write_text(subtitles.srt(zh), encoding="utf-8")
    if "subtitles" in modes:
        text = "\n\n".join(f"[{subtitles.stamp(s['start'])} --> {subtitles.stamp(s['end'])}]\n{s['text']}" for s in zh)
        exports.write_text(output / "subtitles.zh.txt", text)
        exports.publish(store, job_id)

    if modes & {"illustrated", "text", "mindmap"}:
        progress("chapters", 60, "按完整内容组织中文汇报章节")
        plan = intelligence.plan(zh, metadata["duration"], options, metadata, folder / f"plan-{VERSION}.json")
        store.update(job_id, report_title=plan["title"])
        (folder / "chapters.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if modes & {"illustrated", "text", "mindmap"}:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            parts, illustrated_parts, used = [], [], []
            chapters = plan["chapters"]

            def chapter_work(index, chapter):
                check()
                draft_path = folder / f"draft-{VERSION}-{index}.md"
                def draft():
                    if draft_path.exists():
                        return draft_path.read_text(encoding="utf-8")
                    body = intelligence.draft(chapter, zh, options)
                    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
                    atomic_text(draft_path, body)
                    return body
                images = []
                with ThreadPoolExecutor(max_workers=2) as parallel:
                    body_future = parallel.submit(draft)
                    if "illustrated" in modes:
                        images_future = parallel.submit(media.candidates, media_path, chapter, folder / f"frames-{VERSION}", index, [], check)
                        images = images_future.result()
                    body = body_future.result()
                evidence = "\n".join(s["text"] for s in zh if s["end"] > chapter["start"] and s["start"] < chapter["end"])
                body = exports.grounded_body(body, evidence)
                body = re.sub(r"(?m)^#{1,2}\s+[^\n]*\n?", "", body).strip()
                body = re.sub(r"\n+\s*[（(]注[:：]\s*(?:已删除|已核查|已修订|已去除|已移除)[\s\S]*?[）)]\s*$", "", body).strip()
                ranked = []
                if images:
                    if intelligence.config.get("use_vlm", True):
                        try:
                            ranked = intelligence.select(chapter, images, body)
                        except Exception as exc:
                            check()
                            warn(f"第 {index + 1} 章视觉评选不可用，采用清晰度选帧：{type(exc).__name__}")
                            ranked = [dict(index=i, candidate=image, caption="原视频画面 · " + chapter["title"], reason="清晰度与去重筛选") for i, image in enumerate(images)]
                    else:
                        ranked = [dict(index=i, candidate=image, caption="原视频画面 · " + chapter["title"], reason="清晰度与去重筛选") for i, image in enumerate(images)]
                return index, chapter, body, ranked

            results = {}
            with ThreadPoolExecutor(max_workers=2) as parallel:
                futures = [parallel.submit(chapter_work, index, chapter) for index, chapter in enumerate(chapters)]
                for future in as_completed(futures):
                    try:
                        index, chapter, body, ranked = future.result()
                    except Exception:
                        for pending in futures:
                            pending.cancel()
                        raise
                    results[index] = (chapter, body, ranked)
                    progress("report", 63 + len(results) / len(chapters) * 28, f"已完成 {len(results)}/{len(chapters)} 章正文与配图评选")
            selections = []
            for index in range(len(chapters)):
                chapter, body, ranked = results[index]
                choice = next((entry for entry in ranked if not any((entry["candidate"]["hash"] ^ other).bit_count() < 12 for other in used)), None)
                heading = f"## {chapter['title']}\n\n*{subtitles.stamp(chapter['start']).split(',')[0]} — {subtitles.stamp(chapter['end']).split(',')[0]}*\n\n"
                parts.append(heading + body)
                figure = ""
                if choice:
                    selected = choice["candidate"]
                    used.append(selected["hash"])
                    relative = f"image/chapter-{index + 1:02d}.jpg"
                    (output / "image").mkdir(exist_ok=True)
                    atomic_bytes(output / relative, Path(selected["path"]).read_bytes())
                    caption = re.sub(r"[\[\]<>\n]", "", str(choice.get("caption") or chapter["title"]))
                    figure = f"\n\n![{caption}]({relative})\n\n*{caption} · 视频 {subtitles.stamp(selected['time']).split(',')[0]}*\n\n"
                    selections.append(dict(chapter=chapter["title"], image=relative, time=selected["time"], caption=caption, reason=choice.get("reason", ""), score=choice.get("score")))
                elif "illustrated" in modes:
                    warn(f"第 {index + 1} 章没有清晰、相关且不重复的画面，未强行插图")
                # Put the illustration after the opening explanation, inside its chapter.
                opening, separator, remainder = body.partition("\n\n")
                illustrated_parts.append(heading + opening + figure + (separator + remainder if separator else ""))
            (folder / "image-selections.json").write_text(json.dumps(selections, ensure_ascii=False, indent=2), encoding="utf-8")
            if "mindmap" in modes:
                outline = exports.report_outline(plan, [results[i][1] for i in range(len(chapters))])
                exports.mindmap_data(folder / "mindmap.json", outline)
                progress("mindmap", 91, "生成思维导图 PDF")
                pdf_export.render(folder, check)
                exports.publish(store, job_id)
            progress("overview", 92, "提炼技术结论与关键数据")
            overview = cached(folder / f"overview-{VERSION}.json", lambda: intelligence.overview([(results[i][0], results[i][1]) for i in range(len(chapters))])) if modes & {"text", "illustrated"} else dict(one_sentence="", key_conclusions=[], data_terms=[])
            title = re.sub(r"[<>\n]", "", str(plan["title"]))
            lead = f"# {title}\n\n**一句话总结：** {overview['one_sentence']}\n\n"
            if overview["key_conclusions"]:
                lead += "## 核心结论\n\n" + "\n".join("- " + v for v in overview["key_conclusions"]) + "\n\n"
            lead += "**目录**\n\n[TOC]\n\n"
            terms = "\n\n## 关键数据与术语\n\n" + "\n".join("- " + v for v in overview["data_terms"]) if overview["data_terms"] else ""
            if "text" in modes:
                exports.report(output, "text", lead + "\n\n".join(parts) + terms)
            if "illustrated" in modes:
                front = "![视频封面](image/cover.jpg)\n\n" if (output / "image/cover.jpg").exists() else ""
                exports.report(output, "illustrated", lead + front + "\n\n".join(illustrated_parts) + terms)
                exports.bundle(output)
            exports.publish(store, job_id)
    if "download" in modes:
        progress("bilingual", 93, "准备中英双字幕轨")
        if source["type"] == "url" and modes & {"illustrated", "text", "mindmap"}:
            progress("download", 92, "汇报已可下载，继续下载最高画质视频")
            original = folder / "original"
            original.mkdir(exist_ok=True)
            media_path = media.download(source["url"], original, best=True, audio=False, check=check,
                                        progress=lambda p: progress("download", 92, f"汇报已可下载，最高画质视频下载 {p}%"))
        en = tracks.get("en")
        if en is None:
            en = cached(folder / "translated.en.json", lambda: intelligence.transform(zh, language="en"))
        (folder / "subtitles.en.srt").write_text(subtitles.srt(en), encoding="utf-8")
        final_video = output / "video.mp4"
        if not final_video.exists():
            progress("embed", 94, "封装 MP4 视频与可切换中英字幕轨")
            temporary = folder / "video.partial.mp4"
            media.embed(media_path, folder / "subtitles.zh.srt", folder / "subtitles.en.srt", temporary, check)
            temporary.replace(final_video)
        exports.publish(store, job_id)

    progress("export", 95, "校验生成文件与资源路径")
    provenance = {"title": metadata.get("title"), "source": source.get("url") or source.get("original_name"),
                  "transcript_source": transcript_source, "language": "zh-CN", "whisper_model": store.get(job_id).get("whisper_model"),
                  "warnings": store.get(job_id)["warnings"]}
    (folder / "source.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    exports.publish(store, job_id)
