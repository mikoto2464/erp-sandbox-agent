<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import { request, post } from '../lib/api';
import type { BudgetTemplate, TemplateLibraryItem, TemplateReading } from '../lib/types';
import AppModal from '../components/AppModal.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import FileUploadZone from '../components/FileUploadZone.vue';
import FileEntry from '../components/FileEntry.vue';
import TemplateReadingReview from '../components/TemplateReadingReview.vue';
const { run, operations, status, notify, allJobs, refreshTasks, cancelJob, taskOpen } =
  useWorkspaceContext();
defineEmits<{ settings: [] }>();
const items = ref<TemplateLibraryItem[]>([]),
  selected = ref<string[]>([]),
  preview = ref<BudgetTemplate | null>(null),
  detail = ref<{ id: string; template: BudgetTemplate; reading: TemplateReading } | null>(null),
  removing = ref<TemplateLibraryItem | null>(null),
  reviewPanel = ref<HTMLElement>();
const jobs = computed(() => allJobs.value.filter((job) => job.project_id === 'template-library'));
const active = computed(() =>
  jobs.value.filter((job) => ['queued', 'running'].includes(job.status)),
);
const ready = computed(() =>
  items.value.filter((item) => item.parsed && !item.reading && !pending(item)),
);
const selectedReady = computed(() =>
  ready.value.filter((item) => selected.value.includes(item.id)),
);
let disposed = false,
  refreshing: Promise<void> | null = null;
async function refresh(updateTasks = true) {
  if (updateTasks) await refreshTasks();
  if (refreshing) {
    await refreshing;
    return refresh(false);
  }
  const operation = (async () => {
    const values = await request<TemplateLibraryItem[]>('/api/template-library');
    if (disposed) return;
    const newlyParsed = values.filter(
      (item) =>
        item.parsed &&
        !item.reading &&
        !items.value.some((old) => old.id === item.id && old.parsed),
    );
    items.value = values;
    selected.value = [
      ...new Set([...selected.value, ...newlyParsed.map((item) => item.id)]),
    ].filter((id) => values.some((item) => item.id === id && item.parsed && !item.reading));
  })();
  refreshing = operation;
  try {
    await operation;
  } finally {
    if (refreshing === operation) refreshing = null;
  }
}
async function upload(files: File[]) {
  for (const file of files) {
    if (!/\.(xlsx|xls)$/i.test(file.name)) throw new Error('请选择 XLSX 或 XLS 空表。');
    if (status.value && file.size > status.value.max_upload_mb * 1024 * 1024)
      throw new Error(`单文件不能超过 ${status.value.max_upload_mb} MB。`);
  }
  for (const file of files) {
    await request('/api/template-library', {
      method: 'POST',
      headers: {
        'X-Filename': encodeURIComponent(file.name),
        'Content-Type': 'application/octet-stream',
      },
      body: file,
    });
    await refresh();
  }
}
function latest(item: TemplateLibraryItem) {
  return jobs.value.filter((job) => job.metadata.template_id === item.id).at(-1);
}
function processing(item: TemplateLibraryItem) {
  return active.value.some((job) => job.metadata.template_id === item.id);
}
function pending(item: TemplateLibraryItem) {
  return processing(item) || operations.value.has('library-read-' + item.id);
}
function state(item: TemplateLibraryItem) {
  if (processing(item)) return item.parsed ? '正在解读…' : '正在解析…';
  if (pending(item)) return '正在提交…';
  if (latest(item)?.status === 'failed') return item.parsed ? '解读失败' : '解析失败';
  if (latest(item)?.status === 'cancelled') return '已取消';
  if (item.reading) return item.reading.reviewed ? '已确认' : '待核对';
  return item.parsed ? '已解析' : '待解析';
}
async function read(item: TemplateLibraryItem) {
  await post(`/api/template-library/${item.id}/read`, {});
  await refresh();
}
async function readSelected() {
  const targets = [...selectedReady.value];
  for (const item of targets) await run('library-read-' + item.id, () => read(item));
}
async function open(item: TemplateLibraryItem, review = false) {
  const value = await request<{ template: BudgetTemplate; reading: TemplateReading | null }>(
    `/api/template-library/${item.id}`,
  );
  if (disposed || !items.value.some((current) => current.id === item.id)) return;
  if (review && value.reading) {
    detail.value = { id: item.id, template: value.template, reading: value.reading };
    await nextTick();
    reviewPanel.value?.scrollIntoView?.({ block: 'start' });
  } else preview.value = value.template;
}
async function confirm(explanation: string) {
  if (!detail.value) return;
  const id = detail.value.id;
  const reading = await post<TemplateReading>(`/api/template-library/${id}/review`, {
    reviewed: true,
    explanation,
  });
  if (detail.value?.id === id) detail.value.reading = reading;
  await refresh();
  notify('空表已确认，可供各项目导入。', 'success');
}
async function cancel(item: TemplateLibraryItem) {
  const job = active.value.find((value) => value.metadata.template_id === item.id);
  if (job) await cancelJob(job);
  await refresh();
}
async function remove() {
  if (!removing.value) return;
  const id = removing.value.id;
  await request(`/api/template-library/${id}`, { method: 'DELETE' });
  removing.value = null;
  if (detail.value?.id === id) detail.value = null;
  if (preview.value?.source.id === id) preview.value = null;
  await refresh();
}
onMounted(() => {
  void run('library', () => refresh());
});
watch(
  () => jobs.value.map((job) => `${job.id}:${job.status}`).join('|'),
  () => {
    if (!disposed) void refresh(false).catch((error: Error) => notify(error.message, 'error'));
  },
);
onBeforeUnmount(() => {
  disposed = true;
});
</script>
<template>
  <section>
    <div class="flow-heading"><h1>空表库</h1></div>
    <section class="card">
      <div class="section-heading">
        <h2>空表文件</h2>
        <span class="muted">单文件最大 {{ status?.max_upload_mb }} MB</span>
      </div>
      <FileUploadZone
        accept=".xlsx,.xls"
        formats="XLSX / XLS"
        :disabled="operations.has('library-upload')"
        @files="run('library-upload', () => upload($event))"
      />
      <div class="file-list">
        <FileEntry
          v-for="item in items"
          :key="item.id"
          :filename="item.source.filename"
          :size="item.source.size"
          :metadata="
            item.parsed
              ? `${item.sheet_names?.length || 0} 张工作表${item.sheet_names?.length ? ' · ' + item.sheet_names.join(' · ') : ''}`
              : state(item)
          "
        >
          <template #selection>
            <input
              v-if="item.parsed"
              v-model="selected"
              type="checkbox"
              :value="item.id"
              :disabled="!!item.reading || pending(item)"
              :aria-label="`选择${item.source.filename}用于解读`"
            />
          </template>
          <template #actions>
            <button
              v-if="!item.reading && !processing(item)"
              class="review-action"
              :disabled="pending(item) || operations.has('library-batch')"
              @click="run('library-read-' + item.id, () => read(item))"
            >
              {{
                latest(item)?.status === 'failed' || latest(item)?.status === 'cancelled'
                  ? '继续未完成内容'
                  : item.parsed
                    ? '解读此文件'
                    : '重新解析'
              }}
            </button>
            <span
              class="badge"
              :class="
                latest(item)?.status === 'failed'
                  ? 'conflicted'
                  : (item.reading && !item.reading.reviewed) || pending(item)
                    ? 'assumed'
                    : item.parsed
                      ? 'known'
                      : 'missing'
              "
              >{{ state(item) }}</span
            >
            <button
              v-if="item.parsed"
              class="review-action"
              :disabled="operations.has('library-preview-' + item.id)"
              @click="run('library-preview-' + item.id, () => open(item))"
            >
              预览
            </button>
            <button
              v-if="item.reading"
              class="review-action"
              :disabled="operations.has('library-review-' + item.id)"
              @click="run('library-review-' + item.id, () => open(item, true))"
            >
              {{ item.reading.reviewed ? '查看解读' : '核对解读' }}
            </button>
            <button
              v-if="processing(item)"
              class="review-action"
              :disabled="operations.has('library-cancel-' + item.id)"
              @click="run('library-cancel-' + item.id, () => cancel(item))"
            >
              取消
            </button>
            <button
              class="review-action danger-text"
              :aria-label="`删除${item.source.filename}`"
              :disabled="operations.has('library-delete-' + item.id)"
              @click="removing = item"
            >
              删除
            </button>
          </template>
          <p
            v-if="latest(item)?.status === 'failed' && latest(item)?.error"
            class="error file-feedback"
          >
            {{ latest(item)?.error?.message }}
          </p>
        </FileEntry>
      </div>
      <p
        v-if="!items.length"
        class="empty"
      >
        暂无空表文件。
      </p>
      <p
        v-if="!status?.llm_configured"
        class="notice"
      >
        请先配置读题模型。<button
          class="review-action"
          @click="$emit('settings')"
        >
          打开模型预设
        </button>
      </p>
      <footer class="panel-footer">
        <span class="muted">已选择 {{ selectedReady.length }} 个文件</span>
        <div class="actions">
          <button
            v-if="jobs.length"
            class="review-action"
            @click="taskOpen = true"
          >
            查看任务详情
          </button>
          <button
            class="primary"
            :disabled="
              !selectedReady.length || operations.has('library-batch') || !status?.llm_configured
            "
            @click="run('library-batch', readSelected)"
          >
            {{ operations.has('library-batch') ? '正在提交…' : '解读所选空表 →' }}
          </button>
        </div>
      </footer>
    </section>
    <div
      v-if="detail"
      ref="reviewPanel"
      class="template-review-panel"
    >
      <TemplateReadingReview
        :key="detail.id"
        :template="detail.template"
        :reading="detail.reading"
        :busy="operations.has('library-confirm-' + detail.id)"
        @confirm="run('library-confirm-' + detail!.id, () => confirm($event))"
        @preview="preview = detail!.template"
      />
    </div>
    <BudgetTemplatePreview
      :template="preview"
      @close="preview = null"
    />
    <AppModal
      :open="!!removing"
      title="删除文件"
      @close="removing = null"
    >
      <p>删除「{{ removing?.source.filename }}」？</p>
      <p class="muted">从空表库移除，已导入项目的副本保留。</p>
      <footer class="dialog-footer">
        <button
          class="secondary"
          @click="removing = null"
        >
          取消
        </button>
        <button
          class="primary"
          :disabled="!!removing && operations.has('library-delete-' + removing.id)"
          @click="run('library-delete-' + removing!.id, remove)"
        >
          {{
            removing && operations.has('library-delete-' + removing.id) ? '正在删除…' : '删除文件'
          }}
        </button>
      </footer>
    </AppModal>
  </section>
</template>
