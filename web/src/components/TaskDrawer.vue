<script setup lang="ts">
import { ref } from 'vue';
import type { AgentTask, Job } from '../lib/types';
import AppModal from './AppModal.vue';
import { useWorkspaceContext } from '../composables/context';
import { statusNames, stageNames } from '../lib/labels';
import { readingProgress } from '../lib/tasks';
const { taskOpen, allJobs: jobs, run, cancelJob, retryJob, operations } = useWorkspaceContext();
const expanded = ref(new Set<string>());
const difficultyNames: Record<string, string> = {
  simple: '简单',
  moderate: '中等',
  complex: '复杂',
  unassessed: '未评估',
};
const taskStatus: Record<string, string> = {
  ...statusNames,
  waiting: '等待模型额度',
  cached: '复用已完成结果',
  split: '已拆分',
  needs_input: '需要核对',
};
function toggle(job: Job) {
  if (expanded.value.has(job.id)) expanded.value.delete(job.id);
  else expanded.value.add(job.id);
}
function tasksFor(job: Job): AgentTask[] {
  const tasks = job.tasks || [];
  const active = tasks.filter((t) => ['running', 'waiting', 'queued'].includes(t.status));
  const finished = tasks.filter((t) => !['running', 'waiting', 'queued'].includes(t.status));
  return [...active, ...finished.slice(-80).reverse()];
}
function duration(task: AgentTask) {
  if (!task.started_at || !task.finished_at) return '';
  return `${Math.max(1, Math.round((Date.parse(task.finished_at) - Date.parse(task.started_at)) / 1000))} 秒`;
}
</script>
<template>
  <AppModal
    :open="taskOpen"
    title="任务记录"
    drawer
    @close="taskOpen = false"
    ><p class="muted">全部项目与空表库的任务</p>
    <p
      v-if="!jobs.length"
      class="empty"
    >
      暂无任务。上传文件或读取题目后会显示进度。
    </p>
    <article
      v-for="job in [...jobs].reverse()"
      :key="job.id"
      class="task-row task-card"
    >
      <div class="task-info">
        <div class="task-row-heading">
          <strong>{{
            job.project_id === 'template-library'
              ? job.kind === 'template'
                ? '空表解析'
                : '空表解读'
              : job.kind === 'parse'
                ? '文件解析'
                : job.kind === 'template'
                  ? '预算表解析'
                  : job.kind === 'mapping'
                    ? '预算表映射'
                    : job.kind === 'artifact'
                      ? '文件生成'
                      : job.kind === 'read'
                        ? '读题'
                        : '方案生成'
          }}</strong
          ><span
            class="task-state"
            :class="job.status"
            >{{ statusNames[job.status] }}</span
          >
        </div>
        <p class="muted">
          {{ job.scope_name || '项目' }} · {{ stageNames[job.stage] || job.stage }}
        </p>
        <p
          v-if="job.metadata.filename"
          class="muted"
        >
          {{ job.metadata.filename }}
        </p>
        <p
          v-if="job.metadata.model_preset"
          class="muted"
        >
          {{ job.metadata.model_preset.preset_name }}
        </p>
        <p
          v-if="job.error"
          class="error"
        >
          {{ job.error.message }}
        </p>
      </div>
      <div
        v-if="['read', 'solve'].includes(job.kind) && readingProgress(job).total"
        class="reading-progress"
      >
        <div class="task-row-heading">
          <strong>{{ job.kind === 'solve' ? '角色协作' : '章节 / 工作表解析' }}</strong>
          <span
            >已处理 {{ readingProgress(job).processed }} / {{ readingProgress(job).total }}</span
          >
        </div>
        <progress
          :value="readingProgress(job).processed"
          :max="readingProgress(job).total"
          aria-label="子任务处理进度"
        />
        <div class="task-metrics">
          <span>完成 {{ readingProgress(job).completed }}</span>
          <span>执行中 {{ readingProgress(job).active }}</span>
          <span>待执行 {{ readingProgress(job).waiting }}</span>
          <span v-if="readingProgress(job).unresolved"
            >待核对 {{ readingProgress(job).unresolved }}</span
          >
          <span v-if="readingProgress(job).failed"
            >失败 / 取消 {{ readingProgress(job).failed }}</span
          >
        </div>
        <p
          v-if="
            readingProgress(job).processed === readingProgress(job).total &&
            job.status === 'running'
          "
          class="muted"
        >
          子任务已结束，{{ stageNames[job.stage] || job.stage }}。
        </p>
      </div>
      <button
        v-if="['running', 'queued'].includes(job.status)"
        class="review-action"
        :disabled="operations.has(job.id)"
        @click="run(job.id, () => cancelJob(job))"
      >
        取消</button
      ><button
        v-if="['failed', 'cancelled'].includes(job.status) && !job.metadata.upload_deleted"
        class="review-action"
        :disabled="operations.has(job.id)"
        @click="run(job.id, () => retryJob(job))"
      >
        重试
      </button>
      <div
        v-if="job.tasks?.length"
        class="task-detail"
      >
        <button
          class="task-disclosure"
          :aria-expanded="expanded.has(job.id)"
          @click="toggle(job)"
        >
          <span>{{ expanded.has(job.id) ? '收起处理详情' : '展开处理详情' }}</span>
          <span class="muted"
            >{{ job.tasks.filter((t) => t.status === 'running').length }} 执行 ·
            {{ job.tasks.filter((t) => t.status === 'waiting').length }} 等待</span
          >
          <span aria-hidden="true">{{ expanded.has(job.id) ? '▴' : '▾' }}</span>
        </button>
        <div
          v-if="expanded.has(job.id)"
          class="agent-tasks"
        >
          <article
            v-for="task in tasksFor(job)"
            :key="task.id"
            class="agent-task"
            :class="{ 'child-task': task.parent_id }"
          >
            <div class="task-row-heading">
              <strong>{{ task.agent }} · {{ task.role }}</strong>
              <span
                class="task-state"
                :class="task.status"
                >{{ taskStatus[task.status] || task.status }}</span
              >
            </div>
            <p class="task-object">{{ task.object }}</p>
            <p
              v-if="task.model || task.preset_name"
              class="muted"
            >
              {{ [task.preset_name, task.model].filter(Boolean).join(' · ') }}
            </p>
            <p
              v-if="['queued', 'waiting'].includes(task.status) && task.candidates?.length"
              class="muted"
            >
              {{ task.status === 'waiting' ? '等待可用模型' : '主 Agent 分配候选' }}：{{
                task.candidates.join('、')
              }}
            </p>
            <p
              v-if="task.stage && task.status === 'running'"
              class="muted"
            >
              {{ stageNames[task.stage] || task.stage }}
            </p>
            <div
              v-if="task.dispatch_reason || task.assignment_reason"
              class="dispatch-decision"
            >
              <p v-if="task.difficulty">
                <strong>任务难度：{{ difficultyNames[task.difficulty] || task.difficulty }}</strong>
                <span
                  v-if="task.dispatch_fallback"
                  class="badge assumed"
                  >调度失败，保守回退</span
                >
              </p>
              <p>{{ task.assignment_reason || task.dispatch_reason }}</p>
              <p
                v-if="task.dispatch_planned"
                class="muted"
              >
                由主 Agent 首次统一分配，按实时并发额度执行。
              </p>
              <p
                v-if="task.dispatch_reused"
                class="muted"
              >
                复用主 Agent 对同表数据的分配，当前并发额度实时检查。
              </p>
              <details
                v-if="task.model_loads?.length"
                class="dispatch-loads"
              >
                <summary>分配时的模型负载</summary>
                <ul>
                  <li
                    v-for="model in task.model_loads"
                    :key="model.id"
                  >
                    {{ model.name }} · 优先级 {{ model.priority }} · 占用 {{ model.in_use }}/{{
                      model.max_parallel
                    }}
                    · 剩余 {{ model.available }}
                  </li>
                </ul>
              </details>
            </div>
            <div class="task-metrics">
              <span v-if="task.input_chars != null">输入 {{ task.input_chars }} 字符</span>
              <span v-if="task.output_limit">请求输出预算 {{ task.output_limit }} Token</span>
              <span v-if="task.child_count">{{ task.child_count }} 个恢复子任务</span>
              <span v-if="task.input_group_count != null"
                >{{ task.input_group_count }} 组输入项目</span
              >
              <span v-if="task.recommendation_count != null"
                >{{ task.recommendation_count }} 条协作建议</span
              >
              <span v-if="task.planned_count != null"
                >已分配 {{ task.planned_count }} 个子任务</span
              >
              <span v-if="task.evidence_count">{{ task.evidence_count }} 处原文</span>
              <span v-if="task.fact_count != null">{{ task.fact_count }} 个参数</span>
              <span v-if="task.rule_count != null">{{ task.rule_count }} 条规则</span>
              <span v-if="(task.attempt || 0) > 1">第 {{ task.attempt }} 次请求</span>
              <span v-if="duration(task)">{{ duration(task) }}</span>
            </div>
            <p
              v-if="task.recovery"
              class="muted"
            >
              {{ task.recovery }}
            </p>
            <p
              v-if="task.error"
              class="error"
            >
              {{ task.error }}
            </p>
          </article>
          <p
            v-if="(job.tasks?.length || 0) > tasksFor(job).length || job.archived_tasks"
            class="muted"
          >
            展示当前处理与最近 80 条结束记录，完整读题结果保留在题目中。
          </p>
        </div>
      </div>
    </article></AppModal
  >
</template>
