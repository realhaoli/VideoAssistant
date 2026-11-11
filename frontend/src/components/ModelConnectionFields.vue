<script setup>
defineProps({
  prefix: { type: String, required: true },
  name: { type: String, required: true },
  settings: { type: Object, required: true },
  busy: Boolean,
});

const fields = defineModel({ type: Object, required: true });
defineEmits(["edit"]);
</script>

<template>
  <fieldset class="model-connection" :disabled="busy" @input="$emit('edit')">
    <label>接口格式
      <select v-model="fields[prefix + '_api_format']" :aria-label="name + '接口格式'">
        <option value="openai">OpenAI 风格</option>
        <option value="anthropic">Anthropic 风格</option>
      </select>
    </label>
    <label>接口地址
      <input v-model="fields[prefix + '_base_url']" :aria-label="name + '接口地址'" placeholder="https://…/v1" />
    </label>
    <div class="model-two">
      <label>模型名称
        <input v-model="fields[prefix + '_model']" :aria-label="name + '名称'" />
      </label>
      <label>API Key
        <input
          v-model="fields[prefix + '_api_key']"
          :aria-label="name + ' API Key'"
          type="password"
          autocomplete="new-password"
          :placeholder="settings[prefix + '_key_mask'] || '请输入 API Key'"
        />
      </label>
    </div>
  </fieldset>
</template>
