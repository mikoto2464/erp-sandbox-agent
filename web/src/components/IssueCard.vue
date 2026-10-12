<script setup lang="ts">
import { computed } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import { annotateIssue, issueAdvice, issueFact, resolveIssue } from '../lib/review';
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
const explanation = computed({
  get: () =>
    fact.value?.explanation ||
    editable.value?.issues.find(
      (issue) =>
        issue.code === 'review.note.' + props.issue.code && issue.target === props.issue.target,
    )?.message ||
    '',
  set: (value) => {
    if (!editable.value) return;
    if (fact.value) fact.value.explanation = value;
    else {
      editable.value.issues = editable.value.issues.filter(
        (issue) =>
          !(
            issue.code === 'review.note.' + props.issue.code && issue.target === props.issue.target
          ),
      );
      if (value.trim())
        editable.value.issues.push({
          code: 'review.note.' + props.issue.code,
          severity: 'info',
          target: props.issue.target,
          message: value,
          evidence_ids: props.issue.evidence_ids,
        });
    }
    markReadingDirty();
  },
});
const unitIssue = computed(() => /UNIT|unit|单位/.test(props.issue.code + props.issue.message));
const canAnnotate = computed(
  () => props.issue.severity !== 'error' && (unitIssue.value || fact.value?.value === null),
);
function resolve() {
  resolveIssue(editable.value!, props.issue, explanation.value.trim() || '默认');
  markReadingDirty();
  notify('处理说明已记录，保存后生效。');
}
function annotate(choice: string) {
  annotateIssue(editable.value!, props.issue, choice, unitIssue.value, explanation.value);
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
    <label class="issue-explanation"
      >处理说明（选填）<textarea
        v-model="explanation"
        rows="2"
        placeholder="填写理解或暂定口径；留空为默认"
      />
    </label>
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
