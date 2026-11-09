# Vue 网页前端

功能：上传或视频链接、多选输出、本地任务列表、处理进度、下载、图文预览和思维导图。模型密钥只在后端保存。

```bash
npm install
npm run build
npm run dev
```

开发模式通过 Vite 将 `/api` 代理到 `127.0.0.1:8080`。生产环境由 FastAPI 提供 `dist` 静态资源，不需要单独运行 Node 服务。

完整安装、启动和模型配置见根目录 [README.md](../README.md)。
