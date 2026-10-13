<script setup lang="ts">
import { ref } from 'vue';
import AppModal from './AppModal.vue';
import { request } from '../lib/api';
import { useWorkspaceContext } from '../composables/context';
const { run, importLibraryTemplate, goTemplateLibrary } = useWorkspaceContext();
const open = ref(false),
  items = ref<
    { id: string; source: { filename: string }; reading: { reviewed: boolean } | null }[]
  >([]);
async function show() {
  items.value = await request('/api/template-library');
  open.value = true;
}
</script>
<template>
  <button
    class="review-action"
    @click="run('choose-template', show)"
  >
    导入已处理空表
  </button>
  <AppModal
    :open="open"
    title="导入已处理空表"
    @close="open = false"
  >
    <p class="muted">使用空表库中已确认的解读，无需重新请求模型。</p>
    <p
      v-if="!items.some((i) => i.reading?.reviewed)"
      class="empty"
    >
      暂无已确认空表。
    </p>
    <div
      v-for="item in items.filter((i) => i.reading?.reviewed)"
      :key="item.id"
      class="section-heading input-section"
    >
      <strong>{{ item.source.filename }}</strong
      ><button
        class="review-action"
        @click="
          run('import-template', async () => {
            await importLibraryTemplate(item.id);
            open = false;
          })
        "
      >
        导入
      </button>
    </div>
    <footer class="dialog-footer">
      <button
        class="review-action"
        @click="
          open = false;
          goTemplateLibrary();
        "
      >
        管理空表库
      </button>
    </footer>
  </AppModal>
</template>
