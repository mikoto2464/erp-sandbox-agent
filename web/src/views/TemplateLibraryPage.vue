<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import { request, post } from '../lib/api';
import type { BudgetTemplate, TemplateReading } from '../lib/types';
import AppModal from '../components/AppModal.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
const { run, notify, allJobs, refreshTasks, cancelJob, taskOpen } = useWorkspaceContext();
interface Item {
  id: string;
  source: BudgetTemplate['source'];
  parsed: boolean;
  reading: TemplateReading | null;
  sheet_names?: string[];
}
const items = ref<Item[]>([]),
  preview = ref<BudgetTemplate | null>(null),
  detail = ref<{ id: string; reading: TemplateReading } | null>(null),
  removing = ref<Item | null>(null),
  busy = ref(false);
const jobs = computed(() => allJobs.value.filter((j) => j.project_id === 'template-library'));
const active = computed(() => jobs.value.filter((j) => ['queued', 'running'].includes(j.status)));
let disposed = false,
  refreshing = false;
async function refresh(updateTasks = true) {
  if (refreshing) return;
  refreshing = true;
  try {
    const [values] = await Promise.all([
      request<Item[]>('/api/template-library'),
      updateTasks ? refreshTasks() : Promise.resolve(),
    ]);
    if (disposed) return;
    items.value = values;
  } finally {
    refreshing = false;
  }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement,
    files = [...(input.files || [])];
  input.value = '';
  busy.value = true;
  try {
    for (const file of files) {
      if (!/\.(xlsx|xls)$/i.test(file.name)) throw new Error('请选择 XLSX 或 XLS 空表。');
      await request('/api/template-library', {
        method: 'POST',
        headers: {
          'X-Filename': encodeURIComponent(file.name),
          'Content-Type': 'application/octet-stream',
        },
        body: file,
      });
    }
    await refresh();
  } finally {
    busy.value = false;
  }
}
function latest(item: Item) {
  return jobs.value.filter((j) => j.metadata.template_id === item.id).at(-1);
}
function processing(item: Item) {
  return active.value.some((j) => j.metadata.template_id === item.id);
}
async function open(item: Item, review = false) {
  const value = await request<{ template: BudgetTemplate; reading: TemplateReading | null }>(
    `/api/template-library/${item.id}`,
  );
  if (review && value.reading) detail.value = { id: item.id, reading: value.reading };
  else preview.value = value.template;
}
async function confirm() {
  if (!detail.value) return;
  await post(`/api/template-library/${detail.value.id}/review`, {
    reviewed: true,
    explanation: detail.value.reading.explanation,
  });
  detail.value = null;
  await refresh();
  notify('空表已确认，可供各项目导入。', 'success');
}
onMounted(() => {
  void run('library', () => refresh());
});
watch(jobs, () => {
  if (!disposed) void run('library-poll', () => refresh(false));
});
onBeforeUnmount(() => {
  disposed = true;
});
</script>
<template>
  <section>
    <div class="page-heading">
      <div>
        <h1>空表库</h1>
        <p class="muted">解读并确认一次，各项目可重复导入。保留公式和已有数据。</p>
      </div>
      <label class="review-action"
        >上传空表<input
          class="sr-only"
          type="file"
          accept=".xlsx,.xls"
          multiple
          :disabled="busy"
          @change="run('library-upload', () => upload($event))"
      /></label>
    </div>
    <p
      v-if="!items.length"
      class="empty"
    >
      上传预算空表后，点击“解读空表”。
    </p>
    <article
      v-for="item in items"
      :key="item.id"
      class="card input-section"
    >
      <div class="section-heading">
        <div>
          <h3>{{ item.source.filename }}</h3>
          <p class="muted">{{ item.sheet_names?.join(' · ') }}</p>
        </div>
        <span
          class="badge"
          :class="item.reading?.reviewed ? 'known' : 'missing'"
          >{{
            processing(item)
              ? '正在处理'
              : item.reading?.reviewed
                ? '已确认'
                : item.reading
                  ? '待核对'
                  : '待解读'
          }}</span
        >
      </div>
      <p
        v-if="latest(item)?.error"
        class="error"
      >
        {{ latest(item)?.error?.message }}
      </p>
      <div class="card-actions">
        <button
          v-if="item.parsed"
          class="review-action"
          @click="run('library-preview', () => open(item))"
        >
          预览原表
        </button>
        <button
          class="review-action"
          :disabled="processing(item)"
          @click="
            run('library-read', async () => {
              await post(`/api/template-library/${item.id}/read`, {});
              await refresh();
            })
          "
        >
          {{
            item.reading
              ? '复用已完成解读'
              : latest(item)?.status === 'failed'
                ? '继续未完成内容'
                : '解读空表'
          }}
        </button>
        <button
          v-if="item.reading"
          class="review-action"
          @click="run('library-review', () => open(item, true))"
        >
          {{ item.reading.reviewed ? '查看解读' : '核对解读' }}
        </button>
        <button
          v-if="processing(item)"
          class="review-action"
          @click="
            run('library-cancel', async () => {
              const job = active.find((j) => j.metadata.template_id === item.id);
              if (job) await cancelJob(job);
              await refresh();
            })
          "
        >
          取消
        </button>
        <button
          class="quiet danger-text"
          @click="removing = item"
        >
          删除
        </button>
      </div>
      <button
        v-if="latest(item)"
        class="review-action"
        @click="taskOpen = true"
      >
        查看任务详情
      </button>
    </article>
    <BudgetTemplatePreview
      :template="preview"
      @close="preview = null"
    />
    <AppModal
      :open="!!detail"
      title="核对空表解读"
      wide
      @close="detail = null"
    >
      <template v-if="detail"
        ><div
          v-for="sheet in detail.reading.sheets"
          :key="sheet.name"
          class="input-section"
        >
          <h3>{{ sheet.name }}</h3>
          <p>{{ sheet.purpose }} · {{ sheet.periods || '期间未注明' }}</p>
          <p
            v-for="group in sheet.input_groups"
            :key="group.range + group.label"
          >
            {{ group.label }} · {{ group.range }} · {{ group.unit || '无单位'
            }}<small class="muted"> {{ group.description }}</small>
          </p>
        </div>
        <p
          v-for="issue in detail.reading.issues"
          :key="issue.message"
          class="notice"
        >
          {{ issue.message }}
        </p>
        <label
          >处理说明（选填）<textarea
            v-model="detail.reading.explanation"
            rows="3"
            placeholder="存在疑点时说明期间、用途或条目口径。"
          />
        </label>
        <footer class="dialog-footer">
          <button
            class="primary"
            @click="run('library-confirm', confirm)"
          >
            确认解读
          </button>
        </footer></template
      >
    </AppModal>
    <AppModal
      :open="!!removing"
      title="删除空表"
      @close="removing = null"
      ><p>从空表库删除 {{ removing?.source.filename }}？已导入项目的副本保留。</p>
      <footer class="dialog-footer">
        <button
          class="primary"
          @click="
            run('library-delete', async () => {
              if (removing)
                await request(`/api/template-library/${removing.id}`, { method: 'DELETE' });
              removing = null;
              await refresh();
            })
          "
        >
          删除
        </button>
      </footer></AppModal
    >
  </section>
</template>
