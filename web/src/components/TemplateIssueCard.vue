<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { Issue, TemplateIssueReview } from '../lib/types';
const props = defineProps<{
  issue: Issue;
  title: string;
  saved?: TemplateIssueReview;
  busy: boolean;
}>();
const emit = defineEmits<{ confirm: [answer: string]; dirty: [changed: boolean]; preview: [] }>();
const answer = ref(props.saved?.answer || '');
const confirmed = computed(() => !!props.saved && props.saved.answer === answer.value.trim());
watch(confirmed, (value) => emit('dirty', !value && !!props.saved), { immediate: true });
</script>
<template>
  <article
    class="issue-item"
    :class="issue.severity"
  >
    <div class="issue-heading">
      <span
        class="badge"
        :class="confirmed ? 'known' : issue.severity === 'error' ? 'conflicted' : 'assumed'"
        >{{ confirmed ? '已确认' : saved ? '修改待确认' : '需核对' }}</span
      >
      <strong>{{ title }}</strong>
    </div>
    <p>{{ issue.message }}</p>
    <label class="issue-explanation">
      回答这个问题
      <textarea
        v-model="answer"
        rows="3"
        maxlength="5000"
        :disabled="busy"
        :aria-label="`回答：${issue.message}`"
        placeholder="说明这条疑点的正确含义、适用期间或处理口径；不确定时请明确写不清楚。"
      />
    </label>
    <div class="actions review-actions">
      <button
        class="review-action"
        :disabled="busy || !answer.trim() || confirmed"
        @click="emit('confirm', answer.trim())"
      >
        {{ confirmed ? '已确认' : '确认处理' }}
      </button>
      <button
        class="review-action"
        @click="emit('preview')"
      >
        查看原表
      </button>
    </div>
  </article>
</template>
