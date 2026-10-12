<script setup lang="ts">
import { computed } from 'vue';
import type { Cell, DocumentBlock } from '../lib/types';
const props = defineProps<{ block: DocumentBlock; primary?: string[]; related?: string[] }>();
const rows = computed(() => {
  const result = new Map<number, Cell[]>();
  for (const cell of props.block.cells) {
    if (!result.has(cell.row)) result.set(cell.row, []);
    result.get(cell.row)!.push(cell);
  }
  return [...result.entries()];
});
</script>
<template>
  <div class="table-wrap">
    <table class="source-table evidence-table">
      <tbody>
        <tr
          v-for="[index, cells] in rows"
          :key="index"
        >
          <td
            v-for="cell in cells"
            :key="cell.evidence_id"
            :colspan="cell.column_span"
            :class="{
              continuation: cell.vertical_merge === 'continue',
              'evidence-problem': primary?.includes(cell.evidence_id),
              'evidence-related':
                !primary?.includes(cell.evidence_id) && related?.includes(cell.evidence_id),
            }"
            :aria-label="primary?.includes(cell.evidence_id) ? `待核对：${cell.text}` : undefined"
          >
            {{ cell.text }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
