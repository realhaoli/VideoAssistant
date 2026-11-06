import { Picture, Document, Share, Tickets, VideoCamera } from "@element-plus/icons-vue";

export const features = [
  { id: "illustrated", name: "图文汇报", icon: Picture, description: "中文正文与配图，下载完整资源包", note: "约", duration: "1–2 倍" },
  { id: "text", name: "文字汇报", icon: Document, description: "纯文字中文报告，HTML / MD", note: "约", duration: "0.5–1.5 倍" },
  { id: "mindmap", name: "思维导图", icon: Share, description: "梳理内容结构，导出 PDF", note: "约", duration: "0.5–1.5 倍" },
  { id: "subtitles", name: "字幕文件", icon: Tickets, description: "下载原版中文字幕，缺少时转写", note: "约", duration: "0.5–1 倍" },
  { id: "download", name: "视频下载", icon: VideoCamera, description: "下载中英文字幕的最高画质视频", note: "约", duration: "0.5–1 倍" },
];
