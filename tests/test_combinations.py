"""Every nonempty output combination, with upload/link and ASR/native captions."""
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
import json
import threading
import time

from PIL import Image
import pytest

from local import exports, media, pdf_export, pipeline
from local.intelligence import Intelligence
from local.jobs import JobStore, OutputFailure

MODES = ['illustrated','text','mindmap','subtitles','download']
COMBINATIONS = [list(group) for size in range(1,6) for group in combinations(MODES,size)]


def prepare(tmp_path, monkeypatch, modes, source='upload', native=True):
    monkeypatch.setattr('local.jobs.load', lambda:{})
    jobs = JobStore(tmp_path/'tasks')
    task = jobs.create(modes=modes, source={'type':source,'filename':'source.mp4','url':'https://example.test/video'},
                       options={'clean_subtitles':False}, title='组合测试', enqueue=False)
    folder = jobs.directory(task['id'])
    if source == 'upload':
        (folder/'source.mp4').write_bytes(b'fixture')
    Image.new('RGB',(20,10),'white').save(folder/'cover.jpg')
    cues = [{'start':0,'end':7200,'text':'电池容量100kWh，续航目标600km。'}]
    tracks = {'zh':cues,'en':cues} if native else {}
    downloads, calls = [], []
    def download(url, target, *, best, audio, check, progress):
        target.mkdir(exist_ok=True)
        path = target/('source.m4a' if audio else 'source.mp4')
        path.write_bytes(b'fixture')
        (target/'download-complete.json').write_text(json.dumps({'best':best,'audio':audio,'filename':path.name}),encoding='utf-8')
        downloads.append((best,audio)); progress(100)
        return path
    monkeypatch.setattr(media,'download',download)
    monkeypatch.setattr(media,'inspect_url',lambda *args:({'title':'组合测试','duration':7200}, tracks.copy()))
    monkeypatch.setattr(media,'probe',lambda *args:{'format':{'duration':'7200'},'streams':[{'codec_type':'video'},{'codec_type':'audio'}]})
    monkeypatch.setattr(media,'embedded_subtitles',lambda *args:tracks.copy())
    def transcribe(*args, **kwargs):
        calls.append('asr');args[3](50);return cues,'zh'
    monkeypatch.setattr(media,'transcribe',transcribe)
    monkeypatch.setattr(Intelligence,'plan',lambda *args:{'title':'组合测试','chapters':[{'title':'电池技术','start':0,'end':7200,'points':['容量100kWh']}]})
    def draft(*args):calls.append('draft');return '电池容量100kWh，续航目标600km。'
    monkeypatch.setattr(Intelligence,'draft',draft)
    monkeypatch.setattr(Intelligence,'overview',lambda *args:{'one_sentence':'电池容量100kWh。','key_conclusions':[],'data_terms':[]})
    monkeypatch.setattr(Intelligence,'transform',lambda self,segments,**kwargs:segments)
    monkeypatch.setattr(media,'candidates',lambda *args:[])
    monkeypatch.setattr(pdf_export,'render',lambda folder,check:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    def embed(source,zh,en,target,check,**kwargs):
        calls.append('embed');kwargs['progress'](50);target.write_bytes(b'finished video')
    monkeypatch.setattr(media,'embed',embed)
    jobs.update(task['id'],status='running')
    return jobs, task, folder, downloads, calls


@pytest.mark.parametrize('modes', COMBINATIONS, ids=lambda modes:'+'.join(modes))
@pytest.mark.parametrize('source,native', [('upload',True),('upload',False),('url',True),('url',False)])
def test_all_output_combinations_complete_without_unselected_outputs(tmp_path, monkeypatch, modes, source, native):
    jobs, task, folder, downloads, calls = prepare(tmp_path,monkeypatch,modes,source,native)
    pipeline.run(jobs,task['id'])
    finished = jobs.get(task['id'])
    expected = {name for mode in modes for name,_,_ in exports.ARTIFACTS[mode]}
    assert {a['name'] for a in finished['artifacts']} == expected
    assert all(value['status']=='completed' for value in finished['output_states'].values())
    assert sum(not audio for _,audio in downloads) <= 1, 'do not download two full video copies'
    before = list(calls)
    pipeline.run(jobs,task['id'])
    assert calls == before, 'retry must reuse already completed outputs'


def test_pdf_failure_preserves_other_outputs_and_retry_only_rebuilds_pdf(tmp_path,monkeypatch):
    jobs,task,folder,downloads,calls = prepare(tmp_path,monkeypatch,MODES)
    def fail(*args):raise RuntimeError('PDF renderer failed')
    monkeypatch.setattr(pdf_export,'render',fail)
    with pytest.raises(OutputFailure) as error:
        pipeline.run(jobs,task['id'])
    assert set(error.value.errors)=={'mindmap'}
    assert (folder/'output/video.mp4').is_file() and (folder/'output/text.html').is_file()
    assert jobs.get(task['id'])['output_states']['mindmap']['status']=='failed'
    before = list(calls)
    monkeypatch.setattr(pdf_export,'render',lambda folder,check:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    pipeline.run(jobs,task['id'])
    assert calls == before
    assert jobs.get(task['id'])['output_states']['mindmap']['status']=='completed'


def test_frame_failure_does_not_block_text_or_pdf(tmp_path, monkeypatch):
    jobs, task, folder, _, calls = prepare(tmp_path, monkeypatch, ['illustrated', 'text', 'mindmap'])

    def broken_frames(*args):
        assert (folder / 'output/text.html').is_file()
        raise OSError('frame decoding failed')

    monkeypatch.setattr(media, 'candidates', broken_frames)
    with pytest.raises(OutputFailure) as failure:
        pipeline.run(jobs, task['id'])
    assert set(failure.value.errors) == {'illustrated'}
    assert (folder / 'output/mindmap.pdf').is_file()
    monkeypatch.setattr(media, 'candidates', lambda *args: [])
    pipeline.run(jobs, task['id'])
    assert calls.count('draft') == 1
    assert (folder / 'output/illustrated.zip').is_file()


def test_existing_subtitles_let_report_finish_while_shared_video_downloads(tmp_path, monkeypatch):
    jobs, task, folder, downloads, _ = prepare(tmp_path, monkeypatch, ['illustrated', 'text', 'download'], source='url')
    started, release = threading.Event(), threading.Event()
    download = media.download

    def slow_download(*args, **kwargs):
        started.set()
        assert release.wait(8)
        return download(*args, **kwargs)

    monkeypatch.setattr(media, 'download', slow_download)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(pipeline.run, jobs, task['id'])
        try:
            assert started.wait(3)
            deadline = time.monotonic() + 5
            while not (folder / 'output/text.html').is_file():
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert not (folder / 'output/video.mp4').exists()
        finally:
            release.set()
        pending.result(timeout=5)
    assert downloads == [(True, False)]
    assert (folder / 'output/video.mp4').is_file()


def test_retry_with_current_model_rebuilds_unfinished_model_stages(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['text'])
    jobs.update(task['id'], options={'clean_subtitles': True})
    snapshot = folder / 'model-settings.json'
    snapshot.write_text('{"llm_model":"model-a","whisper_model":"base"}', encoding='utf-8')
    seen = []

    def transform(engine, cues, **kwargs):
        seen.append(engine.config['llm_model'])
        return cues

    monkeypatch.setattr(Intelligence, 'transform', transform)
    overview = Intelligence.overview

    def broken_overview(*args):
        raise RuntimeError('model endpoint failed')

    monkeypatch.setattr(Intelligence, 'overview', broken_overview)
    with pytest.raises(OutputFailure):
        pipeline.run(jobs, task['id'])
    jobs.update(task['id'], status='failed')
    monkeypatch.setattr('local.jobs.load', lambda: {'llm_model': 'model-b', 'whisper_model': 'small'})
    jobs.retry(task['id'], use_current_model=True)
    monkeypatch.setattr(Intelligence, 'overview', overview)
    pipeline.run(jobs, task['id'])
    assert seen == ['model-a', 'model-b']
    assert jobs.settings(task['id'])['whisper_model'] == 'base'
    assert (folder / 'output/text.html').is_file()


def test_video_is_published_while_report_is_still_waiting(tmp_path,monkeypatch):
    jobs,task,folder,*_ = prepare(tmp_path,monkeypatch,['text','download'])
    entered, release = threading.Event(), threading.Event()
    def slow_draft(*args):
        entered.set()
        assert release.wait(5)
        return '电池容量100kWh。'
    monkeypatch.setattr(Intelligence,'draft',slow_draft)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(pipeline.run,jobs,task['id'])
        try:
            assert entered.wait(3)
            deadline=time.monotonic()+3
            while not any(a['name']=='video.mp4' for a in jobs.get(task['id'])['artifacts']):
                assert time.monotonic()<deadline
                time.sleep(.01)
            assert not future.done()
            assert jobs.get(task['id'])['progress'] < 99
            with pytest.raises(ValueError,match='取消'):
                jobs.remove(task['id'])
        finally:
            release.set()
        future.result(timeout=5)


def test_worker_marks_partial_then_successful_retry(tmp_path,monkeypatch):
    jobs,task,folder,*_ = prepare(tmp_path,monkeypatch,MODES)
    monkeypatch.setattr(pdf_export,'render',lambda *args:(_ for _ in ()).throw(RuntimeError('PDF failed')))
    jobs.update(task['id'],status='queued')
    jobs.start()
    try:
        jobs.queue.join()
        assert jobs.get(task['id'])['status']=='partial'
        monkeypatch.setattr(pdf_export,'render',lambda folder,check:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
        jobs.retry(task['id']);jobs.queue.join()
        assert jobs.get(task['id'])['status']=='completed'
    finally:
        jobs.stop()
