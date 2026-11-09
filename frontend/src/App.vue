<script setup>
import {
  ref,
  computed,
  onMounted,
  onBeforeUnmount,
} from "vue";
import { ElMessage, ElMessageBox, ElPagination } from "element-plus";
import {
  Plus,
  Document,
  Picture,
  Download,
  UploadFilled,
  Link,
  FolderOpened,
  Setting,
  ArrowLeft,
  Refresh,
  Close,
  Check,
  VideoCamera,
} from "@element-plus/icons-vue";

import { features } from "./features.js";
import OutputModes from "./components/OutputModes.vue";
import TaskActions from "./components/TaskActions.vue";

const view = ref("new"),
  sourceTab = ref("url"),
  modes = ref([]);
const file = ref(null), url = ref("");
const fileInput = ref(null),
  dragging = ref(false),
  submitting = ref(false),
  uploadPercent = ref(0);
const tasks = ref([]),
  historyTasks = ref([]),
  historyPage = ref(1),
  taskCount = ref(0),
  activeCount = ref(0),
  completedCount = ref(0),
  selectedId = ref(null),
  settings = ref({}),
  models = ref(null),
  modelBusy = ref(false);
const statusError = ref("");
const historyRecovery = ref({ recovered_tasks: 0, unreadable_tasks: 0 });
const pageSize = 25;
const taskDetail = ref(null);
const activeTask = computed(() =>
  taskDetail.value?.id === selectedId.value ? taskDetail.value : null,
);
const exportingPdf = ref(false);
const artifactOrder = ["illustrated.html", "illustrated.md", "text.html", "text.md", "video.mp4", "mindmap.pdf", "subtitles.zh.txt"];
const orderedArtifacts = computed(() => [...(activeTask.value?.artifacts || [])]
  .filter(a => artifactOrder.includes(a.name))
  .sort((a,b) => artifactOrder.indexOf(a.name) - artifactOrder.indexOf(b.name)));
const canSubmit = computed(
  () =>
    modes.value.length &&
    (sourceTab.value === "upload" ? file.value : url.value.trim()) &&
    !submitting.value,
);
const progressText = (message = "") => message.replace(/Whisper\s+(?:large-v3-turbo|large-v3|medium|small|base|tiny)\s+(?=转写|本地转写)/g, "Whisper ");
const stageProgressText = computed(() => {
  const task = activeTask.value;
  const running = Object.values(task?.output_states || {}).filter(output => output.status === 'running');
  const stages = [...new Set(running.map(output => output.message).filter(Boolean))];
  if (stages.length > 1) return '并行处理：' + stages.join(' / ');
  if (running.length && stages.length === 1) {
    const value = running[0].stage_progress;
    return progressText(stages[0]).replace(/[\s，,]*\d+(?:\.\d+)?%$/, '') + (typeof value === 'number' ? `，${Math.round(value)}%` : '');
  }
  const message = progressText(task?.message || "等待处理");
  if (typeof task?.stage_progress === "number" && Number.isFinite(task.stage_progress)) {
    const label = message.replace(/[\s，,]*\d+(?:\.\d+)?%$/, "");
    return `${label}，${Math.round(task.stage_progress)}%`;
  }
  return message.replace(/[\s，,]*(\d+(?:\.\d+)?)%$/, "，$1%");
});
const statusName = (state) =>
  ({
    queued: "等待处理",
    pending: "等待处理",
    running: "处理中",
    completed: "已完成",
    partial: "部分完成",
    failed: "处理失败",
    cancelled: "已取消",
    cancelling: "取消中",
    uploading: "上传中",
  })[state] || state;
const date = (stamp) =>
  new Date(stamp * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
const bytes = (size) =>
  size > 1024 ** 3
    ? (size / 1024 ** 3).toFixed(2) + " GB"
    : size > 1024 ** 2
      ? (size / 1024 ** 2).toFixed(1) + " MB"
      : (size / 1024).toFixed(1) + " KB";
const fileUrl = (task, name, inline = false) =>
  `/api/v1/tasks/${task.id}/files/${name.split("/").map(encodeURIComponent).join("/")}${inline ? "?inline=true" : ""}`;
function notifyError(error) {
  ElMessage.error(error.message || String(error));
}
function request(path, options = {}) {
  return fetch("/api/v1" + path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
}
async function decodeResponse(response) {
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(
      typeof data.detail === "string"
        ? data.detail
        : "请求未成功，请检查输入或服务状态",
    );
    error.status = response.status;
    throw error;
  }
  return data;
}
async function api(path, options) {
  return decodeResponse(await request(path, options));
}
async function conditional(path, previous) {
  const headers = previous?.path === path && previous.etag ? { "If-None-Match": previous.etag } : {};
  const response = await request(path, { headers, cache: "no-store" });
  if (response.status === 304) return previous;
  return { path, etag: response.headers.get("ETag"), data: await decodeResponse(response) };
}
let listSnapshot, detailSnapshot, refreshing, refreshAgain = false;
async function refreshView() {
  const page = view.value === "history" ? historyPage.value : 1;
  const taskId = view.value === "detail" ? selectedId.value : null;
  try {
    listSnapshot = await conditional(`/tasks?limit=${pageSize}&offset=${(page - 1) * pageSize}`, listSnapshot);
    const summary = listSnapshot.data;
    tasks.value = summary.recent;
    taskCount.value = summary.task_count;
    activeCount.value = summary.active_count;
    completedCount.value = summary.completed_count;
    historyRecovery.value = summary;
    if (view.value === "history" && page === historyPage.value) {
      historyTasks.value = summary.items;
      historyPage.value = summary.offset / pageSize + 1;
    }
    if (taskId && view.value === "detail" && selectedId.value === taskId) {
      try {
        detailSnapshot = await conditional(`/tasks/${taskId}`, detailSnapshot);
        if (selectedId.value === taskId) taskDetail.value = detailSnapshot.data;
      } catch (error) {
        if (error.status !== 404) throw error;
        if (selectedId.value === taskId) openHistory();
      }
    }
    statusError.value = "";
  } catch (error) {
    statusError.value = error.message;
  }
}
function refresh(force = false) {
  if (refreshing) {
    if (force) refreshAgain = true;
    return refreshing;
  }
  refreshing = (async () => {
    do {
      refreshAgain = false;
      await refreshView();
    } while (refreshAgain);
  })().finally(() => { refreshing = null; });
  return refreshing;
}
async function loadSettings() {
  try {
    settings.value = await api("/settings");
  } catch (error) {
    statusError.value = error.message;
  }
}
function toggleMode(id) {
  modes.value = modes.value.includes(id)
    ? modes.value.filter((m) => m !== id)
    : [...modes.value, id];
}
function chooseFile(event) {
  file.value = event.target.files?.[0] || null;
}
function dropped(event) {
  dragging.value = false;
  if (event.dataTransfer.files.length) file.value = event.dataTransfer.files[0];
}
function clearTaskForm() {
  file.value = null;
  url.value = "";
  modes.value = [];
  sourceTab.value = "url";
  dragging.value = false;
  if (fileInput.value) fileInput.value.value = "";
  if (!submitting.value) uploadPercent.value = 0;
}
function newTask() {
  clearTaskForm();
  view.value = "new";
}
function openTask(task) {
  selectedId.value = task.id;
  taskDetail.value = task.events ? task : null;
  view.value = "detail";
  refresh(true);
}
function openHistory() {
  view.value = "history";
  refresh(true);
}
function changeHistoryPage(page) {
  historyPage.value = page;
  refresh(true);
}
function reupload(task) {
  newTask();
  sourceTab.value = "upload";
  modes.value = [...task.modes];
}
async function showArtifact(task, artifact) {
  const target = artifact.name === "mindmap.pdf"
    ? window.open("about:blank", "_blank")
    : window.open(`/api/v1/tasks/${task.id}/preview/${encodeURIComponent(artifact.name)}`, "_blank", "noopener");
  if (artifact.name !== "mindmap.pdf") return;
  if (!target) { ElMessage.error("请允许打开预览页面"); return; }
  target.opener = null;
  target.document.title = "正在准备思维导图预览";
  target.document.body.textContent = "正在准备 PDF 预览…";
  try {
    await ensureMindmapPdf(task);
    target.location.replace(fileUrl(task, "mindmap.pdf", true));
  } catch (error) {
    target.close();
    notifyError(error);
  }
}
async function ensureMindmapPdf(task) {
  const artifact = task.artifacts.find(a => a.name === "mindmap.pdf");
  if (!artifact || !artifact.pending) return;
  await api(`/tasks/${task.id}/mindmap/render`, {method: "POST"});
  await refresh();
}
async function downloadArtifact(task, artifact) {
  if (exportingPdf.value) return;
  exportingPdf.value = true;
  try {
    if (artifact.name === "mindmap.pdf") await ensureMindmapPdf(task);
    const a = document.createElement("a");
    a.href = fileUrl(task, artifact.download_name || artifact.name);
    a.download = artifact.download_name || artifact.name;
    a.click();
  } catch (error) {
    notifyError(error);
  } finally {
    exportingPdf.value = false;
  }
}
async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  uploadPercent.value = 0;
  const options = {};
  try {
    let task;
    if (sourceTab.value === "url") {
      task = await api("/tasks/url", {
        method: "POST",
        body: JSON.stringify({
          url: url.value.trim(),
          modes: modes.value,
          options,
        }),
      });
    } else {
      const body = new FormData();
      body.append("file", file.value);
      body.append("modes", JSON.stringify(modes.value));
      body.append("options", JSON.stringify(options));
      task = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/v1/tasks/upload");
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable)
            uploadPercent.value = Math.round((e.loaded / e.total) * 100);
        };
        xhr.onload = () => {
          let result;
          try {
            result = JSON.parse(xhr.responseText);
          } catch {
            reject(new Error("服务返回异常"));
            return;
          }
          if (xhr.status >= 200 && xhr.status < 300) resolve(result);
          else
            reject(
              new Error(
                typeof result.detail === "string" ? result.detail : "上传失败",
              ),
            );
        };
        xhr.onerror = () => reject(new Error("上传连接中断，请重试"));
        xhr.send(body);
      });
    }
    await refresh(true);
    clearTaskForm();
    openTask(task);
    ElMessage.success("任务已提交");
  } catch (error) {
    notifyError(error);
  } finally {
    submitting.value = false;
  }
}
async function action(task, name, useCurrentModel = false) {
  try {
    const options = { method: "POST" };
    if (useCurrentModel) options.body = JSON.stringify({ use_current_model: true });
    await api(`/tasks/${task.id}/${name}`, options);
    await refresh(true);
  } catch (error) {
    notifyError(error);
  }
}
async function remove(task) {
  try {
    await ElMessageBox.confirm(
      "将删除此任务及服务器本地保存的全部文件。",
      "删除任务",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "保留" },
    );
    await api(`/tasks/${task.id}`, { method: "DELETE" });
    if (selectedId.value === task.id) view.value = "history";
    await refresh(true);
  } catch (error) {
    if (error !== "cancel" && error !== "close") notifyError(error);
  }
}
async function renameTask(task) {
  try {
    const { value } = await ElMessageBox.prompt("", "重命名任务", {
      inputValue: task.title,
      inputPlaceholder: "请输入任务名称",
      inputValidator: value => value?.trim() && value.trim().length <= 200 ? true : "请输入 1–200 个字符的任务名称",
      confirmButtonText: "保存",
      cancelButtonText: "取消",
    });
    await api(`/tasks/${task.id}`, {method: "PATCH", body: JSON.stringify({title: value.trim()})});
    await refresh(true);
    ElMessage.success("任务已重命名");
  } catch (error) {
    if (error !== "cancel" && error !== "close") notifyError(error);
  }
}
async function openModels() {
  if (modelBusy.value) return;
  modelBusy.value = true;
  try {
    settings.value = await api("/settings");
    models.value = { ...settings.value, llm_api_key: "", vlm_api_key: "" };
  } catch (error) {
    notifyError(error);
  } finally {
    modelBusy.value = false;
  }
}
async function saveModels(test = false) {
  modelBusy.value = true;
  try {
    settings.value = await api("/settings", {
      method: "PUT",
      body: JSON.stringify(models.value),
    });
    // Once saved, keep only the configured status in the form. The fixed
    // asterisks are the server's mask, never the stored key or a value to submit.
    if (models.value) {
      models.value.llm_api_key = "";
      models.value.vlm_api_key = "";
    }
    if (test) {
      const result = await api("/settings/test", { method: "POST" });
      ElMessage.success(result.message);
    } else {
      models.value = null;
      ElMessage.success("模型配置已保存在本机");
    }
  } catch (error) {
    notifyError(error);
  } finally {
    modelBusy.value = false;
  }
}
let timer;
onMounted(async () => {
  await Promise.all([refresh(), loadSettings()]);
  let idleTicks = 0;
  timer = setInterval(() => {
    if (document.hidden) return;
    idleTicks++;
    if (activeCount.value || idleTicks % 4 === 0) refresh();
  }, 2500);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
  <div class="workspace">
    <aside class="sidebar">
      <a class="brand" href="/" aria-label="Video Assistant 首页" @click.prevent="newTask"
        ><span class="brand-mark"
          ><img src="./assets/logo.png" alt="" /></span
        ><span>Video Assistant</span></a
      >
      <div class="nav-label">工作空间</div>
      <button
        class="nav-item"
        :class="{ active: view === 'new' }"
        @click="newTask"
      >
        <el-icon><Plus /></el-icon>新建任务<span class="key-hint">＋</span>
      </button>
      <button
        class="nav-item"
        :class="{ active: view === 'history' || view === 'detail' }"
        @click="openHistory"
      >
        <el-icon><FolderOpened /></el-icon>全部任务<span class="count">{{
          taskCount
        }}</span>
      </button>
      <div class="nav-divider"></div>
      <div class="nav-label">最近任务</div>
      <div v-if="!tasks.length" class="empty-recent">暂无任务</div>
      <div v-for="task in tasks.slice(0, 6)" :key="task.id" class="recent-task"
        :class="{active: view === 'detail' && selectedId === task.id}">
        <button class="recent-task-open" :title="task.title" @click="openTask(task)">
          <span class="status-dot" :class="task.status"></span><span>{{ task.title }}</span>
        </button>
        <TaskActions :task="task" @rename="renameTask" @remove="remove" />
      </div>
      <div class="sidebar-bottom">
        <button class="nav-item settings-link" @click="openModels">
          <el-icon><Setting /></el-icon>模型设置
        </button>
        <div class="sidebar-version">本地运行</div>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div>
          <span>工作台</span><span class="slash">/</span
          ><strong>{{
            view === "new"
              ? "新建任务"
              : view === "history"
                ? "全部任务"
                : "任务详情"
          }}</strong>
        </div>
        <div class="topbar-right">
          <span class="pill"><i></i>本地保存</span
          ><span class="language-label">简体中文</span>
        </div>
      </header>
      <div v-if="statusError" class="connection-error">
        {{ statusError
        }}<button
          @click="
            refresh();
            loadSettings();
          "
        >
          重新连接
        </button>
      </div>
      <div v-if="historyRecovery.recovered_tasks || historyRecovery.unreadable_tasks" class="history-recovery" role="status">
        <span v-if="historyRecovery.recovered_tasks">已从备份恢复 {{ historyRecovery.recovered_tasks }} 条历史任务。</span>
        <span v-if="historyRecovery.unreadable_tasks">有 {{ historyRecovery.unreadable_tasks }} 个任务目录的记录无法读取，原文件已保留，请检查服务器数据目录和启动日志。</span>
      </div>

      <div v-if="view === 'new'" class="page new-page">
        <div class="page-heading new-task-heading"><h1>新建处理任务</h1></div>

        <section class="composer">
          <div class="section-heading">
            <div>
              <h2>添加素材</h2>
            </div>
          </div>
          <div class="source-tabs" role="tablist">
            <button
              role="tab"
              :aria-selected="sourceTab === 'url'"
              :class="{ selected: sourceTab === 'url' }"
              @click="sourceTab = 'url'"
            >
              <el-icon><Link /></el-icon>视频链接</button
            ><button
              role="tab"
              :aria-selected="sourceTab === 'upload'"
              :class="{ selected: sourceTab === 'upload' }"
              @click="sourceTab = 'upload'"
            >
              <el-icon><UploadFilled /></el-icon>上传文件
            </button>
          </div>
          <div
            v-if="sourceTab === 'upload'"
            class="dropzone"
            :class="{ dragging, 'has-file': file }"
            @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false"
            @drop.prevent="dropped"
          >
            <input
              ref="fileInput"
              type="file"
              accept="video/*,audio/*,.mkv,.flac,.m4a"
              class="file-input"
              aria-label="选择音视频文件"
              @change="chooseFile"
            />
            <div class="upload-symbol">
              <el-icon><VideoCamera /></el-icon>
            </div>
            <div v-if="file" class="chosen-file">
              <strong>{{ file.name }}</strong
              ><span>{{ bytes(file.size) }}</span>
            </div>
            <template v-else
              ><strong
                >拖放音视频文件，或<span
                  class="text-link"
                  @click="fileInput.click()"
                  >选择文件</span
                ></strong
              >
              <p>
                支持 MP4、MOV、MKV、WebM，以及 MP3、M4A、WAV 等音频
              </p></template
            >
            <button v-if="file" class="text-button" @click="fileInput.click()">
              更换文件</button
            ><button v-else class="secondary small" @click="fileInput.click()">
              浏览文件
            </button>
          </div>
          <div v-else class="url-source">
            <div class="upload-symbol"><el-icon><Link /></el-icon></div>
            <label for="video-url">视频链接</label>
            <div class="url-entry"><input
              id="video-url"
              v-model="url"
              type="url"
              placeholder="请输入视频链接"
              aria-label="视频链接"
            /></div>
          </div>

          <div class="section-heading output-heading">
            <div>
              <h2>选择输出</h2>
            </div>
            <span class="muted">可多选</span>
          </div>
          <div class="feature-grid">
            <button
              v-for="feature in features"
              :key="feature.id"
              class="feature-card"
              :class="{ chosen: modes.includes(feature.id) }"
              role="checkbox"
              :aria-checked="modes.includes(feature.id)"
              :aria-labelledby="`feature-name-${feature.id}`"
              :aria-describedby="`feature-description-${feature.id}${feature.note ? ` feature-note-${feature.id}` : ''}`"
              @click="toggleMode(feature.id)"
            >
              <span class="feature-title">
                <span class="feature-icon" aria-hidden="true"><el-icon><component :is="feature.icon" /></el-icon></span>
                <strong :id="`feature-name-${feature.id}`">{{ feature.name }}</strong>
              </span>
              <span class="feature-details">
                <span class="feature-description" :id="`feature-description-${feature.id}`" :title="feature.description">{{ feature.description }}</span>
                <span v-if="feature.note" class="feature-note" :id="`feature-note-${feature.id}`">{{ feature.note }} <span class="duration-factor">{{ feature.duration }}</span>视频时长</span>
              </span>
            </button>
          </div>
          <p class="timing-note">耗时为参考预估，不含排队；实际随设备、字幕、网速和模型速度变化。</p>
          <div v-if="submitting" class="upload-progress">
            <span
              >正在{{ sourceTab === "upload" ? "上传素材" : "提交任务" }}
              {{ sourceTab === "upload" ? uploadPercent + "%" : "" }}</span
            ><el-progress
              :percentage="uploadPercent"
              :show-text="false"
              color="#409eff"
            />
          </div>
          <footer class="composer-footer">
            <span class="muted">默认生成中文文件</span>
            <button class="primary" :disabled="!canSubmit" @click="submit">
              <el-icon><Plus /></el-icon
              >{{
                submitting
                  ? "提交中…"
                  : modes.length === 1 && modes[0] === "download"
                    ? "开始下载"
                    : "提交任务"
              }}
            </button>
          </footer>
        </section>
        <section v-if="tasks.length" class="recent-section">
          <div class="section-heading">
            <div>
              <h2>最近任务</h2>
              <span class="muted">{{ activeCount }} 个处理中</span>
            </div>
            <button class="text-button" @click="openHistory">
              查看全部 →
            </button>
          </div>
          <div
            v-for="task in tasks.slice(0, 3)"
            :key="task.id"
            class="task-row"

          >
            <button class="task-row-open" @click="openTask(task)">
            <span class="task-icon"
              ><el-icon><Document /></el-icon></span
            ><span class="task-main"
              ><strong>{{ task.title }}</strong
              ><small><OutputModes :ids="task.modes" /></small></span
            ><span class="status-badge" :class="task.status">{{
              statusName(task.status)
            }}</span
            ><span class="muted row-date">{{ date(task.created_at) }}</span
            >
          </button>
            <TaskActions :task="task" @rename="renameTask" @remove="remove" />
          </div>
        </section>
      </div>

      <div v-else-if="view === 'history'" class="page history-page">
        <div class="page-heading">
          <div>
            <h1>
              全部任务<span class="heading-count">{{ taskCount }}</span>
            </h1>
            <p class="muted">
              {{ activeCount }} 个处理中 · {{ completedCount }} 个已完成
            </p>
          </div>
          <button class="primary" @click="newTask">
            <el-icon><Plus /></el-icon>新建任务
          </button>
        </div>
        <div v-if="!taskCount" class="empty-state">
          <el-icon><FolderOpened /></el-icon>
          <h2>暂无任务</h2>
          <p>历史记录保存在运行服务的电脑上；更换设备或项目目录时，需要一起迁移本地数据。</p>
          <button class="primary" @click="newTask">添加素材</button>
        </div>
        <div
          v-for="task in historyTasks"
          :key="task.id"
          class="task-row"

        >
            <button class="task-row-open" @click="openTask(task)">
          <span class="task-icon"
            ><el-icon><VideoCamera /></el-icon></span
          ><span class="task-main"
            ><strong>{{ task.title }}</strong
            ><small
              ><OutputModes :ids="task.modes" /> · {{ date(task.created_at) }}</small
            ></span
          ><span class="status-badge" :class="task.status">{{
            statusName(task.status)
          }}</span
          ><span>{{ task.progress.toFixed(0) }}%</span>
        </button>
            <TaskActions :task="task" @rename="renameTask" @remove="remove" />
          </div>
          <ElPagination v-if="taskCount > pageSize" :total="taskCount" :page-size="pageSize"
            :current-page="historyPage" layout="prev, pager, next" @current-change="changeHistoryPage" />
      </div>

      <div v-else-if="activeTask" class="page detail-page">
        <button class="back-link" @click="openHistory">
          <el-icon><ArrowLeft /></el-icon>全部任务
        </button>
        <div class="page-heading">
          <div>
            <div class="eyebrow"><OutputModes :ids="activeTask.modes" /></div>
            <h1>{{ activeTask.custom_title || activeTask.report_title || activeTask.title }}</h1>
            <p class="muted">
              {{ date(activeTask.created_at)
              }}<span v-if="activeTask.transcript_source">
                · {{ progressText(activeTask.transcript_source) }}</span
              >
            </p>
          </div>
          <span class="status-badge" :class="activeTask.status">{{
            statusName(activeTask.status)
          }}</span>
        </div>
        <section class="progress-card">
          <div>
            <strong>当前阶段：{{ stageProgressText }}</strong
            ><span class="total-progress">总进度：{{ activeTask.progress.toFixed(0) }}%</span>
          </div>
          <el-progress
            :percentage="activeTask.progress"
            :show-text="false"
            color="#409eff"
          />
          <div v-if="activeTask.output_states" class="output-progress-list">
            <div v-for="feature in features.filter(item => activeTask.modes.includes(item.id))" :key="feature.id" class="output-progress-item">
              <div><strong>{{ feature.name }}</strong><span class="status-badge" :class="activeTask.output_states[feature.id]?.status">{{ statusName(activeTask.output_states[feature.id]?.status || 'pending') }}</span><small>产物进度：{{ activeTask.output_states[feature.id]?.progress || 0 }}%</small></div>
              <p>{{ progressText(activeTask.output_states[feature.id]?.message || '等待处理') }}<span v-if="activeTask.output_states[feature.id]?.error"> · {{ activeTask.output_states[feature.id].error }}</span></p>
            </div>
          </div>
          <p v-if="activeTask.error" class="error-text">
            {{ activeTask.error }}
          </p>
          <div v-if="['failed', 'partial', 'cancelled', 'queued', 'running'].includes(activeTask.status)" class="task-controls">
            <button v-if="activeTask.needs_upload && ['failed', 'cancelled'].includes(activeTask.status)"
              class="secondary small" @click="reupload(activeTask)">重新上传</button>
            <button
              v-if="!activeTask.needs_upload && ['failed', 'partial', 'cancelled'].includes(activeTask.status)"
              class="secondary small"
              @click="action(activeTask, 'retry')"
            >
              <el-icon><Refresh /></el-icon>继续处理</button
            ><button v-if="!activeTask.needs_upload && ['failed', 'partial', 'cancelled'].includes(activeTask.status)"
              class="text-button" @click="action(activeTask, 'retry', true)">用当前模型继续</button>
            <button
              v-if="['queued', 'running'].includes(activeTask.status)"
              class="text-button"
              @click="action(activeTask, 'cancel')"
            >
              取消任务</button

            >
          </div>
        </section>
        <div class="detail-grid">
          <section class="results-panel">
            <div class="section-heading">
              <div>
                <h2>生成文件</h2>
                <span class="muted">{{ orderedArtifacts.length }} 项</span>
              </div>
              <span class="local-text">本地保存</span>
            </div>
            <p v-if="!activeTask.artifacts.length" class="muted result-wait">
              等待文件生成
            </p>
            <div
              v-for="artifact in orderedArtifacts"
              :key="artifact.name"
              class="artifact-row"
            >
              <span class="artifact-icon"
                ><el-icon
                  ><component
                    :is="
                      artifact.kind === 'video'
                        ? VideoCamera
                        : artifact.kind === 'image'
                          ? Picture
                          : Document
                    " /></el-icon></span
              ><span class="task-main"
                ><strong>{{ artifact.label }}</strong
                ><small
                  >{{ artifact.name }} · {{ artifact.pending ? '渲染后下载' : bytes(artifact.size) }}</small
                ></span
              ><button
                class="text-button"
                @click="showArtifact(activeTask, artifact)"
              >
                预览</button
              ><button
                class="download-button"
                :disabled="exportingPdf"
                @click="downloadArtifact(activeTask, artifact)"
                :aria-label="'下载' + artifact.label"
                ><el-icon><Download /></el-icon
              ></button>
            </div>
          </section>
          <section class="log-panel">
            <h2>处理记录</h2>
            <ol>
              <li v-for="(event, i) in activeTask.events.slice(-9)" :key="i">
                <span></span>
                <div>
                  {{ progressText(event.message) }}<small>{{ date(event.time) }}</small>
                </div>
              </li>
            </ol>
          </section>
        </div>

      </div>
    </main>

    <div v-if="models" class="modal-backdrop" @click.self="models = null">
      <section
        class="model-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-title"
      >
        <div class="section-heading">
          <h2 id="model-title">模型设置</h2>
          <button
            class="icon-button"
            aria-label="关闭模型设置"
            @click="models = null"
          >
            <el-icon><Close /></el-icon>
          </button>
        </div>
        <p class="muted">密钥留空则保留已有配置。</p>
        <h3>大语言模型</h3>
        <label>接口格式<select v-model="models.llm_api_format" aria-label="语言模型接口格式">
          <option value="openai">OpenAI 兼容（Chat Completions）</option>
          <option value="anthropic">Anthropic（Messages）</option>
        </select></label>
        <label
          >接口地址<input
            v-model="models.llm_base_url"
            aria-label="大语言模型接口地址"
            placeholder="https://…/v1"
        /></label>
        <div class="model-two">
          <label>模型名称<input v-model="models.llm_model" aria-label="大语言模型名称" /></label
          ><label
            >API Key<input
              v-model="models.llm_api_key"
              aria-label="大语言模型 API Key"
              type="password"
              autocomplete="new-password"
              :placeholder="
                settings.llm_key_configured
                  ? settings.llm_key_mask
                  : '请输入 API Key'
              "
          /></label>
        </div>
        <h3>多模态模型</h3>
        <label class="checkbox-label"
          ><input
            v-model="models.use_vlm"
            type="checkbox"
          />启用多模态选图</label
        ><template v-if="models.use_vlm"
          ><label>接口格式<select v-model="models.vlm_api_format" aria-label="多模态模型接口格式">
            <option value="openai">OpenAI 兼容（Chat Completions）</option>
            <option value="anthropic">Anthropic（Messages）</option>
          </select></label><label
            >接口地址<input
              v-model="models.vlm_base_url"
              aria-label="多模态模型接口地址"
              placeholder="https://…/v1"
          /></label>
          <div class="model-two">
            <label
              >模型名称<input
                v-model="models.vlm_model"
                aria-label="多模态模型名称" /></label
            ><label
              >API Key<input
                v-model="models.vlm_api_key"
                aria-label="多模态模型 API Key"
                type="password"
                autocomplete="new-password"
                :placeholder="settings.vlm_key_configured ? settings.vlm_key_mask : '请输入 API Key'"
            /></label></div
        ></template>
        <details class="model-budgets"><summary>模型容量限制</summary>
          <div class="model-two"><label>语言模型上下文（token）<input type="number" min="2048" max="2000000" v-model.number="models.llm_context_tokens" /></label><label>语言模型最大输出（token）<input type="number" min="256" max="131072" v-model.number="models.llm_max_output_tokens" /></label></div>
          <div class="model-two"><label>多模态上下文（token）<input type="number" min="2048" max="2000000" v-model.number="models.vlm_context_tokens" /></label><label>多模态最大输出（token）<input type="number" min="256" max="131072" v-model.number="models.vlm_max_output_tokens" /></label></div>
          <p class="device-note">1M 上下文可填 1000000；最大输出请按具体模型设置，两者并不相同。</p>
        </details>
        <h3>本地语音识别</h3>
        <div class="model-two">
          <label
            >Whisper 模型<select v-model="models.whisper_model">
              <option value="tiny">Tiny</option>
              <option value="base">Base</option>
              <option value="small">Small（默认）</option>
              <option value="medium">Medium</option>
              <option value="large-v3">Large-v3</option>
              <option value="large-v3-turbo">Large-v3 Turbo</option>
            </select></label
          >
          <label
            >计算设备<select v-model="models.whisper_device">
              <option value="auto">自动（优先 GPU）</option>
              <option value="cpu">CPU</option>
              <option value="cuda">GPU（CUDA）</option>
            </select></label
          >
        </div>
        <p
          class="device-note"
          v-if="models.whisper_device !== 'cpu' && !settings.gpu_available"
        >
          未检测到可用 GPU，将自动使用 CPU。
        </p>
        <div class="settings-runtime">
          <span
            >FFmpeg：{{ settings.ffmpeg_available ? "就绪" : "未安装" }}</span
          ><span
            >模型：{{
              settings.whisper_downloaded &&
              models.whisper_model === settings.whisper_model
                ? "已缓存"
                : "首次使用时下载"
            }}</span
          >
        </div>
        <div class="modal-footer">
          <button
            class="secondary"
            :disabled="modelBusy"
            @click="saveModels(true)"
          >
            {{ modelBusy ? "处理中…" : "保存并测试语言模型" }}</button
          ><button
            class="primary"
            :disabled="modelBusy"
            @click="saveModels(false)"
          >
            保存配置
          </button>
        </div>
      </section>
    </div>

  </div>
</template>
