<script setup lang="ts">
import { ref } from 'vue';
import type { AgentTask, Job } from '../lib/types';
import AppModal from './AppModal.vue';
import { useWorkspaceContext } from '../composables/context';
import { statusNames, stageNames } from '../lib/labels';
const { taskOpen, jobs, project, run, cancelJob, retryJob, operations } = useWorkspaceContext();
const expanded = ref(new Set<string>());
const difficultyNames: Record<string, string> = {
  simple: '简单',
  moderate: '中等',
  complex: '复杂',
  unassessed: '单模型直达',
};
const taskStatus: Record<string, string> = {
  ...statusNames,
  waiting: '等待模型额度',
  cached: '复用已完成结果',
  split: '已拆分',
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
    ><p class="muted">{{ project?.name }}</p>
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
            job.kind === 'parse'
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
        <p class="muted">{{ stageNames[job.stage] || job.stage }}</p>
        <p
          v-if="job.kind === 'read' && (job.completed ?? 0) > 0"
          class="muted"
        >
          已完成 {{ job.completed }} 个提取子任务
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
              v-if="task.status === 'waiting' && task.candidates"
              class="muted"
            >
              等待可用模型：{{ task.candidates.join('、') }}
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
              <span v-if="task.evidence_count">{{ task.evidence_count }} 处原文</span>
              <span v-if="task.fact_count != null">{{ task.fact_count }} 个参数</span>
              <span v-if="task.rule_count != null">{{ task.rule_count }} 条规则</span>
              <span v-if="(task.attempt || 0) > 1">第 {{ task.attempt }} 次请求</span>
              <span v-if="duration(task)">{{ duration(task) }}</span>
            </div>
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
