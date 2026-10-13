<script setup lang="ts">
import { computed, ref } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import {
  annotateIssue,
  issueAdvice,
  issueAnswer,
  issueFact,
  ratePeriodIssue,
  resolveIssue,
  setIssueAnswer,
  TERM_TOTAL_ANSWER,
  UNKNOWN_RATE_ANSWER,
} from '../lib/review';
import { fieldNames } from '../lib/labels';
import type { Fact, Issue } from '../lib/types';
const props = defineProps<{ issue: Issue }>();
const emit = defineEmits<{
  edit: [fact: Fact];
  evidence: [ids: string[], title: string, fact?: Fact];
  rule: [];
}>();
const { editable, markReadingDirty, notify } = useWorkspaceContext();
const fact = computed(() => (editable.value ? issueFact(editable.value, props.issue) : undefined));
const rule = computed(() => editable.value?.rules.find((rule) => rule.id === props.issue.target));
const title = computed(() =>
  fact.value
    ? `${fact.value.entity_name} · ${fieldNames[fact.value.key] || fact.value.key}`
    : rule.value?.name || (props.issue.target === 'horizon' ? '经营周期' : '题目提示'),
);
const answerInput = ref<HTMLTextAreaElement>();
const rateIssue = computed(() => ratePeriodIssue(props.issue, fact.value));
const extractionIssue = computed(() =>
  ['reading.invalid_number', 'reading.extraction_unresolved', 'reading.review_unresolved'].includes(
    props.issue.code,
  ),
);
const answer = computed({
  get: () => (editable.value ? issueAnswer(editable.value, props.issue) : ''),
  set: (value) => {
    if (!editable.value) return;
    setIssueAnswer(editable.value, props.issue, value);
    markReadingDirty();
  },
});
const unitIssue = computed(
  () =>
    !rateIssue.value &&
    !extractionIssue.value &&
    /UNIT|unit|单位/.test(props.issue.code + props.issue.message),
);
const canAnnotate = computed(
  () =>
    (props.issue.severity !== 'error' || props.issue.code === 'reading.invalid_number') &&
    (unitIssue.value || fact.value?.value === null),
);
function resolve() {
  if ((rateIssue.value || extractionIssue.value) && !answer.value.trim()) {
    notify(
      rateIssue.value
        ? '请选择计息口径或填写对这个问题的回答。'
        : '请说明正确内容或明确标记不清楚。',
    );
    answerInput.value?.focus();
    return;
  }
  resolveIssue(editable.value!, props.issue, answer.value.trim() || '默认');
  markReadingDirty();
  notify('处理说明已记录，保存后生效。');
}
function annotate(choice: string) {
  annotateIssue(editable.value!, props.issue, choice, unitIssue.value, answer.value);
  markReadingDirty();
  notify(`已标注“${choice}”，保存后生效。`);
}
</script>
<template>
  <article
    class="issue-item"
    :class="issue.severity"
  >
    <div class="issue-heading">
      <span
        class="badge"
        :class="issue.severity === 'error' ? 'conflicted' : 'assumed'"
        >{{ issue.severity === 'error' ? '阻止生成' : '需核对' }}</span
      ><strong>{{ title }}</strong>
    </div>
    <p
      v-if="fact"
      class="issue-value"
    >
      当前{{ fact.value_type === 'text' ? '文字' : '数值' }}：{{ fact.value ?? '未提供'
      }}{{ fact.unit ? ' · ' + fact.unit : ' · 无单位' }}
    </p>
    <p>{{ issue.message }}</p>
    <p class="muted">{{ issueAdvice(issue, fact) }}</p>
    <details
      v-if="fact?.explanation"
      class="issue-extraction"
    >
      <summary>已有提取说明</summary>
      <p>{{ fact.explanation }}</p>
    </details>
    <label class="issue-explanation"
      >{{
        rateIssue
          ? '回答这个问题 · 计息口径'
          : extractionIssue
            ? '回答这个问题'
            : '回答这个问题（选填）'
      }}<textarea
        ref="answerInput"
        v-model="answer"
        rows="3"
        :placeholder="
          rateIssue
            ? '例如：这是对应完整贷款期限 / 收款期的总利率，不按每季重复计息。'
            : extractionIssue
              ? '说明正确参数、类型及适用对象；无法确定请明确填写不清楚。'
              : '填写对上方疑问的回答、依据或暂定口径；留空按默认处理。'
        "
      />
    </label>
    <div
      v-if="rateIssue && !issue.code.startsWith('validation.')"
      class="review-actions issue-answer-options"
    >
      <button
        type="button"
        class="review-action"
        @click="answer = TERM_TOTAL_ANSWER"
      >
        整个期限总利率
      </button>
      <button
        type="button"
        class="review-action"
        @click="answer = UNKNOWN_RATE_ANSWER"
      >
        暂不清楚
      </button>
    </div>
    <div class="actions review-actions">
      <button
        v-if="fact"
        class="review-action"
        @click="emit('edit', fact)"
      >
        确认 / 修改</button
      ><button
        v-else-if="rule"
        class="review-action"
        @click="emit('rule')"
      >
        修正规则</button
      ><button
        v-if="issue.evidence_ids.length"
        class="review-action"
        @click="emit('evidence', issue.evidence_ids, title, fact)"
      >
        查看原文</button
      ><button
        v-if="!issue.code.startsWith('validation.')"
        class="review-action"
        @click="resolve"
      >
        确认处理</button
      ><template v-if="canAnnotate"
        ><button
          v-for="choice in ['无', '不清楚']"
          :key="choice"
          class="review-action"
          @click="annotate(choice)"
        >
          {{ choice }}
        </button></template
      >
    </div>
  </article>
</template>
