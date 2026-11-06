# Video Assistant · 中文讲解汇报工作台

基于 Video Assistant 改造的完整本地 Web 应用。用户在浏览器中上传音视频或输入视频链接，创建任务、查看处理进度，预览并下载结果。适用于新品发布会、技术讲解、调研汇报和专家讲座。

**文件、任务状态、字幕与模型权重保存在运行本系统的电脑上。无需 OSS、数据库、Redis 或远程语音识别服务。** 只有文本整理、翻译及可选的多模态评图调用所配置的大模型 API；相关字幕和候选图会发送给该模型服务。

## 五项功能

| 功能 | 默认行为 | 下载结果 |
| --- | --- | --- |
| 图文汇报 | 中文技术结论、机制与适量规格表；按正文评选原视频画面与封面 | 展示 HTML、MD；两项下载均为该图文报告完整 ZIP（HTML + MD + image/） |
| 文字汇报 | 与图文汇报共享技术报告正文，不含插图 | HTML、MD |
| 思维导图 | 沿用节点连线渲染，完整展开后输出 PDF | PDF |
| 字幕文件 | 优先现有字幕；缺失时 Whisper 转写；外语翻译为中文并默认清洗 | 中文 TXT（保留时间标记） |
| 视频下载 | 最高可用清晰度，优先 MP4/M4A，带可切换中英字幕轨 | MP4 |

多项功能可以一起选择。可独立下载已完成文件，不必等待整个任务结束。图文汇报只针对有画面的视频；纯音频可生成文字汇报、导图和字幕。

## 安装

### 1. 环境要求

支持目标为 **Windows、macOS、Linux**，统一使用 Python 启动入口。前端生成的是静态网页，可以在三个平台使用；Python 虚拟环境、`node_modules`、FFmpeg 与 Chromium 必须在目标平台重新安装，不能从 Mac 直接复制。

| 平台 | 建议环境 |
| --- | --- |
| Windows | Windows 11 / Windows Server 2022，x64 Python；安装 Microsoft Visual C++ x64 运行库 |
| macOS | macOS 14 或更新版本，Apple Silicon 或 Intel |
| Linux | Ubuntu 22.04 / 24.04，或 Debian 12 / 13；安装 Chromium 系统依赖和中文字体 |

依赖的支持范围见 [Playwright](https://playwright.dev/python/docs/intro#system-requirements) 和 [CTranslate2](https://opennmt.net/CTranslate2/installation)。Windows ARM64 原生 Python 不在当前预编译依赖的适配目标内。

- Python **3.12**（建议使用这一版本）。
- Node.js **22 或更新版本**，用于构建 Vue 前端。
- **FFmpeg 与 ffprobe**，应能在终端直接执行。
- 推荐安装 [uv](https://docs.astral.sh/uv/)。
- Whisper small 多语言权重约 500 MB，首次准备时需要联网。CPU 模式也能运行，速度取决于视频时长和硬件。

Windows 安装 [Python 3.12 x64](https://www.python.org/downloads/release/python-31210/)、[Node.js](https://nodejs.org/)、[FFmpeg（含 ffprobe）](https://ffmpeg.org/download.html) 和 [Visual C++ x64 运行库](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)。将 FFmpeg 的 `bin` 目录加入 `PATH`，重新打开终端，确认 `ffmpeg -version`、`ffprobe -version` 和 `node --version` 可执行。

macOS 可通过 Homebrew 安装系统工具：

```bash
brew install python@3.12 node ffmpeg uv
```

Ubuntu / Debian：

```bash
sudo apt update
sudo apt install ffmpeg python3-venv fonts-noto-cjk
# 另行安装 Python 3.12、Node.js 22+ 和 uv
```

### 2. 安装后端与前端

在本项目根目录执行：

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe start-local.py --install-browser
npm --prefix frontend ci
npm --prefix frontend run build
```

macOS / Linux：

```bash
cd "/你的项目目录"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt
.venv/bin/python start-local.py --install-browser
npm --prefix frontend ci
npm --prefix frontend run build
```

Linux 手动部署还需安装 Chromium 系统依赖：

```bash
.venv/bin/python start-local.py --install-browser --with-deps
```

后台导图 PDF 与网页使用同一个本地渲染器，完整展开后自动保存，不要求用户保持网页打开；导出全局单路运行以控制内存。Chromium 只处理本地导图数据，渲染时阻止所有外部请求。

如果没有 uv，可使用标准 Python 虚拟环境：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
```

依赖下载较慢时，可使用可信的镜像。例如：

```bash
uv pip install --python .venv/bin/python \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  -r backend/requirements.txt
```

### 3. 配置大模型

复制模板，编辑根目录的 `variables.env`：

Windows PowerShell（仅首次配置）：

```powershell
if (!(Test-Path variables.env)) { Copy-Item variables_template.env variables.env }
```

macOS / Linux（仅首次配置）：

```bash
cp variables_template.env variables.env
chmod 600 variables.env
```

最小配置示例：

```dotenv
LLM_BASE_URL=https://tokenhub.tencentmaas.com/v1
MODEL_ID=hy-vision-2.0-instruct
LLM_API_KEY=替换为你自己的Key
HOST=0.0.0.0
TASK_WORKERS=2
```

支持 OpenAI Chat Completions 兼容接口。此前配置好的密钥无需重新复制模板，以免覆盖已有配置。

可以在网页左下角的“模型设置”中分别设置大语言模型和多模态模型。每次打开都会读取服务器最新配置；只有一边有配置时，自动填到另一边，已存在的字段分别保留，缺失字段互相补齐。接口格式均明确选择 OpenAI 兼容或 Anthropic，不显示“沿用”选项。密钥在服务器端补齐，网页仅显示是否已配置，留空保存会保留已有密钥。共用模型时，该模型需要支持图片输入。也可以关闭多模态选图，退回清晰度与去重筛选。

配置保存到本地 `data/settings.json`。macOS / Linux 的密钥文件权限为 `0600`；Windows 继承数据目录的 ACL，请将数据放在自己的用户目录中。API 不向网页返回密钥。网页保存的设置优先于环境变量。

默认数据目录相对于项目根目录，不随终端工作目录改变。可在 `variables.env` 设置 `MEDIA_DATA_DIR`，例如 `MEDIA_DATA_DIR=D:/VideoAssistantData` 或 `MEDIA_DATA_DIR=workspace-data`。Windows 配置路径建议使用 `/`；路径可以包含空格和中文。Chromium 默认安装到该数据目录的 `browser/`，也可用 `PLAYWRIGHT_BROWSERS_PATH` 单独覆盖；变更路径后重新运行 `start-local.py --install-browser`。

Windows 数据目录建议放在本机 NTFS 分区，以支持下载中的文件与新生成文件之间的原子替换；已有下载保持读取原来的内容，新下载读取更新后的文件。

### 4. 准备 Whisper small

Windows 使用 `.\.venv\Scripts\python.exe scripts/prepare-whisper.py`；macOS / Linux 使用：

```bash
.venv/bin/python scripts/prepare-whisper.py
```

下载到 `data/models/faster-whisper-small/`。脚本支持断点重试；官方元数据可用时，会校验权重的 SHA-256。也可以不手动下载，首次转写时由 faster-whisper 准备模型。

在网页“模型设置 → 本地语音识别”中可选择 Tiny、Base、Small、Medium、Large-v3 或 Large-v3 Turbo，以及自动 / CPU / GPU（CUDA）。默认使用 small，并自动选择可用设备；无可用 GPU、GPU 初始化失败或推理失败时自动退回 CPU / int8。GPU 正常时使用 float16。Apple Silicon 的 MPS 不作为 CUDA GPU 使用，因此默认走 CPU。

也可通过环境变量指定首次配置：

```dotenv
WHISPER_MODEL=small
WHISPER_DEVICE=auto
```

更换模型会在下次需要转写的任务中生效；已缓存的转写继续复用，不会自动重跑。未缓存的模型会在首次使用时下载。

## 启动与停止

### 日常使用：一个地址提供网页与 API

前端构建后，在根目录执行：

Windows（PowerShell 或 CMD）：

```powershell
.\start-local.cmd
```

macOS / Linux：

```bash
./start-local.sh
```

也可以直接使用 `.venv` 中的 Python 执行 `start-local.py`；两个启动脚本都转交给同一入口，无需激活虚拟环境。入口支持 `--host`、`--port` 和 `--check`。例如 Windows 执行 `.\start-local.cmd --check`，macOS / Linux 执行 `./start-local.sh --check` 检查运行依赖。

浏览器访问 **http://localhost:8080**。终端按 `Ctrl+C` 停止。也可以直接启动后端：

```bash
.venv/bin/python -m uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8080
```

健康检查：`http://localhost:8080/health`；API 文档：`http://localhost:8080/docs`。

### 局域网内其他电脑访问

默认监听 `0.0.0.0:8080`，同一局域网可直接访问，不需要登录或访问密码：

```bash
HOST=0.0.0.0 ./start-local.sh
```

其他电脑访问 `http://运行本系统电脑的局域网IP:8080`。任务列表、产物与模型设置为共享工作台，没有用户账号或资源归属隔离。每个已提交任务有独立的源文件、模型配置快照、检查点和输出目录；修改模型设置只影响后续提交的任务。支持多人同时提交、预览和下载同一个资源。正在被读取的资源暂不能删除，下载结束后可删除。

网页收到提交成功结果后，刷新、切换任务或关闭浏览器都不会终止后台任务。文件上传须先完成；关闭尚未完成的上传页面需要重新上传。服务必须持续运行，关闭服务器进程、电脑关机或休眠会暂停处理；重启后自动恢复排队中的任务，复用已完成的阶段检查点。长期部署可使用下文 `docker compose up -d --build`，容器配置了自动重启和本地数据挂载。

正式部署由同一个 8080 端口提供网页、API 与文件，无需额外开放 5173。应用不限制访问主机名；若 localhost 正常但局域网地址超时，检查系统代理或 Proxifier 的局域网直连规则、服务器入站 8080 端口与路由器客户端隔离。使用 Nginx 等反向代理时，上传大小限制须与 `MAX_UPLOAD_MB`（默认 4096 MB）对应，并允许 PDF 补生成等请求的等待时间，例如 `proxy_read_timeout 300s`。任务运行不依赖持续 HTTP 连接。

### 开发模式

Windows：`.\start-local.cmd --dev`。macOS / Linux：

```bash
./start-local.sh --dev
```

前端 `http://localhost:5173`，后端 `http://localhost:8080`。日志位于 `logs/`。修改前端代码后，正式使用前重新执行 `npm --prefix frontend run build`。

可以通过 `--port 8081 --frontend-port 5174` 更换端口，Vite 的 API 代理会随之后端端口调整。Windows 局域网访问还需允许 Python 使用相应入站端口。停止启动器时会清理它启动的服务进程。

## 使用

1. 首页默认使用“视频链接”，也可切换到“上传文件”。两种输入区尺寸一致，字幕由系统自动检测和处理。
2. 勾选一种或多种输出。
   每个功能附有产物说明及参考耗时范围：图文汇报为视频时长的 1–2 倍，文字汇报和思维导图为 0.5–1.5 倍，字幕和视频下载为 0.5–1 倍。这些是参考预估，不是独立实测统计，不含排队时间；实际会受设备、字幕是否齐全、网速和模型速度影响，也可能超出范围。
3. 内容场景自动判断，字幕清洗由默认提示词执行，主页不显示额外配置。
4. 提交后，在任务详情中查看进度和生成文件。所有结果的“预览”均在新页面打开；Markdown 预览会渲染为正文，视频支持播放与定位。
5. 已生成文件会陆续开放下载。失败任务可继续处理；已缓存的素材、转写和章节结果会复用。

“视频下载”使用 **可切换字幕轨**，不将字幕烧录进画面。原素材没有中英字幕时，会补充本地转写和模型翻译。只有字幕轨校验通过才会提供最终 MP4。最高画质受平台授权、登录要求、来源分辨率和可用流限制；MP4 偏好不会把较高清晰度强行降为较低档。

系统保留原始 SRT 和原时间轴，默认整理中文字幕。清洗只允许移除纯语气词；实质内容返回空值时保留原句。翻译缺失或为空时仅补译有问题的编号，不把原始英文充作中文结果。翻译/清洗的批次检查点保存在各任务的 `subtitle-batches/`，重试会复用已完成条目。模型要求保留人名、数字、型号、单位及否定关系，并对清洗前后的数字做检查。重要信息仍应结合原素材核对。

## 图文汇报流程与效率

本次参考了同目录的 `media_to_doc-1.3.0` 的阶段产物/断点思想、`video-devour` 的字幕优先与视觉选帧思路，以及 Video Assistant 的网页交互和思维导图组件，重新实现本地流水线，不引入其整套依赖。

1. **字幕优先**：先检查平台字幕、本地内嵌文字字幕或配套字幕；缺失才下载音频并用所选 Whisper 模型转写（默认 small）。保留时间戳与原始版本。
2. **中文整理**：外语字幕先翻译为简体中文，再默认清洗；长视频分块处理并汇总，不直接截断到视频前几分钟。
3. **章节规划**：按实质技术主题与真实字幕编号划分章节，开场、场地和公司历史并入第一个实质章节。正文直接陈述技术结论、机制和数据；仅至少两项产品/技术客观规格时使用参数表，禁止用历史年数、场地面积等凑表。结论与关键数据从核对后的正文提炼。默认提示词位于 `backend/local/report_prompts.py`，图文与文字汇报共用。
4. **正文与抽帧并行**：每章在其时间范围内取约 8 个候选画面，过滤低质量和相似画面，留下最多 4 张有差异的候选。
正文生成后按原始字幕逐句核查，收紧没有依据的格式、效果和技术机制描述。

5. **图文联合评审**：将实际章节正文、配图目标、候选图和真实时间戳一起提交多模态模型，返回排序、理由和中文图注。允许全部不合适，避免强行插图。
6. **跨章去重**：按章节顺序选择没有重复使用的画面；图片放在对应章节的开头说明之后。封面单独用作首页素材，不替代各章节配图。
7. **便携导出**：HTML 与 Markdown 都引用 `image/...jpg` 相对路径；图文 ZIP 只包含 `illustrated.html`、`illustrated.md`、实际使用的封面和配图，图片统一位于 `image/`。解压后保持文件结构即可离线打开。不会把文字报告、字幕、视频、导图和诊断文件混入图文包。打包前逐个检查图片引用，缺图直接报错。

限流策略：默认 **2 个后台任务并行**，其余任务持久化排队；通过 `TASK_WORKERS=1–8` 调整，普通 CPU 服务器建议 1–2。每个任务最多 **2 个章节并行**；全服务共享 **2 个模型请求并发**和 **3 个 FFmpeg 抽帧并行**。Whisper 在模型加载与完整转写期间单路运行，避免多任务切换模型或争用显存。等待资源时可以取消任务，其他阶段可继续并行。模型请求不共享会话或内容。

长内容处理：章节规划先按文字量和时间范围划小窗口，保留真实字幕时间和编号；正文按小段提取事实、撰写及核查后合并。全篇结论和选图所用长正文经过分层摘要。模型明确返回输入或输出长度超限时，先有限增加输出预算，再按阶段缩小分段重试，不将截断的 JSON 当成完整结果。已完成的规划窗口、正文分段、摘要与字幕批次单独保存，失败重试可复用。接口不可用、持续限流或最小请求仍不被模型支持时，任务保留检查点并显示失败原因。

平台提供清晰度分档时，图文素材下载上限为 **1080p**。普通单文件直链没有分档可选时使用原文件，抽帧输出限制在 1920×1080 内。仅文字、导图或字幕任务如果有现成字幕，不下载完整视频；缺失字幕时只下载音频。

同时选择汇报和最高画质视频下载时，先生成文档并开放下载，再下载最高画质视频，最后封装中英字幕 MP4。下载优先采用原生 MP4/M4A，并直接以 MP4 合并；只有编码确实不兼容时才转换相应流。字幕封装仍需要读写文件，但一般不重编码视频。

## 本地保存位置

```text
data/
├── settings.json                    # 私有模型配置
├── models/faster-whisper-small/      # Whisper small 本地权重
├── browser/                         # 本地 PDF 渲染运行时（Docker 位于镜像内）
└── tasks/<任务名称>/                 # 同名追加 (2)、(3)；URL 使用唯一任务 ID
    ├── task.json                    # 唯一 ID、状态、日志和可下载文件清单
    ├── model-settings.json          # 提交时的私有模型配置快照，不对外下载
    ├── subtitle-batches/            # 字幕、正文分段与摘要检查点（内部）
    ├── source.*                     # 原始上传或分析素材
    ├── platform.*.srt               # 已有平台字幕（若有）
    ├── whisper.json                 # 本地转写结果（若有）
    ├── plan-technical-*.json         # 章节检查点（内部）
    ├── draft-*.md                   # 各章节正文检查点（内部）
    ├── mindmap.json                 # 导图数据（内部）
    ├── subtitles.*.srt              # 封装视频所需字幕（内部）
    ├── frames-technical-*/          # 候选帧（内部）
    └── output/                      # 仅所选功能的最终文件
        ├── illustrated.html / .md
        ├── illustrated.zip          # 仅此图文报告完整包
        ├── image/                   # 实际配图和 cover.jpg
        ├── text.md / text.html
        ├── mindmap.pdf              # 后台使用原版导图渲染器自动生成
        ├── subtitles.zh.txt
        └── video.mp4                # 可切换中英字幕轨
```

可用 `MEDIA_DATA_DIR` 更换数据根目录。重启服务会把未完成任务重新排队；中断的上传需要重新提交。删除任务会删除该任务的本地文件。任务及模型不会自动上传到远程存储。

请使用**单个 Uvicorn worker**，不要使用 `--workers 2` 或生产环境 `--reload`；后台并发通过 `TASK_WORKERS` 控制。任务目录包含服务进程锁，第二个进程不能重复执行相同目录中的任务。文件任务队列用于单机多人共享部署，不能让多台服务器同时调度同一个数据目录。任务显示名称修改后保持原目录稳定。原 UUID 目录会在首次升级启动时迁移，任务 ID 和旧链接保持有效。

## Docker（可选）

根目录已提供本地构建的 Dockerfile 与 Compose 配置，不再拉取原项目旧的云服务镜像：

```bash
cp variables_template.env variables.env
# 填写模型配置后执行
mkdir -p data
docker compose up -d --build
```

访问 `http://localhost:8080`。`./data` 挂载到容器中，重建容器不会丢失任务与模型。首次转写可能需要下载 Whisper small。

## 验证

```bash
uv pip install --python .venv/bin/python -r backend/requirements-dev.txt
.venv/bin/python -m pytest -q
npm --prefix frontend run build
```

Windows 将上述 `.venv/bin/python` 换为 `.\.venv\Scripts\python.exe`；如果没有 uv，使用 `.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt` 安装测试依赖。不需要单独设置 `PYTHONPATH`。实际后台 PDF 测试需先安装浏览器并构建前端，然后设置 `RUN_PDF_RENDER_TEST=1`（PowerShell：`$env:RUN_PDF_RENDER_TEST='1'`；Bash：`RUN_PDF_RENDER_TEST=1 .venv/bin/python -m pytest -q`）。

GitHub Actions 的 `Windows, macOS and Linux` 工作流在三个系统分别安装依赖、构建前端、运行测试，包括真实 FFmpeg 中英字幕封装和后台 Chromium PDF 生成。只有三项作业通过后，才表示该提交已通过跨平台 CI 验证。

测试覆盖字幕解析与时间轴、原生字幕优先、1080p/MP4 下载策略、清洗数字保护、任务持久化与取消、上传/访问鉴权、路径越界、图文 ZIP 解压后的图片引用，以及实际 FFmpeg 生成的 MP4 中英字幕轨。

## 已知范围

- 在线平台的反爬、登录、会员或地区限制不能由通用下载器保证绕过；当前网页不收集平台账号密码。如需登录态，可在服务器上自行准备 Netscape 格式 cookies 文件并通过 `YTDLP_COOKIES_FILE` 指定路径。
- 多模态模型不可用时，会记录提示并退回清晰度选帧；没有合适画面时不硬插图。
- 本次没有图片生成功能，不调用生图模型。
- 最终生成物按功能仅提供 HTML、MD、PDF、中文 TXT、MP4；图文下载为仅含该报告及所用图片的 ZIP。内部 SRT、JSON 和检查点不作为下载文件展示。未实现 PPTX / DOCX 导出。

## 参考

- [yt-dlp 官方文档](https://github.com/yt-dlp/yt-dlp)
- [faster-whisper 官方文档](https://github.com/SYSTRAN/faster-whisper)
- [腾讯云 TokenHub 接口说明](https://cloud.tencent.com/document/product/1823/130079)

原项目采用 MIT 许可证，保留原项目 LICENSE。

## 模型接口格式

在“模型设置”中，大语言模型和多模态模型分别选择 OpenAI 兼容（Chat Completions）或 Anthropic（Messages）。缺少一边配置时会从另一边补齐，已有独立配置保持各自的格式与模型。两者使用同一套报告、翻译和选图流程，效果主要取决于实际模型及其图片理解、上下文和输出能力。接口格式应与供应商实际提供的接口匹配。

- OpenAI 兼容：`POST /v1/chat/completions`，Bearer API Key。
- Anthropic：`POST /v1/messages`，`x-api-key` 和 `anthropic-version` 请求头，图片自动转换为 Messages 的 base64 内容块。

地址可填写根地址、以 `/v1` 结尾的基础地址，或对应完整端点。现有腾讯云配置默认保留 OpenAI 兼容格式。暂未填写单独多模态模型信息时会复用文本模型配置；混用协议时请填写对应供应商的接口地址、模型名及密钥。
