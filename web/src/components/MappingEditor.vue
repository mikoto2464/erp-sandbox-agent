<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import AppModal from './AppModal.vue';
import AppSelect from './AppSelect.vue';
import { cloneJson } from '../lib/json';
import { useWorkspaceContext } from '../composables/context';
import type { WorkbookEntry, WorkbookMapping } from '../lib/types';
const props = defineProps<{ mapping: WorkbookMapping | null; initialIndex?: number }>();
const emit = defineEmits<{ close: [] }>();
const { confirmMapping } = useWorkspaceContext();
const entries = ref<WorkbookEntry[]>([]),
  selected = ref(0),
  search = ref(''),
  error = ref(''),
  busy = ref(false);
watch(
  () => props.mapping,
  (value) => {
    entries.value = value ? cloneJson(value.entries) : [];
    selected.value = Math.min(props.initialIndex || 0, Math.max(0, entries.value.length - 1));
    error.value = '';
    search.value = '';
  },
  { immediate: true },
);
const entry = computed(() => entries.value[selected.value]);
const sources = computed(
  () =>
    props.mapping?.sources.filter((source) =>
      `${source.label} ${source.value ?? ''} ${source.unit}`
        .toLowerCase()
        .includes(search.value.toLowerCase()),
    ) || [],
);
function toggle(id: string) {
  if (!entry.value) return;
  const ids = entry.value.source_ids;
  entry.value.source_ids = ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id];
}
async function save() {
  if (!props.mapping) return;
  busy.value = true;
  error.value = '';
  try {
    await confirmMapping(props.mapping, entries.value);
    emit('close');
  } catch (value) {
    error.value = value instanceof Error ? value.message : '保存失败。';
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <AppModal
    :open="!!mapping"
    title="核对预算表条目"
    wide
    @close="$emit('close')"
  >
    <template v-if="mapping">
      <p class="muted">
        核对已规划条目的来源与说明。预算值或条目需要调整时，请返回经营方案生成下一版。
      </p>
      <AppSelect
        :model-value="String(selected)"
        label="预算条目"
        :options="
          entries.map((item, index) => ({
            value: String(index),
            label: `${item.label} · ${item.sheet} ${item.cell}`,
          }))
        "
        @update:model-value="selected = Number($event)"
      />
      <template v-if="entry">
        <div class="form-grid mapping-target">
          <label
            >工作表<input
              :value="entry.sheet"
              readonly /></label
          ><label
            >目标单元格<input
              :value="entry.cell"
              readonly /></label
          ><label
            >预算项目<input
              :value="entry.label"
              readonly /></label
          ><label
            >转换 / 汇总<AppSelect
              v-model="entry.conversion"
              label="转换方式"
              :options="[
                { value: 'identity', label: '直接引用' },
                { value: 'sum', label: '合计所选数值' },
                { value: 'yuan_to_wan', label: '元 → 万元' },
                { value: 'wan_to_yuan', label: '万元 → 元' },
                { value: 'ratio_to_percent', label: '比例 → 百分数数值' },
              ]"
          /></label>
        </div>
        <label
          >选择方案来源<input
            v-model="search"
            type="search"
            placeholder="搜索操作、预算项目、值或单位"
        /></label>
        <div class="mapping-source-list">
          <label
            v-for="source in sources"
            :key="source.id"
            class="evidence-choice"
            ><input
              type="checkbox"
              :checked="entry.source_ids.includes(source.id)"
              @change="toggle(source.id)"
            /><span
              >{{ source.label
              }}<small
                >{{ source.value ?? '未确定' }} {{ source.unit
                }}{{ source.period ? ` · 第 ${source.period} 期` : '' }}</small
              ></span
            ></label
          >
        </div>
        <button
          class="review-action"
          @click="entry.source_ids = []"
        >
          保留为空
        </button>
        <label
          >对应依据 / 未确定原因<textarea
            v-model="entry.explanation"
            rows="2"
          />
        </label>
      </template>
      <p
        v-if="error"
        class="error"
        role="alert"
      >
        {{ error }}
      </p>
      <footer class="dialog-footer">
        <button
          class="secondary"
          @click="$emit('close')"
        >
          取消</button
        ><button
          class="primary"
          :disabled="busy"
          @click="save"
        >
          {{ busy ? '正在保存…' : '保存并确认映射' }}
        </button>
      </footer>
    </template>
  </AppModal>
</template>
