import asyncio
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread

from fastapi import FastAPI
import httpx
import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.generic import GenericIE
from yt_dlp.utils import OnDemandPagedList

from local.jobs import JobStore
from local.links import BydReplayListIE, LinkDownloader, MAX_LINK_ENTRIES
from local.official_media import OfficialAutoIE, PublicPageMediaIE
from routers import tasks


PAGE = "https://example.test/replay"


def video(number, stream):
    return {"id": str(number), "title": f"视频 {number}", "duration": number,
            "extractor_key": "HTML5MediaEmbed", "formats": [{"url": stream, "http_headers": {"Referer": PAGE}}]}


def playlist(entries):
    return {"_type": "playlist", "id": "replay", "title": "回放", "webpage_url": PAGE,
            "extractor": "generic", "extractor_key": "Generic", "entries": entries}


def test_duplicate_players_and_reordered_entries_keep_the_selected_stream(monkeypatch):
    first, second = "https://example.test/a.m3u8", "https://example.test/b.m3u8"
    entries = [video(1, first), video(2, first), video(3, second)]
    with LinkDownloader({}, preview=True) as ydl:
        resources = ydl.process_ie_result(playlist(entries), download=False)["resources"]
    assert [resource["title"] for resource in resources] == ["视频 1", "视频 3"]
    downloaded = []

    def process_video(self, entry, download):
        if download:
            downloaded.append(entry["formats"][0])
        return entry

    monkeypatch.setattr(YoutubeDL, "process_video_result", process_video)
    with LinkDownloader({}, resource_id=resources[1]["id"]) as ydl:
        assert ydl.process_ie_result(playlist(list(reversed(entries))), download=False)["id"] == "3"
        ydl.process_ie_result(playlist(entries), download=True)
        with pytest.raises(ValueError, match="所选资源已变化"):
            ydl.process_ie_result(playlist(entries[:2]), download=True)
    assert downloaded == [{"url": second, "http_headers": {"Referer": PAGE}}]
    with LinkDownloader({}) as ydl:
        with pytest.raises(ValueError, match="选择要处理的单个"):
            ydl.process_ie_result(playlist(entries), download=True)
        assert ydl.process_ie_result(playlist(entries[:2]), download=False)["id"] == "1"


def test_preview_only_reads_the_first_pages():
    pages = []

    def page(index):
        pages.append(index)
        return [video(n, f"https://example.test/{n}.mp4") for n in range(index * 10, (index + 1) * 10)]

    with LinkDownloader({}, preview=True) as ydl:
        result = ydl.process_ie_result(playlist(OnDemandPagedList(page, 10)), download=False)
    assert result["truncated"] and len(result["resources"]) == MAX_LINK_ENTRIES
    assert pages == [0, 1, 2, 3]


def test_byd_home_lists_replays_and_resolves_only_the_chosen_detail(monkeypatch):
    home = "https://www.byd.com/cn/index.html"
    detail = "https://www.byd.com/cn/livedetail12"
    cards = {"contentList": [
        {"title": "发布会一", "link": "/cn/livedetail11"},
        {"title": "发布会二", "link": "/cn/livedetail12"},
        {"title": "重复条目", "link": detail},
        {"title": "非回放页面", "link": "https://example.test/cn/livedetail13"},
    ]}
    html = f'<div class="live-live-kv-mount-root-container" data-json="{escape(json.dumps(cards))}"></div>'
    seen = []

    def fetch_catalog(self, url, video_id):
        seen.append(url)
        return html

    def fetch_detail(self, url):
        seen.append(url)
        stream = "https://example.test/replay.m3u8"
        return {**playlist([video(1, stream), video(2, stream)]), "webpage_url": url}

    monkeypatch.setattr(BydReplayListIE, "_download_webpage", fetch_catalog)
    monkeypatch.setattr(GenericIE, "_real_extract", fetch_detail)
    with LinkDownloader({}, preview=True) as ydl:
        resources = ydl.extract_info(home, download=False)["resources"]
    assert [item["title"] for item in resources] == ["发布会一", "发布会二"]
    assert seen == [BydReplayListIE._DIRECTORY]
    with LinkDownloader({}, resource_id=resources[1]["id"]) as ydl:
        result = ydl.extract_info(home, download=False)
    assert result["webpage_url"] == detail and result["id"] == "1"
    assert seen[1:] == [BydReplayListIE._DIRECTORY, detail]


@pytest.mark.parametrize('url,markup,expected', [
    ('https://www.chery.cn/',
     '<video data-pc-src="https://material.chery.cn/intro.mp4" data-m-src="/mobile.mp4"></video>'
     '<div data-pcBig-src="/launch.mp4"></div>',
     ['https://material.chery.cn/intro.mp4', 'https://www.chery.cn/launch.mp4']),
    ('https://www.lixiang.com/',
     '<script>var pmsData = {"list":[{"title":"家庭发布会",'
     '"videoPc":{"hd":"/preview.mp4"},"fullVideoPc":{"hd":"/full.mp4","sd":"/smaller.mp4"}}]};</script>',
     ['https://www.lixiang.com/full.mp4', 'https://www.lixiang.com/preview.mp4']),
    ('https://www.tesla.cn/model3',
     r'<script>{"desktop_video":"https:\/\/digitalassets.tesla.com\/Model-3-Range.mp4","mobile_video":"/mobile.mp4"}</script>'
     r'<script>{"desktop_video":"https:\/\/digitalassets.tesla.com\/Model-3-Range.mp4"}</script>',
     ['https://digitalassets.tesla.com/Model-3-Range.mp4']),
])
def test_official_page_previews_deduplicated_media_and_preserves_page_headers(monkeypatch, url, markup, expected):
    monkeypatch.setattr(OfficialAutoIE, '_read', lambda *args, **kwargs: '<title>官网</title>' + markup)
    with LinkDownloader({}, preview=True) as ydl:
        resources = ydl.extract_info(url, download=False)['resources']
    assert len(resources) == len(expected)
    for choice, media_url in zip(resources, expected):
        with LinkDownloader({}, resource_id=choice['id']) as ydl:
            info = ydl.extract_info(url, download=False)
        assert info['url'] == media_url and info['http_headers']['Referer'] == url


def test_xiaomi_home_checks_model_assets_and_skips_mobile_variants(monkeypatch):
    home = 'https://www.xiaomiev.com/'
    scripts = 'https://s1.xiaomiev.com/activity-outer-assets/assets/_next/static/chunks/'
    pages = {
        home: '<title>小米汽车</title>',
        home + 'su7': '<title>SU7</title>' + f'<script src="{scripts}part.js"></script>'
        f'<script src="{scripts}app/su7/page-one.js"></script>',
        scripts + 'app/su7/page-one.js': 'void 0;',
        scripts + 'part.js': '''const desktop="/images/su7/pc/full.mp4", mobile="/images/su7/m/full.mp4";''',
    }
    monkeypatch.setattr(OfficialAutoIE, '_read', lambda self, url, **kwargs: pages.get(url, ''))
    with LinkDownloader({}, preview=True) as ydl:
        [entry] = ydl.extract_info(home, download=False)['resources']
    assert entry['title'] == 'SU7 · full'
    with LinkDownloader({}, resource_id=entry['id']) as ydl:
        info = ydl.extract_info(home, download=False)
    assert info['url'] == 'https://s1.xiaomiev.com/activity-outer-assets/0328/images/su7/pc/full.mp4'


def test_xpeng_activity_resolves_the_selected_public_player(monkeypatch):
    activity = 'https://events.xiaopeng.com/abc123.html'
    player = 'https://www.xiaopeng.com/landing_video.html?id=12'
    stream = 'https://xps02.xiaopeng.com/launch.mp4'
    pages = {activity: r'<title>车型发布会</title><script>{"url":"https:\u002f\u002fwww.xiaopeng.com\u002flanding_video.html?id=12"}</script>',
             player: '<script>window.__INITIAL_STATE__={"csrf":"test-token"};</script>'}
    calls = []

    def material(self, request, number, *, fatal, headers, data):
        calls.append((request.url, number, json.loads(data)))
        return {'data': {'name': '发布会回放', 'materialItemList': [{'accessUrl': stream}]}}

    monkeypatch.setattr(OfficialAutoIE, '_read', lambda self, url, **kwargs: pages[url])
    monkeypatch.setattr(OfficialAutoIE, '_download_json', material)
    with LinkDownloader({}, preview=True) as ydl:
        [entry] = ydl.extract_info(activity, download=False)['resources']
    assert not calls and entry['title'] == '车型发布会 · 官方视频'
    with LinkDownloader({}, resource_id=entry['id']) as ydl:
        info = ydl.extract_info(activity, download=False)
    assert info['url'] == stream and info['title'].startswith('发布会回放')
    assert calls == [('https://www.xiaopeng.com/api/getMaterialById', '12', {'materialId': '12', '_csrf': 'test-token'})]


def test_brand_home_follows_only_bounded_related_official_pages(monkeypatch):
    home = 'https://hima.auto/'
    pages = {
        home: '<a href="https://example.test/news/press">第三方</a>'
              '<a href="/news/videos">视频中心</a><a href="/model/test">车型详情</a>',
        home + 'news/videos': '<title>发布会回放</title><div data-url="/replay.mp4"></div>',
        home + 'model/test': '<video><source media="(max-width: 700px)" data-src="/mobile.mp4">'
                             '<source media="(min-width: 701px)" data-src="/desktop.mp4"></video>',
    }
    loaded = []

    def read(self, url, **kwargs):
        loaded.append(url)
        return pages[url]

    monkeypatch.setattr(OfficialAutoIE, '_read', read)
    with LinkDownloader({}, preview=True) as ydl:
        resources = ydl.extract_info(home, download=False)['resources']
    assert set(loaded) == set(pages)
    assert len(resources) == 2
    with LinkDownloader({}, resource_id=resources[0]['id']) as ydl:
        selected = ydl.extract_info(home, download=False)
    assert selected['url'] == home + 'replay.mp4'


def test_unknown_official_pages_can_use_declared_media_without_a_new_extractor(monkeypatch):
    assert not OfficialAutoIE.suitable(PAGE)
    assert not OfficialAutoIE.suitable('https://hima.auto.example.test/')
    assert not OfficialAutoIE.suitable('https://www.avatr.com:fake@example.test/')
    monkeypatch.setattr(GenericIE, '_extract_embeds', lambda *args, **kwargs: [])
    with LinkDownloader({}) as ydl:
        ie = ydl.get_info_extractor('Generic')
        assert isinstance(ie, PublicPageMediaIE)
        [entry] = ie._extract_embeds(PAGE, r'<script>const config={videoUrl:"\/footage.mp4"};</script>')
        assert entry['formats'][0]['url'] == 'https://example.test/footage.mp4'
        assert not ie._extract_embeds(PAGE, '<script src="https://unrelated.test/script.js"></script>')


@pytest.mark.skipif(os.getenv('RUN_PDF_RENDER_TEST') != '1', reason='requires installed Chromium')
def test_dynamic_official_page_discovers_media_without_downloading_it(monkeypatch):
    from local import official_media_browser
    from local.official_media import MEDIA, QUOTED_MEDIA

    requested = []

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            document = b"""<body><script>
                fetch('/config.json').then(response => response.json()).then(data => {
                    const player = document.createElement('video');
                    player.src = data.videoUrl;
                    document.body.append(player);
                });
                </script></body>"""
            payload = b'{"videoUrl":"/clip.mp4"}' if self.path == '/config.json' else document
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == '/config.json' else 'text/html')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    monkeypatch.setattr(official_media_browser, 'automaker_domain', lambda host: host)
    with ThreadingHTTPServer(('127.0.0.1', 0), Origin) as server:
        root = f'http://127.0.0.1:{server.server_port}/'
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert official_media_browser.browser_sources(root, '127.0.0.1', MEDIA,
                                                          QUOTED_MEDIA.findall) == [root + 'clip.mp4']
        finally:
            server.shutdown()
            thread.join(timeout=3)
    assert '/config.json' in requested and '/clip.mp4' not in requested


def test_preview_does_not_create_a_job_and_selection_is_persisted(tmp_path, monkeypatch):
    jobs = JobStore(tmp_path)
    monkeypatch.setattr(tasks, "store", jobs)
    monkeypatch.setattr(tasks, "preview_link", lambda url: {"resources": [{"id": "a" * 64}], "truncated": False})
    app = FastAPI()
    app.include_router(tasks.router)

    async def submit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            inspected = await client.post("/api/v1/tasks/url/inspect", json={"url": PAGE})
            assert inspected.status_code == 200 and jobs.list() == []
            selected = inspected.json()["resources"][0]["id"]
            result = await client.post("/api/v1/tasks/url", json={"url": PAGE, "resource_id": selected, "modes": ["text"]})
            assert result.status_code == 202
            return result.json()["id"]

    job_id = asyncio.run(submit())
    assert jobs.get(job_id)["source"] == {"type": "url", "url": PAGE, "resource_id": "a" * 64}
