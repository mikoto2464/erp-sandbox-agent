<script setup lang="ts">
import { computed, ref, watch, type CSSProperties } from 'vue';
import AppModal from './AppModal.vue';
import AppSelect from './AppSelect.vue';
import type { BudgetTemplate, WorkbookMapping } from '../lib/types';
const props = defineProps<{ template: BudgetTemplate | null; mapping?: WorkbookMapping | null }>();
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
const mapped = computed(
  () =>
    new Map(
      props.mapping?.entries
        .map((entry, index) => ({ entry, value: props.mapping?.values[index] }))
        .filter(({ entry }) => entry.sheet === sheetName.value)
        .map(({ entry, value }) => [entry.cell, value]) || [],
    ),
);
function displayCell(address: string) {
  const cell = cells.value.get(address);
  if (cell?.formula) return cell.display_text || 'ƒx';
  return cell?.text || (mapped.value.has(address) ? (mapped.value.get(address) ?? '待确定') : '');
}
const columns = computed(() =>
  Array.from({ length: Math.min(sheet.value?.columns || 0, 100) }, (_, i) => i + 1),
);
const rows = computed(() =>
  Array.from({ length: Math.min(sheet.value?.rows || 0, 300) }, (_, i) => i + 1),
);
function width(column: number) {
  return Math.max(36, sheet.value?.column_widths?.[column - 1] || 88);
}
const tableWidth = computed(
  () => 44 + columns.value.reduce((sum, column) => sum + width(column), 0),
);
function cellStyle(address: string): CSSProperties {
  const cell = cells.value.get(address);
  const horizontal = cell?.horizontal;
  const vertical = cell?.vertical;
  return {
    textAlign: (['left', 'center', 'right', 'justify'].includes(horizontal || '')
      ? horizontal
      : 'left') as CSSProperties['textAlign'],
    verticalAlign: (vertical === 'center'
      ? 'middle'
      : vertical === 'top'
        ? 'top'
        : 'bottom') as CSSProperties['verticalAlign'],
  };
}
function cellTitle(address: string) {
  const cell = cells.value.get(address);
  return cell?.formula
    ? `${address} · 原公式（不填写；展示值为文件保存时的结果）\n${cell.text}`
    : `${address}${cell?.text ? ' · ' + cell.text : ''}`;
}
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
      rows: Math.min(last.row, 300) - first.row + 1,
      columns: Math.min(last.column, 100) - first.column + 1,
    });
    for (let row = first.row; row <= Math.min(last.row, 300); row++)
      for (let column = first.column; column <= Math.min(last.column, 100); column++) {
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
        <p
          v-if="mapping"
          class="muted"
        >
          蓝色为拟填写数据，橙色为待确定输入；原公式与已有数据保持原样。
        </p>
        <p class="muted">
          {{ sheet.rows }} 行 · {{ sheet.columns }} 列{{ sheet.hidden ? ' · 隐藏' : ''
          }}{{ sheet.protected ? ' · 受保护' : '' }}
        </p>
        <p class="muted">ƒx 为原公式；浅蓝为空白可写候选。只填写需要的输入，已有数据保持不变。</p>
        <div
          class="table-scroll budget-preview"
          role="region"
          aria-label="表格预览，可横向滚动"
          tabindex="0"
        >
          <table :style="{ width: tableWidth + 'px' }">
            <colgroup>
              <col :style="{ width: '44px' }" />
              <col
                v-for="column in columns"
                :key="column"
                :style="{ width: width(column) + 'px' }"
              />
            </colgroup>
            <thead>
              <tr>
                <th></th>
                <th
                  v-for="column in columns"
                  :key="column"
                >
                  {{ columnName(column) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row"
                :style="{ height: (sheet.row_heights?.[row - 1] || 28) + 'px' }"
              >
                <th>{{ row }}</th>
                <template
                  v-for="column in columns"
                  :key="column"
                  ><td
                    v-if="!merges.covered.has(`${columnName(column)}${row}`)"
                    :rowspan="merges.anchors.get(`${columnName(column)}${row}`)?.rows || 1"
                    :colspan="merges.anchors.get(`${columnName(column)}${row}`)?.columns || 1"
                    :style="cellStyle(`${columnName(column)}${row}`)"
                    :title="cellTitle(`${columnName(column)}${row}`)"
                    :class="{
                      'budget-input':
                        !sheet.hidden &&
                        cells.get(`${columnName(column)}${row}`)?.writable &&
                        !cells.get(`${columnName(column)}${row}`)?.text,
                      'budget-formula': cells.get(`${columnName(column)}${row}`)?.formula,
                      'budget-filled':
                        mapped.has(`${columnName(column)}${row}`) &&
                        mapped.get(`${columnName(column)}${row}`) != null,
                      'budget-unresolved':
                        mapped.has(`${columnName(column)}${row}`) &&
                        mapped.get(`${columnName(column)}${row}`) == null,
                    }"
                  >
                    {{ displayCell(`${columnName(column)}${row}`) }}
                  </td></template
                >
              </tr>
            </tbody>
          </table>
        </div>
        <p
          v-if="sheet.rows > 300 || sheet.columns > 100"
          class="muted"
        >
          预览显示前 300 行、100 列；规划使用完整解析结构。
        </p>
      </template>
    </template>
  </AppModal>
</template>
