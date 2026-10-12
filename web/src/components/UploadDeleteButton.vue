<script setup lang="ts">
import { ref } from 'vue';
import AppModal from './AppModal.vue';
import { useWorkspaceContext } from '../composables/context';
const props = defineProps<{ id: string; filename: string; kind: 'document' | 'template' }>();
const { deleteUpload, operations, run } = useWorkspaceContext();
const open = ref(false);
async function remove() {
  await run('delete-upload-' + props.id, async () => {
    await deleteUpload(props.kind, props.id);
    open.value = false;
  });
}
</script>
<template>
  <button
    class="review-action danger-text"
    :aria-label="`删除${filename}`"
    :disabled="operations.has('delete-upload-' + id)"
    @click="open = true"
  >
    删除
  </button>
  <AppModal
    :open="open"
    title="删除文件"
    @close="open = false"
  >
    <p>删除「{{ filename }}」？</p>
    <p class="muted">
      {{
        kind === 'document' ? '删除后需重新读取题目。' : '删除后将从预算空表选择中移除。'
      }}已有题目与方案保留归档依据。
    </p>
    <footer class="dialog-footer">
      <button
        class="secondary"
        @click="open = false"
      >
        取消
      </button>
      <button
        class="primary"
        :disabled="operations.has('delete-upload-' + id)"
        @click="remove"
      >
        {{ operations.has('delete-upload-' + id) ? '正在删除…' : '删除文件' }}
      </button>
    </footer>
  </AppModal>
</template>
