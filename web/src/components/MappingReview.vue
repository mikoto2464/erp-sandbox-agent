<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import AppSelect from './AppSelect.vue';
import type { WorkbookMapping } from '../lib/types';
const props = defineProps<{ mapping: WorkbookMapping }>();
defineEmits<{ edit: [index: number] }>();
const sheet = ref(''),
  filter = ref('pending'),
  page = ref(0);
const rows = computed(() =>
  props.mapping.entries.map((entry, index) => {
    const issues = props.mapping.issues.filter((issue) => issue.target === `entries.${index}`);
    return {
      entry,
      index,
      value: props.mapping.values[index],
      issues,
      pending:
        props.mapping.values[index] == null || issues.some((issue) => issue.severity === 'error'),
    };
  }),
);
const pendingCount = computed(() => rows.value.filter((row) => row.pending).length);
const sheets = computed(() => [...new Set(rows.value.map((row) => row.entry.sheet))]);
const filtered = computed(() =>
  rows.value.filter(
    (row) =>
      (!sheet.value || row.entry.sheet === sheet.value) &&
      (filter.value === 'all' || (filter.value === 'pending' ? row.pending : !row.pending)),
  ),
);
const visible = computed(() => filtered.value.slice(page.value * 20, (page.value + 1) * 20));
watch(
  () => props.mapping.id,
  () => {
    sheet.value = '';
    filter.value = 'pending';
    page.value = 0;
  },
);
watch([sheet, filter], () => {
  page.value = 0;
});
const generalIssues = computed(() =>
  props.mapping.issues.filter((issue) => !/^entries\.\d+$/.test(issue.target)),
);
</script>
<template>
  <div class="mapping-review">
    <div class="mapping-summary">
      <span
        >可填写 <strong>{{ rows.length - pendingCount }}</strong> 项</span
      >
      <span
        >待处理 <strong>{{ pendingCount }}</strong> 项</span
      >
    </div>
    <div class="mapping-filters">
      <AppSelect
        v-model="sheet"
        label="工作表"
        :options="[
          { value: '', label: '全部工作表' },
          ...sheets.map((name) => ({ value: name, label: name })),
        ]"
      />
      <AppSelect
        v-model="filter"
        label="显示条目"
        :options="[
          { value: 'pending', label: '待处理' },
          { value: 'ready', label: '可填写' },
          { value: 'all', label: '全部条目' },
        ]"
      />
    </div>
    <p
      v-for="issue in generalIssues"
      :key="issue.code + issue.message"
      class="notice"
    >
      {{ issue.message }}
    </p>
    <p
      v-if="!filtered.length"
      class="mapping-empty muted"
    >
      {{
        filter === 'pending' ? '当前范围没有待处理项，可预览填写结果后确认。' : '当前范围没有条目。'
      }}
    </p>
    <article
      v-for="row in visible"
      :key="row.index"
      class="mapping-row"
    >
      <div class="mapping-row-heading">
        <div>
          <strong>{{ row.entry.label }}</strong
          ><small>{{ row.entry.sheet }} · {{ row.entry.cell }}</small>
        </div>
        <span
          class="mapping-value"
          :class="{ muted: row.value == null }"
          >{{ row.value ?? '留空' }}</span
        >
        <button
          class="review-action"
          @click="$emit('edit', row.index)"
        >
          查看 / 修改
        </button>
      </div>
      <p
        v-if="row.pending"
        class="mapping-reason"
      >
        {{
          row.issues.find((issue) => issue.severity === 'error')?.message ||
          row.entry.explanation ||
          '方案尚未确定此项，请返回经营方案补充要求。'
        }}
      </p>
    </article>
    <div
      v-if="filtered.length > 20"
      class="mapping-pagination"
    >
      <button
        class="review-action"
        :disabled="page === 0"
        @click="page--"
      >
        上一页
      </button>
      <span class="muted"
        >{{ page + 1 }} / {{ Math.ceil(filtered.length / 20) }} 页 · {{ filtered.length }} 项</span
      >
      <button
        class="review-action"
        :disabled="(page + 1) * 20 >= filtered.length"
        @click="page++"
      >
        下一页
      </button>
    </div>
  </div>
</template>
