"""Inspect link collections and resolve only the chosen media entry."""
import hashlib
from itertools import islice
import json
import math
import re

from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import PagedList, extract_attributes, get_element_html_by_class


COLLECTIONS = {"playlist", "multi_video", "compat_list"}
MAX_LINK_ENTRIES = 30


class BydReplayListIE(InfoExtractor):
    _VALID_URL = r"https?://(?:www\.)?byd\.com/cn/(?:index\.html|live-list/?)?(?:[?#]|$)"
    _DIRECTORY = "https://www.byd.com/cn/live-list"

    def _real_extract(self, url):
        # The homepage loads its replay cards with JavaScript. The public
        # replay directory includes the featured cards in its initial HTML.
        page = self._download_webpage(self._DIRECTORY, "byd-replays")
        component = get_element_html_by_class("live-live-kv-mount-root-container", page)
        payload = extract_attributes(component or "").get("data-json")
        data = self._parse_json(payload or "{}", "byd-replays", fatal=False)
        cards = data.get("contentList") if isinstance(data, dict) else None
        entries = []
        for item in cards if isinstance(cards, list) else ():
            if not isinstance(item, dict) or not isinstance(item.get("link"), str):
                continue
            detail = re.fullmatch(r"(?:(?:https?:)?//(?:www\.)?byd\.com)?/cn/livedetail([0-9]+)(?:[?#].*)?",
                                  item["link"])
            if detail:
                entries.append(self.url_result(f"https://www.byd.com/cn/livedetail{detail[1]}",
                                               video_title=item.get("title") if isinstance(item.get("title"), str) else None))
        if not entries:
            raise ValueError("未能读取比亚迪直播回顾列表，请打开具体的回放页面并提交该链接")
        return self.playlist_result(entries, "byd-replays", "比亚迪直播回顾")


def is_live(info):
    return info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}


def link_entries(info):
    entries = info.get("entries")
    if isinstance(entries, PagedList):
        entries = entries.getslice(0, MAX_LINK_ENTRIES + 1)
    else:
        entries = list(islice(entries or (), MAX_LINK_ENTRIES + 1))
    unique = {}
    for entry in entries[:MAX_LINK_ENTRIES]:
        if not entry or is_live(entry) or entry.get("_type") in COLLECTIONS:
            continue
        if not (entry.get("url") or entry.get("formats")):
            continue
        extractor = entry.get("ie_key") or entry.get("extractor_key") or info.get("extractor_key")
        if entry.get("_type") in {"url", "url_transparent"}:
            identity = [extractor, entry["url"]]
        elif entry.get("id") and extractor not in {None, "Generic", "HTML5MediaEmbed"}:
            identity = [extractor, entry["id"]]
        else:
            # Generic HTML5 entries have synthetic IDs. Two players can point
            # at the same source or the same adaptive manifest.
            urls = {fmt.get("manifest_url") or fmt.get("url") for fmt in entry.get("formats") or ()}
            identity = [sorted(url for url in urls if url) or entry.get("url") or entry.get("id")]
        identity.extend([entry.get("section_start"), entry.get("section_end")])
        resource_id = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
        unique.setdefault(resource_id, entry)
    return unique, len(entries) > MAX_LINK_ENTRIES


def describe_resource(info, resource_id=None):
    duration = info.get("duration")
    return {
        "id": resource_id,
        "title": (info.get("title") or "未命名资源")[:200],
        "duration": duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else None,
    }


class LinkDownloader(YoutubeDL):
    def __init__(self, options, *, resource_id=None, preview=False):
        super().__init__(options)
        self.resource_id = resource_id
        self.preview = preview
        self._selection_depth = 0

    def add_default_info_extractors(self):
        self.add_info_extractor(BydReplayListIE())
        super().add_default_info_extractors()

    def save_cookies(self):
        if not self.preview:
            super().save_cookies()

    def process_ie_result(self, info, download=True, extra_info=None):
        kind = info.get("_type", "video")
        if kind not in COLLECTIONS:
            if kind == "video":
                if is_live(info):
                    raise ValueError("请提交已结束的音视频，暂不处理进行中或尚未开始的直播")
                if self.resource_id and not self._selection_depth:
                    raise ValueError("所选资源已变化，请重新检查链接")
            return super().process_ie_result(info, download, extra_info)

        entries, truncated = link_entries(info)
        if self.preview:
            return {"resources": [describe_resource(entry, key) for key, entry in entries.items()],
                    "truncated": truncated}
        if self._selection_depth:
            # A selected replay detail can contain two players for the same
            # stream. Resolve one nested collection only when it is unambiguous.
            if self._selection_depth > 1 or len(entries) != 1 or truncated:
                raise ValueError("所选条目无法确定单一音视频，请提交具体视频链接")
            entry = next(iter(entries.values()))
        elif self.resource_id:
            entry = entries.get(self.resource_id)
            if entry is None:
                raise ValueError("所选资源已变化或不可用，请重新检查链接")
        elif len(entries) == 1 and not truncated:
            entry = next(iter(entries.values()))
        else:
            raise ValueError("请重新提交链接并选择要处理的单个音视频资源")

        # Let yt-dlp resolve the selected entry with the original page context,
        # including extractor metadata and headers such as Referer.
        selected = {**info, "entries": [entry]}
        selected.pop("requested_entries", None)
        self._selection_depth += 1
        try:
            result = super().process_ie_result(selected, download, extra_info)
        finally:
            self._selection_depth -= 1
        resolved = (result or {}).get("entries") or []
        if not resolved or not resolved[0]:
            raise ValueError("所选资源当前不可用，请重新检查链接")
        return resolved[0]
