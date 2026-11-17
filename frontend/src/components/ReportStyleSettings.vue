<script setup>
import { computed, nextTick, ref } from "vue";
import { ElMessage } from "element-plus";
import { ArrowLeft, ArrowRight, Plus } from "@element-plus/icons-vue";

const props = defineProps({
  styles: { type: Array, required: true },
  request: { type: Function, required: true },
});
const emit = defineEmits(["saved", "busy"]);
const selected = ref(null);
const page = ref("list");
const nameField = ref(null);
const promptField = ref(null);
const name = ref("");
const prompt = ref("");
const rules = ref(null);
const reviewedPrompt = ref("");
const busy = ref("");
const error = ref("");
const confirmedRules = computed(() => rules.value && prompt.value.trim() === reviewedPrompt.value);
const fields = [
  ["evidence", "取材重点"],
  ["writing", "正文与段落布局"],
  ["introduction", "前文"],
  ["audit", "事实核对与条件规则"],
  ["visuals", "配图偏好"],
];

function openStyle(style) {
  selected.value = style ?? null;
  if (style) page.value = "detail";
  else editStyle();
}

function editStyle() {
  const style = selected.value;
  name.value = style?.name || "";
  prompt.value = style?.prompt || "";
  rules.value = style?.rules || null;
  reviewedPrompt.value = prompt.value.trim();
  error.value = "";
  page.value = "edit";
  nextTick(() => (style ? promptField : nameField).value?.focus());
}

async function run(action, work) {
  busy.value = action;
  error.value = "";
  emit("busy", true);
  try {
    await work();
  } catch (cause) {
    error.value = cause.message;
  } finally {
    busy.value = "";
    emit("busy", false);
  }
}

function parse() {
  const source = prompt.value.trim();
  rules.value = null;
  return run("parse", async () => {
    rules.value = await props.request("/report-styles/parse", {
      method: "POST",
      body: JSON.stringify({ name: name.value, prompt: source }),
    });
    reviewedPrompt.value = source;
  });
}

function save() {
  return run("save", async () => {
    const id = selected.value?.id;
    const saved = await props.request(id ? `/report-styles/${id}` : "/report-styles", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify({ name: name.value, prompt: prompt.value, rules: rules.value, revision: selected.value?.revision }),
    });
    emit("saved", saved);
    selected.value = saved;
    page.value = "detail";
    ElMessage.success("报告风格已保存");
  });
}
</script>

<template>
  <template v-if="page === 'list'">
    <div class="report-style-header">
      <h3>可用的报告风格</h3>
      <button type="button" class="secondary small" @click="openStyle()"><el-icon><Plus /></el-icon> 添加风格</button>
    </div>
    <p v-if="!styles.length" class="muted">还没有报告风格。添加后可在图文汇报和文字汇报中选择。</p>
    <ul v-else class="report-style-list">
      <li v-for="style in styles" :key="style.id">
        <button type="button" @click="openStyle(style)">
          <span>
            <strong>{{ style.name }}</strong>
            <small>{{ style.rules?.order === "topic" ? "按主题编排" : "按时间编排" }}</small>
          </span>
          <el-icon aria-hidden="true"><ArrowRight /></el-icon>
        </button>
      </li>
    </ul>
  </template>
  <template v-else>
    <div class="report-style-header">
      <button type="button" class="text-button" :disabled="Boolean(busy)" @click="page = 'list'">
        <el-icon><ArrowLeft /></el-icon> 返回列表
      </button>
      <button v-if="page === 'detail'" type="button" class="secondary small" @click="editStyle">编辑</button>
      <button v-else-if="selected" type="button" class="text-button" :disabled="Boolean(busy)" @click="page = 'detail'">取消编辑</button>
      <h3 v-else>添加风格</h3>
    </div>
    <section v-if="page === 'detail'" class="report-style-preview">
      <h3>{{ selected.name }}</h3>
      <p class="report-style-prompt">{{ selected.prompt }}</p>
    </section>
    <template v-else>
      <label>风格名称
        <input ref="nameField" v-model="name" maxlength="60" :readonly="Boolean(selected?.built_in)"
          :disabled="Boolean(busy)" placeholder="例如车型发布会简报" />
      </label>
      <label>风格提示词
        <textarea ref="promptField" v-model="prompt" rows="6" maxlength="8000" :disabled="Boolean(busy)"
          placeholder="填写前文、栏目顺序、写作重点、竞品判断条件或配图要求" />
      </label>
      <section v-if="confirmedRules" class="report-style-preview" aria-label="风格解析结果">
        <h3>解析结果</h3>
        <p>{{ rules.order === "topic" ? "按业务主题编排" : "沿原时间轴编排" }}</p>
        <ol v-if="rules.sections.length">
          <li v-for="section in rules.sections" :key="section.title">
            <strong>{{ section.title }}</strong><span v-if="section.focus">　{{ section.focus }}</span>
          </li>
        </ol>
        <p v-if="rules.order === 'topic'">{{ rules.missing === "label" ? "无资料时保留栏目并标记" : "无资料时省略栏目" }}</p>
        <p v-if="rules.annotate_figures"><strong>配图标注</strong><br />对复核通过的局部画面裁切并标注。</p>
        <template v-for="[key, label] in fields" :key="key">
          <p v-if="rules[key]"><strong>{{ label }}</strong><br />{{ rules[key] }}</p>
        </template>
      </section>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <div class="report-style-actions">
        <button type="button" class="secondary" :disabled="Boolean(busy) || !name.trim() || !prompt.trim()" @click="parse">
          {{ busy === "parse" ? "解析中…" : "解析并预览" }}
        </button>
        <button type="button" class="primary" :disabled="Boolean(busy) || !name.trim() || !confirmedRules" @click="save">
          {{ busy === "save" ? "保存中…" : "确认并保存" }}
        </button>
      </div>
    </template>
  </template>
</template>
