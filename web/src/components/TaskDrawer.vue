<script setup lang="ts">
import AppModal from './AppModal.vue';
import { useWorkspaceContext } from '../composables/context';
import { statusNames, stageNames } from '../lib/labels';
const { taskOpen, jobs, project, run, cancelJob, retryJob, operations } = useWorkspaceContext();
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
      class="task-row"
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
        v-if="['failed', 'cancelled'].includes(job.status)"
        class="review-action"
        :disabled="operations.has(job.id)"
        @click="run(job.id, () => retryJob(job))"
      >
        重试
      </button>
    </article></AppModal
  >
</template>
