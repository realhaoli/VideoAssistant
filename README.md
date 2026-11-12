# Video Assistant · 中文讲解汇报工作台

基于 Video Assistant 改造的完整本地 Web 应用。用户在浏览器中上传音视频或输入视频链接，创建任务、查看处理进度，预览并下载结果。适用于新品发布会、技术讲解、调研汇报和专家讲座。

**文件、任务状态、字幕与模型权重保存在运行本系统的电脑上。无需 OSS、数据库、Redis 或远程语音识别服务。** 只有文本整理、翻译及可选的多模态评图调用所配置的大模型 API；相关字幕和候选图会发送给该模型服务。

## 五项功能

| 功能 | 默认行为 | 下载结果 |
| --- | --- | --- |
| 图文汇报 | 中文技术结论、机制与适量规格表；按正文评选原视频画面与封面 | 展示 HTML、MD；两项下载均为该图文报告完整 ZIP（HTML + MD + image/） |
| 文字汇报 | 与图文汇报共享技术报告正文，不含插图 | HTML、MD |
| 思维导图 | 沿用节点连线渲染，完整展开后输出 PDF | PDF |
| 字幕文件 | 共用中文字幕，保留时间标记 | 中文 TXT，英文来源另附英文 TXT |
| 视频下载 | 最高可用清晰度，中文来源嵌入中文字幕，英文来源嵌入中英双轨 | MP4 |

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

网页左下角的「模型设置」包含大语言模型、多模态模型、字幕模型和本地语音识别四个标签。「连接测试」检查当前标签的模型并更新容量显示。「保存配置」从任意标签统一保存所有设置，并读取各模型接口公布的容量。接口未公布某项容量时，同一模型沿用原值，新模型使用服务器的默认值。字幕模型可继承大语言模型，或使用独立的接口、模型名和密钥。接口可选择 OpenAI 风格或 Anthropic 风格。同一接口的密钥留空时沿用已保存的密钥。多模态选图需要模型支持图片输入。

配置保存到本地 `data/settings.json`。macOS / Linux 的密钥文件权限为 `0600`；Windows 继承数据目录的 ACL，请将数据放在自己的用户目录中。API 不向网页返回密钥。网页保存的设置优先于环境变量。

默认数据目录相对于项目根目录，不随终端工作目录改变。可在 `variables.env` 设置 `MEDIA_DATA_DIR`，例如 `MEDIA_DATA_DIR=D:/VideoAssistantData` 或 `MEDIA_DATA_DIR=workspace-data`。Windows 配置路径建议使用 `/`，路径可以包含空格和中文。Chromium 默认安装到该数据目录的 `browser/`，也可用 `PLAYWRIGHT_BROWSERS_PATH` 单独覆盖，变更路径后重新运行 `start-local.py --install-browser`。Git 和 Docker 都忽略 `data/` 与 `workspace-data/`。若将任务数据、Chromium 或登录 Cookie 放在仓库内的其他路径，请把实际路径加入 `.gitignore` 和 `.dockerignore`。

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

其他电脑访问 `http://运行本系统电脑的局域网IP:8080`。任务列表、产物与模型设置为共享工作台，没有用户账号或资源归属隔离。每个任务有独立素材、模型快照、检查点和输出目录。失败后，「继续处理」沿用任务的配置，「用当前模型继续」采用当前语言和视觉模型配置。已生成的产物和本地转写可以复用，未完成的模型阶段按模型与输入区分检查点。支持多人同时提交、预览和下载同一个资源。正在被读取的资源暂不能删除，下载结束后可删除。

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
   每个功能附有产物说明及参考耗时范围：图文汇报为视频时长的 1–2 倍，文字汇报和思维导图为 0.5–1.5 倍，字幕为 0.5–1 倍，视频下载耗时取决于网速、已有字幕及封装。这些是参考预估，不是独立实测统计，不含排队时间；实际会受设备、字幕是否齐全、网速和模型速度影响，也可能超出范围。
3. 内容场景自动判断。视频、汇报、导图和字幕文件共用同一份中文字幕。
4. 提交后，在任务详情中查看进度和生成文件。所有结果的“预览”均在新页面打开；Markdown 预览会渲染为正文，视频支持播放与定位。
5. 已生成文件会陆续开放下载。每种产物分别显示状态和进度；某个分支失败、其他文件完成时，任务显示“部分完成”。点击继续只补齐未完成产物，复用素材、转写和章节检查点。

“视频下载”导出带可切换字幕的 MP4。中文来源嵌入中文字幕，英文来源同时嵌入原有或转写的英文字幕与中文译文。字幕轨校验通过后才发布 MP4，网页预览默认显示中文。最高画质取决于平台授权、登录状态和可用流。

### 进度显示

任务详情左侧为“当前阶段：…，xx%”，右侧为“总进度：xx%”。阶段百分比与总进度独立；总进度按处理阶段加权，不代表已耗时占比或剩余时间。

| 阶段 | 百分比依据 |
| --- | --- |
| 素材下载 | 下载字节数 / 平台提供的总大小（部分平台为估计大小） |
| 提取转写音轨、视频封装 | FFmpeg 实际处理到的媒体时间 / 总时长 |
| Whisper 转写 | 已返回字幕段的音频位置 / 音频总时长，成功结束后到 100% |
| 外语转中文字幕、可选的模型清洗 | 已完成批次 / 总批次 |
| 分段章节规划 | 前 80% 按已处理文本量，随后按已检查的窗口边界数；单窗口在返回时完成 |
| 章节正文和配图 | 已完成章节 / 总章节 |
| 章节抽帧 | 当前区段已检查的采样位置 / 该区段采样位置总数 |
| 多页导图 PDF | 已生成的章节页 / 章节总数 |

等待转写资源、模型加载、单次模型请求和单页 PDF 等无法可靠计量的阶段显示已运行或等待时间。页面按秒更新计时，百分比来自实际工作量。并行产物分别显示阶段。静音跳过、分批返回或读取已有缓存时，进度可能跳跃；GPU 失败后切换 CPU 会重新计算本次转写进度，总进度仍保持不回退。

系统保留原始 SRT 和原时间轴。已有的人工或平台自动中文字幕（包括 `ai-zh`）跳过转写清洗。中文成稿在封装和生成报告前由 OpenCC 在本机统一转成简体。Whisper 转写默认用本地规则移除整条纯语气词，保留其余转写原句。可在字幕模型标签的「如何清洗字幕」中选择「大模型清洗（准确性更高但速度更慢）」。首次配置也可用 `variables.env` 的 `ASR_CLEANUP_MODE=model` 设置默认值。翻译缺失或为空时仅补译有问题的编号。翻译及模型清洗的批次检查点保存在各任务的 `subtitle-batches/`，重试会复用已完成条目。

## 图文汇报流程与效率

本次参考了同目录的 `media_to_doc-1.3.0` 的阶段产物/断点思想、`video-devour` 的字幕优先与视觉选帧思路，以及 Video Assistant 的网页交互和思维导图组件，重新实现本地流水线，不引入其整套依赖。

1. **字幕优先**：先检查平台字幕、本地内嵌文字字幕或配套字幕；缺失才下载音频并用所选 Whisper 模型转写（默认 small）。保留时间戳与原始版本。
2. **中文整理**：只有本地转写结果默认清洗。来源字幕为外语时翻译成中文，各产物共用同一份中文字幕。长视频分块处理并汇总。
3. **章节规划**：按实质技术主题与真实字幕编号划分章节，开场、场地和公司历史并入第一个实质章节。正文直接陈述技术结论、机制和数据；仅至少两项产品/技术客观规格时使用参数表，禁止用历史年数、场地面积等凑表。结论与关键数据从核对后的正文提炼。默认提示词位于 `backend/local/report_prompts.py`，图文与文字汇报共用。
4. **并行准备产物**。章节正文按字幕核查后，汇报摘要、导图 PDF 和图文候选帧可以并行准备。超过 8 分钟的章节分成最多 4 个选图区段。每段抽取 8 个位置，优先覆盖字幕提到参数或演示的时点，最多送审 4 张。导图从整章正文均匀选取最多 6 条要点。

5. **图文联合评审**：将实际章节正文、配图目标、候选图和真实时间戳一起提交多模态模型，返回排序、理由和中文图注。允许全部不合适，避免强行插图。
6. **跨章去重**。按时间顺序每段最多选用一张，记录已完成的配图和对应时间。图片放在章节开头说明之后。封面单独用作首页素材。
7. **便携导出**：HTML 与 Markdown 都引用 `image/...jpg` 相对路径；图文 ZIP 只包含 `illustrated.html`、`illustrated.md`、实际使用的封面和配图，图片统一位于 `image/`。解压后保持文件结构即可离线打开。不会把文字报告、字幕、视频、导图和诊断文件混入图文包。打包前逐个检查图片引用，缺图直接报错。

任务按检查素材、下载、媒体提取、转写、字幕模型处理、视频封装和报告生成等阶段分别排队。`TASK_WORKERS=1–8` 默认值为 2，控制检查和报告生成阶段的并发。每个任务最多占用一个模型请求，全服务默认最多两个模型请求。下载、重型 FFmpeg、Whisper 和 PDF 默认单路。文字正文、已配好图的图文包可以在摘要完成前下载。摘要失败时点「继续处理」可复用正文和选图结果补做。历史列表按页读取摘要，打开任务详情时读取事件记录。

后台通过 `MAX_ACTIVE_TASKS` 限制同时加载素材和字幕的任务数，默认取 8 与四倍 `TASK_WORKERS` 中的较大值。其他排队任务只保留任务 ID，等有空位再加载。`MAX_PENDING_TASKS` 默认 64，合计上传中、排队中和运行中的任务，达到上限时新提交或重试会收到 HTTP 429。

长内容先按文字量和时间范围划小窗口规划，保留真实字幕时间和编号。拼接时只检查相邻窗口交界，每侧最多取 400 字的原始字幕和简短的章节信息。模型确认同一主题且两侧引文均能核对时合并，超限或无法验证时保留原边界。合并后的长章节仍按小段提取事实、撰写及核查。全篇结论和选图所用长正文经过分层摘要。模型明确返回输入或输出长度超限时，先有限增加输出预算，再按阶段缩小分段重试，不将截断的 JSON 当成完整结果。规划窗口、已验证的边界、正文分段、摘要与字幕批次单独保存，失败重试可复用。接口不可用、持续限流或最小请求仍不被模型支持时，任务保留检查点并显示失败原因。

平台提供清晰度分档时，图文素材下载上限为 **1080p**。普通单文件直链没有分档可选时使用原文件，抽帧输出限制在 1920×1080 内。仅文字、导图或字幕任务如果有现成字幕，不下载完整视频；缺失字幕时只下载音频。

同时选择多项输出时，共用字幕读取、Whisper 转写、清洗和翻译结果。有中文字幕时，文稿和视频下载可并行。需要下载视频检查字幕或转写时，下载一次最高可用画质的素材，并复用其音轨和画面。各产物独立发布。封装优先保留兼容的音视频编码。

### RTX 3070 与 CPU 兜底

可将 `variables_gpu_3070_template.env` 的参数合并进本机 `variables.env`。这是可选部署模板，代码没有按 3070 或显卡名称硬编码，也可用于其他 CUDA 显卡。默认保持两个后台任务、一个 Whisper 推理，不因显卡存在而直接提高全部并发；GPU 主要加速本地转写，远程百炼/方舟请求仍受接口吞吐和并发限制。`WHISPER_COMPUTE_TYPE=auto` 查询设备实际支持的精度，优先 `int8_float16`，再选择支持的其他精度；CPU 使用 `int8`，CPU 线程默认 4。Whisper 模型默认 small，可在设置中按质量和显存需求调整。当前转写引擎未接入 AMD/Intel GPU 或 Apple MPS，这些机器自动使用 CPU。

`WHISPER_DEVICE=auto` 优先 CUDA；显卡不可用、CUDA 初始化失败、精度不支持或推理失败时自动切回 CPU，即使设置为 `cuda` 也保留兜底。长任务一次回退后，本次运行后续分块直接使用 CPU，避免每块重复触发 GPU 错误。新一次继续任务可重新尝试修复后的 GPU。也可显式选择 `cpu`。当前 faster-whisper 的 GPU 依赖 CUDA 12 / cuDNN 9，安装说明以 [faster-whisper 官方文档](https://github.com/SYSTRAN/faster-whisper#gpu) 为准；没有对应运行库时仍可使用 CPU。

CUDA 模型在独立进程中加载和转写。模型加载含首次下载，默认最多等待 30 分钟（`WHISPER_GPU_LOAD_TIMEOUT_SECONDS`）；加载后，语言检测或转写连续 180 秒无进度（`WHISPER_GPU_STALL_TIMEOUT_SECONDS`）时结束 GPU 进程，复用当前音频改用 CPU。超时后服务会暂停尝试 GPU 5 分钟，已完成的转写分块可继续复用。

### 长视频容量与断点

超过 5 分钟的转写默认切成 300 秒音频块，边缘额外读取 2 秒重叠，按原始时间轴归并。每块完成即保存，随后删除该块临时 WAV；两小时素材约 24 个块，无需一次展开整段音频。中断仅重做当前未完成块；检查点损坏时仅重做损坏块。按素材指纹、模型和分块设置区分缓存，修改这些条件会新建检查点。

上下文和单次最大输出各有独立的上限，图片数量、请求体和响应时间也有各自的限制。网页保存模型配置时会自动读取接口提供的容量。接口缺少容量数据时，服务器使用已保存值或环境变量中的默认值。提交时会保存该任务的模型设置快照。

所有文本阶段按小段请求，规划保留完整时间范围，正文分段提取、撰写与核对，再分层提炼全篇结论。每次请求预检输入和输出预算；服务端返回上下文/输出/请求体超限时按阶段缩小文本或图片批次。发现供应商要求更小的输出参数时下调并记住本任务上限；输出被截断不接受为完整 JSON。文本按 UTF-8 字节数保守估计，视觉 token 成本仍以接口返回为准。摘要不收敛时有轮数上限并保留缓存，不无限调用接口。

| 环节 | 默认限制与调整方式 |
| --- | --- |
| 上传媒体 | `MAX_UPLOAD_MB=4096`，3070 大文件模板为 16384。上传为临时文件和任务副本按磁盘卷共享预留，素材和配套字幕全部写完后入队。上传中断需要重新上传。配套字幕上限 16 MiB |
| 在线下载 | `MAX_DOWNLOAD_MB=16384`，模板为 32768；下载中检查字节量，合并前检查空间；分离的视频/音频组件不当成完整缓存 |
| 磁盘 | `MIN_FREE_DISK_MB=1024`，模板为 4096；同时预留上传副本、当前音频块和视频封装空间。输入、输出、下载合并均可能需要额外空间 |
| 时长 | `MAX_DURATION_HOURS=24`；设为 0 取消业务时长上限，仍受磁盘、平台和接口限制 |
| 转写 | `WHISPER_CHUNK_SECONDS=300`（30–900）；CPU/GPU 共用单路模型，分块后保存和让出资源 |
| 模型输入/输出 | 报告、字幕和多模态模型各自配置上下文和输出，默认分别为 32768 和 8192 |
| 多模态 | 单次最多 4 图；最长边缩到 1600，每张 JPEG ≤2 MiB；`MODEL_REQUEST_MB=16` 限制估算请求体，超限进一步拆分 |
| PDF | 超过 12 章或 150 节点时按章分页；单页画布最多 1600 万像素/边长 12000，无法清晰容纳的单章明确报错；`PDF_TIMEOUT_SECONDS=300`（60–600） |
| 进程与接口时间 | FFmpeg 单次默认上限 6 小时，`FFMPEG_TIMEOUT_SECONDS` 可调 60–172800 秒。模型响应读取默认 `MODEL_TIMEOUT_SECONDS=150`（15–600 秒）。字幕翻译遇到超时会缩小批次一次并保存已完成的译文，可选的模型清洗超时后将剩余条目交给本地规则。 |

接口仍可能有供应商独立的限流、模型响应超时和最大输出限制，网络失败后可继续复用已完成分段。测试包含两小时分块中断恢复、GPU 初始化/推理失败转 CPU、全部 31 种非空功能组合（上传/链接与有/无字幕共 124 组）和并行产物故障隔离；这些使用合成素材及故障注入，不代替目标 3070 实机或实际供应商两小时素材的性能与质量验收。

## 本地保存位置

```text
data/
├── settings.json                    # 私有模型配置
├── models/faster-whisper-small/      # Whisper small 本地权重
├── browser/                         # 本地 PDF 渲染运行时（Docker 位于镜像内）
└── tasks/<任务名称>/                 # 同名追加 (2)、(3)；URL 使用唯一任务 ID
    ├── task.json                    # 唯一 ID、状态、日志和可下载文件清单
    ├── task.backup.json             # 自动更新的任务记录备份，用于启动恢复
    ├── model-settings.json          # 提交时的私有模型配置快照，不对外下载
    ├── subtitle-batches/            # 字幕、正文分段与摘要检查点（内部）
    ├── source.*                     # 原始上传或分析素材
    ├── platform.*.srt               # 已有平台字幕（若有）
    ├── whisper.json                 # 本地转写结果（若有）
    ├── whisper-chunks/              # 长视频逐块检查点；已完成块无需重新转写
    ├── plan-technical-*.json         # 章节检查点（内部）
    ├── draft-*.md                   # 各章节正文检查点（内部）
    ├── mindmap.json                 # 导图数据（内部）
    ├── subtitles.*.srt              # 原始与处理后的字幕（内部）
    ├── frames-technical-*/          # 候选帧（内部）
    └── output/                      # 仅所选功能的最终文件
        ├── illustrated.html / .md
        ├── illustrated.zip          # 仅此图文报告完整包
        ├── image/                   # 实际配图和 cover.jpg
        ├── text.md / text.html
        ├── mindmap.pdf              # 后台使用原版导图渲染器自动生成
        ├── subtitles.zh.txt
        ├── subtitles.en.txt         # 英文来源且选择字幕文件时
        └── video.mp4                # 带可切换字幕轨
```

可用 `MEDIA_DATA_DIR` 更换数据根目录。重启服务会把未完成任务重新排队；中断的上传需要重新提交。删除任务会删除该任务的本地文件。任务及模型不会自动上传到远程存储。

**重启不会清空历史。** 启动时从同一个数据目录读取已完成、失败、取消和未完成的任务；记录无法读取时尝试 `task.backup.json`。无法恢复的记录及原素材保持原样，网页会提示数量，具体路径可在服务器启动日志查看，不再静默跳过。启动迁移或解析视频标题时，目录改名失败会记录到服务日志，任务沿用原目录继续处理。

更新代码时保留原 `data/`；重新克隆仓库、更换电脑或指定另一个 `MEDIA_DATA_DIR` 会打开另一个工作区。Git 不包含任务数据，跨机器迁移历史需要在停止服务后完整复制数据目录（含任务记录、素材、输出和配置），或让 `MEDIA_DATA_DIR` 指向原数据目录。迁移后在目标平台重新安装 Chromium；后台模型权重可以按需重新下载。

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

GitHub Actions 的 `Windows, macOS and Linux` 工作流在三个系统分别安装依赖、构建前端、运行测试，包括真实 FFmpeg 字幕封装和后台 Chromium PDF 生成。只有三项作业通过后，才表示该提交已通过跨平台 CI 验证。

测试覆盖字幕解析与时间轴、原生字幕优先、1080p/MP4 下载策略、清洗数字保护、任务持久化与取消、上传/访问鉴权、路径越界、图文 ZIP 解压后的图片引用，以及实际 FFmpeg 导出的 MP4 字幕轨。

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

大语言模型负责章节规划、正文、事实核对和摘要。字幕模型负责字幕翻译和可选的 Whisper 转写校订。多模态模型负责选图与图注。各模型的接口格式应与供应商实际提供的接口匹配，可分别选择 OpenAI 兼容（Chat Completions）或 Anthropic（Messages）。

- OpenAI 兼容：`POST /v1/chat/completions`，Bearer API Key。
- Anthropic：`POST /v1/messages`，`x-api-key` 和 `anthropic-version` 请求头，图片自动转换为 Messages 的 base64 内容块。

地址可填写根地址、以 `/v1` 结尾的基础地址，或对应完整端点。现有腾讯云配置默认保留 OpenAI 兼容格式。暂未填写单独多模态模型信息时会复用文本模型配置；混用协议时请填写对应供应商的接口地址、模型名及密钥。
