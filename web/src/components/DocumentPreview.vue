<script setup lang="ts">
import AppModal from './AppModal.vue';
import ParsedTable from './ParsedTable.vue';
import type { ParsedDocument } from '../lib/types';
defineProps<{ document: ParsedDocument | null }>();
defineEmits<{ close: [] }>();
</script>
<template>
  <AppModal
    :open="!!document"
    :title="document?.source.filename || '解析预览'"
    wide
    @close="$emit('close')"
    ><template v-if="document"
      ><p class="muted">解析预览 · 正文与表格</p>
      <p
        v-for="warning in document.warnings"
        :key="warning"
        class="notice"
      >
        {{ warning }}
      </p>
      <template
        v-for="block in document.blocks"
        :key="block.index"
        ><p
          v-if="block.kind === 'paragraph' && block.text"
          class="preview-paragraph"
        >
          {{ block.text }}
        </p>
        <ParsedTable
          v-if="block.kind === 'table'"
          :block="block"
      /></template>
      <p
        v-if="!document.blocks.length"
        class="empty"
      >
        未提取到可预览的文字或表格。
      </p></template
    ></AppModal
  >
</template>
