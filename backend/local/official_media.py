"""Discover public videos published on supported automaker websites."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html import unescape
from itertools import zip_longest
import json
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlsplit

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.generic import GenericIE
from yt_dlp.networking import Request
from yt_dlp.utils import clean_html, extract_attributes, smuggle_url

from .automaker_sites import DYNAMIC_HOME_PAGES, HOME_PAGES, automaker_domain, is_root


MEDIA = re.compile(r"\.(mp4|m4v|mov|webm|m3u8|mpd)(?:[?#]|$)", re.I)
TAG = re.compile(r"<(video|source|script|div|a|meta|button)\b[^>]*>", re.I)
MEDIA_FIELDS = ('contentUrl', 'videoUrl', 'video_url', 'playUrl', 'play_url', 'fileUrl', 'mediaUrl', 'desktop_video',
                'videoSrc', 'videoPath', 'videoPc', 'video', 'pcVideo', 'pcVideoUrl', 'videoAddress', 'src')
MEDIA_TAG_FIELDS = ('data-video-url', 'data-video', 'data-video-src', 'data-play-url', 'data-mp4', 'data-url')
MEDIA_PAGE = re.compile(r'(?:^|[/_-])(?:videos?|shipin|replay|live|event|press|media|gallery|news|model|product|vehicl|cars|carguide)', re.I)
PAGE_SCRIPT = re.compile(r'/(?:app|main|index|home|page|umi|[a-z0-9]+_detail)[.~_-][^/]+\.js$', re.I)
QUOTED_MEDIA = re.compile(r'''["']((?:(?:https?:)?//|/(?!/))[^"'<>\s\\]{2,1000}\.(?:mp4|m4v|mov|m3u8|webm|mpd)(?:\?[^"'<>\s\\]{1,1000})?)["']''', re.I)
XPENG_PLAYER = re.compile(r"https?://(?:www\.)?xiaopeng\.com/landing_video\.html\?id=\d+")
XPENG_EVENT = re.compile(r"https?://events\.xiaopeng\.com/[a-z0-9]+\.html", re.I)
XIAOMI_ASSETS = "https://s1.xiaomiev.com/activity-outer-assets/0328/"
XIAOMI_VIDEO = re.compile(r"['\"](/images/[^'\"<>\s]+\.(?:mp4|mov|webm))['\"]", re.I)


def _unescape_data(webpage):
    for escape, value in ((r'\u002F', '/'), (r'\u003A', ':'), (r'\u0026', '&'), (r'\u003D', '='), (r'\u003F', '?')):
        webpage = webpage.replace(escape, value).replace(escape.lower(), value)
    return unescape(webpage).replace(r'\/', '/').replace(r'\"', '"')


def _html_media(webpage):
    for match in TAG.finditer(webpage):
        tag = match[1].lower()
        if tag == 'script':
            continue
        attrs = extract_attributes(match[0])
        if tag == 'source' and re.search(r'max-width', attrs.get('media') or '', re.I):
            continue
        if tag == 'meta':
            if (attrs.get('property') or attrs.get('name')) in {'og:video', 'og:video:url', 'og:video:secure_url'}:
                if MEDIA.search(attrs.get('content') or ''):
                    yield attrs['content']
            continue
        fields = MEDIA_TAG_FIELDS + ('data-pcbig-src', 'data-mbig-src')
        if tag in {'video', 'source'}:
            fields = ('src', 'data-src', 'data-pc-src', 'data-desktop-src', 'data-m-src') + fields
        elif tag == 'a':
            fields = ('href',) + fields
        source = next((attrs[key] for key in fields if MEDIA.search(attrs.get(key) or '')), None)
        if source:
            yield source


def _json_media(webpage, fields):
    pattern = r"(?<![\w-])['\"]?(?:" + '|'.join(fields) + r")[\"']?\s*:\s*(['\"])([^'\"<>\s]+)\1"
    for match in re.finditer(pattern, webpage, re.I):
        if MEDIA.search(match[2]):
            yield match[2]


def _media_entry(extractor, url, page, title):
    try:
        url = urldefrag(urljoin(page, url.strip()))[0]
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        return None
    if any(value.startswith('video/snapshot') for value in parse_qs(parsed.query).get('x-oss-process', ())):
        return None
    format_match = MEDIA.search(parsed.path)
    if not format_match:
        return None
    extension = format_match[1].lower()
    resource = hashlib.sha256(url.encode()).hexdigest()[:24]
    if extension in {'m3u8', 'mpd'}:
        entry = extractor.url_result(smuggle_url(url, {'referer': page}), 'Generic', resource, title, url_transparent=True)
    else:
        entry = {'id': resource, 'title': title, 'webpage_url': page,
                 'formats': [{'url': url, 'ext': extension, 'http_headers': {'Referer': page}}]}
    return url, entry


class PublicPageMediaIE(GenericIE):
    @classmethod
    def ie_key(cls):
        return 'Generic'

    def _extract_embeds(self, url, webpage, *, urlh=None, info_dict=None):
        info_dict = info_dict or {}
        existing = super()._extract_embeds(url, webpage, urlh=urlh, info_dict=info_dict)
        if existing:
            return existing
        page = urlh.url if urlh else url
        title = info_dict.get('title') or self._generic_title(page)
        entries = {}
        for source in (*_html_media(webpage), *_json_media(_unescape_data(webpage), MEDIA_FIELDS)):
            media = _media_entry(self, source, page, title)
            if media:
                entries.setdefault(*media)
            if len(entries) >= 31:
                break
        if len(entries) > 1:
            for index, entry in enumerate(entries.values(), 1):
                entry['title'] = f'{title} · 视频 {index}'
        return list(entries.values())


def _li_videos(webpage):
    payload = re.search(r'\bvar\s+pmsData\s*=\s*(?=\{)', webpage)
    if payload is None:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(webpage, payload.end())
    except (ValueError, RecursionError):
        return {}
    stack = [data]
    full, previews = {}, {}
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        if not isinstance(item, dict):
            continue
        stack.extend(reversed(item.values()))
        for key, group, label in (('fullVideoPc', full, '完整视频'), ('videoPc', previews, '页面片段')):
            versions = item.get(key)
            if isinstance(versions, dict):
                url = versions.get('hd') or versions.get('sd')
                if isinstance(url, str) and MEDIA.search(url):
                    title = item.get('title')
                    name = ' '.join(clean_html(title).split()) if isinstance(title, str) else ''
                    group.setdefault(url, f'{name} · {label}' if name else label)
    return {**full, **{url: title for url, title in previews.items() if url not in full}}


class OfficialAutoIE(InfoExtractor):
    _VALID_URL = r'(?i)https?://(?P<id>[\w.-]+)(?=[:/?#]|$)'

    @classmethod
    def suitable(cls, url):
        matched = cls._match_valid_url(url)
        if not matched:
            return False
        parsed = urlsplit(url)
        domain = automaker_domain(parsed.hostname)
        return (parsed.hostname == matched['id'].lower() and domain not in {None, 'byd.com'} and parsed.username is None
                and not MEDIA.search(parsed.path))

    def _read(self, url, *, referer=None, fatal=True):
        headers = {'Accept-Encoding': 'gzip, deflate'}
        if referer:
            headers['Referer'] = referer
        return self._download_webpage(Request(url, headers=headers, extensions={'timeout': 5}),
                                      self._match_id(referer or url), fatal=fatal) or ''

    def _title(self, webpage, page):
        return self._og_search_title(webpage, default=None) or self._html_extract_title(webpage, default=None) or page

    def _xiaomi_videos(self, page, webpage):
        scripts = []
        for match in TAG.finditer(webpage):
            if match[1].lower() != 'script':
                continue
            src = urljoin(page, extract_attributes(match[0]).get('src') or '')
            if src.startswith('https://s1.xiaomiev.com/activity-outer-assets/assets/_next/static/chunks/'):
                scripts.append(src)
        for position, source in enumerate(scripts):
            if not re.search(r'/app/(?:[^/]+/)*page-[^/]+\.js$', source):
                continue
            clips = set()
            for script in reversed(scripts[max(0, position - 2):position + 1]):
                code = self._read(script, referer=page, fatal=False)
                clips.update(urljoin(XIAOMI_ASSETS, match[1].lstrip('/')) for match in XIAOMI_VIDEO.finditer(code)
                             if '/m/' not in match[1])
                if clips:
                    break
            yield from sorted(clips, key=lambda value: ('full.' not in value, value))
            break

    def _page_scripts(self, page, webpage, domain):
        scripts = []
        for match in TAG.finditer(webpage):
            if match[1].lower() != 'script':
                continue
            src = urljoin(page, extract_attributes(match[0]).get('src') or '')
            parsed = urlsplit(src)
            if automaker_domain(parsed.hostname) == domain and PAGE_SCRIPT.search(parsed.path):
                scripts.append(src)
        return list(dict.fromkeys(scripts))[-3:]

    def _xpeng_material(self, page, webpage):
        parsed = urlsplit(page)
        ids = parse_qs(parsed.query).get('id', [])
        if parsed.path != '/landing_video.html' or not ids or not re.fullmatch(r'\d{1,18}', ids[0]):
            return None
        csrf = self._search_regex(r'"csrf"\s*:\s*"([\w-]{1,200})"', webpage, 'csrf', default=None)
        if csrf is None:
            return None
        base = 'https://www.xiaopeng.com'
        result = self._download_json(Request(base + '/api/getMaterialById', extensions={'timeout': 5}),
                                     ids[0], fatal=False, headers={'Referer': page, 'Origin': base,
                                     'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                                     data=json.dumps({'materialId': ids[0], '_csrf': csrf}).encode())
        material = result.get('data') if isinstance(result, dict) else None
        return material if isinstance(material, dict) else None

    def _page_entries(self, page, webpage, domain, *, scripts=False, browser=False):
        title = self._title(webpage, page)
        data = _unescape_data(webpage)
        sources = list(_html_media(webpage))
        sources.extend(_json_media(data, MEDIA_FIELDS))
        labels = {}
        if domain == 'lixiang.com':
            labels = _li_videos(webpage)
            sources = [*labels, *sources]
        elif domain == 'xiaomiev.com' and urlsplit(page).path.rstrip('/'):
            sources.extend(self._xiaomi_videos(page, webpage))
        elif domain == 'deepal.com.cn' and is_root(urlsplit(page).path):
            config_url = 'https://app-api.deepal.com.cn/cms/appapi/v1/cmsow/firstPageConfig/configDetail'
            public_data = _unescape_data(self._read(config_url, referer=page, fatal=False))
            sources.extend(match[1] for match in QUOTED_MEDIA.finditer(public_data))
        elif domain.startswith('tesla.'):
            # The shared site navigation can include videos from other models.
            model = {'/model3': 'model-3-', '/modely': 'model-y-'}.get(urlsplit(page).path.rstrip('/'))
            if model:
                sources.sort(key=lambda source: model not in PurePosixPath(urlsplit(source).path.lower()).name)
        if not sources and domain not in {'xiaomiev.com', 'xiaopeng.com', 'tesla.cn', 'tesla.com'}:
            sources = [match[1] for match in QUOTED_MEDIA.finditer(data)]
        if not sources and scripts and domain not in {'xiaomiev.com', 'xiaopeng.com', 'tesla.cn', 'tesla.com'}:
            for script in reversed(self._page_scripts(page, webpage, domain)):
                text = _unescape_data(self._read(script, referer=page, fatal=False))
                sources.extend(match[1] for match in QUOTED_MEDIA.finditer(text))
                if sources:
                    break
        if not sources and browser and self._page_scripts(page, webpage, domain):
            from .official_media_browser import browser_sources
            sources = browser_sources(page, domain, MEDIA,
                                      lambda payload: QUOTED_MEDIA.findall(_unescape_data(payload)))

        entries = {}
        if domain == 'xiaopeng.com':
            material = self._xpeng_material(page, webpage)
            if material:
                title = clean_html(material.get('name') or '') or title
                sources = [item['accessUrl'] for item in material.get('materialItemList') or []
                           if isinstance(item, dict) and isinstance(item.get('accessUrl'), str)] + sources
            if urlsplit(page).path != '/landing_video.html':
                for player in dict.fromkeys(XPENG_PLAYER.findall(data)):
                    entries[player] = self.url_result(player, self.ie_key(), video_title=f'{title} · 官方视频')

        for source in dict.fromkeys(sources):
            clip = _media_entry(self, source, page, title)
            if clip and clip[0] not in entries:
                url, entry = clip
                name = PurePosixPath(unquote(urlsplit(url).path)).stem
                label = labels.get(source) or (name if any(character.isalpha() for character in name)
                                               else f'视频 {len(entries) + 1}')
                entry['title'] = f'{title} · {label}'
                entries[url] = entry
            if len(entries) >= 31:
                break
        return entries

    def _related(self, url, webpage, domain):
        path = urlsplit(url).path
        if not is_root(path):
            return []
        if domain.startswith('tesla.'):
            return [urljoin(url, '/model3'), urljoin(url, '/modely')]
        if domain == 'xiaomiev.com':
            return [urljoin(url, path) for path in ('/su7', '/yu7', '/ultra')]
        if domain == 'xiaopeng.com':
            return list(dict.fromkeys(XPENG_EVENT.findall(_unescape_data(webpage))))[:3]
        linked = {}
        for match in TAG.finditer(webpage):
            if match[1].lower() != 'a':
                continue
            href = extract_attributes(match[0]).get('href') or ''
            if not href or href.startswith('#'):
                continue
            child = urldefrag(urljoin(url, href))[0]
            parsed = urlsplit(child)
            if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                    or automaker_domain(parsed.hostname) != domain or is_root(parsed.path)
                    or not MEDIA_PAGE.search(parsed.path)
                    or PurePosixPath(parsed.path).suffix.lower() not in {'', '.html', '.htm', '.shtml'}):
                continue
            linked.setdefault(child, not bool(re.search(r'video|shipin|replay|live', parsed.path, re.I)))
        featured = [urljoin(url, target) for target in HOME_PAGES.get(domain, ())]
        return list(dict.fromkeys([*featured, *sorted(linked, key=linked.get)]))[:3]

    def _real_extract(self, url):
        host = self._match_id(url).lower()
        domain = automaker_domain(host)
        webpage = self._read(url)
        pages = [self._page_entries(url, webpage, domain)]
        related = self._related(url, webpage, domain)
        if related and (not pages[0] or domain in HOME_PAGES or domain in {'xiaopeng.com', 'xiaomiev.com'}):
            def discover(child):
                html = self._read(child, referer=url, fatal=False)
                child_domain = automaker_domain(urlsplit(child).hostname)
                return self._page_entries(child, html, child_domain) if html else {}

            with ThreadPoolExecutor(max_workers=3) as pool:
                pages.extend(pool.map(discover, related))
        if not any(pages):
            dynamic = DYNAMIC_HOME_PAGES.get(domain) if is_root(urlsplit(url).path) else None
            if dynamic:
                html = self._read(dynamic, referer=url, fatal=False)
                if html:
                    pages.append(self._page_entries(dynamic, html, automaker_domain(urlsplit(dynamic).hostname),
                                                    scripts=True, browser=True))
            if not any(pages):
                pages = [self._page_entries(url, webpage, domain, scripts=True, browser=True)]
        unique = {}
        for group in zip_longest(*(page.items() for page in pages)):
            for pair in group:
                if pair:
                    unique.setdefault(*pair)
        if not unique:
            return self.url_result(url, 'Generic')
        return self.playlist_result(unique.values(), host, self._title(webpage, host))
