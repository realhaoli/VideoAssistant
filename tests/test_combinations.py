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
ORIGINAL_DRAFT = Intelligence.draft


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
    def download(url, target, *, best, audio, check, progress, resource_id=None):
        target.mkdir(exist_ok=True)
        path = target/('source.m4a' if audio else 'source.mp4')
        path.write_bytes(b'fixture')
        (target/'download-complete.json').write_text(json.dumps({'best':best,'audio':audio,'filename':path.name}),encoding='utf-8')
        downloads.append((best,audio)); progress(100)
        return path
    monkeypatch.setattr(media,'download',download)
    monkeypatch.setattr(media,'inspect_url',lambda *args, **kwargs:({'title':'组合测试','duration':7200}, tracks.copy()))
    monkeypatch.setattr(media,'probe',lambda *args:{'format':{'duration':'7200'},'streams':[{'codec_type':'video'},{'codec_type':'audio'}]})
    monkeypatch.setattr(media,'embedded_subtitles',lambda *args:tracks.copy())
    def transcribe(*args, **kwargs):
        calls.append('asr');args[3](50);return cues,'zh'
    monkeypatch.setattr(media,'transcribe',transcribe)
    monkeypatch.setattr(Intelligence,'plan',lambda *args:{'title':'组合测试','chapters':[{'title':'电池技术','start':0,'end':7200,'points':['容量100kWh']}]})
    def draft(*args, source_language='zh'):
        calls.append('draft')
        body = '电池容量100kWh，续航目标600km。'
        return body if source_language == 'zh' else {'body': body, 'facts': []}
    monkeypatch.setattr(Intelligence,'draft',draft)
    monkeypatch.setattr(Intelligence,'overview',lambda *args:{'one_sentence':'电池容量100kWh。','key_conclusions':[],'data_terms':[]})
    monkeypatch.setattr(Intelligence,'transform',lambda self,segments,**kwargs:segments)
    monkeypatch.setattr(media,'candidates',lambda *args, **kwargs:[])
    monkeypatch.setattr(pdf_export,'render',lambda folder,check,**kwargs:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    def export_video(source,target,details,check,**kwargs):
        calls.append('export_video');kwargs['progress'](50);target.write_bytes(b'finished video')
    monkeypatch.setattr(media,'export_video',export_video)
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
    assert calls.count('asr') <= 1, 'do not transcribe separately for each output'
    if modes == ['download']:
        assert calls == (['export_video'] if native else ['asr', 'export_video'])
    before = list(calls)
    pipeline.run(jobs,task['id'])
    assert calls == before, 'retry must reuse already completed outputs'


@pytest.mark.parametrize('track, asr_language, expected', [
    ('zh', None, []), ('en', None, ['zh']), ('original', None, ['zh']),
    (None, 'zh', []), (None, 'en', ['zh']),
])
@pytest.mark.parametrize('with_document', [False, True])
def test_video_and_subtitle_files_share_translation_while_reports_use_source(tmp_path, monkeypatch, track, asr_language, expected, with_document):
    monkeypatch.delenv('ASR_CLEANUP_MODE', raising=False)
    modes = ['download', 'subtitles', 'text'] if with_document else ['download']
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, modes)
    jobs.update(task['id'], options={'clean_subtitles': True})
    cues = [{'start': 0, 'end': 1, 'text': '字幕样本'}]
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {track: cues} if track else {})
    spoken = [*cues, {'start': 1, 'end': 2, 'text': 'Um.' if asr_language == 'en' else '嗯。'}]
    monkeypatch.setattr(media, 'transcribe', lambda *args, **kwargs: (spoken, asr_language) if asr_language else pytest.fail('existing subtitles should bypass ASR'))
    calls = []

    def transform(engine, segments, *, language=None, clean=False, **kwargs):
        stage = 'clean' if clean else language
        calls.append(stage)
        return [{**cue, 'text': f'{cue["text"]} {stage}'} for cue in segments]

    monkeypatch.setattr(Intelligence, 'transform', transform)
    plan = Intelligence.plan
    report_cues = []

    def report_plan(engine, segments, *args):
        report_cues.extend(segments)
        return plan(engine, segments, *args)

    monkeypatch.setattr(Intelligence, 'plan', report_plan)
    pipeline.run(jobs, task['id'])
    assert (folder / 'output/video.mp4').is_file()
    video_text = (folder / 'subtitles.zh.srt').read_text(encoding='utf-8').splitlines()[2]
    assert video_text == ' '.join(['字幕样本', *expected])
    if asr_language:
        assert '00:00:02,000' not in (folder / 'subtitles.zh.srt').read_text(encoding='utf-8')
    english = folder / 'output/subtitles.en.txt'
    if with_document:
        assert report_cues == cues
        assert video_text in (folder / 'output/subtitles.zh.txt').read_text(encoding='utf-8')
    if with_document and (track or asr_language) == 'en':
        assert english.read_text(encoding='utf-8').splitlines()[1] == '字幕样本'
        assert {a['name'] for a in jobs.get(task['id'])['artifacts']} >= {'subtitles.en.txt', 'subtitles.zh.txt'}
    else:
        assert not english.exists()
    assert calls == expected


def test_existing_subtitles_allow_video_when_report_generation_fails(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['download', 'text'])
    jobs.update(task['id'], options={'clean_subtitles': True})

    def unavailable(*args, **kwargs):
        raise RuntimeError('model unavailable')

    monkeypatch.setattr(Intelligence, 'overview', unavailable)
    with pytest.raises(OutputFailure) as failure:
        pipeline.run(jobs, task['id'])
    assert set(failure.value.errors) == {'text'}
    assert (folder / 'output/video.mp4').is_file()
    assert jobs.get(task['id'])['output_states']['download']['status'] == 'completed'


def test_english_illustrated_report_uses_verified_source_time_without_translation(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated'])
    quote = 'The display shows a battery capacity of 100 kWh.'
    cues = [{'start': 0, 'end': 4, 'text': 'Welcome to the presentation.'},
            {'start': 44, 'end': 49, 'text': quote}]
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {'en': cues})
    monkeypatch.setattr(Intelligence, 'transform', lambda *args, **kwargs: pytest.fail('报告无需整轨翻译'))
    body = '屏幕显示电池容量为100kWh。'
    prompts = []

    def chat(engine, prompt, **kwargs):
        prompts.append(prompt)
        if kwargs.get('json_result'):
            return {'facts': [
                {'cue_index': 0, 'quote': quote, 'text': '错误时间点的事实。'},
                {'cue_index': 1, 'quote': quote, 'text': body},
            ]}
        return body

    def candidates(path, chapter, output, index, used, check, **kwargs):
        assert chapter['evidence_time'] == 44
        assert chapter['evidence_cue_id'] == 1
        assert chapter['evidence_text'] == quote
        return [dict(path=folder / 'cover.jpg', time=45, hash=0)]

    monkeypatch.setattr(Intelligence, 'draft', ORIGINAL_DRAFT)
    monkeypatch.setattr(Intelligence, 'chat', chat)
    monkeypatch.setattr(media, 'candidates', candidates)
    monkeypatch.setattr(Intelligence, 'select',
                        lambda engine, chapter, images, text: [dict(candidate=images[0], caption='容量画面')])
    pipeline.run(jobs, task['id'])
    assert quote in prompts[0] and quote in prompts[-1]
    assert '错误时间点的事实' not in prompts[-1]
    assert not (folder / 'subtitles.zh.srt').exists()
    report = (folder / 'output/illustrated.md').read_text(encoding='utf-8')
    assert body in report and '![容量画面]' in report
    assert json.loads((folder / 'image-selections.json').read_text(encoding='utf-8'))[0]['evidence_cue_id'] == 1


def test_english_report_finishes_while_translation_waits_and_survives_its_failure(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['text', 'subtitles', 'download'], source='url')
    cues = [{'start': 0, 'end': 5, 'text': 'The battery capacity is 100 kWh.'}]
    monkeypatch.setattr(media, 'inspect_url', lambda *args, **kwargs: ({'title': '组合测试', 'duration': 7200}, {'en': cues}))
    translation_started, release = threading.Event(), threading.Event()

    def transform(engine, segments, **kwargs):
        assert segments == cues and kwargs['language'] == 'zh'
        translation_started.set()
        assert release.wait(8)
        raise RuntimeError('translation gateway failed')

    monkeypatch.setattr(Intelligence, 'transform', transform)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(pipeline.run, jobs, task['id'])
        try:
            assert translation_started.wait(3)
            deadline = time.monotonic() + 5
            while jobs.output_states(task['id'])['text']['status'] != 'completed':
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert not (folder / 'output/video.mp4').exists()
        finally:
            release.set()
        with pytest.raises(OutputFailure) as failure:
            running.result(timeout=5)
    assert set(failure.value.errors) == {'subtitles', 'download'}
    assert (folder / 'output/text.html').is_file()


def test_pdf_failure_preserves_other_outputs_and_retry_only_rebuilds_pdf(tmp_path,monkeypatch):
    jobs,task,folder,downloads,calls = prepare(tmp_path,monkeypatch,MODES)
    def fail(*args, **kwargs):raise RuntimeError('PDF renderer failed')
    monkeypatch.setattr(pdf_export,'render',fail)
    with pytest.raises(OutputFailure) as error:
        pipeline.run(jobs,task['id'])
    assert set(error.value.errors)=={'mindmap'}
    assert (folder/'output/video.mp4').is_file() and (folder/'output/text.html').is_file()
    assert jobs.get(task['id'])['output_states']['mindmap']['status']=='failed'
    before = list(calls)
    monkeypatch.setattr(pdf_export,'render',lambda folder,check,**kwargs:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
    pipeline.run(jobs,task['id'])
    assert calls == before
    assert jobs.get(task['id'])['output_states']['mindmap']['status']=='completed'


def test_frame_failure_does_not_block_text_or_pdf(tmp_path, monkeypatch):
    jobs, task, folder, _, calls = prepare(tmp_path, monkeypatch, ['illustrated', 'text', 'mindmap'])

    def broken_frames(*args, **kwargs):
        raise OSError('frame decoding failed')

    monkeypatch.setattr(media, 'candidates', broken_frames)
    with pytest.raises(OutputFailure) as failure:
        pipeline.run(jobs, task['id'])
    assert set(failure.value.errors) == {'illustrated'}
    assert (folder / 'output/text.html').is_file()
    assert (folder / 'output/mindmap.pdf').is_file()
    monkeypatch.setattr(media, 'candidates', lambda *args, **kwargs: [])
    pipeline.run(jobs, task['id'])
    assert calls.count('draft') == 1
    assert (folder / 'output/illustrated.zip').is_file()


def test_long_chapter_reuses_completed_figures_after_a_later_segment_fails(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated'])
    selected = []
    interrupt = True
    subjects = ['定价策略', '底盘结构', '座舱屏幕', '辅助驾驶']
    cues = [{'start': part * 1800 + 40, 'end': part * 1800 + 50, 'text': f'{subject}展示{part}项技术细节。'}
            for part, subject in enumerate(subjects)]
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {'zh': cues})
    monkeypatch.setattr(Intelligence, 'draft', lambda *args: '\n\n'.join(cue['text'] for cue in cues))

    def candidates(path, chapter, output, index, used, check, **kwargs):
        start = chapter['evidence_time']
        part = int(start // 1800)
        if interrupt and part == 2:
            raise OSError('fixture interrupted')
        return [dict(path=folder/'cover.jpg', time=start+1, hash=((1 << 24) - 1) << (24*part))]

    def select(engine, chapter, images, body):
        selected.append((chapter['evidence_time'], body))
        return [dict(candidate=images[0], caption='技术画面')]

    monkeypatch.setattr(media, 'candidates', candidates)
    monkeypatch.setattr(Intelligence, 'select', select)
    with pytest.raises(OutputFailure):
        pipeline.run(jobs, task['id'])
    assert len(list(folder.glob('model-checkpoints/**/figure-*.json'))) == 2
    interrupt = False
    pipeline.run(jobs, task['id'])
    assert selected == [(cue['start'], cue['text']) for cue in cues]
    assert len(json.loads((folder/'image-selections.json').read_text(encoding='utf-8'))) == 4
    report = (folder / 'output' / 'illustrated.md').read_text(encoding='utf-8')
    for part, cue in enumerate(cues, 1):
        assert cue['text'] + f'\n\n![技术画面](image/chapter-01-{part:02d}.jpg)' in report


@pytest.mark.parametrize('vision_unavailable', [False, True])
def test_unrelated_or_unreviewed_frames_are_not_published(tmp_path, monkeypatch, vision_unavailable):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated'])
    searches = []
    reviewed = []

    def candidates(path, chapter, output, index, used, check, *, exclude_times, **kwargs):
        radius = chapter['search_radius']
        searches.append((radius, chapter['previous_search_radius'], list(exclude_times)))
        return [dict(path=folder / 'cover.jpg', time=radius, hash=((1 << 16) - 1) << radius)]

    def select(engine, chapter, images, body):
        reviewed.extend(image['time'] for image in images)
        if vision_unavailable:
            raise TimeoutError('gateway timeout')
        return []

    monkeypatch.setattr(media, 'candidates', candidates)
    monkeypatch.setattr(Intelligence, 'select', select)
    pipeline.run(jobs, task['id'])
    assert searches == ([(8, None, [])] if vision_unavailable else [(8, None, []), (24, 8, [8]), (48, 24, [8, 24])])
    assert reviewed == ([8] if vision_unavailable else [8, 24, 48])
    assert json.loads((folder / 'image-selections.json').read_text(encoding='utf-8')) == []
    assert '电池容量100kWh' in (folder / 'output' / 'illustrated.md').read_text(encoding='utf-8')
    assert (folder / 'output' / 'illustrated.zip').is_file()


def test_expanded_rounds_skip_near_duplicate_frames_across_chapters(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated'])
    cues = [{'start': 60, 'end': 65, 'text': '电池容量100kWh。'},
            {'start': 180, 'end': 185, 'text': '座舱屏幕展示导航界面。'}]
    chapters = [{'title': '电池参数', 'start': 0, 'end': 120},
                {'title': '座舱界面', 'start': 120, 'end': 7200}]
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {'zh': cues})
    monkeypatch.setattr(Intelligence, 'plan', lambda *args: {'title': '组合测试', 'chapters': chapters})
    monkeypatch.setattr(Intelligence, 'draft', lambda engine, chapter, *args: cues[chapter['start'] > 0]['text'])
    reviewed = []
    searches = []
    rejected_hash = (1 << 24) - 1
    final_hash = rejected_hash << 32

    def candidates(path, chapter, output, index, used, check, **kwargs):
        radius = chapter['search_radius']
        searches.append((index, radius))
        hashes = {8: [0] if index == 0 else [1, rejected_hash],
                  24: [2, rejected_hash ^ 1], 48: [3, final_hash]}[radius]
        return [dict(path=folder / 'cover.jpg', time=chapter['evidence_time'] + radius + i, hash=value)
                for i, value in enumerate(hashes)]

    def select(engine, chapter, images, body):
        hashes = [image['hash'] for image in images]
        reviewed.append(hashes)
        return [] if hashes == [rejected_hash] else [dict(candidate=images[0], caption='技术画面')]

    monkeypatch.setattr(media, 'candidates', candidates)
    monkeypatch.setattr(Intelligence, 'select', select)
    pipeline.run(jobs, task['id'])
    assert searches == [(0, 8), (1, 8), (1, 24), (1, 48)]
    assert reviewed == [[0], [rejected_hash], [final_hash]]
    assert [entry['time'] for entry in json.loads((folder / 'image-selections.json').read_text())] == [68, 229]


def test_illustrated_report_leaves_background_without_inline_images(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated'])
    text = '发布会首先向长期关注品牌发展的来宾表示感谢。'
    cue = {'start': 0, 'end': 12, 'text': text}
    monkeypatch.setattr(media, 'embedded_subtitles', lambda *args: {'zh': [cue]})
    monkeypatch.setattr(Intelligence, 'draft', lambda *args: '### 产品展示\n\n' + text)
    monkeypatch.setattr(media, 'candidates', lambda *args, **kwargs: pytest.fail('背景段落无需抽帧'))
    pipeline.run(jobs, task['id'])
    assert json.loads((folder / 'image-selections.json').read_text(encoding='utf-8')) == []
    report = (folder / 'output/illustrated.md').read_text(encoding='utf-8')
    assert text in report and '![视频封面]' in report


def test_illustrations_use_distinct_product_points_inside_a_short_chapter():
    body = ("### 技术要点\n"
            "- 车底风扇可提供高达2G吸力，支持倒挂行驶。\n"
            "- 后尾翼具有可变倾角，可动态调节下压力。\n\n"
            "### 风险与限制\n\n"
            "风扇演示成功并不保证。")
    cues = [{'start': 12, 'end': 16, 'text': '车底风扇能产生高达2G的吸力。'},
            {'start': 30, 'end': 35, 'text': '后尾翼具有可变倾角。'},
            {'start': 42, 'end': 46, 'text': '风扇演示并不保证成功。'}]
    blocks, targets = pipeline._illustration_targets({'start': 0, 'end': 60}, body, cues, lambda: None)
    assert [(blocks[position], cue['start']) for position, cue in targets] == [
        ('- 车底风扇可提供高达2G吸力，支持倒挂行驶。', 12),
        ('- 后尾翼具有可变倾角，可动态调节下压力。', 30),
    ]


def test_video_outputs_start_when_both_report_slots_are_occupied(tmp_path, monkeypatch):
    jobs, first, _, _, _ = prepare(tmp_path, monkeypatch, ['text'])
    occupied = threading.Event()
    release = threading.Event()
    report_count = 0
    lock = threading.Lock()
    draft = Intelligence.draft

    def slow_draft(*args):
        nonlocal report_count
        with lock:
            report_count += 1
            if report_count == 2:
                occupied.set()
        assert release.wait(8)
        return draft(*args)

    def submit(modes):
        task = jobs.create(modes=modes, source={'type':'upload', 'filename':'source.mp4'},
                           options={}, title='排队测试', enqueue=False)
        (jobs.directory(task['id']) / 'source.mp4').write_bytes(b'fixture')
        jobs.update(task['id'], status='queued')
        jobs.queue.put(task['id'])
        return task['id']

    monkeypatch.setattr(Intelligence, 'draft', slow_draft)
    jobs.update(first['id'], status='queued')
    jobs.start()
    try:
        submit(['text'])
        assert occupied.wait(4)
        mixed = submit(['text', 'download'])
        only_video = submit(['download'])
        deadline = time.monotonic() + 4
        while any(not (jobs.directory(task) / 'output/video.mp4').exists() for task in (mixed, only_video)):
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert jobs.get(mixed)['output_states']['text']['status'] == 'queued'
    finally:
        release.set()
        jobs.stop()


def test_slow_remote_download_does_not_hold_the_video_export_worker(tmp_path, monkeypatch):
    jobs, remote, _, _, _ = prepare(tmp_path, monkeypatch, ['download'], source='url')
    jobs.update(remote['id'], status='queued')
    entered, release = threading.Event(), threading.Event()
    download = media.download

    def slow_download(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return download(*args, **kwargs)

    monkeypatch.setattr(media, 'download', slow_download)
    jobs.start()
    try:
        assert entered.wait(2)
        upload = jobs.create(modes=['download'], source={'type':'upload', 'filename':'source.mp4'},
                             options={}, title='本地上传', enqueue=False)
        folder = jobs.directory(upload['id'])
        (folder / 'source.mp4').write_bytes(b'fixture')
        jobs.update(upload['id'], status='queued')
        jobs.queue.put(upload['id'])
        deadline = time.monotonic() + 3
        while jobs.get(upload['id'])['status'] != 'completed':
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert (folder / 'output/video.mp4').is_file()
        assert jobs.get(remote['id'])['status'] == 'running'
    finally:
        release.set()
        jobs.stop()


@pytest.mark.parametrize('failure', ['bundle', 'install'])
def test_report_publishes_no_new_files_if_bundle_or_install_fails(tmp_path, monkeypatch, failure):
    import zipfile

    output = tmp_path / 'output'
    output.mkdir()
    lock = threading.RLock()
    exports.install_report(output, 'illustrated', '# 旧版', lock, lambda: None)
    names = exports.report_files('illustrated')
    old = {name: (output / name).read_bytes() for name in names}
    replace = exports.replace_file

    def fail_install(source, target):
        if target == output / 'illustrated.html' and source.parent.name.startswith('.report-stage-'):
            raise OSError('fixture install failure')
        return replace(source, target)

    def fail_bundle(*args, **kwargs):
        raise OSError('fixture package failure')

    with monkeypatch.context() as patch:
        if failure == 'install':
            patch.setattr(exports, 'replace_file', fail_install)
        else:
            patch.setattr(exports, 'bundle', fail_bundle)
        with pytest.raises(OSError, match='fixture'):
            exports.install_report(output, 'illustrated', '# 新版', lock, lambda: None)
    assert {name: (output / name).read_bytes() for name in names} == old
    exports.install_report(output, 'illustrated', '# 新版', lock, lambda: None)
    with zipfile.ZipFile(output / 'illustrated.zip') as archive:
        assert archive.read('illustrated.html') == (output / 'illustrated.html').read_bytes()
    assert not list(tmp_path.glob('.report-stage-*'))


def test_restart_rolls_back_an_interrupted_report_install(tmp_path, monkeypatch):
    monkeypatch.setattr('local.jobs.load', lambda: {})
    jobs = JobStore(tmp_path)
    job = jobs.create(modes=['illustrated'], source={}, options={}, enqueue=False)
    folder = jobs.directory(job['id'])
    output = folder / 'output'
    output.mkdir()
    exports.install_report(output, 'illustrated', '# 旧版', jobs.lock, lambda: None)
    names = exports.report_files('illustrated')
    originals = {name: (output / name).read_bytes() for name in names}
    exports.publish(jobs, job['id'])
    jobs.update(job['id'], status='completed')

    staged = folder / '.report-stage-interrupted'
    backup = staged / 'previous'
    backup.mkdir(parents=True)
    (staged / 'install.json').write_text(json.dumps({'mode':'illustrated', 'originals':names}), encoding='utf-8')
    for name in names[:2]:
        (output / name).replace(backup / name)
    (output / names[0]).write_bytes(b'unfinished new version')

    recovered = JobStore(tmp_path)
    recovered.start()
    try:
        assert recovered.get(job['id'])['status'] == 'completed'
        assert {name: (output / name).read_bytes() for name in names} == originals
        assert not staged.exists()
    finally:
        recovered.stop()


def test_failed_shared_plan_names_the_stage_and_preserves_subtitles(tmp_path, monkeypatch):
    from local import model_client

    plan = Intelligence.plan
    modes = ['illustrated', 'text', 'mindmap', 'subtitles']
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, modes)
    monkeypatch.setattr(Intelligence, 'plan', plan)
    requests = []

    def incomplete(*args):
        requests.append(args[4])
        return '{"'

    monkeypatch.setattr(model_client, 'complete', incomplete)
    with pytest.raises(OutputFailure) as failure:
        pipeline.run(jobs, task['id'])
    assert len(requests) == 2
    assert set(failure.value.errors) == {'illustrated', 'text', 'mindmap'}
    assert all(message.startswith('章节规划失败') for message in failure.value.errors.values())
    assert (folder / 'output/subtitles.zh.txt').is_file()
    assert not (folder / 'chapters.json').exists()


def test_pdf_is_published_and_frames_start_while_report_summary_is_waiting(tmp_path, monkeypatch):
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['illustrated', 'text', 'mindmap'])
    summary_started, frames_started, release = threading.Event(), threading.Event(), threading.Event()
    overview = Intelligence.overview

    def slow_overview(*args):
        summary_started.set()
        assert release.wait(8)
        return overview(*args)

    def sample_frames(*args, **kwargs):
        frames_started.set()
        return []

    monkeypatch.setattr(Intelligence, 'overview', slow_overview)
    monkeypatch.setattr(media, 'candidates', sample_frames)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(pipeline.run, jobs, task['id'])
        try:
            assert summary_started.wait(3) and frames_started.wait(3)
            deadline = time.monotonic() + 3
            while jobs.get(task['id'])['output_states']['mindmap']['status'] != 'completed':
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert any(a['name'] == 'mindmap.pdf' and not a.get('pending') for a in jobs.get(task['id'])['artifacts'])
            expected = {'text.html', 'illustrated.html'}
            while not expected <= {a['name'] for a in jobs.get(task['id'])['artifacts']}:
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert '电池容量' in (folder / 'output/text.html').read_text(encoding='utf-8')
            assert '一句话总结' not in (folder / 'output/text.html').read_text(encoding='utf-8')
            draft_report = (folder / 'output/illustrated.md').read_text(encoding='utf-8')
            assert draft_report.index('# 组合测试') < draft_report.index('![视频封面]') < draft_report.index('**目录**')
        finally:
            release.set()
        pending.result(timeout=5)
    assert '一句话总结' in (folder / 'output/text.html').read_text(encoding='utf-8')
    report = (folder / 'output/illustrated.md').read_text(encoding='utf-8')
    assert report.index('**一句话总结：**') < report.index('![视频封面]') < report.index('**目录**')
    assert '![视频封面]' not in (folder / 'output/text.md').read_text(encoding='utf-8')
    assert not list(folder.glob('*.summary-pending'))


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
    monkeypatch.setenv('ASR_CLEANUP_MODE', 'model')
    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['text'], native=False)
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
    assert (folder / 'output/text.html').is_file()
    assert (folder / 'text.summary-pending').is_file()
    jobs.update(task['id'], status='failed')
    monkeypatch.setattr('local.jobs.load', lambda: {'llm_model': 'model-b', 'whisper_model': 'small'})
    jobs.retry(task['id'], use_current_model=True)
    monkeypatch.setattr(Intelligence, 'overview', overview)
    pipeline.run(jobs, task['id'])
    assert seen == ['model-a', 'model-b']
    assert jobs.settings(task['id'])['whisper_model'] == 'base'
    assert (folder / 'output/text.html').is_file()
    assert not (folder / 'text.summary-pending').exists()


def test_subtitle_model_cleans_english_reports_without_translating_and_keeps_cache_on_report_switch(tmp_path, monkeypatch):
    from local.limits import budget

    jobs, task, folder, _, _ = prepare(tmp_path, monkeypatch, ['text'], native=False)
    jobs.update(task['id'], options={'clean_subtitles':True})
    settings = {'llm_model':'report-one', 'llm_context_tokens':1000000, 'use_subtitle_model':True,
                'subtitle_model':'subtitle-model', 'subtitle_context_tokens':24000, 'subtitle_max_output_tokens':4096,
                'asr_cleanup_mode':'model'}
    (folder / 'model-settings.json').write_text(json.dumps(settings), encoding='utf-8')
    transcript = [{'start':0, 'end':5, 'text':'The battery stores 100 kWh.'}]
    monkeypatch.setattr(media, 'transcribe', lambda *args, **kwargs: (transcript, 'en'))
    subtitle_calls, report_calls = [], []
    plan = Intelligence.plan

    def transform(engine, cues, *, language=None, clean=False, **kwargs):
        subtitle_calls.append((engine.config['llm_model'], budget(engine.config), language, clean))
        return cues if clean else [dict(cue, text='电池容量100kWh。') for cue in cues]

    def report_plan(engine, *args):
        report_calls.append(engine.config['llm_model'])
        if engine.config['llm_model'] == 'report-one':
            raise RuntimeError('fixture report failure')
        return plan(engine, *args)

    monkeypatch.setattr(Intelligence, 'transform', transform)
    monkeypatch.setattr(Intelligence, 'plan', report_plan)
    with pytest.raises(OutputFailure):
        pipeline.run(jobs, task['id'])
    jobs.update(task['id'], status='failed')
    monkeypatch.setattr('local.jobs.load', lambda: {**settings, 'llm_model':'report-two'})
    jobs.retry(task['id'], use_current_model=True)
    pipeline.run(jobs, task['id'])
    assert subtitle_calls == [('subtitle-model', (24000, 4096), None, True)]
    assert report_calls == ['report-one', 'report-two']
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
    monkeypatch.setattr(pdf_export,'render',lambda *args, **kwargs:(_ for _ in ()).throw(RuntimeError('PDF failed')))
    jobs.update(task['id'],status='queued')
    jobs.start()
    try:
        jobs.queue.join()
        assert jobs.get(task['id'])['status']=='partial'
        monkeypatch.setattr(pdf_export,'render',lambda folder,check,**kwargs:(folder/'output/mindmap.pdf').write_bytes(b'%PDF-1.7\n%%EOF'))
        jobs.retry(task['id']);jobs.queue.join()
        assert jobs.get(task['id'])['status']=='completed'
    finally:
        jobs.stop()
