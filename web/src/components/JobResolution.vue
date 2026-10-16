<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import AppModal from './AppModal.vue';
import { useWorkspaceContext } from '../composables/context';

const { allJobs, resolutionJobId, answerJob, authenticated } = useWorkspaceContext();
const job = computed(() =>
  allJobs.value.find((j) => j.id === resolutionJobId.value && j.intervention),
);
const question = computed(() => job.value?.intervention);
const selected = ref(''),
  text = ref(''),
  error = ref(''),
  submitting = ref(false);
const seen = new Set<string>();
watch(
  [allJobs, authenticated],
  () => {
    if (!authenticated.value) {
      resolutionJobId.value = null;
      seen.clear();
      return;
    }
    if (job.value) return;
    const next = allJobs.value.find((j) => j.intervention && !seen.has(j.intervention.id));
    resolutionJobId.value = next?.id || null;
  },
  { immediate: true },
);
watch(
  question,
  (q, previous) => {
    if (!q || q.id === previous?.id) return;
    seen.add(q.id);
    selected.value = q.options[0]?.id || '';
    text.value = '';
    error.value = '';
  },
  { immediate: true },
);
function close() {
  resolutionJobId.value = null;
}
async function submit() {
  if (!job.value || !question.value || submitting.value) return;
  const currentJob = job.value,
    id = question.value.id;
  submitting.value = true;
  error.value = '';
  try {
    await answerJob(currentJob, id, selected.value, text.value);
    if (question.value?.id === id) close();
  } catch (e) {
    error.value = e instanceof Error ? e.message : '提交失败，请重试。';
  } finally {
    submitting.value = false;
  }
}
</script>
<template>
  <AppModal
    :open="!!question"
    :title="question?.title || '处理任务问题'"
    wide
    @close="close"
  >
    <form
      v-if="question && job"
      @submit.prevent="submit"
      class="resolution-form"
    >
      <p class="muted">
        {{ job.scope_name || '当前项目' }} ·
        {{ job.tasks?.find((t) => t.id === question?.task_id)?.role || '方案生成' }}
      </p>
      <p class="resolution-message">{{ question.message }}</p>
      <p
        v-if="question.help_text"
        class="muted"
      >
        {{ question.help_text }}
      </p>
      <div
        v-if="question.details.affected_actions?.length"
        class="table-scroll"
      >
        <table>
          <thead>
            <tr>
              <th>受影响期间</th>
              <th>经营操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(action, index) in question.details.affected_actions"
              :key="index"
            >
              <td>{{ action.period_label }}</td>
              <td>{{ action.description }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="question.details.cash_limit">
        分段支出限额：{{ question.details.cash_limit }} {{ question.details.cash_unit }}
      </p>
      <div
        v-if="question.details.actions?.length"
        class="table-scroll"
      >
        <table>
          <thead>
            <tr>
              <th>经营期</th>
              <th>行动</th>
              <th>金额</th>
              <th>原单位</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="action in question.details.actions"
              :key="action.action_id"
            >
              <td>{{ action.period }}</td>
              <td>{{ action.description }}</td>
              <td>{{ action.amount }}</td>
              <td>{{ action.unit || '未知' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <fieldset class="resolution-options">
        <legend>选择处理方式</legend>
        <label
          v-for="option in question.options"
          :key="option.id"
          :class="{ chosen: selected === option.id }"
        >
          <input
            type="radio"
            v-model="selected"
            :value="option.id"
            name="resolution"
            :disabled="submitting"
          />
          <span
            ><strong>{{ option.label }}</strong
            ><small>{{ option.description }}</small></span
          >
        </label>
      </fieldset>
      <label for="resolution-guidance">{{ question.input_label || '你的修正说明（选填）' }}</label>
      <textarea
        id="resolution-guidance"
        v-model="text"
        maxlength="2000"
        rows="3"
        :disabled="submitting"
        :placeholder="question.input_placeholder || '可留空直接继续，也可补充经营口径。'"
      />
      <details
        v-if="question.technical_details"
        class="technical-diagnostic"
      >
        <summary>查看诊断详情（无需填写其中的编号）</summary>
        <pre>{{ question.technical_details }}</pre>
      </details>
      <p class="muted">
        关闭窗口会保留问题，可从任务记录重新打开。选择后仍会校验来源、期间、现金和限额。
      </p>
      <p
        v-if="error"
        role="alert"
        class="error"
      >
        {{ error }}
      </p>
      <div class="actions">
        <button
          type="button"
          @click="close"
        >
          稍后处理</button
        ><button
          class="primary"
          :disabled="submitting || !selected"
        >
          {{ submitting ? '提交中…' : '提交处理方式' }}
        </button>
      </div>
    </form>
  </AppModal>
</template>
<style scoped>
.resolution-form {
  display: grid;
  gap: 12px;
}
.resolution-message {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.resolution-options {
  border: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}
.resolution-options label {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px;
  border: 1px solid #dbe5ef;
  border-radius: 10px;
  cursor: pointer;
}
.resolution-options label.chosen {
  border-color: #2875d0;
  background: #eef6ff;
}
.resolution-options small {
  display: block;
  margin-top: 4px;
  color: #52657a;
}
.resolution-options input {
  width: auto;
  margin-top: 4px;
}
.table-scroll {
  overflow-x: auto;
}
.technical-diagnostic pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 12px;
}
</style>
