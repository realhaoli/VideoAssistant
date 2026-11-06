import MindMap from "simple-mind-map";
import Export from "simple-mind-map/src/plugins/Export.js";
import ExportPDF from "simple-mind-map/src/plugins/ExportPDF.js";

MindMap.usePlugin(Export).usePlugin(ExportPDF);

export function fitMap(map) {
  map.resize();
  const box = map.draw.bbox();
  if (!box.width || !box.height) return;
  const scale = Math.min(1, (map.width - 48) / box.width, (map.height - 48) / box.height);
  map.view.setScale(scale);
  map.view.translateXTo((map.width - box.width * scale) / 2 - box.x * scale);
  map.view.translateYTo((map.height - box.height * scale) / 2 - box.y * scale);
}

export function createMap(el, data, options = {}) {
  return new MindMap({
    el,
    data: typeof data === "string" ? JSON.parse(data) : structuredClone(data),
    theme: "primary",
    layout: "logicalStructure",
    readonly: true,
    fit: false,
    keypress: false,
    nodeTextEdit: false,
    enableNodeDragging: false,
    mousewheelAction: "zoom",
    textAutoWrap: true,
    // Explicit local fonts keep Chinese readable on macOS, Windows and Linux.
    themeConfig: {
      root: { fontFamily: "PingFang SC,Microsoft YaHei,Noto Sans CJK SC,sans-serif" },
      second: { fontFamily: "PingFang SC,Microsoft YaHei,Noto Sans CJK SC,sans-serif" },
      node: { fontFamily: "PingFang SC,Microsoft YaHei,Noto Sans CJK SC,sans-serif" },
    },
    ...options,
  });
}

export async function waitForMap(map) {
  // The renderer emits after its async layout, not after an arbitrary timeout.
  await new Promise((resolve, reject) => {
    if (map.renderer.root && !map.renderer.isRendering) {
      requestAnimationFrame(resolve);
      return;
    }
    const timeout = setTimeout(() => {
      map.off("node_tree_render_end", ready);
      reject(new Error("思维导图渲染超时，请重试"));
    }, 15000);
    const ready = () => {
      clearTimeout(timeout);
      map.off("node_tree_render_end", ready);
      requestAnimationFrame(resolve);
    };
    map.on("node_tree_render_end", ready);
  });
  await document.fonts.ready;
}

export async function renderPdf(data) {
  // Render a fresh, fully expanded tree. Scrolling, zoom and collapsed nodes in
  // the preview must never crop the downloadable document.
  const el = document.createElement("div");
  el.style.cssText = "position:fixed;left:0;top:0;width:1400px;height:900px;opacity:0;pointer-events:none;z-index:-1";
  document.body.append(el);
  const expand = (node) => ({
    ...node,
    data: { ...node.data, expand: true, isActive: false },
    children: (node.children || []).map(expand),
  });
  let map;
  try {
    const tree = typeof data === "string" ? JSON.parse(data) : data;
    map = createMap(el, expand(tree));
    await waitForMap(map);
    // Use local SVG coordinates. Library rbox() export is affected by page
    // scroll and off-screen containers, which can otherwise clip the map.
    map.doExport.getSvgData = async () => {
      const box = map.draw.bbox();
      const clone = map.svg.clone().remove();
      const draw = clone.findOne(".smm-container");
      draw.attr("transform", `translate(${32 - box.x},${32 - box.y})`);
      clone.size(Math.ceil(box.width + 64), Math.ceil(box.height + 64));
      clone.attr("viewBox", `0 0 ${Math.ceil(box.width + 64)} ${Math.ceil(box.height + 64)}`);
      clone.css("background-color", "#ffffff");
      const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
      style.textContent = map.joinCss();
      clone.node.prepend(style);
      return { node: clone, str: clone.svg(), clipData: null };
    };
    const uri = await map.export("pdf", false, "思维导图");
    if (!uri) throw new Error("PDF 导出失败");
    return await (await fetch(uri)).blob();
  } finally {
    map?.destroy();
    el.remove();
  }
}

export function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
