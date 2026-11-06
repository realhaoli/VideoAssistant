import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest

from local.jobs import JobStore


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_history_and_downloads_survive_repeated_restarts(tmp_path, status):
    jobs = JobStore(tmp_path/'data/tasks')
    task = jobs.create(modes=['text'], source={'type':'upload','filename':'source.mp4'}, options={}, title='历史任务😀', enqueue=False)
    jobs.rename(task['id'], '保留的自定义名称')
    jobs.update(task['id'], status=status, stage='report', progress=70, stage_progress=50)
    folder = jobs.directory(task['id'])/'output'
    folder.mkdir()
    (folder/'text.md').write_text('已有汇报结果', encoding='utf-8')
    jobs.artifact(task['id'], 'text.md', '文字汇报')
    for _ in range(2):
        restored = JobStore(jobs.root)
        restored.start()
        try:
            current = restored.get(task['id'])
            assert current['status'] == status and current['custom_title'] == '保留的自定义名称'
            assert current['progress'] == 70 and current['stage_progress'] == 50
            assert restored.history_status() == {'task_count':1,'recovered_tasks':0,'unreadable_tasks':0}
            with restored.open_resource(task['id'], 'text.md') as stream:
                assert stream.read().decode('utf-8') == '已有汇报结果'
        finally:
            restored.stop()


@pytest.mark.parametrize('broken', ['{', '[]', '{"id":"incomplete"}'])
def test_invalid_primary_record_recovers_from_backup(tmp_path, broken):
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['text'], source={}, options={}, title='待恢复记录', enqueue=False)
    jobs.update(task['id'], status='completed', progress=100, stage_progress=100)
    record = jobs.directory(task['id'])/'task.json'
    record.write_text(broken, encoding='utf-8')
    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert restored.get(task['id'])['status'] == 'completed'
        assert restored.history_status() == {'task_count':1,'recovered_tasks':1,'unreadable_tasks':0}
        assert json.loads(record.read_text(encoding='utf-8'))['id'] == task['id']
    finally:
        restored.stop()


def test_unrecoverable_record_stays_on_disk_and_is_reported_to_browser(tmp_path, monkeypatch):
    from routers import tasks
    jobs = JobStore(tmp_path)
    healthy = jobs.create(modes=['text'], source={}, options={}, enqueue=False)
    jobs.update(healthy['id'], status='completed')
    broken = tmp_path/'无法读取的任务'
    broken.mkdir()
    (broken/'task.json').write_bytes(b'{broken')
    (broken/'source.mp4').write_bytes(b'original media must survive')
    monkeypatch.setattr(tasks, 'store', jobs)
    app = FastAPI(); app.include_router(tasks.router)
    jobs.start()
    try:
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                listed = (await client.get('/api/v1/tasks')).json()
                assert [job['id'] for job in listed] == [healthy['id']]
                assert (await client.get('/api/v1/workspace')).json() == {'task_count':1,'recovered_tasks':0,'unreadable_tasks':1}
        asyncio.run(check())
        assert (broken/'task.json').read_bytes() == b'{broken'
        assert (broken/'source.mp4').read_bytes() == b'original media must survive'
    finally:
        jobs.stop()


def test_folder_migration_failure_cannot_hide_readable_history(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['text'], source={}, options={}, title='旧任务名称', enqueue=False)
    jobs.update(task['id'], status='completed')
    old = jobs.directory(task['id'])
    legacy = old.with_name(task['id'])
    old.rename(legacy)
    def fail(*args, **kwargs):
        raise PermissionError('directory held by another process')
    monkeypatch.setattr(Path, 'rename', fail)
    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert restored.get(task['id'])['title'] == '旧任务名称'
        assert restored.directory(task['id']) == legacy
        assert restored.history_status()['unreadable_tasks'] == 0
    finally:
        restored.stop()


def test_utf8_bom_record_remains_visible(tmp_path):
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['subtitles'], source={}, options={}, enqueue=False)
    jobs.update(task['id'], status='completed')
    record = jobs.directory(task['id'])/'task.json'
    record.write_bytes(b'\xef\xbb\xbf' + record.read_bytes())
    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert restored.get(task['id'])['id'] == task['id']
        assert restored.history_status()['recovered_tasks'] == 0
    finally:
        restored.stop()
