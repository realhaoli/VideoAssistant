from pathlib import Path
import shutil
from types import SimpleNamespace
import wave

import pytest

from local import media
from local.jobs import Cancelled, JobStore


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
