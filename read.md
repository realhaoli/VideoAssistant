# 安装与启动

完整说明与工作流程见 [README.md](README.md)。以下是本地安装的最短步骤。

## 前置环境

安装 Python 3.12、Node.js 22+、FFmpeg（包含 ffprobe）和 uv。本文以下命令用于 macOS / Linux；Windows 的完整安装、运行库要求和启动步骤见 [README.md](README.md#安装)。Windows 使用 `start-local.cmd`，三个系统都使用同一个 `start-local.py` 启动入口。

macOS：

```bash
brew install python@3.12 node ffmpeg uv
```

## 安装依赖

在项目根目录执行：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt
.venv/bin/python start-local.py --install-browser
cd frontend
npm ci
npm run build
cd ..
```

## 配置模型

第一次部署且没有配置文件时：

```bash
cp variables_template.env variables.env
chmod 600 variables.env
```

编辑 `variables.env`，填写 `LLM_BASE_URL`、`MODEL_ID`、`LLM_API_KEY`。已有配置时请直接编辑，不要覆盖。也可在网页左下角分别配置语言模型和多模态模型。

## 准备本地语音模型

```bash
.venv/bin/python scripts/prepare-whisper.py
```

默认使用 Whisper small，权重保存在 `data/models/`。网页“模型设置”可选择其他 Whisper 型号及自动 / CPU / GPU；没有可用 GPU 或 GPU 失败会退回 CPU。未缓存的模型会在首次转写时下载。

## 启动

```bash
./start-local.sh
```

访问 **http://localhost:8080**。按 `Ctrl+C` 停止服务。

开发模式：`./start-local.sh --dev`，访问 `http://localhost:5173`。

默认监听 `0.0.0.0:8080`。同一局域网访问 `http://服务器局域网IP:8080`，无需登录，任务和资源共享。也可显式执行 `HOST=0.0.0.0 ./start-local.sh`。

提交成功后，刷新或关闭网页不会中断后台任务。服务器进程须持续运行；长期部署可执行 `docker compose up -d --build`。服务重启会恢复未完成任务并复用检查点。上传未完成时关闭页面需要重新上传。

默认同时处理 2 个任务，可在 `variables.env` 设置 `TASK_WORKERS=2`（1–8）；仅启动一个 Web 进程，不使用多个 Uvicorn workers。模型请求、抽帧分别限流，Whisper 单路转写。每次提交固定模型配置，后续修改设置不影响已有任务。

任务文件位于 `data/tasks/<任务名称>/`，同名目录追加 `(2)`、`(3)`，每个目录统一保存源文件、检查点和 `output/` 产物。任务 ID 和下载 URL 唯一。长视频分段处理，触及模型长度限制时拆分重试，并复用已保存结果。各功能仅显示最终产物：图文汇报 HTML、MD；文字汇报 HTML、MD；思维导图 PDF；字幕中文 TXT；视频 MP4。点击“预览”在新页面打开。

图文汇报的 HTML 和 MD 下载按钮都获取该图文报告完整 ZIP，包内只有 HTML、MD 与 `image/`（实际使用的封面和配图）。解压后保留相对目录即可离线打开，不混入其他功能文件。视频 MP4 保留可切换的中英字幕轨，内部 SRT 不单独展示。

思维导图由后台通过本机 Chromium 和原版导图渲染器自动生成 PDF，关闭网页也能生成完整产物，无需远程 PDF 服务；旧任务缺少 PDF 时会在首次预览时补生成。Linux 手动安装还需运行 `PLAYWRIGHT_BROWSERS_PATH=data/browser .venv/bin/python -m playwright install --with-deps chromium --only-shell` 并安装中文字体（如 `fonts-noto-cjk`）；Docker 镜像已包含这些依赖。模型整理、章节规划和选图的中间文件保存在任务工作目录，不作为生成物下载。默认技术汇报提示词位于 `backend/local/report_prompts.py`。
