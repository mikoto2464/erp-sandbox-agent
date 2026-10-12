<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import AppModal from './AppModal.vue';
import ParsedTable from './ParsedTable.vue';
import { useWorkspaceContext } from '../composables/context';
import { evidenceIndex, evidenceTables } from '../lib/evidence';
import type { Fact } from '../lib/types';
const props = defineProps<{ open: boolean; ids: string[]; title: string; fact?: Fact | null }>();
defineEmits<{ close: [] }>();
const { documents, snapshot } = useWorkspaceContext();
const sources = computed(() => [...documents.value, ...(snapshot.value?.additional_sources || [])]);
const index = computed(() => evidenceIndex(sources.value)),
  tables = computed(() => evidenceTables(sources.value, props.ids));
const paragraphs = computed(() =>
  props.ids
    .map((id) => index.value.get(id))
    .filter(
      (item) =>
        !item?.table_context ||
        !tables.value.some(
          (table) => table.key === `${item.document_id}:${item.table_context!.block_index}`,
        ),
    ),
);
const content = ref<HTMLElement>();
watch(
  () => props.open,
  async (value) => {
    if (value) {
      await nextTick();
      content.value
        ?.querySelector<HTMLElement>('.evidence-problem')
        ?.scrollIntoView({ block: 'center', inline: 'center' });
    }
  },
);
</script>
<template>
  <AppModal
    :open="open"
    :title="title"
    wide
    @close="$emit('close')"
    ><div ref="content">
      <p class="evidence-legend">
        <span class="problem-dot" />待核对内容<span class="related-dot" />相关表头或对象
      </p>
      <p
        v-if="fact"
        class="notice"
      >
        当前值：{{ fact.value ?? '未提供' }} {{ fact.unit }} ·
        {{
          fact.conversion === 'percent_to_ratio'
            ? '原值 ÷ 100'
            : fact.conversion === 'wan_to_yuan'
              ? '原值 × 10000'
              : '不转换'
        }}
      </p>
      <section
        v-for="table in tables"
        :key="table.key"
        class="evidence-section"
      >
        <small class="muted">{{ table.filename }}</small>
        <h3>{{ table.heading }}</h3>
        <ParsedTable
          :block="table.block"
          :primary="table.primary"
          :related="table.related"
        />
      </section>
      <section
        v-for="(item, index) in paragraphs"
        :key="index"
        class="evidence-section"
      >
        <small class="muted">{{ item?.filename || '证据不存在' }}</small>
        <p class="evidence-paragraph">{{ item?.text || '未找到对应原文。' }}</p>
      </section>
      <p
        v-if="!ids.length"
        class="empty"
      >
        未关联原文证据。
      </p>
    </div></AppModal
  >
</template>
