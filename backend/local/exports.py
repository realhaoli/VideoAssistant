import base64
import html
import json
import mimetypes
import re
import threading
import shutil
import zipfile
from pathlib import Path

import markdown

from .storage import atomic_text, path_lock
from .resources import reserve_disk

BUNDLE_LOCK = threading.RLock()

STYLE = """body{font:16px/1.85 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#222;background:#fff;margin:0}main{max-width:920px;margin:32px auto;padding:24px 40px;background:white}h1{font-size:28px;line-height:1.5;margin:0 0 24px;font-weight:600;color:#222;border-bottom:2px solid #4a90e2;padding-bottom:8px}h2{font-size:22px;line-height:1.5;margin:32px 0 16px;font-weight:600;color:#2a6496;border-bottom:1px solid #d0e0f0;padding-bottom:6px}h3{font-size:18px;margin:24px 0 12px;color:#3a749c}p{margin:14px 0;text-align:justify;overflow-wrap:anywhere}img{display:block;max-width:100%;height:auto;margin:20px auto 8px}em{font-size:13px;color:#888;font-style:normal}blockquote{margin:16px 0;padding:8px 16px;border-left:3px solid #dcdfe6;color:#606266}table{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px;line-height:1.7;table-layout:auto;overflow-wrap:anywhere}td,th{border:1px solid #dcdfe6;padding:10px 14px;text-align:left;vertical-align:top}th{background:#f8f9fa;color:#333;font-weight:600}td{color:#555}pre{overflow:auto;background:#f5f7fa;padding:16px}a{color:#409eff}.toc{padding:16px 24px;background:#f0f7ff;border:1px solid #d0e0f0;border-radius:6px;margin:24px 0}.toc ul{margin:0;padding-left:20px}.toc a{color:#2a6496}.transcript{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}footer{margin-top:40px;padding-top:16px;border-top:1px solid #e4e7ed;font-size:12px;color:#909399}@media(max-width:700px){main{margin:0;padding:20px}h1{font-size:24px}table{font-size:13px}td,th{padding:7px}}@media print{main{max-width:none;margin:0;padding:0}h2,h3{break-after:avoid}tr,img{break-inside:avoid}img{max-height:70vh;object-fit:contain}a{color:inherit}}"""


def write_text(path, content):
    atomic_text(path, content)


def inline_images(text, output):
    def replace(match):
        path = (output / match[2]).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file():
            return html.escape(match[1])
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return f"![{match[1]}](data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()})"
    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", replace, text)


def page(title, body):
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}<footer>由 Video Assistant 根据原始音视频整理 · 请结合原始材料核对关键信息</footer></main></body></html>'


def report_html(text, output, title="技术汇报", embedded=False):
    # Escape model-generated HTML. Only our renderer emits image markup.
    safe = clean_report(text)
    rendered = inline_images(safe, output) if embedded else safe
    content = markdown.markdown(html.escape(rendered, quote=False), extensions=["tables", "fenced_code", "sane_lists", "toc"], extension_configs={"toc": {"toc_depth": "2-2"}})
    # Disallow links to scripts; report images are embedded local bytes.
    content = re.sub(r'(href|src)="(?:javascript|vbscript):[^\"]*"', r'\1="#"', content, flags=re.I)
    def safe_link(match):
        value = re.sub(r"[\x00-\x20]", "", html.unescape(match[1]))
        if not value.lower().startswith(("https://", "http://", "mailto:", "#")):
            return 'href="#"'
        return match[0]
    content = re.sub(r'href="([^\"]*)"', safe_link, content, flags=re.I)
    def safe_image(match):
        value = html.unescape(match[1])
        if value.startswith("data:image/"):
            return match[0]
        image = (output / value).resolve()
        if value.startswith("image/") and image.is_relative_to(output.resolve()) and image.is_file():
            return match[0]
        return 'src=""'
    content = re.sub(r'src="([^\"]*)"', safe_image, content, flags=re.I)
    return page(title, content)


def report(output, basename, text):
    safe = clean_report(text)
    write_text(output / f"{basename}.md", safe)
    write_text(output / f"{basename}.html", report_html(safe, output, "图文汇报" if basename == "illustrated" else "文字汇报"))


def clean_report(text):
    text = re.sub(r"<[|｜][^>]*[|｜]>", "", text)
    text = re.sub(r"</?(?:script|style|iframe|img|svg|object|div|span|p|h[1-6]|a|br|table|tr|td|th)\b[^>]*>", "", text, flags=re.I)
    # Models sometimes wrap each entire chapter in a Markdown code block.
    # Unwrap these presentation fences so paragraphs and tables render as such.
    text = re.sub(r"(?m)^\s*```(?:markdown|md)?\s*$", "", text, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def grounded_body(text, source):
    """Remove recurring technical embellishments when absent from evidence.

    This complements model review; it is deliberately conservative because the
    report must not supply an unstated test method, precision or failure mode.
    """
    terms = ("风洞", "毫秒级", "疲劳寿命", "非簧载", "非铺装", "热管理", "故障容错", "瞬时扭矩",
             "矢量控制精度", "结构强度", "传感器采集", "小批量试产", "频繁充能", "人体耐受上限", "市售高性能电动车",
             "两个或四个推进器", "多推进器演示", "不同推进器配置", "配置推进器", "推力或质量优化上的不足")
    unsupported = [term for term in terms if term not in source]
    text = re.sub(r"[（(]注[：:][^）)]*[）)]", "", clean_report(text))
    parts = re.split(r"(?<=[。！？；])|(?<=\n)", text)
    result = "".join(part for part in parts if not any(term in part for term in unsupported)
                     and not re.search(r"原文未提供|字幕未|视频未|故不设置|不设置参数表|这反映了|这提示|例如，若|表明当前处于|需集成两套独立|若无法解决", part))
    result = re.sub(r"[（(]注[：:][^）)]*[）)]", "", result)
    result = re.sub(r"(?m)^\s*[（）()，。、；;]+\s*$", "", result)
    # A future speed target is not a measured top speed. Resolve its status
    # from the corresponding source sentence before formatting the report.
    for sentence in re.split(r"[。！？\n]", source):
        if re.search(r"目标|计划|预期|未来|希望|应该|将|我想|我认为|可能|你会", sentence):
            for number in re.findall(r"\d+(?:\.\d+)?", sentence):
                result = re.sub(r"(?<!目标)(?:实测)?最高(?:速度|时速)([^。\n]{0,12}" + re.escape(number) + r"(?![\d.]))", r"目标最高速度\1", result)
                result = re.sub(r"(" + re.escape(number) + r"\s*(?:mph|英里[^，。\n]{0,4})最高速度[：:]\s*)实测值", r"\1目标值", result, flags=re.I)
    result = re.sub(r"(?m)^### [^\n]+\n(?:\s*\n)*(?=### |\Z)", "", result)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def report_outline(plan, bodies):
    """Use the final report facts, rather than draft planning hints, in maps."""
    chapters = []
    for chapter, body in zip(plan["chapters"], bodies):
        points = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or re.fullmatch(r"[（）()，。、；;\s]+", line):
                continue
            if line.startswith("|"):
                cells = [part.strip() for part in line.strip("|").split("|")]
                if not cells or cells[0] in {"参数", "技术项目"} or all(re.fullmatch(r"[-: ]*", cell) for cell in cells):
                    continue
                candidates = ["：".join(cells[:2])]
            else:
                candidates = [line]
            for sentence in candidates:
                sentence = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", sentence)
                sentence = re.sub(r"[\*`]|\[\[IMG:[^\]]+\]\]", "", sentence)
                sentence = re.split(r"[。！？]", sentence)[0].strip()
                if sentence and sentence not in points:
                    points.append(sentence if len(sentence) <= 90 else sentence[:89] + "…")
                if len(points) >= 6:
                    break
            if len(points) >= 6:
                break
        chapters.append(dict(chapter, points=points))
    return dict(plan, chapters=chapters)


def mindmap(output, plan):
    def node(text, children=()):
        return {"data": {"text": re.sub(r"<[^>]*>", "", str(text))[:240]}, "children": list(children)}
    tree = node(plan["title"], [node(c["title"], [node(p) for p in c.get("points", [])[:8]]) for c in plan["chapters"]])
    content = json.dumps(tree, ensure_ascii=False, indent=2)
    target = output / "mindmap.json"
    if target.exists() and target.read_text(encoding="utf-8") != content:
        (output / "mindmap.pdf").unlink(missing_ok=True)
    write_text(target, content)

    mindmap_html(output, tree)
    lines = [f"# {plan['title']}"]
    for ch in plan["chapters"]:
        lines.append("\n## " + ch["title"])
        lines.extend("- " + str(p) for p in ch.get("points", []))
    (output / "mindmap.md").write_text("\n".join(lines), encoding="utf-8")


def mindmap_html(output, tree):
    runtime = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "mindmap-renderer.js"
    if not runtime.is_file():
        raise RuntimeError("缺少思维导图渲染文件，请在 frontend 执行 npm run build")
    # Both data and bundled JS are inline, so the HTML works offline. Escape
    # script terminators even when generated text contains hostile markup.
    script = re.sub(r"</script", r"<\\/script", runtime.read_text(encoding="utf-8"), flags=re.I)
    data = json.dumps(tree, ensure_ascii=False).replace("<", "\\u003c")
    title = html.escape(tree["data"]["text"])
    content = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>思维导图</title>
<style>body{margin:0;font:14px system-ui,sans-serif;color:#303133;background:white}
header{height:56px;padding:0 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #dcdfe6}
strong{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}button{cursor:pointer;padding:7px 12px;background:white;border:1px solid #dcdfe6;border-radius:4px;color:#303133}
button:disabled{opacity:.5;cursor:wait}#map{width:100vw;height:calc(100vh - 57px)}#map *{margin:0;padding:0}</style></head><body>
<header><strong>__TITLE__</strong><button id="fit">适应画布</button><button id="pdf" disabled>下载 PDF</button><span id="status">正在渲染</span></header><div id="map"></div>
<script>__RUNTIME__</script><script>
const tree=__DATA__;
const api=window.MediaMindMap, status=document.getElementById('status'), button=document.getElementById('pdf');
const map=api.createMap(document.getElementById('map'),tree);
api.waitForMap(map).then(()=>{api.fitMap(map);status.textContent='';button.disabled=false}).catch(e=>status.textContent=e.message);
document.getElementById('fit').onclick=()=>api.fitMap(map);
window.addEventListener('resize',()=>api.fitMap(map));
button.onclick=async()=>{button.disabled=true;status.textContent='正在导出 PDF';try{api.downloadBlob(await api.renderPdf(tree),'mindmap.pdf');status.textContent=''}catch(e){status.textContent=e.message}finally{button.disabled=false}};
</script></body></html>'''
    # Replace data last: user text must never be interpreted as a template token.
    content = content.replace("__TITLE__", title).replace("__RUNTIME__", script).replace("__DATA__", data)
    write_text(output / "mindmap.html", content)


def bundle(output, illustrated_only=True):
    """The illustrated download contains only the report and its used images."""
    output = Path(output).resolve()
    with path_lock(output):
        documents = [output / "illustrated.md", output / "illustrated.html"]
        if not all(p.is_file() for p in documents):
            raise ValueError("图文汇报尚未生成完整")
        referenced = set()
        for relative in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", documents[0].read_text(encoding="utf-8")):
            path = (output / relative).resolve()
            if not relative.startswith("image/") or not path.is_relative_to((output / "image").resolve()) or not path.is_file():
                raise ValueError(f"图文汇报图片路径无效：{relative}")
            referenced.add(path)
        temporary = output / "illustrated.zip.part"
        temporary.unlink(missing_ok=True)
        files = documents + sorted(referenced)
        size = sum(path.stat().st_size for path in files)
        try:
            with reserve_disk(output, size + size // 100 + 1024**2):
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in files:
                        archive.write(path, path.relative_to(output))
                temporary.replace(output / "illustrated.zip")
        finally:
            temporary.unlink(missing_ok=True)


ARTIFACTS = {
    "illustrated": [("illustrated.html", "图文汇报 HTML", "html"), ("illustrated.md", "图文汇报 Markdown", "markdown")],
    "text": [("text.html", "文字汇报 HTML", "html"), ("text.md", "文字汇报 Markdown", "markdown")],
    "mindmap": [("mindmap.pdf", "思维导图 PDF", "pdf")],
    "subtitles": [("subtitles.zh.txt", "中文字幕 TXT", "text")],
    "download": [("video.mp4", "视频 MP4 · 中英字幕", "video")],
}


def publish(store, job_id):
    with store.lock:
        job = store.get(job_id)
        folder = store.directory(job_id)
        output = folder / "output"
        artifacts = []
        for mode, entries in ARTIFACTS.items():
            if mode not in job["modes"]:
                continue
            if mode == 'illustrated' and not (output/'illustrated.zip').is_file():
                continue
            for name, label, kind in entries:
                path = output / name
                pending = name == "mindmap.pdf" and (folder / "mindmap.json").is_file() and not path.exists()
                if not path.is_file() and not pending:
                    continue
                entry = dict(name=name, label=label, kind=kind, size=path.stat().st_size if path.exists() else 0)
                if pending:
                    entry["pending"] = True
                if mode == "illustrated":
                    entry["download_name"] = "illustrated.zip"
                artifacts.append(entry)
        store.update(job_id, artifacts=artifacts)


def mindmap_data(path, plan):
    def node(text, children=()):
        return {"data": {"text": re.sub(r"<[^>]*>", "", str(text))[:240]}, "children": list(children)}
    tree = node(plan["title"], [node(c["title"], [node(p) for p in c.get("points", [])[:8]]) for c in plan["chapters"]])
    content = json.dumps(tree, ensure_ascii=False, indent=2)
    pdf = path.parent / "output" / "mindmap.pdf"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        pdf.unlink(missing_ok=True)
    write_text(path, content)


def normalize_outputs(folder, modes):
    """Migrate old results while preserving previous files outside downloads."""
    output = folder / "output"
    output.mkdir(exist_ok=True)
    if not (folder / "cover.jpg").exists() and (output / "cover.jpg").exists():
        shutil.copy2(output / "cover.jpg", folder / "cover.jpg")
    for name in ("subtitles.original.srt", "subtitles.zh.srt", "subtitles.en.srt", "mindmap.json", "image-selections.json"):
        if (output / name).is_file() and not (folder / name).exists():
            shutil.copy2(output / name, folder / name)
    if "download" in modes and (output / "video.bilingual.mp4").is_file() and not (output / "video.mp4").exists():
        (output / "video.bilingual.mp4").rename(output / "video.mp4")
    if "subtitles" in modes and not (output / "subtitles.zh.txt").exists() and (output / "transcript.zh.txt").is_file():
        shutil.copy2(output / "transcript.zh.txt", output / "subtitles.zh.txt")
    if "illustrated" in modes and (output / "illustrated.md").is_file():
        content = (output / "illustrated.md").read_text(encoding="utf-8")
        original_content = content
        def migrate_image(match):
            relative = match[2]
            source = (output / relative).resolve()
            if not source.is_relative_to(output.resolve()) or not source.is_file():
                return match[0]
            target = output / "image" / source.name
            target.parent.mkdir(exist_ok=True)
            if source != target.resolve():
                shutil.copy2(source, target)
            return f"![{match[1]}](image/{target.name})"
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", migrate_image, content)
        if content != original_content or not (output/'illustrated.html').is_file() or not (output/'illustrated.zip').is_file():
            report(output, "illustrated", content)
            bundle(output)
    allowed = {name for mode in modes for name, _, _ in ARTIFACTS.get(mode, [])}
    if "illustrated" in modes:
        allowed |= {"image", "illustrated.zip"}
    previous = folder / "previous-results"
    for path in list(output.iterdir()):
        if path.name in allowed:
            continue
        previous.mkdir(exist_ok=True)
        destination = previous / path.name
        number = 1
        while destination.exists():
            destination = previous / f"{number}-{path.name}"
            number += 1
        path.rename(destination)
