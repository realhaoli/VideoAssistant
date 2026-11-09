<script setup>
import { computed } from "vue";
import { ElDropdown, ElDropdownMenu, ElDropdownItem, ElIcon } from "element-plus";
import { MoreFilled, Edit, Delete } from "@element-plus/icons-vue";
const props = defineProps({ task: { type: Object, required: true } });
const emit = defineEmits(["rename", "remove"]);
const running = computed(() => ["queued", "running", "cancelling", "uploading"].includes(props.task.status));
</script>

<template>
  <ElDropdown trigger="click" placement="bottom-end" @command="command => emit(command, task)">
    <button class="task-more" :aria-label="task.title + '的更多操作'" title="更多操作" @click.stop>
      <ElIcon><MoreFilled /></ElIcon>
    </button>
    <template #dropdown>
      <ElDropdownMenu>
        <ElDropdownItem command="rename" :icon="Edit">重命名</ElDropdownItem>
        <ElDropdownItem command="remove" :icon="Delete" :disabled="running">删除</ElDropdownItem>
      </ElDropdownMenu>
    </template>
  </ElDropdown>
</template>

<style scoped>
.task-more { width: 28px; height: 30px; padding: 0; border-radius: 4px; display: grid; place-items: center; color: #909399; }
.task-more:hover, .task-more:focus-visible { color: #409eff; background: #ecf5ff; }
.task-more .el-icon { font-size: 18px; }
</style>
