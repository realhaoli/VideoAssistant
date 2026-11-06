# 本地后端

后端已改为 FastAPI + 本地文件任务队列，移除 OSS、数据库和远程 ASR 依赖。完整安装、模型配置、启动和运行说明见项目根目录 [README.md](../README.md)。

```bash
.venv/bin/python -m uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8080
```

生产模式由同一后端提供构建后的网页与 API。免登录、资源共享。请只运行一个 Web worker，后台任务默认并行 2 个，可通过 TASK_WORKERS（1–8）调整；不要使用多个 Uvicorn workers。
