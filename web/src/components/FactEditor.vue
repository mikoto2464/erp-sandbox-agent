<script setup lang="ts">
import { computed, ref, toRaw, watch } from 'vue';
import AppModal from './AppModal.vue';
import AppSelect from './AppSelect.vue';
import { useWorkspaceContext } from '../composables/context';
import { evidenceContext, evidenceIndex } from '../lib/evidence';
import { confirmFact, factInputError } from '../lib/review';
import { fieldNames } from '../lib/labels';
import type { Fact } from '../lib/types';
const props = defineProps<{ fact: Fact | null }>();
const emit = defineEmits<{ close: [] }>();
const { documents, snapshot, editable, markReadingDirty, notify } = useWorkspaceContext();
const draft = ref<Fact | null>(null),
  search = ref(''),
  error = ref('');
const index = computed(() =>
  evidenceIndex([...documents.value, ...(snapshot.value?.additional_sources || [])]),
);
const choices = computed(() =>
  [...index.value.values()]
    .filter(
      (item) =>
        draft.value?.evidence_ids.includes(item.id) ||
        (search.value &&
          `${item.text} ${item.location} ${evidenceContext(item, index.value)}`
            .toLowerCase()
            .includes(search.value.toLowerCase())),
    )
    .slice(0, 80),
);
watch(
  () => props.fact,
  (value) => {
    draft.value = value ? structuredClone(toRaw(value)) : null;
    search.value = '';
    error.value = '';
  },
  { immediate: true },
);
function unit(choice: string) {
  if (!draft.value) return;
  draft.value.unit = '';
  draft.value.explanation =
    choice === '无' ? '用户标注：无单位' : '用户标注：单位不清楚，按无单位保存';
}
function save() {
  const fact = draft.value;
  if (!fact || !props.fact || !editable.value) return;
  fact.value = fact.value?.trim() || null;
  error.value = factInputError(fact);
  if (error.value) return;
  if (fact.state === 'known' && (!fact.value || !fact.evidence_ids.length)) {
    error.value = '已知字段必须填写值并选择支持该值的来源。';
    return;
  }
  if (fact.state === 'missing' && fact.value) {
    error.value = '缺失状态应留空，暂定值请选择假设。';
    return;
  }
  fact.explanation = fact.explanation.trim() || '默认';
  Object.assign(props.fact, structuredClone(toRaw(fact)));
  confirmFact(editable.value, props.fact);
  markReadingDirty();
  emit('close');
  notify('字段已确认，保存后生效。');
}
</script>
<template>
  <AppModal
    :open="!!fact"
    :title="fact ? `${fact.entity_name} · ${fieldNames[fact.key] || fact.key}` : '确认数据'"
    wide
    @close="$emit('close')"
    ><form
      v-if="draft"
      @submit.prevent="save"
    >
      <div class="form-grid">
        <label
          >值<input
            v-model="draft.value"
            placeholder="留空表示题目未提供" /></label
        ><label
          >单位<input
            v-model="draft.unit"
            placeholder="留空表示无单位" /></label
        ><label
          >数据类型<AppSelect
            v-model="draft.value_type"
            label="数据类型"
            :options="[
              { value: 'number', label: '数值' },
              { value: 'integer', label: '整数' },
              { value: 'text', label: '文字' },
            ]" /></label
        ><label
          >状态<AppSelect
            v-model="draft.state"
            label="字段状态"
            :options="[
              { value: 'known', label: '已知：有原文依据' },
              { value: 'assumed', label: '假设：暂定口径' },
              { value: 'conflicted', label: '冲突：尚未确定' },
              { value: 'missing', label: '缺失：未提供' },
            ]"
        /></label>
      </div>
      <label
        >单位转换<AppSelect
          v-model="draft.conversion"
          label="单位转换"
          :options="[
            { value: 'identity', label: '不转换' },
            { value: 'percent_to_ratio', label: '百分数转比例（50% → 0.5）' },
            { value: 'wan_to_yuan', label: '万元转元（10 万 → 100000）' },
          ]"
      /></label>
      <p class="muted">值填写转换后的结果，选择转换方式不会再次换算输入值。</p>
      <div class="unit-shortcuts">
        <span class="muted">单位标注</span
        ><button
          type="button"
          class="review-action"
          @click="unit('无')"
        >
          无</button
        ><button
          type="button"
          class="review-action"
          @click="unit('不清楚')"
        >
          不清楚
        </button>
      </div>
      <label
        >说明（选填）<textarea
          v-model="draft.explanation"
          rows="2"
          placeholder="留空按默认处理"
        /></label
      ><label
        >原文依据<input
          v-model="search"
          type="search"
          placeholder="搜索对象、表头或数值"
      /></label>
      <div class="evidence-picker">
        <label
          v-for="item in choices"
          :key="item.id"
          class="evidence-choice"
          ><input
            v-model="draft.evidence_ids"
            type="checkbox"
            :value="item.id"
          /><span
            ><small>{{ item.filename }} · {{ item.location }}</small
            ><small>{{ evidenceContext(item, index) }}</small
            ><span>{{ item.text || '空白单元格' }}</span></span
          ></label
        >
        <p
          v-if="!choices.length"
          class="muted"
        >
          输入关键词查找原文。
        </p>
      </div>
      <p
        v-if="error"
        class="error"
        role="alert"
      >
        {{ error }}
      </p>
      <footer class="dialog-footer">
        <button
          type="button"
          class="secondary"
          @click="$emit('close')"
        >
          取消</button
        ><button class="primary">确认</button>
      </footer>
    </form></AppModal
  >
</template>
