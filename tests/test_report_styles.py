import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from local import config, media, pipeline, report_styles, styled_report
from local.intelligence import Intelligence
from local.jobs import JobStore
from routers import tasks


def test_builtin_default_style_uses_original_shared_report_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    jobs = JobStore(tmp_path / "tasks")
    monkeypatch.setattr(tasks, "store", jobs)
    app = FastAPI()
    app.include_router(tasks.router)
    root = "/api/v1"
    with TestClient(app) as client:
        [default] = client.get(root + "/report-styles").json()
        assert default["name"] == "视频详细流程（默认）" and default["built_in"] and default["prompt"]
        link = {"url": "https://example.test/video", "modes": ["text"]}
        explicit = client.post(root + "/tasks/url", json={**link, "options": {
            "report_style": {"id": default["id"], "revision": default["revision"]},
        }})
        assert explicit.status_code == 202 and explicit.json()["options"]["report_style"] == default
        map_only = client.post(root + "/tasks/url", json={**link, "modes": ["mindmap"]})
        assert map_only.status_code == 202 and "report_style" not in map_only.json()["options"]
        upload = client.post(root + "/tasks/upload", files={"file": ("fixture.mp4", b"fixture")},
                             data={"modes": '["text", "mindmap"]'})
        assert upload.status_code == 202

    task = upload.json()
    assert task["options"]["report_style"] == default
    assert not (tmp_path / "report-styles.json").exists()
    monkeypatch.setattr(media, "probe", lambda *args: {"format": {"duration": 10}, "streams": [{"codec_type": "video"}]})
    monkeypatch.setattr(media, "embedded_subtitles", lambda *args: {"zh": [{"start": 0, "end": 5, "text": "技术演示"}]})
    calls = []

    def report(store, job_id, folder, output, modes, options, *args, **kwargs):
        calls.append((modes, options))

    monkeypatch.setattr(pipeline, "_report_outputs", report)
    pipeline.run(jobs, task["id"])
    assert calls == [({"text", "mindmap"}, {key: value for key, value in task["options"].items() if key != "report_style"})]

    with TestClient(app) as client:
        path = root + "/report-styles/" + default["id"]
        body = {"name": default["name"], "prompt": "优先整理关键参数", "rules": {"order": "timeline", "evidence": "整理关键参数"},
                "revision": default["revision"]}
        updated = client.put(path, json=body)
        assert updated.status_code == 200
        saved = updated.json()
        assert saved["built_in"] and saved["name"] == default["name"] and saved["revision"] == 2
        assert client.put(path, json=body).status_code == 409
        assert client.get(root + "/report-styles").json() == [saved]
        chosen = client.post(root + "/tasks/url", json=link)
        assert chosen.status_code == 202 and chosen.json()["options"]["report_style"] == saved
        assert jobs.get(task["id"])["options"]["report_style"] == default


def test_report_style_confirm_edit_and_task_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    jobs = JobStore(tmp_path / "tasks")
    monkeypatch.setattr(tasks, "store", jobs)
    monkeypatch.setattr(Intelligence, "chat", lambda *args, **kwargs: {
        "order": "topic", "sections": [{"title": "车型与价格"}, {"title": "底盘"}],
        "introduction": "写车型和定位", "audit": "有可比资料才对比", "missing": "label",
    })
    app = FastAPI()
    app.include_router(tasks.router)
    with TestClient(app) as client:
        root = "/api/v1"
        draft = {"name": "车型汇报", "prompt": "先写车型和定位，再依次写车型与价格、底盘。"}
        parsed = client.post(root + "/report-styles/parse", json=draft)
        assert parsed.status_code == 200
        assert client.get(root + "/report-styles").json() == [report_styles.DEFAULT_STYLE]
        body = {**draft, "rules": parsed.json()}
        created = client.post(root + "/report-styles", json=body)
        assert created.status_code == 201
        style = created.json()
        request = {"url": "https://example.test/video", "modes": ["text"],
                   "options": {"report_style": {"id": style["id"], "revision": 1}}}
        linked = client.post(root + "/tasks/url", json=request)
        assert linked.status_code == 202
        upload = client.post(root + "/tasks/upload", files={"file": ("fixture.mp4", b"fixture")},
                             data={"modes": '["illustrated"]', "options": json.dumps(request["options"])})
        assert upload.status_code == 202
        path = root + "/report-styles/" + style["id"]
        updated = client.put(path, json={**body, "name": "新版车型汇报", "revision": 1})
        assert updated.status_code == 200 and updated.json()["revision"] == 2
        assert client.put(path, json={**body, "revision": 1}).status_code == 409
        assert client.post(root + "/tasks/url", json=request).status_code == 409
        for submitted in (linked, upload):
            snapshot = jobs.get(submitted.json()["id"])["options"]["report_style"]
            assert snapshot == style


@pytest.mark.parametrize('style_id', ['a' * 32, report_styles.DEFAULT_STYLE['id']])
def test_topic_report_uses_original_evidence_and_reorders_sections(tmp_path, monkeypatch, style_id):
    monkeypatch.setattr(pipeline.scene_index, 'build', lambda *args: None)
    monkeypatch.setattr(pipeline, '_final_figure', lambda folder, path, body, sample, choice, engine, check:
                        (choice['candidate']['path'].read_bytes(), choice['caption'], False))
    jobs = JobStore(tmp_path / "tasks")
    rules = report_styles.StyleRules(
        order="topic", sections=[{"title": "车型定位与价格区间"}, {"title": "底盘"}, {"title": "主动安全"}],
        writing="先给结论", introduction="汇总车型和价格", audit="依据不足时省略竞品对比",
        visuals="优先价格表和底盘实物", missing="label",
    ).model_dump()
    job = jobs.create(modes=["illustrated", "text"], source={"type": "upload", "filename": "source.mp4"},
                      options={"report_style": {"id": style_id, "revision": 2, "name": "车型汇报", "rules": rules}},
                      enqueue=False)
    folder = jobs.directory(job["id"])
    (folder / "source.mp4").write_bytes(b"fixture")
    (folder / "cover.jpg").write_bytes(b"fixture")
    earlier, later = "The chassis uses adaptive suspension.", "The launch price is 129800 yuan."
    cues = [{"start": 10, "end": 14, "text": earlier}, {"start": 720, "end": 724, "text": later}]
    monkeypatch.setattr(media, "probe", lambda *args: {"format": {"duration": 725}, "streams": [{"codec_type": "video"}]})
    monkeypatch.setattr(media, "embedded_subtitles", lambda *args: {"en": cues})
    monkeypatch.setattr(Intelligence, "transform", lambda *args, **kwargs: pytest.fail("报告无需整轨翻译"))
    messages = []

    def chat(engine, prompt, **kwargs):
        messages.append(prompt)
        if kwargs.get("json_result"):
            if earlier in prompt:
                return {"facts": [{"section": 1, "cue": 0, "text": "底盘采用自适应悬架。", "quote": earlier}]}
            return {"facts": [
                {"section": 0, "cue": 0, "text": "售价为129800元。", "quote": later},
                {"section": 0, "cue": 0, "text": "错误报价999元。", "quote": later},
            ]}
        material = prompt.split("已核对事实和原文\n", 1)[1]
        facts, _ = json.JSONDecoder().raw_decode(material)
        return "\n\n".join(fact["text"] for fact in facts)

    attempted = []

    def candidates(path, chapter, output, index, used, check, **kwargs):
        attempted.append((chapter["evidence_time"], chapter["evidence_text"], chapter["visual_rules"]))
        frame = folder / f"fixture-{index}.jpg"
        frame.write_bytes(str(index).encode())
        return [dict(path=frame, time=chapter["evidence_time"], hash=((1 << 32) - 1) << (index * 32))]

    monkeypatch.setattr(Intelligence, "chat", chat)
    monkeypatch.setattr(media, "candidates", candidates)
    monkeypatch.setattr(Intelligence, "select", lambda engine, chapter, images, text: [
        dict(candidate=images[0], caption=chapter["title"])])
    pipeline.run(jobs, job["id"])
    for mode in ("text", "illustrated"):
        report = (folder / "output" / f"{mode}.md").read_text(encoding="utf-8")
        assert report.index("## 前文") < report.index("## 一、车型定位与价格区间") < report.index("## 二、底盘")
        assert "## 三、主动安全\n\n素材未披露。" in report
        assert "999元" not in report
    assert attempted == [(720, later, rules["visuals"]), (10, earlier, rules["visuals"])]
    assert any("依据不足时省略竞品对比" in message and "逐句复核" in message for message in messages)
    assert not (folder / "subtitles.zh.srt").exists()


def test_timeline_style_keeps_plan_boundaries_and_writing_rules(tmp_path, monkeypatch):
    engine = Intelligence({}, lambda: None, checkpoint_dir=tmp_path / "cache")
    plan_calls = []

    def plan(cues, duration, options, metadata, checkpoint):
        plan_calls.append(options["instructions"])
        return {"title": "原顺序", "chapters": [{"title": "产品实物", "start": 0, "end": duration}]}

    monkeypatch.setattr(engine, "plan", plan)
    quote = "展示产品实物及其结构。"
    monkeypatch.setattr(engine, "chat", lambda prompt, **kwargs: {
        "facts": [{"section": 0, "cue": 0, "text": quote, "quote": quote}],
    } if kwargs.get("json_result") else quote)

    class Progress:
        def __call__(self, *args):
            pass

        def complete(self, *args):
            pass

    title, sections, _, _, _ = styled_report.build(
        engine, [{"start": 2, "end": 4, "text": quote}], {"duration": 5},
        {"report_style": {"rules": {"order": "timeline", "evidence": "聚焦产品", "writing": "简洁"}}},
        tmp_path / "report", Progress())
    assert plan_calls == ["聚焦产品"] and title == "原顺序"
    assert "00:00:02 — 00:00:04" in sections[0][1]
