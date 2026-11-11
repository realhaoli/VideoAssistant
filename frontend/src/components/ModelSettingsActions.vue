<script setup>
defineProps({
  busy: Boolean,
  saving: Boolean,
  testLabel: { type: String, default: "连接测试" },
  result: Object,
});
defineEmits(["test", "save"]);
</script>

<template>
  <div class="model-test">
    <div v-if="result" class="model-test-result" :class="result.state" role="status">
      <span v-if="result.state === 'success'" aria-hidden="true">✓ </span>
      <span v-if="result.state === 'error'" aria-hidden="true">✕ </span>
      {{ result.message }}
    </div>
    <div class="model-test-buttons">
      <button type="button" class="secondary" :disabled="busy" @click="$emit('test')">
        {{ testLabel }}
      </button>
      <button type="button" class="primary" :disabled="busy" @click="$emit('save')">
        {{ saving ? "保存中…" : "保存配置" }}
      </button>
    </div>
  </div>
</template>
