<script setup>
import {
  ref,
  computed,
  onMounted,
  onBeforeUnmount,
} from "vue";
import { ElDialog, ElMessage, ElMessageBox, ElPagination, ElTabs, ElTabPane } from "element-plus";
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
import ModelConnectionFields from "./components/ModelConnectionFields.vue";
import ModelSettingsActions from "./components/ModelSettingsActions.vue";
import OutputModes from "./components/OutputModes.vue";
import TaskActions from "./components/TaskActions.vue";

const view = ref("new"),
  sourceTab = ref("url"),
  modes = ref([]);
const file = ref(null), url = ref("");
const fileInput = ref(null),
  dragging = ref(false),
  submitting = ref(false),
  uploadPercent = ref(null);
const checkingLink = ref(false);
const submitStartedAt = ref(0);
const nowSeconds = ref(Date.now() / 1000);
const linkChoice = ref(null);
const selectedResource = ref(null);
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
const processingEvents = computed(() => (activeTask.value?.events || []).slice().reverse());
const exportingPdf = ref(false);
const artifactOrder = ["illustrated.html", "illustrated.md", "text.html", "text.md", "video.mp4", "mindmap.pdf", "subtitles.zh.txt", "subtitles.en.txt"];
const orderedArtifacts = computed(() => [...(activeTask.value?.artifacts || [])]
  .filter(a => artifactOrder.includes(a.name))
  .sort((a,b) => artifactOrder.indexOf(a.name) - artifactOrder.indexOf(b.name)));
const canSubmit = computed(
  () =>
    modes.value.length &&
    (sourceTab.value === "upload" ? file.value : url.value.trim()) &&
    !submitting.value && !linkChoice.value,
);
const progressText = (message = "") => message.replace(/Whisper\s+(?:large-v3-turbo|large-v3|medium|small|base|tiny)\s+(?=转写|本地转写)/g, "Whisper ");
function elapsedSince(start) {
  const seconds = Math.max(0, Math.floor(nowSeconds.value - start));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  return `${hours ? `${hours}小时` : ""}${hours || minutes ? `${minutes}分` : ""}${seconds % 60}秒`;
}
const submissionProgressText = computed(() => {
  const elapsed = ` · 已运行 ${elapsedSince(submitStartedAt.value)}`;
  if (checkingLink.value) return `正在检查链接中的音视频资源${elapsed}`;
  if (sourceTab.value === "upload") return `正在上传素材${uploadPercent.value === null ? elapsed : ` ${uploadPercent.value}%`}`;
  return `正在提交任务${elapsed}`;
});
function stageLabel(state, fallbackStart) {
  const message = progressText(state?.message || "等待处理");
  const value = state?.stage_progress;
  const known = Number.isFinite(value);
  const label = known ? `${message.replace(/[\s，,]*\d+(?:\.\d+)?%$/, "")}，${Math.round(value)}%` : message;
  const startedAt = state?.stage_started_at || fallbackStart;
  if (!["running", "queued", "uploading", "cancelling"].includes(state?.status) || !Number.isFinite(startedAt)) return label;
  if (known && (value >= 100 || (value > 0 && nowSeconds.value - startedAt < 10))) return label;
  return `${label} · 已${state.status === "queued" ? "等待" : "运行"} ${elapsedSince(startedAt)}`;
}
const stageProgressText = computed(() => {
  const task = activeTask.value;
  if (task?.stage === 'export') return stageLabel(task, task.updated_at);
  const outputs = Object.values(task?.output_states || {});
  const running = outputs.filter(output => output.status === 'running');
  const visible = running.length ? running : outputs.filter(output => output.status === 'queued');
  const distinct = new Map(visible.map(output => [JSON.stringify([output.stage, output.message, output.stage_progress]), output]));
  const stages = [...distinct.values()].map(output => stageLabel(output, task.stage_started_at || task.updated_at));
  if (stages.length > 1) return (running.length ? '并行处理：' : '') + stages.join(' / ');
  return stages[0] || stageLabel(task, task?.events?.at(-1)?.time || task?.created_at);
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
  if (!submitting.value) uploadPercent.value = null;
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
function createLinkTask(payload, resourceId) {
  return api("/tasks/url", {
    method: "POST",
    body: JSON.stringify({ ...payload, resource_id: resourceId }),
  });
}
async function taskSubmitted(task) {
  await refresh(true);
  clearTaskForm();
  openTask(task);
  ElMessage.success("任务已提交");
}
async function confirmResource() {
  if (!linkChoice.value || !selectedResource.value || submitting.value) return;
  submitting.value = true;
  submitStartedAt.value = Date.now() / 1000;
  try {
    const task = await createLinkTask(linkChoice.value.payload, selectedResource.value);
    linkChoice.value = null;
    await taskSubmitted(task);
  } catch (error) {
    notifyError(error);
  } finally {
    submitting.value = false;
  }
}
function resourceDuration(seconds) {
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes} 分 ${total % 60} 秒`;
}
async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  uploadPercent.value = null;
  submitStartedAt.value = Date.now() / 1000;
  const options = {};
  try {
    let task;
    if (sourceTab.value === "url") {
      const payload = { url: url.value.trim(), modes: [...modes.value], options };
      checkingLink.value = true;
      const { resources, truncated } = await api("/tasks/url/inspect", {
        method: "POST",
        body: JSON.stringify({ url: payload.url }),
      });
      checkingLink.value = false;
      submitStartedAt.value = Date.now() / 1000;
      if (view.value !== "new" || url.value.trim() !== payload.url) return;
      if (resources.length > 1 || truncated) {
        selectedResource.value = null;
        linkChoice.value = { payload, resources, truncated };
        return;
      }
      task = await createLinkTask(payload, resources[0].id);
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
    await taskSubmitted(task);
  } catch (error) {
    notifyError(error);
  } finally {
    checkingLink.value = false;
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
    models.value = { ...settings.value, llm_api_key: "", subtitle_api_key: "", vlm_api_key: "" };
    modelChecks.value = {};
    capacityResults.value = {};
    modelTab.value = "llm";
    localRuntime.value = settings.value;
  } catch (error) {
    notifyError(error);
  } finally {
    modelBusy.value = false;
  }
}
const modelTab = ref("llm");
const modelChecks = ref({});
const capacityResults = ref({});
const savingModels = ref(false);
const localRuntime = ref(null);
const connectionFields = ["api_format", "base_url", "model", "api_key"];
const modelRoles = ["llm", "vlm", "subtitle"];

function clearModelCheck(role) {
  delete modelChecks.value[role];
  delete capacityResults.value[role];
  if (role === "llm" && !models.value.use_subtitle_model) delete modelChecks.value.subtitle;
}

function modelValues(role) {
  const current = models.value;
  if (role === "whisper") {
    return { whisper_model: current.whisper_model, whisper_device: current.whisper_device };
  }
  const values = {};
  if (role === "vlm") values.use_vlm = true;
  if (role === "subtitle") {
    values.use_subtitle_model = current.use_subtitle_model;
    values.asr_cleanup_mode = current.asr_cleanup_mode;
    if (!current.use_subtitle_model) return values;
  }
  for (const field of connectionFields) {
    const key = role + "_" + field;
    values[key] = current[key];
  }
  return values;
}

function formatTokens(value) {
  if (!Number.isFinite(value)) return "待检测";
  if (value >= 1_000_000) return Math.round(value / 1_000_000) + " M token";
  if (value >= 1_000) return Math.round(value / 1_000) + " K token";
  return value + " token";
}

function modelCapacity(role) {
  const result = capacityResults.value[role];
  function capacity(field) {
    const key = role + "_" + field;
    const value = result?.capacity[key] ?? settings.value[key];
    const fallback = result && !Object.hasOwn(result.detected, key);
    return formatTokens(value) + (fallback ? "（备用值）" : "");
  }
  return `上下文 ${capacity("context_tokens")} · 最大输出：${capacity("max_output_tokens")}`;
}

async function testModel(role) {
  if (modelBusy.value) return;
  modelBusy.value = true;
  modelChecks.value[role] = { state: "running", message: role === "whisper" ? "正在检测本地环境" : "正在测试连接并读取容量" };
  try {
    if (role === "whisper") {
      const model = models.value.whisper_model;
      const runtime = await api("/settings?whisper_model=" + encodeURIComponent(model));
      localRuntime.value = { ...runtime, whisper_model: model };
      modelChecks.value[role] = runtime.ffmpeg_available
        ? { state: "success", message: "FFmpeg 就绪，" + (runtime.whisper_downloaded ? "模型已缓存" : "模型将在首次使用时下载") }
        : { state: "error", message: "未找到 FFmpeg 或 FFprobe" };
      return;
    }
    const target = role === "subtitle" && !models.value.use_subtitle_model ? "llm" : role;
    const result = await api("/settings/test", {
      method: "POST",
      body: JSON.stringify({ role: target, draft: modelValues(target) }),
    });
    capacityResults.value[target] = result;
    modelChecks.value[role] = { state: "success", message: result.message };
  } catch (error) {
    modelChecks.value[role] = { state: "error", message: error.message || String(error) };
  } finally {
    modelBusy.value = false;
  }
}

async function saveModels() {
  if (modelBusy.value) return;
  modelBusy.value = true;
  savingModels.value = true;
  try {
    const values = Object.assign({}, ...[...modelRoles, "whisper"].map(modelValues));
    for (const role of modelRoles) {
      if (role === "subtitle" && !models.value.use_subtitle_model) continue;
      Object.assign(values, capacityResults.value[role]?.detected);
    }
    const { detected_capacities, ...saved } = await api("/settings?detect_for=all", {
      method: "PUT",
      body: JSON.stringify(values),
    });
    for (const role of modelRoles) {
      if (role === "subtitle" && !models.value.use_subtitle_model) continue;
      if (connectionFields.some(field => field !== "api_key" && models.value[role + "_" + field] !== saved[role + "_" + field])) {
        clearModelCheck(role);
      }
    }
    settings.value = { ...settings.value, ...saved };
    models.value = { ...saved, llm_api_key: "", subtitle_api_key: "", vlm_api_key: "" };
    capacityResults.value = Object.fromEntries(Object.entries(detected_capacities ?? {}).map(
      ([role, detected]) => [role, { capacity: saved, detected }],
    ));
    ElMessage.success("全部配置已保存");
  } catch (error) {
    notifyError(error);
  } finally {
    savingModels.value = false;
    modelBusy.value = false;
  }
}
let timer, elapsedTimer;
onMounted(async () => {
  elapsedTimer = setInterval(() => { nowSeconds.value = Date.now() / 1000; }, 1000);
  await Promise.all([refresh(), loadSettings()]);
  let idleTicks = 0;
  timer = setInterval(() => {
    if (document.hidden) return;
    idleTicks++;
    if (activeCount.value || idleTicks % 4 === 0) refresh();
  }, 2500);
});
onBeforeUnmount(() => {
  clearInterval(timer);
  clearInterval(elapsedTimer);
});
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
      <div class="recent-task-list" role="region" aria-label="最近任务" tabindex="0">
        <div v-if="!tasks.length" class="empty-recent">暂无任务</div>
        <div v-for="task in tasks" :key="task.id" class="recent-task"
          :class="{active: view === 'detail' && selectedId === task.id}">
          <button class="recent-task-open" :title="task.title" @click="openTask(task)">
            <span class="status-dot" :class="task.status"></span><span>{{ task.title }}</span>
          </button>
          <TaskActions :task="task" @rename="renameTask" @remove="remove" />
        </div>
        <button v-if="taskCount > tasks.length" class="nav-item recent-task-more" @click="openHistory">
          查看全部任务
        </button>
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
              :disabled="submitting"
              :class="{ selected: sourceTab === 'url' }"
              @click="sourceTab = 'url'"
            >
              <el-icon><Link /></el-icon>视频链接</button
            ><button
              role="tab"
              :aria-selected="sourceTab === 'upload'"
              :disabled="submitting"
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
              :disabled="submitting"
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
              :disabled="submitting"
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
          <div v-if="submitting" class="upload-progress" role="status">
            <span>{{ submissionProgressText }}</span>
            <el-progress
              v-if="sourceTab === 'upload' && uploadPercent !== null"
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
                checkingLink
                  ? "检查链接中…"
                  : submitting
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
              <p>{{ stageLabel(activeTask.output_states[feature.id], activeTask.stage_started_at || activeTask.updated_at) }}<span v-if="activeTask.output_states[feature.id]?.error"> · {{ activeTask.output_states[feature.id].error }}</span></p>
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
            <h2 id="processing-log-title">处理记录</h2>
            <ol v-if="processingEvents.length" :key="activeTask.id" reversed tabindex="0" aria-labelledby="processing-log-title">
              <li v-for="(event, i) in processingEvents" :key="processingEvents.length - i">
                <span></span>
                <div>
                  {{ progressText(event.message) }}<small>{{ date(event.time) }}</small>
                </div>
              </li>
            </ol>
            <p v-else class="muted result-wait">暂无处理记录</p>
          </section>
        </div>

      </div>
    </main>

    <ElDialog
      :model-value="Boolean(linkChoice)"
      class="link-resource-dialog"
      title="选择音视频资源"
      width="min(520px, calc(100vw - 32px))"
      align-center
      :show-close="!submitting"
      :close-on-press-escape="!submitting"
      :close-on-click-modal="false"
      @update:model-value="(open) => { if (!open) linkChoice = null }"
    >
      <p v-if="linkChoice?.truncated">此链接包含较多条目，以下为前 30 个条目中识别到的资源。也可以返回并粘贴单个视频链接。</p>
      <fieldset class="link-resources">
        <legend>请选择一个要处理的资源</legend>
        <label v-for="(resource, index) in linkChoice?.resources" :key="resource.id"
          :class="{ selected: selectedResource === resource.id }">
          <input v-model="selectedResource" type="radio" name="link-resource" :value="resource.id" :disabled="submitting" />
          <span>{{ index + 1 }}. {{ resource.title }}<small v-if="resource.duration">{{ resourceDuration(resource.duration) }}</small></span>
        </label>
      </fieldset>
      <template #footer>
        <button class="secondary" :disabled="submitting" @click="linkChoice = null">取消</button>
        <button class="primary" :disabled="!selectedResource || submitting" @click="confirmResource">
          {{ submitting ? "提交中…" : "确认并提交" }}
        </button>
      </template>
    </ElDialog>
    <div v-if="models" class="modal-backdrop" @click.self="!modelBusy && (models = null)">
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
            :disabled="modelBusy"
            @click="models = null"
          >
            <el-icon><Close /></el-icon>
          </button>
        </div>
        <ElTabs v-model="modelTab" stretch class="model-tabs">
          <ElTabPane label="大语言模型" name="llm" lazy>
            <p class="muted">用于章节规划、正文整理、核对和汇报摘要。</p>
            <ModelConnectionFields
              v-model="models" :settings="settings" :busy="modelBusy"
              prefix="llm" name="大语言模型" @edit="clearModelCheck('llm')"
            />
            <p class="model-capacity">当前模型配置：{{ modelCapacity('llm') }}<span>（点击“连接测试”更新最大容量）</span></p>
          </ElTabPane>
          <ElTabPane label="多模态模型" name="vlm" lazy>
            <p class="muted">用于结合报告正文评选视频画面，为图文报告配图。</p>
            <ModelConnectionFields
              v-model="models" :settings="settings" :busy="modelBusy"
              prefix="vlm" name="多模态模型" @edit="clearModelCheck('vlm')"
            />
            <p class="model-capacity">当前模型配置：{{ modelCapacity('vlm') }}<span>（点击“连接测试”更新最大容量）</span></p>
          </ElTabPane>
          <ElTabPane label="字幕模型" name="subtitle" lazy>
            <p class="muted">字幕清洗与翻译为简单任务，可选用更快的模型处理。</p>
            <fieldset class="model-connection" :disabled="modelBusy">
              <legend>模型配置</legend>
              <div class="model-radio-options">
                <label class="checkbox-label">
                  <input v-model="models.use_subtitle_model" type="radio" name="subtitle-profile" :value="false" @change="clearModelCheck('subtitle')" />继承大语言模型
                </label>
                <label class="checkbox-label">
                  <input v-model="models.use_subtitle_model" type="radio" name="subtitle-profile" :value="true" @change="clearModelCheck('subtitle')" />独立配置模型
                </label>
              </div>
            </fieldset>
            <p v-if="!models.use_subtitle_model" class="model-capacity">
              {{ models.llm_model ? "当前继承模型 " + models.llm_model : "当前继承大语言模型" }}，{{ modelCapacity('llm') }}
            </p>
            <template v-else>
              <ModelConnectionFields
                v-model="models" :settings="settings" :busy="modelBusy"
                prefix="subtitle" name="字幕模型" @edit="clearModelCheck('subtitle')"
              />
              <p class="model-capacity">当前模型配置：{{ modelCapacity('subtitle') }}<span>（点击“连接测试”更新最大容量）</span></p>
            </template>
            <fieldset class="model-connection subtitle-cleanup" :disabled="modelBusy">
              <legend>如何清洗字幕</legend>
              <div class="model-radio-options">
                <label class="checkbox-label">
                  <input v-model="models.asr_cleanup_mode" type="radio" name="subtitle-cleanup" value="local" />使用本地规则（速度更快但准确性可能不足）
                </label>
                <label class="checkbox-label">
                  <input v-model="models.asr_cleanup_mode" type="radio" name="subtitle-cleanup" value="model" />大模型清洗（准确性更高但速度更慢）
                </label>
              </div>
            </fieldset>
          </ElTabPane>
          <ElTabPane label="本地语音识别" name="whisper" lazy>
            <p class="muted">用于在缺少可用字幕时，将音视频中的语音转写为字幕。</p>
            <div class="model-two">
              <label>Whisper 模型
                <select v-model="models.whisper_model" :disabled="modelBusy" @change="clearModelCheck('whisper')">
                  <option value="tiny">Tiny（参数量：39M · 权重：75.5 MB）</option>
                  <option value="base">Base（参数量：74M · 权重：145 MB）</option>
                  <option value="small">Small（参数量：244M · 权重：484 MB）</option>
                  <option value="medium">Medium（参数量：769M · 权重：1.53 GB）</option>
                  <option value="large-v3">Large-v3（参数量：1550M · 权重：3.09 GB）</option>
                  <option value="large-v3-turbo">Large-v3 Turbo（参数量：809M · 权重：1.62 GB）</option>
                </select>
              </label>
              <label>计算设备
                <select v-model="models.whisper_device" :disabled="modelBusy" @change="clearModelCheck('whisper')">
                  <option value="auto">自动</option>
                  <option value="cuda">GPU</option>
                  <option value="cpu">CPU</option>
                </select>
              </label>
            </div>
            <p v-if="models.whisper_device !== 'cpu' && !localRuntime.gpu_available" class="device-note">
              未检测到可用 GPU，将自动使用 CPU。
            </p>
            <div class="settings-runtime">
              <span>FFmpeg：{{ localRuntime.ffmpeg_available ? "就绪" : "未安装" }}</span>
              <span>模型：{{
                models.whisper_model !== localRuntime.whisper_model ? "待检测" :
                  localRuntime.whisper_downloaded ? "已缓存" : "首次使用时下载"
              }}</span>
            </div>
          </ElTabPane>
        </ElTabs>
        <p class="model-shared-note">当前配置为所有用户共用，谨慎修改</p>
        <ModelSettingsActions
          :busy="modelBusy"
          :saving="savingModels"
          :result="modelChecks[modelTab]"
          :test-label="modelTab === 'whisper' ? '环境检测' : '连接测试'"
          @test="testModel(modelTab)"
          @save="saveModels"
        />
      </section>
    </div>

  </div>
</template>
