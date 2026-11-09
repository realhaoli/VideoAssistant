"""Regressions for failures and resource contention across concurrent tasks."""
import asyncio
import errno
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
import httpx
import pytest

from local import media, pipeline, resources
from local.intelligence import Intelligence
from local.jobs import JobStore
from local.stages import Step
from local.storage import atomic_json
from local.uploads import UploadLimits
from routers import tasks


def test_parallel_uploads_cannot_both_claim_the_same_disk_space(monkeypatch):
    monkeypatch.setenv('MIN_FREE_DISK_MB', '0')
    monkeypatch.setattr(resources.shutil, 'disk_usage', lambda _: SimpleNamespace(free=300))
    # Exercise upload admission when the spool and final files share a volume.
    monkeypatch.setattr(resources, '_disk_location', lambda path: (Path(path), 1))
    app = FastAPI()
    app.add_middleware(UploadLimits)

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        @app.post('/api/v1/tasks/upload')
        async def accept(request: Request):
            assert len(await request.body()) == 100
            entered.set()
            await release.wait()
            return {'ok': True}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            path = '/api/v1/tasks/upload'
            first = asyncio.create_task(client.post(path, content=b'x' * 100))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert (await client.post(path, content=b'x' * 100)).status_code == 507
            finally:
                release.set()
                assert (await first).status_code == 200
            assert (await client.post(path, content=b'x' * 100)).status_code == 200

    asyncio.run(exercise())


def test_enospc_while_saving_failure_does_not_kill_the_worker(tmp_path, monkeypatch):
    store = JobStore(tmp_path, workers=1)
    disk_full = threading.Event()
    persist = store._persist

    def save(job):
        if disk_full.is_set():
            raise OSError(errno.ENOSPC, 'disk full')
        persist(job)

    def steps(store, job_id):
        def run():
            if store.get(job_id)['title'] == '磁盘满':
                disk_full.set()
                raise OSError(errno.ENOSPC, 'disk full')
        yield Step('media', run)

    monkeypatch.setattr(store, '_persist', save)
    monkeypatch.setattr(pipeline, 'steps', steps)
    store.start()
    try:
        bad = store.create(modes=['text'], source={}, options={}, title='磁盘满')
        store.queue.join()
        assert store.worker.is_alive() and store.get(bad['id'])['status'] == 'failed'
        disk_full.clear()
        following = store.create(modes=['text'], source={}, options={})
        store.queue.join()
        assert store.get(following['id'])['status'] == 'completed'
        assert store._read_record(store.directory(bad['id']) / 'task.json')['status'] == 'failed'
    finally:
        disk_full.clear()
        store.stop()


def test_partial_upload_cannot_be_retried_after_restart(tmp_path):
    original = JobStore(tmp_path)
    job = original.create(modes=['text'], source={'type': 'upload', 'filename': 'source.mp4'}, options={}, enqueue=False)
    (original.directory(job['id']) / 'source.mp4').write_bytes(b'partial legacy upload')
    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert restored.needs_upload(restored.get(job['id']))
        with pytest.raises(ValueError, match='重新上传'):
            restored.retry(job['id'])
        assert restored.queue.empty()
    finally:
        restored.stop()


def test_two_long_transcriptions_do_not_block_a_report_with_existing_subtitles(tmp_path, monkeypatch):
    store = JobStore(tmp_path, workers=2)
    monkeypatch.setenv('WHISPER_CHUNK_SECONDS', '30')
    monkeypatch.setattr(media, 'probe', lambda _: {'format': {'duration': '90'}, 'streams': [{'codec_type': 'video'}]})
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {})
    monkeypatch.setattr(media, 'ffmpeg_command', lambda args, *a, **kw: Path(args[-1]).write_bytes(b'audio'))
    monkeypatch.setattr(media, 'cuda_available', lambda: False)
    monkeypatch.setattr(Intelligence, 'plan', lambda *args: {'title': '短报告', 'chapters': [{'title': '结论', 'start': 0, 'end': 3}]})
    monkeypatch.setattr(Intelligence, 'draft', lambda *args: '材料正文。')
    monkeypatch.setattr(Intelligence, 'overview', lambda *args: {'one_sentence': '结论', 'key_conclusions': [], 'data_terms': []})
    entered, release = threading.Event(), threading.Event()
    order = []

    def recognize(path, work, check, progress, **options):
        order.append((Path(path).parent.name, work.name))
        if len(order) == 1:
            entered.set()
            assert release.wait(8)
        atomic_json(work / 'whisper-runtime.json', {'device': 'cpu'})
        return [{'start': 2.5, 'end': 3.5, 'text': '一段字幕。'}], 'zh'

    monkeypatch.setattr(media, '_transcribe', recognize)

    def submit(title, with_caption=False):
        source = {'type': 'upload', 'filename': 'source.mp4'}
        if with_caption:
            source['subtitle'] = 'input.srt'
        job = store.create(modes=['text'] if with_caption else ['subtitles'], source=source,
                           options={'clean_subtitles': False}, title=title, enqueue=False)
        folder = store.directory(job['id'])
        (folder / 'source.mp4').write_bytes(b'media')
        if with_caption:
            (folder / 'input.srt').write_text('1\n00:00:00,000 --> 00:00:03,000\n材料正文。\n', encoding='utf-8')
        store.update(job['id'], status='queued')
        store.queue.put(job['id'])
        return job['id']

    def wait_for(job_id, predicate):
        deadline = time.monotonic() + 5
        while not predicate(store.get(job_id)):
            assert time.monotonic() < deadline, store.get(job_id)
            time.sleep(.01)

    store.start()
    try:
        first = submit('长任务甲')
        assert entered.wait(3)
        second = submit('长任务乙')
        wait_for(second, lambda job: job['stage'] == 'transcribe_wait')
        short = submit('短任务', with_caption=True)
        wait_for(short, lambda job: job['status'] == 'completed')
        assert (store.directory(short) / 'output/text.html').is_file()
        assert all(store.get(job)['status'] == 'running' for job in (first, second))
        release.set()
        store.queue.join()
        assert all(store.get(job)['status'] == 'completed' for job in (first, second))
        assert order[:2] == [('长任务甲', '00000'), ('长任务乙', '00000')]
    finally:
        release.set()
        store.stop()


def test_history_poll_omits_events_and_unchanged_pages_have_no_body(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(tasks, 'store', store)
    for i in range(1000):
        key = str(i)
        store.jobs[key] = {'id': key, 'created_at': i, 'title': '任务', 'status': 'completed', 'progress': 100,
                           'modes': ['text'], 'source': {}, 'events': [{'message': '详情'}] * 200}
    app = FastAPI()
    app.include_router(tasks.router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            path = '/api/v1/tasks?limit=25&offset=25'
            response = await client.get(path)
            page = response.json()
            assert page['task_count'] == 1000 and page['items'][0]['id'] == '974'
            assert len(page['items']) == 25 and len(response.content) < 6000
            assert 'events' not in page['items'][0]
            unchanged = await client.get(path, headers={'If-None-Match': response.headers['ETag']})
            assert unchanged.status_code == 304 and unchanged.content == b''
            detail = await client.get('/api/v1/tasks/974')
            assert len(detail.json()['events']) == 200

    asyncio.run(exercise())


def test_bad_stage_checkpoint_is_regenerated_and_valid_batches_are_not_rewritten(tmp_path, monkeypatch):
    cache = tmp_path / 'broken.json'
    cache.write_text('{invalid', encoding='utf-8')
    assert pipeline.cached(cache, lambda: {'restored': True}) == {'restored': True}
    engine = Intelligence({}, lambda: None, checkpoint_dir=tmp_path / 'batches')
    calls = []

    def respond(rows, *_):
        calls.append(len(rows))
        return [dict(id=row['id'], text=row['text']) for row in rows]

    monkeypatch.setattr(engine, 'subtitle_request', respond)
    writes = []

    def save(path, value):
        writes.append(path)
        atomic_json(path, value)

    monkeypatch.setattr('local.intelligence.atomic_json', save)
    cues = [{'start': i, 'end': i + 1, 'text': '完整内容'} for i in range(48)]
    assert engine.transform(cues, clean=True) == cues
    assert sum(calls) == len(cues) and len(writes) == len(set(writes)) == len(calls) < len(cues)
    count = len(calls)
    assert engine.transform(cues, clean=True) == cues
    assert len(writes) == len(calls) == count
