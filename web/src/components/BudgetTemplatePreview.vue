<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import AppModal from './AppModal.vue';
import AppSelect from './AppSelect.vue';
import type { BudgetTemplate } from '../lib/types';
const props = defineProps<{ template: BudgetTemplate | null }>();
defineEmits<{ close: [] }>();
const sheetName = ref('');
watch(
  () => props.template,
  (value) => {
    sheetName.value = value?.sheets[0]?.name || '';
  },
  { immediate: true },
);
const sheet = computed(() => props.template?.sheets.find((item) => item.name === sheetName.value));
const cells = computed(
  () => new Map(sheet.value?.cells.map((cell) => [cell.coordinate, cell]) || []),
);
const merges = computed(() => {
  const anchors = new Map<string, { rows: number; columns: number }>();
  const covered = new Set<string>();
  for (const range of sheet.value?.merged_ranges || []) {
    const [start, end = start] = range.split(':');
    function position(address: string) {
      const match = address.match(/^([A-Z]+)(\d+)$/);
      if (!match) return null;
      let column = 0;
      for (const char of match[1]) column = column * 26 + char.charCodeAt(0) - 64;
      return { row: Number(match[2]), column };
    }
    const first = position(start),
      last = position(end);
    if (!first || !last) continue;
    anchors.set(start, {
      rows: Math.min(last.row, 150) - first.row + 1,
      columns: Math.min(last.column, 40) - first.column + 1,
    });
    for (let row = first.row; row <= Math.min(last.row, 150); row++)
      for (let column = first.column; column <= Math.min(last.column, 40); column++) {
        const address = `${columnName(column)}${row}`;
        if (address !== start) covered.add(address);
      }
  }
  return { anchors, covered };
});
function columnName(index: number): string {
  let name = '';
  while (index > 0) {
    index--;
    name = String.fromCharCode(65 + (index % 26)) + name;
    index = Math.floor(index / 26);
  }
  return name;
}
</script>
<template>
  <AppModal
    :open="!!template"
    :title="template?.source.filename || '预算表预览'"
    wide
    @close="$emit('close')"
  >
    <template v-if="template">
      <AppSelect
        v-model="sheetName"
        label="工作表"
        :options="template.sheets.map((item) => ({ value: item.name, label: item.name }))"
      />
      <p
        v-for="warning in template.warnings"
        :key="warning"
        class="notice"
      >
        {{ warning }}
      </p>
      <template v-if="sheet">
        <p class="muted">
          {{ sheet.rows }} 行 · {{ sheet.columns }} 列{{ sheet.hidden ? ' · 隐藏' : ''
          }}{{ sheet.protected ? ' · 受保护' : '' }}
        </p>
        <div class="table-scroll budget-preview">
          <table>
            <thead>
              <tr>
                <th></th>
                <th
                  v-for="column in Math.min(sheet.columns, 40)"
                  :key="column"
                >
                  {{ columnName(column) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in Math.min(sheet.rows, 150)"
                :key="row"
              >
                <th>{{ row }}</th>
                <template
                  v-for="column in Math.min(sheet.columns, 40)"
                  :key="column"
                  ><td
                    v-if="!merges.covered.has(`${columnName(column)}${row}`)"
                    :rowspan="merges.anchors.get(`${columnName(column)}${row}`)?.rows || 1"
                    :colspan="merges.anchors.get(`${columnName(column)}${row}`)?.columns || 1"
                    :class="{ 'budget-formula': cells.get(`${columnName(column)}${row}`)?.formula }"
                  >
                    {{ cells.get(`${columnName(column)}${row}`)?.text || '' }}
                  </td></template
                >
              </tr>
            </tbody>
          </table>
        </div>
        <p
          v-if="sheet.rows > 150 || sheet.columns > 40"
          class="muted"
        >
          预览显示前 150 行、40 列；规划使用完整解析结构。
        </p>
        <p
          v-if="sheet.merged_ranges.length"
          class="muted"
        >
          合并区域：{{ sheet.merged_ranges.join('、') }}
        </p>
      </template>
    </template>
  </AppModal>
</template>
