"""Regression checks for shared background jobs and bounded model stages."""
import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI

from local import config, pipeline, media, model_client
from local.intelligence import Intelligence
from local.jobs import JobStore
from local.model_client import ModelLimitError
from local.storage import atomic_bytes


def test_concurrent_same_name_jobs_keep_sources_settings_and_folders_separate(tmp_path, monkeypatch):
    monkeypatch.setattr('local.jobs.load', lambda: {'llm_model': 'model-a', 'llm_api_key': 'private'})
    jobs = JobStore(tmp_path)
    def create(number):
        job = jobs.create(modes=['text'], source={'number': number}, options={'number': number}, title='同名任务', enqueue=False)
        return job, jobs.directory(job['id'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(create, range(12)))
    assert len({path.name for _, path in values}) == 12
    assert (tmp_path/'同名任务').is_dir() and (tmp_path/'同名任务 (12)').is_dir()
    monkeypatch.setattr('local.jobs.load', lambda: {'llm_model': 'model-b'})
    for job, folder in values:
        assert jobs.settings(job['id'])['llm_model'] == 'model-a'
        assert 'llm_api_key' not in json.dumps(job)
        if os.name != 'nt':  # Windows access control uses directory/file ACLs.
            assert (folder/'model-settings.json').stat().st_mode & 0o777 == 0o600
        assert job['source']['number'] == job['options']['number']


def test_two_workers_finish_after_clients_close_and_duplicate_queue_is_ignored(tmp_path, monkeypatch):
    from routers import tasks
    jobs = JobStore(tmp_path, workers=2)
    monkeypatch.setattr(tasks, 'store', jobs)
    app = FastAPI(); app.include_router(tasks.router)
    barrier = threading.Barrier(2)
    seen = []; guard = threading.Lock()
    def run(store, job_id):
        with guard:
            seen.append(job_id)
        barrier.wait(timeout=5)
        folder = store.directory(job_id)/'output'; folder.mkdir()
        (folder/'text.md').write_text(store.get(job_id)['source']['url'], encoding="utf-8")
        store.artifact(job_id, 'text.md', '文字汇报')
    monkeypatch.setattr(pipeline, 'run', run)
    async def submit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            replies = await asyncio.gather(*[client.post('/api/v1/tasks/url', json={'url':f'https://example.test/{n}', 'modes':['text']}) for n in range(2)])
            assert all(r.status_code == 202 for r in replies)
            return [r.json() for r in replies]
    submitted = asyncio.run(submit())  # Both browser connections have gone away.
    jobs.start()
    try:
        jobs.queue.put(submitted[0]['id'])
        jobs.queue.join()
        assert sorted(seen) == sorted(j['id'] for j in submitted)
        for job in submitted:
            assert jobs.get(job['id'])['status'] == 'completed'
            assert (jobs.directory(job['id'])/'output/text.md').read_text(encoding="utf-8") == job['source']['url']
    finally:
        jobs.stop()
    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert {j['id'] for j in restored.list()} == {j['id'] for j in submitted}
        assert all(j['status'] == 'completed' and j['artifacts'] for j in restored.list())
    finally:
        restored.stop()


def test_restart_resumes_queued_job_and_disallows_second_service(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text'], source={}, options={}, title='恢复任务', enqueue=False)
    (jobs.directory(job['id'])/'saved-checkpoint').write_text('done', encoding="utf-8")
    jobs.update(job['id'], status='running')
    def resume(store, job_id):
        assert (store.directory(job_id)/'saved-checkpoint').read_text(encoding="utf-8") == 'done'
    monkeypatch.setattr(pipeline, 'run', resume)
    restored = JobStore(tmp_path)
    restored.start()
    try:
        with pytest.raises(RuntimeError, match='另一个服务'):
            JobStore(tmp_path).start()
        restored.queue.join()
        assert restored.get(job['id'])['status'] == 'completed'
    finally:
        restored.stop()


def test_file_leases_survive_atomic_replace_and_block_delete(tmp_path):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text'], source={}, options={}, enqueue=False)
    jobs.update(job['id'], status='completed')
    folder = jobs.directory(job['id'])/'output'; folder.mkdir()
    target = folder/'text.md'; target.write_bytes(b'old')
    lease = jobs.open_resource(job['id'], 'text.md')
    atomic_bytes(target, b'new')
    assert lease.stream.read() == b'old'
    with pytest.raises(ValueError, match='访问中'):
        jobs.remove(job['id'])
    with jobs.open_resource(job['id'], 'text.md') as stream:
        assert stream.read() == b'new'
    lease.close(); lease.close()
    jobs.remove(job['id'])


def test_parallel_readers_ranges_and_private_settings(tmp_path, monkeypatch):
    from routers import tasks
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['mindmap'], source={}, options={}, enqueue=False)
    folder = jobs.directory(job['id'])/'output'; folder.mkdir()
    content = b'%PDF-1.7\n' + b'0123456789'*1000 + b'\n%%EOF'
    (folder/'mindmap.pdf').write_bytes(content)
    jobs.artifact(job['id'], 'mindmap.pdf', '导图')
    monkeypatch.setattr(tasks, 'store', jobs)
    app = FastAPI(); app.include_router(tasks.router)
    async def read():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = f"/api/v1/tasks/{job['id']}/files/"
            results = await asyncio.gather(*[client.get(url+'mindmap.pdf', headers={'Range': 'bytes=7-27'}) for _ in range(15)])
            assert all(r.status_code == 206 and r.content == content[7:28] for r in results)
            assert (await client.get(url+'mindmap.pdf', headers={'Range':'bytes=-5'})).content == content[-5:]
            assert (await client.get(url+'mindmap.pdf', headers={'Range':'bytes=99999-'})).status_code == 416
            assert (await client.get(url+'../model-settings.json')).status_code == 404
    asyncio.run(read())
    assert jobs.readers[job['id']] == 0


def test_whisper_inference_iterators_are_serialized(tmp_path, monkeypatch):
    active = 0; peak = 0; lock = threading.Lock()
    def recognize(path, folder, check, progress, **kwargs):
        nonlocal active, peak
        import time
        with lock:
            active += 1; peak = max(peak, active)
        time.sleep(.03)
        with lock:
            active -= 1
        return kwargs['settings']['whisper_model']
    monkeypatch.setattr(media, '_transcribe', recognize)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda model: media.transcribe(None, tmp_path, lambda:None, lambda _:None, settings={'whisper_model':model}), ['small','base','tiny','medium']))
    assert peak == 1 and results == ['small','base','tiny','medium']


def test_truncated_json_is_never_decoded_as_complete(monkeypatch):
    calls = []
    def complete(*args):
        calls.append(args[4]); raise ModelLimitError('output')
    monkeypatch.setattr(model_client, 'complete', complete)
    with pytest.raises(ModelLimitError):
        Intelligence({}, lambda:None).chat('test', json_result=True, max_tokens=1000)
    assert calls == [1000, 2000]


def test_long_planning_splits_recursively_and_keeps_global_boundaries(tmp_path):
    engine = Intelligence({}, lambda:None)
    source = [{'start':n*10,'end':n*10+10,'text':str(n)+'技术资料'*150} for n in range(40)]
    calls = []
    def small(batch, duration, options, metadata, path):
        calls.append(len(batch))
        if len(batch) > 3:
            raise ModelLimitError('context')
        return {'title':'中文技术汇报','chapters':[{'title':'实质技术结论', 'start_id':0, 'start':0, 'end':duration}]}
    engine._plan_small = small
    progress = []
    result = engine.plan(source, 400, {}, {}, tmp_path/'plan.json', progress.append)
    assert progress[0] == 0 and progress[-1] == 1
    assert progress == sorted(progress) and any(0 < value < 1 for value in progress)
    assert result['chapters'][0]['start'] == 0 and result['chapters'][-1]['end'] == 400
    assert result['chapters'][-1]['start_id'] > 30
    assert all(a['end'] == b['start'] for a,b in zip(result['chapters'],result['chapters'][1:]))
    assert max(calls) < len(source)
    count = len(calls)
    progress.clear()
    assert engine.plan(source, 400, {}, {}, tmp_path/'plan.json', progress.append) == result
    assert len(calls) == count and progress == [1]


def test_long_drafts_split_on_limits_and_reuse_successful_parts(tmp_path):
    engine = Intelligence({}, lambda:None, checkpoint_dir=tmp_path/'parts')
    source = [{'start':n,'end':n+1,'text':f'事实{n}。'} for n in range(12)]
    calls = []
    def draft(chapter, batch, options, evidence):
        calls.append(len(batch))
        if len(batch) > 2:
            raise ModelLimitError('output')
        return '|'.join(c['text'] for c in batch)
    engine._draft_small = draft
    chapter = {'title':'技术结论','start':0,'end':12}
    value = engine.draft(chapter, source, {})
    assert all(c['text'] in value for c in source)
    count = len(calls)
    assert engine.draft(chapter, source, {}) == value
    assert len(calls) == count


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_transport_output_limit_is_typed_and_partial_json_rejected(protocol, monkeypatch):
    from types import SimpleNamespace
    if protocol == 'openai':
        class Client:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:SimpleNamespace(choices=[SimpleNamespace(finish_reason='length',message=SimpleNamespace(content='[{"valid":"partial"}'))])))
            def __enter__(self): return self
            def __exit__(self, *args): pass
        monkeypatch.setattr(model_client, 'OpenAI', Client)
    else:
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def mount(self, *args): pass
            def post(self, *args, **kwargs):
                return SimpleNamespace(ok=True, json=lambda:{'stop_reason':'max_tokens','content':[{'type':'text','text':'[{"valid":"partial"}'}]})
        monkeypatch.setattr(model_client.requests, 'Session', Session)
    settings = {'llm_api_format':protocol,'llm_base_url':'https://test.invalid/v1','llm_model':'test','llm_api_key':'test'}
    with pytest.raises(ModelLimitError) as failure:
        model_client.complete(settings, 'test', 'test', None, 10, lambda:None)
    assert failure.value.kind == 'output'


def test_single_huge_cue_can_be_split_without_changing_its_timestamps(tmp_path):
    engine = Intelligence({}, lambda:None, checkpoint_dir=tmp_path/'parts')
    source = [{'start':15,'end':20,'text':'技术事实。'*100}]
    def draft(chapter, batch, options, evidence):
        assert batch[0]['start'] == 15 and batch[0]['end'] == 20
        if len(batch[0]['text']) > 150:
            raise ModelLimitError('context')
        return batch[0]['text']
    engine._draft_small = draft
    result = engine.draft({'title':'技术结论','start':15,'end':20}, source, {})
    assert result.replace('\n','') == source[0]['text']


def test_upload_cannot_be_deleted_while_its_stream_is_active(tmp_path):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text'], source={}, options={}, enqueue=False)
    with pytest.raises(ValueError):
        jobs.remove(job['id'])
    assert jobs.cancel(job['id'])['status'] == 'cancelling'
    with pytest.raises(ValueError):
        jobs.remove(job['id'])


@pytest.mark.skipif(__import__('os').getenv('RUN_PDF_RENDER_TEST') != '1', reason='requires installed local Chromium')
def test_real_background_pipeline_saves_pdf_with_no_browser_client(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path, workers=2)
    monkeypatch.setattr(media, 'probe', lambda path:{'format':{'duration':'5'}, 'streams':[{'codec_type':'video'}]})
    monkeypatch.setattr(Intelligence, 'plan', lambda self,segments,*args:{'title':segments[0]['text'], 'chapters':[{'title':'技术参数','start':0,'end':5,'points':['容量100kWh']} ]})
    monkeypatch.setattr(Intelligence, 'draft', lambda *args:'电池容量100kWh，续航目标600km。')
    submitted = []
    for title in ['电池系统一', '电池系统二']:
        job = jobs.create(modes=['mindmap'], source={'type':'upload','filename':'source.mp4','subtitle':'uploaded.srt'}, options={'clean_subtitles':False}, title=title)
        folder = jobs.directory(job['id'])
        (folder/'source.mp4').write_bytes(b'fixture')
        (folder/'uploaded.srt').write_text(f'1\n00:00:00,000 --> 00:00:05,000\n{title}电池容量100kWh。\n', encoding="utf-8")
        submitted.append(job)
    jobs.start()
    try:
        jobs.queue.join()
        for job in submitted:
            finished = jobs.get(job['id'])
            assert finished['status'] == 'completed', finished.get('error')
            artifact = finished['artifacts'][0]
            assert artifact['name'] == 'mindmap.pdf' and not artifact.get('pending')
            data = (jobs.directory(job['id'])/'output/mindmap.pdf').read_bytes()
            assert data.startswith(b'%PDF-') and b'%%EOF' in data[-1024:]
    finally:
        jobs.stop()


@pytest.mark.parametrize('state', ['running', 'uploading'])
def test_repeated_concurrent_cancellation_does_not_allow_early_deletion(tmp_path, state):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text'], source={}, options={}, enqueue=False)
    jobs.update(job['id'], status=state)
    with ThreadPoolExecutor(max_workers=8) as pool:
        cancelled = list(pool.map(lambda _: jobs.cancel(job['id']), range(16)))
    assert all(item['status'] == 'cancelling' for item in cancelled)
    with pytest.raises(ValueError, match='取消完成'):
        jobs.remove(job['id'])
    assert jobs.directory(job['id']).is_dir()


def test_cancelled_audio_extraction_is_not_reused_as_complete_audio(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from local.jobs import Cancelled
    calls = []
    def interrupted(args, check):
        calls.append(args)
        __import__('pathlib').Path(args[-1]).write_bytes(b'incomplete audio')
        raise Cancelled('cancelled')
    monkeypatch.setattr(media, 'command', interrupted)
    with pytest.raises(Cancelled):
        media.transcribe(tmp_path/'source.mp4', tmp_path, lambda:None, lambda _:None)
    assert not (tmp_path/'audio.wav').exists() and not (tmp_path/'audio.partial.wav').exists()
    def complete(args, check):
        calls.append(args)
        __import__('pathlib').Path(args[-1]).write_bytes(b'complete audio')
    monkeypatch.setattr(media, 'command', complete)
    monkeypatch.setattr(media, 'cuda_available', lambda:False)
    monkeypatch.setattr(media, '_load_whisper', lambda *args:SimpleNamespace(transcribe=lambda path,**kwargs:(iter([SimpleNamespace(start=0,end=1,text='完整字幕',words=None)]),SimpleNamespace(duration=1,language='zh'))))
    segments, _ = media.transcribe(tmp_path/'source.mp4', tmp_path, lambda:None, lambda _:None)
    assert len(calls) == 2 and (tmp_path/'audio.wav').read_bytes() == b'complete audio'
    assert segments[0]['text'] == '完整字幕'


@pytest.mark.parametrize('report_mode', ['text', 'mindmap'])
def test_report_with_video_download_accepts_audio_only_analysis_cache(tmp_path, monkeypatch, report_mode):
    from local import pdf_export
    jobs = JobStore(tmp_path/'tasks')
    job = jobs.create(modes=[report_mode,'download'], source={'type':'url','url':'https://example.test/video'}, options={'clean_subtitles':False}, title='example.test', enqueue=False)
    updates = []
    original_progress = jobs.progress
    def record(job_id, stage, percent, message, **kwargs):
        updates.append((stage, kwargs.get('stage_progress')))
        return original_progress(job_id, stage, percent, message, **kwargs)
    monkeypatch.setattr(jobs, 'progress', record)
    monkeypatch.setattr(media, 'inspect_url', lambda *args:({'title':'技术分析','duration':5}, {}))
    downloads = []
    def download(url, folder, *, best, audio, **kwargs):
        downloads.append((best,audio))
        target = folder/('source.m4a' if audio else 'source.mp4')
        target.write_bytes(b'fixture')
        return target
    monkeypatch.setattr(media, 'download', download)
    monkeypatch.setattr(media, 'probe', lambda path:{'format':{'duration':'5'}, 'streams':[{'codec_type':'audio' if path.suffix=='.m4a' else 'video'}]})
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args:{})
    cues = [{'start':0,'end':5,'text':'电池容量100kWh。'}]
    monkeypatch.setattr(media, 'transcribe', lambda *args,**kwargs:(cues,'zh'))
    monkeypatch.setattr(Intelligence, 'plan', lambda *args:{'title':'电池技术','chapters':[{'title':'电池容量','start':0,'end':5,'points':['容量100kWh']}]})
    monkeypatch.setattr(Intelligence, 'draft', lambda *args:'电池容量100kWh。')
    monkeypatch.setattr(Intelligence, 'overview', lambda *args:{'one_sentence':'电池容量100kWh。','key_conclusions':[],'data_terms':[]})
    def transform(*args, **kwargs):
        if 'progress' in kwargs:
            kwargs['progress'](.5)
        return cues
    monkeypatch.setattr(Intelligence, 'transform', transform)
    monkeypatch.setattr(pdf_export, 'render', lambda folder, check:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    def embed(source, zh, en, target, check, **kwargs):
        assert source.suffix == '.mp4' and source.parent.name == 'original'
        kwargs['progress'](50)
        target.write_bytes(b'video')
    monkeypatch.setattr(media, 'embed', embed)
    pipeline.run(jobs, job['id'])
    assert downloads == [(False,True),(True,False)]
    assert (jobs.directory(job['id'])/'output/video.mp4').read_bytes() == b'video'
    assert ('translate_en', 50) in updates and ('translate_en', 100) in updates
    assert ('embed', 50) in updates and ('embed', 100) in updates
    assert {a['name'] for a in jobs.get(job['id'])['artifacts']} == ({'text.html','text.md','video.mp4'} if report_mode=='text' else {'mindmap.pdf','video.mp4'})


def test_preview_returns_404_when_another_client_deletes_before_open(tmp_path, monkeypatch):
    from routers import tasks
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text'], source={}, options={}, enqueue=False)
    jobs.update(job['id'], status='completed')
    output = jobs.directory(job['id'])/'output'; output.mkdir()
    (output/'text.md').write_text('中文报告', encoding="utf-8")
    jobs.artifact(job['id'], 'text.md', '文字汇报')
    monkeypatch.setattr(tasks, 'store', jobs)
    original = jobs.open_resource
    def race(job_id, filename):
        jobs.remove(job_id)
        return original(job_id, filename)
    monkeypatch.setattr(jobs, 'open_resource', race)
    app = FastAPI(); app.include_router(tasks.router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get(f"/api/v1/tasks/{job['id']}/preview/text.md")).status_code == 404
    asyncio.run(exercise())
