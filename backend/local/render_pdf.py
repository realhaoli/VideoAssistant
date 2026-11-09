"""Offline document-rendering subprocess; never attaches to a user's browser."""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path


async def render(source, target):
    from playwright.async_api import async_playwright
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'backend'))
    from local.paths import configure_browser_path
    configure_browser_path()
    runtime = root / 'frontend' / 'dist' / 'mindmap-renderer.js'
    if not runtime.is_file():
        raise RuntimeError('缺少导图渲染文件，请执行 npm --prefix frontend run build')
    tree = json.loads(source.read_text(encoding='utf-8'))
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={'width': 1400, 'height': 900})
            # The document and bundled renderer are local. Block all outbound
            # requests, including links a generated node might contain.
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            await page.set_content('<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>')
            await page.add_script_tag(path=str(runtime))
            encoded = await page.evaluate('''async tree => {
                const blob = await window.MediaMindMap.renderPdf(tree);
                return await new Promise((resolve, reject) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(reader.result.split(',')[1]);
                    reader.onerror = reject;
                    reader.readAsDataURL(blob);
                });
            }''', tree)
            target.write_bytes(base64.b64decode(encoded))
        finally:
            await browser.close()


if __name__ == '__main__':
    timeout = max(60, min(600, int(os.getenv('PDF_TIMEOUT_SECONDS', '300'))))
    asyncio.run(asyncio.wait_for(render(Path(sys.argv[1]), Path(sys.argv[2])), timeout=timeout))
