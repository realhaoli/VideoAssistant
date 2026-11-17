"""Find media loaded by public vehicle pages that need JavaScript."""
import re
from urllib.parse import urljoin, urlsplit

from .automaker_sites import automaker_domain
from .resources import FairLimiter


PAGES = FairLimiter(2)
PLAY = re.compile(r'^(?:观看|播放|查看)(?:完整|宣传|发布会)?(?:视频|回放)|^Watch (?:the )?(?:video|film)$', re.I)
SITE_ALIASES = {'leapmotor.cn': {'leapmotor.com'}, 'leapmotor.com': {'leapmotor.cn'},
                'hyptec.com': {'aion.com.cn'}}
PUBLIC_QUERIES = {
    ('www-app.leapmotor.cn', '/carWebConfig/getBannerList'),
    ('api-gw-toc.zeekrlife.com', '/zeekrlife-mp-bnr/open/v1/container/get'),
    ('api-gw-toc.zeekrlife.com', '/zeekrlife-config-order/v1/model/pub/toc/queryOnlineModelYear/list'),
}


def browser_sources(url, domain, media_pattern, json_sources):
    from playwright.sync_api import Error, sync_playwright

    found = {}
    sites = {domain} | SITE_ALIASES.get(domain, set())
    payloads = []

    def collect(source):
        parsed = urlsplit(source)
        if parsed.scheme in {'http', 'https'} and media_pattern.search(parsed.path):
            found.setdefault(source, None)

    def discover(route):
        request = route.request
        parsed = urlsplit(request.url)
        if media_pattern.search(parsed.path):
            collect(request.url)
            route.abort()
        elif ((request.method in {'GET', 'HEAD'} or (parsed.hostname, parsed.path) in PUBLIC_QUERIES)
              and automaker_domain(parsed.hostname) in sites
              and request.resource_type not in {'font', 'image', 'media'}):
            route.continue_()
        else:
            route.abort()

    def record_payload(request):
        if (len(payloads) < 12 and request.resource_type in {'xhr', 'fetch'}
                and automaker_domain(urlsplit(request.url).hostname) in sites):
            response = request.response()
            if response and response.ok and 'json' in response.headers.get('content-type', ''):
                length = response.headers.get('content-length', '')
                if not length.isdigit() or int(length) <= 512 * 1024:
                    payloads.append(response)

    try:
        with PAGES.slot(), sync_playwright() as driver:
            browser = driver.chromium.launch(timeout=4000)
            try:
                context = browser.new_context(locale='zh-CN', service_workers='block', accept_downloads=False)
                context.route('**/*', discover)
                page = context.new_page()
                page.on('requestfinished', record_payload)
                page.goto(url, wait_until='domcontentloaded', timeout=6000)
                page.wait_for_timeout(1800)
                if not found:
                    button = page.get_by_role('button', name=PLAY).first
                    if button.is_visible():
                        button.click(timeout=1000, no_wait_after=True)
                        page.wait_for_timeout(800)
                sources = page.locator('video, video source').evaluate_all('''nodes => nodes.slice(0, 30)
                    .flatMap(node => [node.currentSrc, node.src, node.getAttribute('data-src')]).filter(Boolean)
                    .map(src => new URL(src, location.href).href)''')
                for source in sources:
                    collect(source)
                if not found:
                    for response in payloads:
                        for source in json_sources(response.text()[:512 * 1024]):
                            if source.startswith(('http://', 'https://', '//')):
                                collect(urljoin(response.url, source))
            finally:
                browser.close()
    except Error:
        pass
    return sorted(found)[:31]
