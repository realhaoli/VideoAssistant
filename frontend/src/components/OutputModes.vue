<script setup>
import { computed } from "vue";
import { ElIcon } from "element-plus";
import { features } from "../features.js";
const props = defineProps({ ids: { type: Array, default: () => [] } });
const modes = computed(() => props.ids.map(id => features.find(f => f.id === id)).filter(Boolean));
</script>

<template>
  <span class="output-modes">
    <span v-for="mode in modes" :key="mode.id" class="output-mode">
      <ElIcon aria-hidden="true"><component :is="mode.icon" /></ElIcon>
      <span>{{ mode.name }}</span>
    </span>
  </span>
</template>

<style scoped>
.output-modes { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
.output-mode { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.output-mode .el-icon { font-size: 15px; }
</style>
