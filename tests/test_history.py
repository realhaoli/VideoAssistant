import asyncio
import errno
import json
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest

from local.jobs import JobStore


@pytest.mark.parametrize('status', ['completed', 'partial', 'failed', 'cancelled'])
def test_history_and_downloads_survive_repeated_restarts(tmp_path, status):
    jobs = JobStore(tmp_path/'data/tasks')
    task = jobs.create(modes=['text'], source={'type':'upload','filename':'source.mp4'}, options={}, title='历史任务😀', enqueue=False)
    jobs.rename(task['id'], '保留的自定义名称')
    jobs.update(task['id'], status=status, stage='report', progress=70, stage_progress=50)
    jobs.update_output(task['id'],'text',status='completed',progress=100,message='已完成')
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
            assert current['output_states']['text']['status']=='completed'
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
        assert not (tmp_path/'旧任务名称').exists()
    finally:
        restored.stop()


def test_url_job_retries_in_place_when_source_title_rename_is_denied(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    task = jobs.create(modes=['subtitles'], source={'type':'url', 'url':'https://example.test/replay'},
                       options={}, title='example.test', enqueue=False)
    job_id = task['id']
    folder = jobs.directory(job_id)
    title = '发布会回放'
    (folder/'source_info.json').write_text(json.dumps({'title':title, 'duration':5}), encoding='utf-8')
    (folder/'platform.zh.srt').write_text('1\n00:00:00,000 --> 00:00:05,000\n发布会字幕\n', encoding='utf-8')
    partial = folder/'source.mp4.part'
    partial.write_bytes(b'existing partial download')
    jobs.update(job_id, status='failed', error='previous rename failed')
    rename = Path.rename
    attempts = []

    def deny_rename(path, target):
        if path == folder:
            attempts.append(target)
            raise PermissionError(errno.EACCES, 'Access is denied', str(path), 5, str(target))
        return rename(path, target)

    monkeypatch.setattr(Path, 'rename', deny_rename)
    jobs.start()
    try:
        jobs.retry(job_id)
        jobs.queue.join()
        assert jobs.get(job_id)['status'] == 'completed'
        assert jobs.directory(job_id) == folder
        assert '发布会字幕' in (folder/'output/subtitles.zh.txt').read_text(encoding='utf-8')
        assert partial.read_bytes() == b'existing partial download'
        assert attempts == [tmp_path/title]
        assert not (tmp_path/title).exists()
        for filename in ('task.json', 'task.backup.json'):
            record = json.loads((folder/filename).read_text(encoding='utf-8'))
            assert record['title'] == title and record['storage_folder'] == folder.name
            assert record['source_title_resolved'] is True
    finally:
        jobs.stop()

    restored = JobStore(tmp_path)
    restored.start()
    try:
        assert restored.get(job_id)['status'] == 'completed'
        assert restored.directory(job_id) == folder
        assert restored.history_status()['unreadable_tasks'] == 0
        assert restored.source_title(job_id, title) == folder
        assert len(attempts) == 1
    finally:
        restored.stop()


def test_source_title_rename_keeps_existing_same_name_task(tmp_path):
    jobs = JobStore(tmp_path)
    existing = jobs.create(modes=['text'], source={}, options={}, title='发布会', enqueue=False)
    task = jobs.create(modes=['text'], source={'type':'url'}, options={}, title='example.test', enqueue=False)
    original = jobs.directory(task['id'])
    folder = jobs.source_title(task['id'], '发布会')
    assert folder == tmp_path/'发布会 (2)' and not original.exists()
    assert jobs.directory(existing['id']) == tmp_path/'发布会'
    assert json.loads((folder/'task.json').read_text(encoding='utf-8'))['storage_folder'] == folder.name


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
