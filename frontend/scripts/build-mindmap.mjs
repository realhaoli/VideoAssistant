import { build } from "vite";

// One self-contained script can be embedded in downloaded HTML, without a CDN
// or a running backend. It shares the app's renderer and PDF exporter.
await build({
  configFile: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: "src/mindmap-standalone.js",
      name: "MediaMindMapBundle",
      formats: ["iife"],
      fileName: () => "mindmap-renderer.js",
    },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
