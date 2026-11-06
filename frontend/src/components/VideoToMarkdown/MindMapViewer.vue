<template>
  <div class="mindmap-wrapper" ref="wrapperRef">
    <div
      id="mindMapContainer"
      ref="containerRef"
      class="mind-map-container"
    ></div>

    <div class="mindmap-actions">
    <el-button size="small" :loading="exporting" @click="$emit('download')">下载 PDF</el-button>
    <el-button size="small" title="放大" @click="mindMapInstance?.view.enlarge()">＋</el-button>
    <el-button size="small" title="缩小" @click="mindMapInstance?.view.narrow()">－</el-button>
    <el-button size="small" @click="adjustView">适应画布</el-button>
    <el-button
      type="primary"
      :icon="FullScreen"
      circle
      size="small"
      title="全屏预览"
      @click="openFullscreen"
    />
    </div>

    <div class="mindmap-tip">
      滚轮缩放，拖动画布移动。
    </div>

    <Teleport to="body">
      <MindMapFullscreen
        v-if="showFullscreen"
        :content="content"
        @close="closeFullscreen"
      />
    </Teleport>
  </div>
</template>

<script setup>
import {
  ref,
  shallowRef,
  onMounted,
  onBeforeUnmount,
  watch,
  nextTick,
  computed,
} from "vue";
import { ElMessage, ElButton } from "element-plus";
import { FullScreen } from "@element-plus/icons-vue";
import { createMap, fitMap, waitForMap } from "../../utils/mindmap.js";
import MindMapFullscreen from "./MindMapFullscreen.vue";

const emit = defineEmits(["ready", "download"]);

const props = defineProps({
  content: { type: String, required: true },
  mapHeight: { type: Number, default: 580 },
  exporting: { type: Boolean, default: false },
});

const mindMapInstance = shallowRef(null);
const showFullscreen = ref(false);

const wrapperRef = ref(null);
const containerRef = ref(null);
const dynamicHeight = ref(0);
const usePropHeight = computed(() => props.mapHeight && props.mapHeight > 0);

const computeHeight = () => {
  if (usePropHeight.value) {
    dynamicHeight.value = props.mapHeight;
    return;
  }
  const wrapper = wrapperRef.value;
  if (wrapper) {
    const rect = wrapper.getBoundingClientRect();
    const bottomGap = 32;
    const target = window.innerHeight - rect.top - bottomGap;
    dynamicHeight.value = Math.max(360, target);
  }
};

const convertToMindMapFormat = (jsonData) => {
  try {
    const data = typeof jsonData === "object" ? jsonData : JSON.parse(jsonData);
    return data.data && (data.data.text || data.data.title)
      ? data
      : {
          data: { text: data.text || data.title || "思维导图" },
          children: data.children || [],
        };
  } catch {
    return { data: { text: "解析失败的思维导图" }, children: [] };
  }
};

const adjustView = () => {
  const map = mindMapInstance.value;
  if (!map) return;
  fitMap(map);
};
let fitTimer;

const initMindMap = async () => {
  try {
    if (!containerRef.value) return;
    computeHeight();
    if (mindMapInstance.value) mindMapInstance.value.destroy();
    await nextTick();
    const container = containerRef.value;
    container.style.width = "100%";
    container.style.height = dynamicHeight.value + "px";
    const mindMapData = convertToMindMapFormat(props.content);
    mindMapInstance.value = createMap(container, mindMapData, {
      height: dynamicHeight.value,
      width: container.clientWidth,
      keypress: false,
      contextMenu: false,
      fit: false,
      scale: 0.9,
      textAutoWrap: true,
      nodeTextEdit: false,
      mousewheelAction: "zoom",
      enableMouseWheel: true,
    });
    await waitForMap(mindMapInstance.value);
    adjustView();
    emit("ready");
  } catch {
    ElMessage.error("思维导图初始化失败");
  }
};

const openFullscreen = () => {
  showFullscreen.value = true;
};

const closeFullscreen = () => {
  showFullscreen.value = false;
};

let resizeRaf = 0;
const handleResize = () => {
  cancelAnimationFrame(resizeRaf);
  resizeRaf = requestAnimationFrame(() => {
    computeHeight();
    if (containerRef.value) {
      containerRef.value.style.height = dynamicHeight.value + "px";
      if (mindMapInstance.value) {
        mindMapInstance.value.resize();
        adjustView();
      }
    }
  });
};

onMounted(() => {
  computeHeight();
  initMindMap();
  window.addEventListener("resize", handleResize);
});

onBeforeUnmount(() => {
  clearTimeout(fitTimer);
  mindMapInstance.value?.destroy();
  window.removeEventListener("resize", handleResize);
  cancelAnimationFrame(resizeRaf);
});

watch(
  () => props.content,
  () => initMindMap(),
);
watch(
  () => props.mapHeight,
  () => initMindMap(),
);
</script>

<style scoped>
.mindmap-wrapper {
  position: relative;
  width: 100%;
}
.mindmap-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin: 0 0 12px;
}
.mindmap-actions .el-button + .el-button { margin-left: 0; }

#mindMapContainer * {
  margin: 0;
  padding: 0;
}

.mind-map-container {
  width: 100%;
  background: #f5f7fa;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  overflow: hidden;
  transition:
    height 0.25s ease,
    background 0.25s ease;
}

.fullscreen-float-btn {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 10;
  box-shadow: none;
}

.mindmap-tip {
  margin-top: 12px;
  font-size: 14px;
  color: #888;
}

.mindmap-wrapper.fullscreen {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  width: 100vw;
  height: 100vh;
  background: #fff;
  z-index: 9999;
}

.mind-map-container.fullscreen {
  height: 100vh !important;
  width: 100vw !important;
  border-radius: 0;
  border: none;
  background: #fff;
}

.fullscreen-toolbar {
  position: fixed;
  top: 20px;
  right: 20px;
  z-index: 10000;
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(255, 255, 255, 0.95);
  padding: 8px 12px;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
}

.toolbar-divider {
  width: 1px;
  height: 24px;
  background: #e5e7eb;
  margin: 0 4px;
}

.mind-map-container.fullscreen ~ .mindmap-tip {
  display: none;
}
</style>
