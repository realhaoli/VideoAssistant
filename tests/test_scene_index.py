from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from local import media, pipeline, scene_index
from local.intelligence import Intelligence


def test_scene_index_keeps_short_dark_parameter_slide_and_reuses_checkpoints(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    stage = Image.new("L", (scene_index.WIDTH, scene_index.HEIGHT), 105)
    card = Image.new("L", stage.size)
    ImageDraw.Draw(card).text((95, 64), "0-60 MPH 0.9 S", fill=255)
    scans = []

    def decode(args, check, *, timeout):
        scans.append(args)
        frames = [stage] * 4 + [card] * 2 + [stage] * 8
        Path(args[-1]).write_bytes(b"".join(frame.tobytes() for frame in frames))

    cache = tmp_path / "scene-index"
    catalog = scene_index.build(source, 7, cache, decode, lambda: None)
    assert len(catalog.scenes) == 3
    assert [scene['time'] for scene in catalog.scenes if scene['card']] == [2.0]
    assert scene_index.build(source, 7, cache, decode, lambda: None).scenes == catalog.scenes
    assert len(scans) == 1
    loaded = scene_index.load(cache)
    with Image.open(BytesIO(loaded.montage(loaded.scenes))) as image:
        assert image.format == "JPEG"
        assert image.width == 810


def test_third_round_can_select_distant_scene_after_two_failed_reviews(tmp_path, monkeypatch):
    scenes = [dict(start=time, end=time + 1, time=time, quality=100, card=time == 170, thumb=f"{time * 1000}.jpg")
              for time in (102, 118, 170)]
    for scene in scenes:
        Image.new("L", (48, 28), 90).save(tmp_path / scene['thumb'])
    catalog = scene_index.SceneIndex(tmp_path, scenes)
    engine = Intelligence({}, lambda: None)
    calls = []
    reviewed = []
    output = tmp_path / "output"
    (output / "image").mkdir(parents=True)
    sample = dict(title="参数页", evidence_time=100, evidence_text="零到六十英里每小时", annotate=False)

    def shortlist(sheet, options, body, kind):
        assert sheet.is_file()
        assert [scene['time'] for scene in options] == [170]
        return options

    def candidates(path, chapter, output, index, fingerprints, check, **kwargs):
        options = kwargs['scenes'] or catalog.local(
            chapter['evidence_time'], chapter['search_radius'], chapter['previous_search_radius'], None,
            kwargs['exclude_times'])
        time = options[0]['time']
        calls.append(time)
        return [dict(time=time, hash=((1 << 32) - 1) << (32 * len(calls)))]

    def frame(source, time, target, check, *, native=False):
        assert native
        Image.new("RGB", (320, 180), (time, 12, 13)).save(target)

    def verify(path, body, sample, choice, *, request_region=False):
        with Image.open(path) as image:
            assert image.format == 'PNG'
        reviewed.append(choice['candidate']['time'])
        return {"caption": "经复核的参数页"} if reviewed[-1] == 170 else None

    monkeypatch.setattr(engine, 'shortlist_scenes', shortlist)
    monkeypatch.setattr(engine, 'select', lambda chapter, images, body:
                        [dict(candidate=images[0], caption="候选画面", kind="product")])
    monkeypatch.setattr(engine, 'verify_figure', verify)
    monkeypatch.setattr(media, 'candidates', candidates)
    monkeypatch.setattr(media, 'frame', frame)

    def choose(checkpoint, used):
        return pipeline._chapter_figure(tmp_path, output, tmp_path / 'source.mp4', sample, "参数页", engine, 0,
                                        used, lambda: None, lambda message: None, lambda *args, **kwargs: None,
                                        '参数页', 0, 'image/chapter.png', checkpoint, catalog=catalog)

    result = choose(tmp_path / 'first.json', [])
    assert calls == reviewed == [102, 118, 170]
    assert result['entry']['time'] == 170
    assert result['entry']['caption'] == '经复核的参数页'
    assert choose(tmp_path / 'first.json', []) == result
    assert len(calls) == 3


def test_parameter_review_requires_a_matching_measurement(monkeypatch):
    engine = Intelligence({}, lambda: None)
    results = iter([
        {"accepted": True, "caption": "加速页面", "visible_text": "0–60 MPH"},
        {"accepted": True, "caption": "加速页面", "visible_text": "0–60 MPH < 1.0 s"},
        {"accepted": True, "caption": "加速 2 秒", "visible_text": "0–60 MPH < 1.0 s"},
    ])
    monkeypatch.setattr(engine, 'chat', lambda *args, **kwargs: next(results))
    body = '目标是将 0–60 mph 加速时间压缩至一秒以内。'
    choice = dict(kind='parameter', caption='加速页面')
    assert engine.verify_figure(None, body, {}, choice) is None
    assert engine.verify_figure(None, body, {}, choice)['caption'] == '加速页面'
    assert engine.verify_figure(None, body, {}, choice) is None
