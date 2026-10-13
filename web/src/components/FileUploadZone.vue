<script setup lang="ts">
import { ref } from 'vue';
const props = defineProps<{ accept: string; formats: string; disabled?: boolean }>();
const emit = defineEmits<{ files: [files: File[]] }>();
const dragging = ref(false),
  input = ref<HTMLInputElement>();
function choose(files: FileList | null) {
  dragging.value = false;
  if (!props.disabled && files?.length) emit('files', [...files]);
  if (input.value) input.value.value = '';
}
</script>
<template>
  <label
    class="drop-zone"
    :class="{ dragover: dragging && !disabled }"
    @dragover.prevent="dragging = true"
    @dragleave.prevent="dragging = false"
    @drop.prevent="choose($event.dataTransfer?.files || null)"
  >
    <span class="upload-icon">↑</span>
    <strong>{{ disabled ? '正在上传…' : '点击选择，或拖入文件' }}</strong>
    <span class="muted">{{ formats }}</span>
    <input
      ref="input"
      type="file"
      :accept="accept"
      multiple
      :disabled="disabled"
      @change="choose(($event.target as HTMLInputElement).files)"
    />
  </label>
</template>
