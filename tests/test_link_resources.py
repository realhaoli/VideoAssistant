import asyncio
from html import escape
import json

from fastapi import FastAPI
import httpx
import pytest
from yt_dlp import YoutubeDL
from yt_dlp.extractor.generic import GenericIE
from yt_dlp.utils import OnDemandPagedList

from local.jobs import JobStore
from local.links import BydReplayListIE, LinkDownloader, MAX_LINK_ENTRIES
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
