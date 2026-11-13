from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from local import media
from local.jobs import Cancelled, JobStore
from local.output_progress import OutputProgress


def test_stage_progress_is_independent_and_unknown_stage_clears_it(tmp_path):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['subtitles'], source={}, options={}, enqueue=False)
    jobs.progress(job['id'], 'transcribe', 38, 'Whisper 转写 69%', stage_progress=69)
    current = jobs.get(job['id'])
    assert current['progress'] == 38 and current['stage_progress'] == 69
    jobs.progress(job['id'], 'whisper_model', 24, '加载 Whisper 模型')
    current = jobs.get(job['id'])
    assert current['progress'] == 38 and current['stage_progress'] is None
    jobs.progress(job['id'], 'transcribe', 24, 'Whisper 转写 0%', stage_progress=0)
    jobs.update(job['id'], status='failed', message='处理失败')
    assert jobs.get(job['id'])['stage_progress'] is None
    jobs.retry(job['id'])
    assert jobs.get(job['id'])['progress'] == 0 and jobs.get(job['id'])['stage_progress'] is None


def test_processing_history_retains_older_records_after_many_updates(tmp_path):
    jobs = JobStore(tmp_path)
    job_id = jobs.create(modes=['text'], source={}, options={}, enqueue=False)['id']
    for index in range(205):
        jobs.progress(job_id, 'report', 80, f'完成第 {index + 1} 段', output_updates={})
    jobs.progress(job_id, 'export', 95, '导出 10%', stage_progress=10)
    jobs.progress(job_id, 'export', 99, '导出 90%', stage_progress=90)
    jobs.update(job_id, status='completed')
    events = jobs._read_record(jobs.directory(job_id) / 'task.json')['events']
    assert len(events) == 206
    assert events[0]['message'] == '完成第 1 段'
    assert events[-1]['message'] == '导出 90%'


def test_parallel_stages_keep_independent_start_times_and_progress(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['text', 'mindmap'], source={}, options={}, enqueue=False)
    now = 100.0
    monkeypatch.setattr('local.jobs.time.time', lambda: now)
    tracker = OutputProgress(jobs, job['id'], job['modes'])
    tracker('chapters', '章节规划完成', 100)
    tracker('report', '正文完成', 100)
    tracker('overview', '等待模型摘要')
    now = 110
    tracker('mindmap', '生成导图', 50)
    now = 115
    tracker('overview', '继续等待模型摘要')
    states = jobs.get(job['id'])['output_states']
    assert states['text']['stage_started_at'] == 100 and states['text']['stage_progress'] is None
    assert states['mindmap']['stage_started_at'] == 110 and states['mindmap']['stage_progress'] == 50
    assert states['text']['progress'] < states['mindmap']['progress']
    now = 120
    tracker('write_report', '写入报告', phase='write_text')
    assert jobs.get(job['id'])['output_states']['text']['stage_started_at'] == 120


def test_frame_progress_persists_at_stage_boundaries_and_throttles_intermediate_writes(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['illustrated'], source={}, options={}, enqueue=False)
    tracker = OutputProgress(jobs, job['id'], job['modes'])
    tick = 100.0
    writes = []
    monkeypatch.setattr('local.jobs.time.monotonic', lambda: tick)
    monkeypatch.setattr('local.jobs.atomic_json', lambda target, value: writes.append(target.name))
    for sample in range(8):
        tracker('illustrated_frames', '抽帧', sample * 12.5, phase='illustrate', fraction=0)
    assert jobs.get(job['id'])['output_states']['illustrated']['stage_progress'] == 87.5
    assert writes == ['task.json', 'task.backup.json']
    tick += 1
    tracker('illustrated_frames', '抽帧', 95, phase='illustrate', fraction=0)
    tracker('illustrated_frames', '抽帧', 100, phase='illustrate', fraction=0)
    assert writes == ['task.json', 'task.backup.json'] * 3


def test_total_progress_counts_shared_phases_once_and_reserves_work_for_slow_outputs(tmp_path):
    jobs = JobStore(tmp_path)
    modes = {'text', 'illustrated', 'download'}
    job_id = jobs.create(modes=sorted(modes), source={}, options={}, enqueue=False)['id']
    tracker = OutputProgress(jobs, job_id, modes)
    tracker.configure(download={'download', 'illustrated'}, transcribe=modes)
    tracker('transcribe', '转写', 50)
    assert tracker.total() == pytest.approx(100 * 21 / sum(
        tracker.weights[phase] for phase, consumers in tracker.phases.items() if consumers))
    tracker.complete('inspect', 'transcribe', 'transcript', 'chapters', 'report', 'overview', 'write_text')
    tracker.done('text')
    tracker('waiting_video', '下载视频', 50, phase='download', visible={'download'})
    current = jobs.get(job_id)
    assert current['output_states']['text']['progress'] == 100
    assert current['output_states']['download']['progress'] < 90
    assert current['output_states']['illustrated']['progress'] < 80
    assert current['progress'] < 80
    tracker.failed('download', '下载失败')
    tracker('export', '校验', visible=set())
    assert jobs.get(job_id)['progress'] == current['progress']


def test_recent_tasks_list_has_25_items_and_history_includes_older_records(tmp_path):
    jobs = JobStore(tmp_path)
    task_ids = [jobs.create(modes=['text'], source={}, options={}, enqueue=False)['id'] for _ in range(27)]
    summary = jobs.summary_page(25, 25)
    assert [task['id'] for task in summary['recent']] == list(reversed(task_ids[-25:]))
    assert [task['id'] for task in summary['items']] == list(reversed(task_ids[:2]))


@pytest.mark.parametrize('failure', [False, True], ids=['success', 'cancelled'])
def test_ffmpeg_progress_handles_partial_windows_lines_and_never_completes_cancelled_work(tmp_path, monkeypatch, failure):
    values, paths = [], []
    def run(args, check, timeout, *, on_poll):
        path = Path(args[args.index('-progress') + 1]); paths.append(path)
        path.write_bytes(b'out_time_us=10')  # No complete record yet.
        on_poll()
        with path.open('ab') as stream:
            stream.write(b'00000\r\nprogress=continue\r\n')
        on_poll()
        with path.open('ab') as stream:
            stream.write(b'out_time_us=N/A\r\nout_time_us=6500000\r\n')
        on_poll()
        if failure:
            raise Cancelled('cancelled')
        with path.open('ab') as stream:
            stream.write(b'out_time_us=2000000\nout_time_us=15000000\nprogress=end\n')
        on_poll()
        return 'done'
    monkeypatch.setattr(media, 'command', run)
    if failure:
        with pytest.raises(Cancelled):
            media.ffmpeg_command(['ffmpeg', '-i', 'source', 'target'], lambda: None, duration=10, progress=values.append)
        assert values == [0, 10, 65]
    else:
        assert media.ffmpeg_command(['ffmpeg', '-i', 'source', 'target'], lambda: None, duration=10, progress=values.append) == 'done'
        assert values == [0, 10, 65, 99, 100]
    assert not paths[0].parent.exists()


def test_whisper_reports_media_position_and_successful_completion(tmp_path, monkeypatch):
    (tmp_path / 'audio.wav').write_bytes(b'fixture')
    segments = [SimpleNamespace(start=n-25, end=n, text='中文内容', words=None) for n in (25, 50, 100)]
    model = SimpleNamespace(transcribe=lambda *args, **kwargs:(iter(segments), SimpleNamespace(duration=100, language='zh')))
    monkeypatch.setattr(media, '_load_whisper', lambda *args:model)
    monkeypatch.setattr(media, 'cuda_available', lambda:False)
    values, phases = [], []
    result, language = media.transcribe(tmp_path/'source.mp4', tmp_path, lambda:None, values.append,
                                       phase=lambda stage,percent:phases.append((stage,percent)))
    assert values == [0, 25, 50, 99, 100]
    assert phases == [('waiting', None), ('model', None)]
    assert len(result) == 3 and language == 'zh'


def test_failed_whisper_does_not_report_completion(tmp_path, monkeypatch):
    (tmp_path/'audio.wav').write_bytes(b'fixture')
    def broken():
        yield SimpleNamespace(start=0,end=5,text='中文',words=None)
        raise RuntimeError('inference failed')
    model = SimpleNamespace(transcribe=lambda *args, **kwargs:(broken(), SimpleNamespace(duration=10, language='zh')))
    monkeypatch.setattr(media, '_load_whisper', lambda *args:model)
    monkeypatch.setattr(media, 'cuda_available', lambda:False)
    values = []
    with pytest.raises(RuntimeError, match='inference failed'):
        media.transcribe(tmp_path/'source.mp4', tmp_path, lambda:None, values.append)
    assert values == [0, 50]


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='ffmpeg required')
def test_real_audio_extraction_has_its_own_progress(tmp_path, monkeypatch):
    source = tmp_path/'原始音轨 with spaces.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 32000)
    model = SimpleNamespace(transcribe=lambda *args,**kwargs:(iter([SimpleNamespace(start=0,end=2,text='测试字幕',words=None)]),SimpleNamespace(duration=2,language='zh')))
    monkeypatch.setattr(media, '_load_whisper', lambda *args:model)
    monkeypatch.setattr(media, 'cuda_available', lambda:False)
    phases = []
    media.transcribe(source, tmp_path, lambda:None, lambda _:None, duration=2,
                     phase=lambda stage,percent:phases.append((stage,percent)))
    audio = [percent for stage,percent in phases if stage == 'audio']
    assert audio[0] == 0 and audio[-1] == 100
    assert (tmp_path/'audio.wav').is_file()
