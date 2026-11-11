import asyncio
import json
import re
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest

from local import exports, media, subtitles
from local.jobs import JobStore, Cancelled
from local.intelligence import Intelligence


def test_srt_vtt_and_rolling_timestamps():
    vtt = 'WEBVTT\n\n00:00.000 --> 00:01.000\n发布会\n\n00:00.900 --> 00:02.000\n发布会 今天开始\n\n00:02.000 --> 00:03.000\n续航 12.5 小时\n'
    cues = subtitles.parse(vtt)
    assert [c['text'] for c in cues] == ['发布会', '今天开始', '续航 12.5 小时']
    assert subtitles.parse(subtitles.srt(cues)) == cues
    assert subtitles.stamp(3661.234) == '01:01:01,234'


def test_native_subtitles_before_auto():
    info = {'subtitles': {'zh-CN': [{'ext':'vtt', 'url':'manual'}]}, 'automatic_captions': {'ai-zh': [{'ext':'vtt', 'url':'auto'}]}}
    assert [track['url'] for track in media.subtitle_candidates(info, 'zh')] == ['manual', 'auto']


def test_analysis_1080_and_download_best_mp4(tmp_path):
    opts = media.download_options(tmp_path)
    assert 'height<=1080' in opts['format']
    assert opts['merge_output_format'] == 'mp4'
    assert opts['format_sort'] == ['res', 'ext:mp4:m4a']
    assert 'height' not in media.download_options(tmp_path, best=True)['format']
    assert media.download_options(tmp_path, audio=True)['format'] == 'bestaudio/best'


def test_cleaning_protects_numbers_and_times():
    engine = Intelligence({}, lambda: None)
    engine.chat = lambda *args, **kwargs: [{'id':0,'text':'续航 20 小时'}]
    source = [{'start':1.5, 'end':4.0, 'text':'续航 12 小时'}]
    assert engine.transform(source, clean=True) == source
    engine.chat = lambda *args, **kwargs: []
    assert engine.transform(source, clean=True) == source


def test_job_persistence_and_cancellation(tmp_path):
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['text'], source={'type':'upload','filename':'source.mp4'}, options={}, enqueue=False)
    jobs.update(task['id'], status='queued')
    jobs.cancel(task['id'])
    with pytest.raises(Cancelled):
        jobs.check(task['id'])
    saved = json.loads((jobs.directory(task['id']) / 'task.json').read_text(encoding="utf-8"))
    assert saved['status'] == 'cancelled'
    assert jobs.retry(task['id'])['status'] == 'queued'


def test_portable_illustrated_zip_and_html(tmp_path):
    from PIL import Image
    output = tmp_path
    (output/'image').mkdir()
    for name in ['selected.jpg', 'unused.jpg', 'cover.jpg']:
        Image.new('RGB', (100,80), '#73926a').save(output/'image'/name)
    (output/'text.md').write_text('不应打包其他报告', encoding="utf-8")
    (output/'video.mp4').write_bytes(b'not part of the report')
    (output/'subtitles.zh.txt').write_text('不应打包字幕', encoding="utf-8")
    text = '# 中文汇报\n\n![封面](image/cover.jpg)\n\n## 参数说明\n\n![参数图](image/selected.jpg)\n\n续航 12 小时。'
    exports.report(output, 'illustrated', text)
    exports.bundle(output)
    with zipfile.ZipFile(output/'illustrated.zip') as archive:
        names = set(archive.namelist())
        assert names == {'illustrated.md','illustrated.html','image/cover.jpg','image/selected.jpg'}
        for relative in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', archive.read('illustrated.md').decode()):
            assert relative in names
        archive.extractall(tmp_path/'unpacked')
        portable = (tmp_path/'unpacked'/'illustrated.html').read_text(encoding="utf-8")
        assert 'src="image/selected.jpg"' in portable and 'src="image/cover.jpg"' in portable
        assert 'src="images/' not in portable


def test_archive_rejects_missing_images(tmp_path):
    exports.report(tmp_path,'illustrated','![丢失图片](image/missing.jpg)')
    with pytest.raises(ValueError, match='路径无效'):
        exports.bundle(tmp_path)


def test_upload_and_artifact_access(tmp_path, monkeypatch):
    from app import app
    from routers import tasks
    jobs = JobStore(tmp_path/'jobs')
    monkeypatch.setattr(tasks, 'store', jobs)
    monkeypatch.setenv('WEB_ACCESS_PASSWORD', 'test-password')
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/api/v1/tasks')).status_code == 200
            assert not client.cookies
            invalid = await client.post('/api/v1/tasks/upload', data={'modes': 'x' * 65536},
                                        files={'file': ('lecture.mp4', b'media-data')})
            assert invalid.status_code == 400
            assert not jobs.list()
            response = await client.post('/api/v1/tasks/upload', data={'modes':'["text"]','options':'{}'}, files={'file':('lecture.mp4',b'media-data','video/mp4')})
            assert response.status_code == 202
            task = response.json()
            folder = jobs.directory(task['id'])
            assert (folder/'source.mp4').read_bytes() == b'media-data'
            (folder/'output').mkdir()
            (folder/'output'/'report.txt').write_text('测试中文结果', encoding="utf-8")
            jobs.artifact(task['id'], 'report.txt', '汇报', 'text')
            result = await client.get(f"/api/v1/tasks/{task['id']}/files/report.txt")
            assert result.status_code == 200 and result.text == '测试中文结果'
            assert (await client.get(f"/api/v1/tasks/{task['id']}/files/%2e%2e/task.json")).status_code == 404
            assert (await client.get(f"/api/v1/tasks/{task['id']}/files/source.mp4")).status_code == 404
            assert (await client.get(f"/api/v1/tasks/{task['id']}/files/%5C%5Cserver%5Cshare%5Cfile")).status_code == 404
            address = f"/api/v1/tasks/{task['id']}/files/report.txt"
            partial = await client.get(address, headers={'Range': 'bytes=0-2'})
            assert partial.status_code == 206 and partial.content == '测'.encode('utf-8')
            invalid = await client.get(address, headers={'Range': f"bytes={'1' * 5000}-"})
            assert invalid.status_code == 416
            assert jobs.readers[task['id']] == 0
    asyncio.run(exercise())


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg required')
@pytest.mark.parametrize('source_language', ['zho', 'eng'])
def test_real_video_download_embeds_subtitle_tracks_for_source_language(tmp_path, monkeypatch, source_language):
    from local import pipeline
    jobs = JobStore(tmp_path/'tasks')
    task = jobs.create(modes=['download', 'subtitles'], source={'type':'upload', 'filename':'source.mkv'}, options={}, enqueue=False)
    folder = jobs.directory(task['id'])
    zh = [{'start':0,'end':1.5,'text':'车辆发布会，续航 100 公里。'}]
    traditional = [{**zh[0], 'text':'車輛發布會，續航 100 公里。'}]
    en = [{**zh[0], 'text':'Welcome to the presentation.'}]
    caption = tmp_path/'source.srt'
    caption.write_text(subtitles.srt(traditional if source_language == 'zho' else en), encoding='utf-8')
    translated = []

    def translate(engine, cues, *, language, **kwargs):
        assert language == 'zh' and cues == en
        translated.append(language)
        return traditional

    monkeypatch.setattr(Intelligence, 'transform', translate)
    video = folder/'source.mkv'
    media.command(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=green:s=320x180:r=10',
                   '-i',str(caption),'-map','0:v','-map','1:s','-t','2','-c:v','libx264','-c:s','srt',
                   '-metadata:s:s:0',f'language={source_language}',str(video)])
    pipeline.run(jobs, task['id'])
    result = folder/'output/video.mp4'
    streams = media.probe(result)['streams']
    tracks = [s for s in streams if s['codec_type'] == 'subtitle']
    assert [s['tags']['language'] for s in tracks] == (['zho', 'eng'] if source_language == 'eng' else ['zho'])
    assert [s['disposition']['default'] for s in tracks] == ([1, 0] if source_language == 'eng' else [1])
    assert all(s['codec_name']=='mov_text' for s in tracks)
    assert next(s for s in streams if s['codec_type']=='video')['width'] == 320
    for index, expected in enumerate([zh, en] if source_language == 'eng' else [zh]):
        vtt = media.command(['ffmpeg','-v','error','-i',str(result),'-map',f'0:s:{index}','-f','webvtt','pipe:1'])
        assert subtitles.parse(vtt) == expected
    assert translated == (['zh'] if source_language == 'eng' else [])
    assert (folder/'output/subtitles.zh.txt').read_text(encoding='utf-8') == subtitles.timestamped_text(zh)
    assert subtitles.parse((folder/'subtitles.original.srt').read_text(encoding='utf-8')) == (traditional if source_language == 'zho' else en)


def test_whisper_word_alignment_creates_real_sentence_cues():
    from types import SimpleNamespace
    words = [SimpleNamespace(word='今天', start=0.2, end=0.8), SimpleNamespace(word='开始。', start=0.8, end=1.3), SimpleNamespace(word='第二步。', start=2.0, end=3.4)]
    cues = media.word_cues(words)
    assert cues == [{'start':0.2,'end':1.3,'text':'今天开始。'}, {'start':2.0,'end':3.4,'text':'第二步。'}]


def test_visual_caption_cannot_add_unsupported_numbers(tmp_path):
    engine = Intelligence({}, lambda: None)
    engine.chat = lambda *args, **kwargs: {'rankings':[{'index':0,'caption':'续航达到 99 小时','score':90,'reason':'展示产品参数'}]}
    image = {'path':tmp_path/'image.jpg','time':2.0,'hash':0,'quality':10}
    rankings = engine.select({'title':'产品参数','visual_query':'续航说明'}, [image], '产品续航为 12 小时。')
    assert rankings[0]['caption'] == '原视频画面 · 产品参数'


def test_portable_html_does_not_load_model_generated_remote_images(tmp_path):
    exports.report(tmp_path, 'text', '# 汇报\n\n![未知图片](https://example.com/tracking.png)')
    result = (tmp_path/'text.html').read_text(encoding="utf-8")
    assert 'src="https://' not in result


def test_chinese_sidecar_skips_whisper_and_model_for_subtitles(tmp_path, monkeypatch):
    from local import pipeline
    jobs = JobStore(tmp_path/'tasks')
    task = jobs.create(modes=['subtitles'],source={'type':'upload','filename':'source.mp4','subtitle':'uploaded.srt'},options={'clean_subtitles':False},title='已有中文字幕',enqueue=False)
    folder = jobs.directory(task['id'])
    (folder/'source.mp4').write_bytes(b'fixture')
    (folder/'uploaded.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n已有的中文字幕。\n', encoding="utf-8")
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':'2'},'streams':[{'codec_type':'video'}]})
    def forbidden(*args,**kwargs):
        raise AssertionError('Existing Chinese subtitles must not trigger ASR or LLM')
    monkeypatch.setattr(media,'transcribe',forbidden)
    monkeypatch.setattr(Intelligence,'chat',forbidden)
    pipeline.run(jobs,task['id'])
    artifact = (folder/'subtitles.zh.srt').read_text(encoding="utf-8")
    assert '已有的中文字幕' in artifact
    assert {p.name for p in (folder/'output').iterdir()} == {'subtitles.zh.txt'}


def test_cleaning_empty_filler_does_not_fail_or_remove_material_text():
    messages = []
    engine = Intelligence({}, lambda: None, warning=messages.append)
    engine.chat = lambda *args, **kwargs: [{'id':0,'text':''},{'id':1,'text':''}]
    source = [{'start':0,'end':1,'text':'嗯，啊。'}, {'start':1,'end':3,'text':'营收增长 12%。'}]
    result = engine.transform(source, clean=True)
    assert result == [source[1]]
    assert any('原句' in message for message in messages)
    assert source[0]['text'] == '嗯，啊。'


def test_translation_repairs_missing_ids_without_shifting_timestamps():
    source = [{'start':i * 2,'end':i * 2 + 1,'text':text} for i,text in enumerate(['First sentence.','Second sentence.','Third sentence.'])]
    engine = Intelligence({}, lambda: None)
    responses = iter([[{'id':'1','text':'第二句。'}], {'translations':[{'id':'0','text':'第一句。'},{'id':2,'text':'第三句。'},{'id':99,'text':'无关内容'}]}])
    engine.chat = lambda *args, **kwargs: next(responses)
    result = engine.transform(source, language='zh')
    assert [s['text'] for s in result] == ['第一句。','第二句。','第三句。']
    assert [(s['start'],s['end']) for s in result] == [(s['start'],s['end']) for s in source]


def test_translation_plain_text_repair_and_chinese_required():
    engine = Intelligence({}, lambda: None)
    replies = iter([[{'id':0,'text':'Thank you.'}], [], '谢谢。'])
    requests = []
    def chat(prompt, **kwargs):
        requests.append(kwargs)
        return next(replies)
    engine.chat = chat
    result = engine.transform([{'start':3,'end':4,'text':'Thank you.'}], language='zh')
    assert result == [{'start':3,'end':4,'text':'谢谢。'}]
    assert 'json_result' not in requests[-1]


def test_partial_translation_checkpoint_is_reused_after_failure(tmp_path):
    source = [{'start':0,'end':1,'text':'First sentence.'},{'start':1,'end':2,'text':'Second sentence.'}]
    engine = Intelligence({}, lambda: None, checkpoint_dir=tmp_path)
    replies = iter([[{'id':0,'text':'第一句。'}], [], ''])
    engine.chat = lambda *args, **kwargs: next(replies)
    with pytest.raises(ValueError, match='第 2 条'):
        engine.transform(source, language='zh')
    requests=[]
    retry = Intelligence({}, lambda: None, checkpoint_dir=tmp_path)
    def chat(prompt, **kwargs):
        requests.append(prompt)
        return [{'id':1,'text':'第二句。'}]
    retry.chat = chat
    result = retry.transform(source, language='zh')
    assert [s['text'] for s in result] == ['第一句。','第二句。']
    assert len(requests)==1 and 'First sentence.' not in requests[0]
    retry.chat = lambda *args, **kwargs: pytest.fail('Completed subtitle batches should not call the model again')
    assert retry.transform(source, language='zh') == result


def test_conflicting_duplicate_subtitle_ids_are_not_silently_accepted():
    values = [{'id':'8','text':'一个版本'},{'id':8,'text':'另一个版本'},{'id':9,'text':'正确条目'}]
    assert Intelligence._subtitle_mapping(values,{8,9}) == {9:'正确条目'}


def test_whisper_gpu_selection_falls_back_without_gpu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path/'audio.wav').write_bytes(b'fixture')
    monkeypatch.setattr(media, 'cuda_available', lambda: False)
    devices=[]
    def load(name, device):
        devices.append((name,device))
        return SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([SimpleNamespace(start=0,end=1,text='语音内容',words=None)]),SimpleNamespace(duration=1,language='zh')))
    monkeypatch.setattr(media,'_load_whisper',load)
    messages=[]
    result, language = media.transcribe(tmp_path/'source.mp4',tmp_path,lambda:None,lambda n:None,settings={'whisper_model':'base','whisper_device':'cuda'},warning=messages.append)
    assert devices == [('base','cpu')]
    assert result[0]['text']=='语音内容' and language=='zh'
    assert any('CPU' in message for message in messages)


def test_whisper_gpu_runtime_failure_restarts_on_cpu_without_duplicate_cues(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path/'audio.wav').write_bytes(b'fixture')
    monkeypatch.setattr(media,'cuda_available',lambda:True)
    devices=[]
    segment=lambda text:SimpleNamespace(start=0,end=1,text=text,words=None)
    def broken():
        yield segment('GPU 部分结果')
        raise RuntimeError('CUDA out of memory')
    def load(name, device):
        devices.append(device)
        stream=broken if device=='cuda' else lambda:iter([segment('CPU 完整结果')])
        return SimpleNamespace(transcribe=lambda *args, **kwargs:(stream(),SimpleNamespace(duration=1,language='zh')))
    monkeypatch.setattr(media,'_load_whisper',load)
    messages=[]
    result,_=media.transcribe(tmp_path/'source.mp4',tmp_path,lambda:None,lambda n:None,settings={'whisper_model':'small','whisper_device':'auto'},warning=messages.append)
    assert devices == ['cuda','cpu']
    assert [s['text'] for s in result] == ['CPU 完整结果']
    assert any('GPU 转写失败' in message for message in messages)


def test_whisper_cache_changes_when_selected_model_changes(tmp_path, monkeypatch):
    import faster_whisper
    created=[]
    monkeypatch.setattr(media,'MODEL',None)
    monkeypatch.setattr(media,'MODEL_KEY',None)
    monkeypatch.setattr(media,'DATA',tmp_path)
    def constructor(name, **kwargs):
        item=object();created.append((name,kwargs,item));return item
    monkeypatch.setattr(faster_whisper,'WhisperModel',constructor)
    first=media._load_whisper('small','cpu')
    assert media._load_whisper('small','cpu') is first
    assert media._load_whisper('base','cpu') is not first
    assert [entry[0] for entry in created] == ['small','base']
    assert all(entry[1]['compute_type']=='int8' for entry in created)


def test_whisper_settings_validation_and_public_values(tmp_path,monkeypatch):
    from local import config
    monkeypatch.setattr(config,'DATA',tmp_path)
    monkeypatch.setattr(config,'SETTINGS',tmp_path/'settings.json')
    result=config.save({'whisper_model':'medium','whisper_device':'cuda'})
    assert result['whisper_model']=='medium' and result['whisper_device']=='cuda'
    assert 'llm_api_key' not in result
    with pytest.raises(ValueError,match='Whisper'):
        config.save({'whisper_model':'unknown-model'})


def test_english_sidecar_produces_both_subtitle_downloads(tmp_path,monkeypatch):
    from local import pipeline
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=['subtitles'],source={'type':'upload','filename':'source.mp4','subtitle':'uploaded.srt'},options={'clean_subtitles':True},title='英文字幕测试',enqueue=False)
    folder=jobs.directory(task['id'])
    (folder/'source.mp4').write_bytes(b'fixture')
    (folder/'uploaded.srt').write_text('1\n00:00:01,000 --> 00:00:03,000\nWelcome to the presentation.\n', encoding="utf-8")
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':'3'},'streams':[{'codec_type':'video'}]})
    responses=iter([[{'id':0,'text':'欢迎参加本次汇报。'}]])
    monkeypatch.setattr(Intelligence,'chat',lambda *args,**kwargs:next(responses))
    pipeline.run(jobs,task['id'])
    assert '欢迎参加本次汇报' in (folder/'subtitles.zh.srt').read_text(encoding="utf-8")
    assert '00:00:01,000 --> 00:00:03,000' in (folder/'subtitles.zh.srt').read_text(encoding="utf-8")
    assert 'Welcome to the presentation.' in (folder/'subtitles.original.srt').read_text(encoding="utf-8")
    assert {a['name'] for a in jobs.get(task['id'])['artifacts']} == {'subtitles.zh.txt', 'subtitles.en.txt'}
    english = folder/'output/subtitles.en.txt'
    assert english.read_text(encoding='utf-8') == '[00:00:01,000 --> 00:00:03,000]\nWelcome to the presentation.'
    english.unlink()
    pipeline.run(jobs,task['id'])
    assert english.is_file()


def test_offline_mindmap_uses_original_renderer_and_escapes_data(tmp_path):
    plan = {'title': '技术 A < 5，</script><img src=x onerror=alert(1)>', 'chapters': [
        {'title': '技术参数', 'points': ['续航 12 小时', '容量 100 kWh']}]}
    exports.mindmap(tmp_path, plan)
    page = (tmp_path / 'mindmap.html').read_text(encoding="utf-8")
    assert 'MediaMindMap' in page and 'createMap' in page
    assert '下载 PDF' in page
    assert '<details' not in page
    tree = json.loads((tmp_path / 'mindmap.json').read_text(encoding="utf-8"))
    tree['data']['text'] = '</script><img src=x onerror=alert(1)>'
    exports.mindmap_html(tmp_path, tree)
    assert '\\u003c' in (tmp_path / 'mindmap.html').read_text(encoding="utf-8")
    assert '<script src=' not in page
    assert json.loads((tmp_path / 'mindmap.json').read_text(encoding="utf-8"))['children'][0]['data']['text'] == '技术参数'


def test_mindmap_pdf_is_the_only_public_map_file(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from local.uploads import PDF_UPLOAD_LIMIT, UploadLimits
    from routers import tasks
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['mindmap'], source={'type': 'upload'}, options={}, enqueue=False)
    jobs.update(task['id'], status='completed')
    output = jobs.directory(task['id']) / 'output'
    output.mkdir()
    (output.parent / 'mindmap.json').write_text('{}', encoding="utf-8")
    monkeypatch.setattr(tasks, 'store', jobs)
    app = FastAPI()
    app.add_middleware(UploadLimits)
    app.include_router(tasks.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            address = f"/api/v1/tasks/{task['id']}/mindmap/pdf"
            oversized = await client.post(address, content=b'', headers={'Content-Length': str(PDF_UPLOAD_LIMIT + 2 * 1024**2)})
            assert oversized.status_code == 413
            assert (await client.post(address, files={'file': ('bad.pdf', b'not a pdf')})).status_code == 400
            content = b'%PDF-1.7\n test\n%%EOF\n'
            assert (await client.post(address, files={'file': ('mindmap.pdf', content)})).status_code == 200
            assert (await client.get(f"/api/v1/tasks/{task['id']}/files/mindmap.pdf")).content == content
        return content
    content = asyncio.run(exercise())
    assert {p.name for p in output.iterdir()} == {'mindmap.pdf'}
    assert {a['name'] for a in jobs.get(task['id'])['artifacts']} == {'mindmap.pdf'}


def test_chapter_boundaries_use_real_cues(tmp_path):
    engine = Intelligence({}, lambda: None)
    source = [{'start': 0, 'end': 9, 'text': '活动开场'}, {'start': 10, 'end': 25, 'text': '介绍背景'},
              {'start': 130, 'end': 170, 'text': '电池容量 100 kWh'}, {'start': 230, 'end': 270, 'text': '计划明年量产'}]
    engine.chat = lambda *args, **kwargs: {'title': '产品发布', 'summary': '发布新产品。', 'chapters': [
        {'title': '背景', 'start_id': 0, 'start_quote': '活动开场', 'points': ['介绍背景']},
        {'title': '技术参数', 'start_id': 2, 'start_quote': '电池容量 100 kWh', 'points': ['容量 100 kWh']}]}
    plan = engine.plan(source, 280, {}, {}, tmp_path/'plan.json')
    assert [(c['start'], c['end']) for c in plan['chapters']] == [(0, 130), (130, 280)]


def test_model_markdown_wrapper_renders_as_paragraphs_and_table(tmp_path):
    body = '```markdown\n新闻报道正文。\n\n| 技术项目 | 数据 |\n| --- | --- |\n| 容量 | 100 kWh |\n```\n<｜hy_Assistant｜>'
    exports.report(tmp_path, 'text', body)
    page = (tmp_path/'text.html').read_text(encoding="utf-8")
    assert '<table>' in page and '<p>新闻报道正文。' in page
    assert '<pre>' not in page and 'hy_Assistant' not in page


@pytest.mark.parametrize('modes', [['illustrated'], ['text'], ['mindmap'], ['subtitles'], ['download'], ['illustrated','text','mindmap','subtitles','download']])
def test_selected_modes_only_publish_requested_final_files(tmp_path, monkeypatch, modes):
    from local import pipeline, pdf_export
    from PIL import Image
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=modes,source={'type':'upload','filename':'source.mp4','subtitle':'uploaded.srt'}, options={'clean_subtitles':False},title='技术测试',enqueue=False)
    folder=jobs.directory(task['id'])
    (folder/'source.mp4').write_bytes(b'fixture')
    (folder/'uploaded.srt').write_text('1\n00:00:00,000 --> 00:00:05,000\n续航12小时，容量100kWh。\n', encoding="utf-8")
    Image.new('RGB',(200,100),'white').save(folder/'cover.jpg')
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':'5'},'streams':[{'codec_type':'video'}]})
    monkeypatch.setattr(Intelligence,'plan',lambda *args:{'title':'電池系統','chapters':[{'title':'續航與容量','points':['续航12小时','容量100kWh'],'start':0,'end':5}]})
    monkeypatch.setattr(Intelligence,'draft',lambda *args:'電池系統續航12小時，容量100kWh。\n\n### 關鍵參數\n\n| 參數 | 數值 |\n| --- | --- |\n| 續航 | 12小時 |\n| 容量 | 100kWh |')
    monkeypatch.setattr(Intelligence,'overview',lambda *args:{'one_sentence':'容量100kWh，續航12小時。','key_conclusions':['續航12小時。'],'data_terms':['100kWh：電池容量。']})
    monkeypatch.setattr(Intelligence,'transform',lambda self,segments,**kwargs:segments)
    def candidates(path,chapter,output,index,used,check,**kwargs):
        target=output/'image.jpg';target.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(100,80),'blue').save(target)
        return [dict(path=target,time=2,hash=0,quality=10)]
    monkeypatch.setattr(media,'candidates',candidates)
    monkeypatch.setattr(Intelligence,'select',lambda self,ch,images,body:[dict(index=0,candidate=images[0],caption='系統示意',reason='相关')])
    monkeypatch.setattr(media,'export_video',lambda path,target,details,check,**kwargs:target.write_bytes(b'mp4'))
    monkeypatch.setattr(pdf_export,'render',lambda folder,check,**kwargs:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    pipeline.run(jobs,task['id'])
    artifacts=jobs.get(task['id'])['artifacts']
    expected={name for mode in modes for name,_,_ in exports.ARTIFACTS[mode]}
    assert {a['name'] for a in artifacts} == expected
    output=folder/'output'
    extra={'illustrated.zip','image'} if 'illustrated' in modes else set()
    assert {p.name for p in output.iterdir()} == expected|extra
    if 'illustrated' in modes:
        with zipfile.ZipFile(output/'illustrated.zip') as z:
            assert set(z.namelist()) == {'illustrated.html','illustrated.md','image/cover.jpg','image/chapter-01.jpg'}
        assert all(a.get('download_name')=='illustrated.zip' for a in artifacts if a['name'].startswith('illustrated.'))
        assert '系统示意' in (output/'illustrated.md').read_text(encoding='utf-8')
    if 'text' in modes:
        report = (output/'text.md').read_text(encoding='utf-8')
        assert '电池系统' in report and '续航12小时' in report and '電池' not in report and '續航' not in report
    if 'mindmap' in modes:
        assert (folder/'mindmap.json').is_file()
        assert '续航与容量' in (folder/'mindmap.json').read_text(encoding='utf-8')
        assert not next(a for a in artifacts if a['name']=='mindmap.pdf').get('pending')


def test_previews_range_requests_and_report_package_access(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from routers import tasks
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=['illustrated','text','subtitles','download'],source={'type':'upload'},options={},enqueue=False)
    out=jobs.directory(task['id'])/'output';out.mkdir();(out/'image').mkdir()
    from PIL import Image
    Image.new('RGB',(10,10),'white').save(out/'image/cover.jpg')
    exports.report(out,'illustrated','# 图文\n\n![封面](image/cover.jpg)')
    exports.report(out,'text','# 文字\n\n| 参数 | 数值 |\n| --- | --- |\n| 容量 | 100kWh |')
    (out/'subtitles.zh.txt').write_text('中文字幕。', encoding="utf-8")
    (out/'video.mp4').write_bytes(bytes(range(100)))
    (out/'internal.json').write_text('不可下载', encoding="utf-8")
    exports.bundle(out);exports.publish(jobs,task['id'])
    monkeypatch.setattr(tasks,'store',jobs);app=FastAPI();app.include_router(tasks.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            prefix=f"/api/v1/tasks/{task['id']}"
            assert (await client.get(prefix+'/files/illustrated.zip')).status_code==200
            assert (await client.get(prefix+'/files/image/cover.jpg')).status_code==200
            assert (await client.get(prefix+'/files/internal.json')).status_code==404
            md=await client.get(prefix+'/preview/text.md');assert '<table>' in md.text
            illustrated=await client.get(prefix+'/preview/illustrated.md');assert 'src="data:image/jpeg;base64,' in illustrated.text
            txt=await client.get(prefix+'/preview/subtitles.zh.txt');assert '中文字幕。' in txt.text
            video=await client.get(prefix+'/preview/video.mp4');assert '<video controls' in video.text
            part=await client.get(prefix+'/files/video.mp4?inline=true',headers={'Range':'bytes=10-19'})
            assert part.status_code==206 and part.content==bytes(range(10,20))
            assert part.headers['content-range']=='bytes 10-19/100'
            assert (await client.get(prefix+'/files/video.mp4',headers={'Range':'bytes=200-'})).status_code==416
    asyncio.run(exercise())


def test_anthropic_messages_text_and_image_transport(tmp_path,monkeypatch):
    from local import model_client
    from PIL import Image
    picture=tmp_path/'frame.jpg';Image.new('RGB',(20,20),'white').save(picture)
    calls=[]
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def mount(self,*args):pass
        def post(self,url,**kwargs):
            calls.append((url,kwargs))
            class Reply:
                ok=True
                def json(self):return {'stop_reason':'end_turn','content':[{'type':'thinking','thinking':'private'},{'type':'text','text':'中文结果'}]}
            return Reply()
    monkeypatch.setattr(model_client.requests,'Session',Session)
    cfg={'llm_api_format':'anthropic','llm_base_url':'https://example.test','llm_api_key':'test-key','llm_model':'test-model','vlm_api_format':''}
    assert model_client.complete(cfg,'问题','系统指令',None,100,lambda:None)=='中文结果'
    assert calls[0][0]=='https://example.test/v1/messages'
    assert calls[0][1]['headers']['x-api-key']=='test-key'
    assert calls[0][1]['json']['system']=='系统指令'
    assert calls[0][1]['json']['messages'][0]['content']=='问题'
    assert model_client.complete(cfg,'评图','系统指令',[picture],100,lambda:None)=='中文结果'
    blocks=calls[1][1]['json']['messages'][0]['content']
    assert blocks[0]['type']=='image' and blocks[0]['source']['media_type']=='image/jpeg'
    assert blocks[1]=={'type':'text','text':'评图'}
    assert model_client.base_url('https://example.test/v1/messages','anthropic')=='https://example.test/v1'


def test_model_protocols_can_be_mixed_and_validated(tmp_path,monkeypatch):
    from local import config,model_client
    monkeypatch.setattr(config,'DATA',tmp_path);monkeypatch.setattr(config,'SETTINGS',tmp_path/'settings.json')
    public=config.save({'llm_api_format':'anthropic','vlm_api_format':'openai'})
    assert public['llm_api_format']=='anthropic' and public['vlm_api_format']=='openai'
    assert not any(k.endswith('_api_key') for k in public)
    assert model_client.connection(config.load(),True)['format']=='openai'
    with pytest.raises(ValueError,match='接口格式'):config.save({'llm_api_format':'invalid'})


def test_report_comparison_symbols_and_unsupported_figures(tmp_path):
    exports.report(tmp_path,'text','# 技术报告\n\n目标<1秒，横向加速度>2G。')
    assert '目标<1秒，横向加速度>2G。' in (tmp_path/'text.md').read_text(encoding="utf-8")
    assert '目标&lt;1秒，横向加速度&gt;2G。' in (tmp_path/'text.html').read_text(encoding="utf-8")


def test_model_json_with_trailing_text_or_second_object():
    from local.intelligence import decode
    assert decode('```json\n{"facts":[{"text":"续航12小时","quote":"续航12小时"}]}\n```\n<｜hy_Assistant｜>') == {'facts':[{'text':'续航12小时','quote':'续航12小时'}]}
    assert decode('{"one_sentence":"结论"}\n{"explanation":"多余对象"}') == {'one_sentence':'结论'}


@pytest.mark.parametrize('first_response', ['length', '{"', '{"chapters":[{"title":"半截"}'])
@pytest.mark.parametrize('output_limit, retry_limit', [(8192, 8192), (16384, 16384), (32768, 16384)])
def test_chapter_planning_retries_incomplete_response_within_configured_budget(
    tmp_path, monkeypatch, first_response, output_limit, retry_limit,
):
    from local import model_client

    requests = []

    def complete(config, prompt, system, visual, maximum, check):
        requests.append(maximum)
        if len(requests) == 1:
            if first_response == 'length':
                raise model_client.ModelLimitError('output')
            return first_response
        return '{"title":"电池技术","chapters":[{"title":"技术规格","start_id":0}]}'

    monkeypatch.setattr(model_client, 'complete', complete)
    engine = Intelligence({'llm_context_tokens': 1_000_000, 'llm_max_output_tokens': output_limit}, lambda: None)
    cues = [{'start': 0, 'end': 10, 'text': '容量100kWh。'}]
    path = tmp_path / 'plan.json'
    plan = engine.plan(cues, 10, {}, {}, path)
    assert requests == [6000, retry_limit]
    assert plan['chapters'][0]['start_quote'] == cues[0]['text']
    assert json.loads(path.read_text(encoding='utf-8')) == plan


def test_structured_output_retry_fits_remaining_context(monkeypatch):
    from local import model_client

    requests = []

    def complete(config, prompt, system, visual, maximum, check):
        requests.append(maximum)
        if len(requests) == 1:
            raise model_client.ModelLimitError('output')
        return '{"result":true}'

    monkeypatch.setattr(model_client, 'complete', complete)
    engine = Intelligence({'llm_context_tokens': 8192}, lambda: None)
    value = engine.report_json('测试', 'x' * 5000, lambda data: 'result' in data, system='s', max_tokens=1200)
    assert value == {'result': True}
    assert requests == [1200, 8192 - 5000 - 1 - 512]


def test_truncated_chapter_json_without_finish_reason_gets_one_smaller_window_retry(tmp_path, monkeypatch):
    from local import model_client

    attempts = []

    def complete(config, prompt, system, visual, maximum, check):
        if prompt.startswith('判断两个相邻章节'):
            return '{"merge":false}'
        material = prompt.rsplit('\n素材：', 1)[1]
        attempts.append(maximum)
        if '前段' in material and '后段' in material:
            return '{"chapters":[{"title":"未闭合","start_id":0}'
        return '{"title":"技术报告","chapters":[{"title":"章节","start_id":0}]}'

    monkeypatch.setattr(model_client, 'complete', complete)
    engine = Intelligence({'llm_context_tokens': 1_000_000, 'llm_max_output_tokens': 16384}, lambda: None)
    cues = [{'start': 0, 'end': 200, 'text': '前段'}, {'start': 200, 'end': 400, 'text': '后段'}]
    plan = engine.plan(cues, 400, {}, {}, tmp_path / 'plan.json')
    assert attempts == [6000, 16384, 6000, 6000]
    assert [(chapter['start'], chapter['end']) for chapter in plan['chapters']] == [(0, 200), (200, 400)]


def test_mindmap_points_cover_the_end_of_a_long_chapter():
    plan = {'title':'技术汇报', 'chapters':[{'title':'车型规格'}]}
    body = '。'.join([*(f'第 {index} 条已核查事实' for index in range(18)), '尾段公布交付时间。'])
    points = exports.report_outline(plan, [body])['chapters'][0]['points']
    assert len(points) == 6 and points[-1] == '尾段公布交付时间'


def test_technical_body_does_not_invent_test_methods_and_map_uses_final_facts():
    source='0–60mph实测0.81秒，推力80kN。'
    body='0–60mph实测0.81秒。\n\n### 演示与验证\n\n风洞测试验证了结构强度。\n\n### 关键参数\n\n| 参数 | 数值 |\n| --- | --- |\n| 推力 | 80kN |'
    cleaned=exports.grounded_body(body,source)
    assert '风洞' not in cleaned and '结构强度' not in cleaned
    assert '0.81秒' in cleaned and '80kN' in cleaned
    plan={'title':'技术报告','chapters':[{'title':'加速性能','points':['错误的0.1秒（字幕363）']}]}
    result=exports.report_outline(plan,[cleaned])
    assert '0.1秒' not in str(result) and '字幕363' not in str(result)
    assert '0.81秒' in str(result)


def test_openai_chat_transport_uses_matching_images_and_model(tmp_path,monkeypatch):
    from local import model_client
    from types import SimpleNamespace
    from PIL import Image
    frame=tmp_path/'frame.jpg';Image.new('RGB',(10,10),'white').save(frame)
    captured=[]
    class Client:
        def __init__(self,**kwargs):
            captured.append(kwargs)
            self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def create(self,**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='中文结果'))])
    monkeypatch.setattr(model_client,'OpenAI',Client)
    cfg={'llm_api_format':'openai','llm_base_url':'https://example.test/v1','llm_api_key':'test-key','llm_model':'hy-vision-2.0-instruct'}
    assert model_client.complete(cfg,'评图','系统指令',[frame],100,lambda:None)=='中文结果'
    assert captured[1]['messages'][0]=={'role':'system','content':'系统指令'}
    assert captured[1]['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
    assert captured[1]['model']=='hy-vision-2.0-instruct'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_model_read_timeout_is_configurable_and_actionable(monkeypatch, protocol):
    from types import SimpleNamespace
    from local import model_client

    class Client:
        def __init__(self, **kwargs):
            if protocol == 'openai':
                assert kwargs['timeout'].read == 210 and kwargs['timeout'].connect == 15
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def mount(self, *args):
            pass

        def post(self, *args, **kwargs):
            assert kwargs['timeout'] == (15, 210)
            raise model_client.requests.exceptions.ReadTimeout()

        def create(self, **kwargs):
            raise model_client.APITimeoutError(httpx.Request('POST', 'https://example.test/v1/chat/completions'))

    monkeypatch.setenv('MODEL_TIMEOUT_SECONDS', '210')
    monkeypatch.setattr(model_client, 'OpenAI', Client)
    monkeypatch.setattr(model_client.requests, 'Session', Client)
    config = {'llm_api_format': protocol, 'llm_base_url':'https://example.test', 'llm_api_key':'test-key', 'llm_model':'test-model'}
    with pytest.raises(model_client.ModelTimeoutError, match='检查点'):
        model_client.complete(config, '问题', '系统指令', None, 100, lambda:None)


def test_frame_cache_does_not_reuse_different_chapter_times(tmp_path,monkeypatch):
    from PIL import Image
    calls=[]
    def frame(path,timestamp,target,check):
        calls.append((timestamp,target.name));Image.new('RGB',(80,40),'white').save(target)
    monkeypatch.setattr(media,'frame',frame)
    monkeypatch.setattr(media,'image_quality',lambda path:5)
    progress=[]
    media.candidates(tmp_path/'source.mp4',{'start':0,'end':60},tmp_path,0,[],lambda:None,progress=progress.append)
    assert progress == [12.5 * n for n in range(1,9)]
    first={name for _,name in calls};calls.clear()
    media.candidates(tmp_path/'source.mp4',{'start':300,'end':360},tmp_path,0,[],lambda:None)
    assert len(calls)==8 and not first.intersection(name for _,name in calls)


def test_predicted_top_speed_does_not_become_measured_fact():
    result=exports.grounded_body('实测最高速度超过230 mph。', '230英里每小时，希望如此。')
    assert '目标最高速度超过230 mph' in result and '实测' not in result
    assert exports.grounded_body('目标最高速度超过230 mph。', '230英里每小时，希望如此。').count('目标')==1


def test_map_keeps_bullets_and_table_after_unspaced_heading():
    plan={'title':'技术报告','chapters':[{'title':'系统参数'}]}
    body='系统提供12小时续航。\n\n### 技术要点\n- **容量**：电池容量100kWh。\n\n### 关键参数\n| 参数 | 数值 |\n| --- | --- |\n| 重量 | 20kg |\n\n）'
    result=exports.report_outline(plan,[body])
    points=result['chapters'][0]['points']
    assert any('100kWh' in p for p in points) and '重量：20kg' in points
    assert '）' not in points
    cleaned=exports.grounded_body('实际参数12小时。\n\n（注：原文未提供其他数据，故不设置参数表。）','实际参数12小时。')
    assert cleaned=='实际参数12小时。'


def test_task_rename_persists_and_survives_source_title_updates(tmp_path):
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=['text'],source={'type':'upload','filename':'source.mp4'},options={},title='原名称',enqueue=False)
    folder=jobs.directory(task['id']);(folder/'source.mp4').write_bytes(b'original')
    renamed=jobs.rename(task['id'],'  新的汇报名称  ')
    assert renamed['title']==renamed['custom_title']=='新的汇报名称'
    jobs.update(task['id'],title='平台自动读取的标题',status='running')
    assert jobs.get(task['id'])['title']=='新的汇报名称'
    assert json.loads((folder/'task.json').read_text(encoding="utf-8"))['custom_title']=='新的汇报名称'
    assert (folder/'source.mp4').read_bytes()==b'original'
    with pytest.raises(ValueError):jobs.rename(task['id'],'  ')


def test_task_rename_api_and_delete_keep_task_id_stable(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from routers import tasks
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=['text'],source={'type':'upload'},options={},title='原名称',enqueue=False)
    jobs.update(task['id'],status='completed')
    monkeypatch.setattr(tasks,'store',jobs);app=FastAPI();app.include_router(tasks.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            address=f"/api/v1/tasks/{task['id']}"
            result=await client.patch(address,json={'title':'重命名后的任务'})
            assert result.status_code==200 and result.json()['id']==task['id']
            assert (await client.get(address)).json()['title']=='重命名后的任务'
            assert (await client.patch(address,json={'title':'  '})).status_code==400
            assert (await client.delete(address)).status_code==200
            assert (await client.get(address)).status_code==404
    asyncio.run(exercise())


def test_whisper_progress_hides_model_size_but_preserves_model_metadata(tmp_path,monkeypatch):
    from local import pipeline
    jobs=JobStore(tmp_path/'tasks')
    task=jobs.create(modes=['subtitles'],source={'type':'upload','filename':'source.mp4'},options={'clean_subtitles':False},enqueue=False)
    folder=jobs.directory(task['id']);(folder/'source.mp4').write_bytes(b'fixture')
    monkeypatch.setattr(media,'probe',lambda path:{'format':{'duration':'2'},'streams':[{'codec_type':'video'}]})
    monkeypatch.setattr(media,'embedded_subtitles',lambda *args:{})
    def transcribe(path,folder,check,progress,**kwargs):
        progress(69);return [{'start':0,'end':2,'text':'中文转写结果。'}],'zh'
    monkeypatch.setattr(media,'transcribe',transcribe)
    monkeypatch.setattr(media,'whisper_status',lambda *args:{'whisper_runtime':{'device':'cpu'}})
    pipeline.run(jobs,task['id'])
    job=jobs.get(task['id']);messages=[e['message'] for e in job['events']]
    assert 'Whisper 转写 69%' in messages
    assert not any('Whisper small' in message for message in messages)
    assert job['whisper_model']=='small'
