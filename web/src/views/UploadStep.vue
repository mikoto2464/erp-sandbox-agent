<script setup lang="ts">
import { computed, ref } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import type { ParsedDocument } from '../lib/types';
import DocumentPreview from '../components/DocumentPreview.vue';
import UploadDeleteButton from '../components/UploadDeleteButton.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import TemplateLibraryPicker from '../components/TemplateLibraryPicker.vue';
import type { BudgetTemplate } from '../lib/types';
const {
  inputs,
  inputsDirty,
  documents,
  jobs,
  selected,
  status,
  canRead,
  activeRead,
  run,
  operations,
  annotation,
  setAnnotation,
  markInputsDirty,
  saveInputs,
  upload,
  startRead,
  templates,
  readTemplateIds,
  uploadTemplates,
  snapshot,
} = useWorkspaceContext();
defineEmits<{ settings: [] }>();
const preview = ref<ParsedDocument | null>(null),
  templatePreview = ref<BudgetTemplate | null>(null),
  dragging = ref(false),
  fileInput = ref<HTMLInputElement>();
const pendingFiles = computed(() => {
  const entries = new Map<string, (typeof jobs.value)[number]>();
  for (const job of jobs.value)
    if (
      job.kind === 'parse' &&
      job.metadata.document_id &&
      !job.metadata.upload_deleted &&
      !documents.value.some((doc) => doc.source.id === job.metadata.document_id)
    )
      entries.set(job.metadata.document_id, job);
  return [...entries.values()];
});
function files(values: FileList | null) {
  if (values) void run('upload', () => upload([...values]));
  if (fileInput.value) fileInput.value.value = '';
}
function templateFiles(values: FileList | null) {
  if (values) void run('upload-template', () => uploadTemplates([...values]));
}
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <h2>题目文件</h2>
      <span class="muted">单文件最大 {{ status?.max_upload_mb }} MB</span>
    </div>
    <label
      class="drop-zone"
      :class="{ dragover: dragging }"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="
        dragging = false;
        files($event.dataTransfer?.files || null);
      "
      ><span class="upload-icon">↑</span
      ><strong>{{ operations.has('upload') ? '正在上传…' : '点击选择，或拖入文件' }}</strong
      ><span class="muted">DOCX / DOC / PDF / XLS / XLSX</span
      ><input
        ref="fileInput"
        type="file"
        accept=".docx,.doc,.pdf,.xls,.xlsx"
        multiple
        :disabled="operations.has('upload')"
        @change="files(($event.target as HTMLInputElement).files)"
    /></label>
    <div class="file-list">
      <article
        v-for="doc in documents"
        :key="doc.source.id"
        class="file-entry"
      >
        <div class="file-row">
          <input
            v-model="selected"
            type="checkbox"
            :value="doc.source.id"
            :aria-label="`选择${doc.source.filename}用于读题`"
          /><span class="file-info"
            >{{ doc.source.filename
            }}<small
              >{{ Math.ceil(doc.source.size / 1024) }} KB · {{ doc.evidence.length }} 处证据</small
            ></span
          >
          <div class="file-actions">
            <button
              v-if="!snapshot?.document_ids?.includes(doc.source.id)"
              class="review-action"
              :disabled="activeRead || operations.has('read')"
              @click="run('read', () => startRead(!!snapshot, [doc.source.id], []))"
            >
              读取此文件
            </button>
            <span class="badge known">已解析</span
            ><button
              class="review-action"
              @click="preview = doc"
            >
              预览
            </button>
            <UploadDeleteButton
              :id="doc.source.id"
              :filename="doc.source.filename"
              kind="document"
            />
          </div>
        </div>
        <label class="file-note"
          ><span>说明</span
          ><textarea
            :value="annotation(doc.source.id)"
            rows="2"
            maxlength="5000"
            :aria-label="`${doc.source.filename}的文件说明`"
            placeholder="文件说明（选填），例如：订单数量不得超过此详单。"
            @input="setAnnotation(doc.source.id, ($event.target as HTMLTextAreaElement).value)"
          />
        </label>
      </article>
      <article
        v-for="job in pendingFiles"
        :key="job.metadata.document_id"
        class="file-entry"
      >
        <div class="file-row">
          <span class="file-info"
            >{{ job.metadata.filename
            }}<small>{{
              job.status === 'failed'
                ? '解析失败'
                : job.status === 'cancelled'
                  ? '已取消'
                  : '正在解析…'
            }}</small></span
          >
          <UploadDeleteButton
            :id="job.metadata.document_id!"
            :filename="job.metadata.filename || '题目文件'"
            kind="document"
          />
        </div>
      </article>
    </div>
    <div class="input-section">
      <div class="section-heading">
        <h3>预算空表 <span class="muted">选填</span></h3>
        <TemplateLibraryPicker />
        <label class="review-action template-upload"
          >上传空表<input
            type="file"
            accept=".xlsx,.xls"
            multiple
            class="sr-only"
            :disabled="activeRead || operations.has('upload-template')"
            @change="
              templateFiles(($event.target as HTMLInputElement).files);
              ($event.target as HTMLInputElement).value = '';
            "
        /></label>
      </div>
      <p class="muted">
        可从空表库导入已确认解读，也可读取新空表；确认后自动保存到空表库供其他项目复用。
      </p>
      <div
        v-for="template in templates"
        :key="template.source.id"
        class="budget-template-row"
      >
        <label class="check"
          ><input
            v-model="readTemplateIds"
            type="checkbox"
            :value="template.source.id"
          />
          <span
            ><strong>{{ template.source.filename }}</strong
            ><small>{{ template.sheets.map((sheet) => sheet.name).join(' · ') }}</small></span
          ></label
        >
        <span
          class="badge"
          :class="
            snapshot?.template_readings?.some((t) => t.template_id === template.source.id)
              ? 'known'
              : 'missing'
          "
        >
          {{
            snapshot?.template_readings?.some((t) => t.template_id === template.source.id)
              ? '已读题'
              : '待读题'
          }}</span
        >
        <button
          class="review-action"
          @click="templatePreview = template"
        >
          预览
        </button>
        <button
          v-if="!snapshot?.template_readings?.some((t) => t.template_id === template.source.id)"
          class="review-action"
          :disabled="activeRead || operations.has('read')"
          @click="run('read', () => startRead(!!snapshot, [], [template.source.id]))"
        >
          读取此空表
        </button>
        <UploadDeleteButton
          :id="template.source.id"
          :filename="template.source.filename"
          kind="template"
        />
      </div>
      <div
        v-for="job in jobs.filter(
          (job) =>
            job.kind === 'template' &&
            !job.metadata.upload_deleted &&
            !templates.some((t) => t.source.id === job.metadata.template_id),
        )"
        :key="job.id"
        class="budget-template-row"
      >
        <span
          >{{ job.metadata.filename }} ·
          {{
            job.status === 'failed'
              ? '解析失败'
              : job.status === 'cancelled'
                ? '已取消'
                : '正在解析…'
          }}</span
        >
        <UploadDeleteButton
          v-if="job.metadata.template_id"
          :id="job.metadata.template_id"
          :filename="job.metadata.filename || '空表'"
          kind="template"
        />
      </div>
    </div>
    <div class="input-section">
      <label for="problem-text">手动输入题目（选填）</label
      ><textarea
        id="problem-text"
        v-model="inputs.problem_text"
        rows="5"
        maxlength="100000"
        placeholder="可直接粘贴题目，不上传文件也可以使用。"
        @input="markInputsDirty"
      />
    </div>
    <div class="input-section">
      <label for="supplement-text">补充内容与限制（选填）</label
      ><textarea
        id="supplement-text"
        v-model="inputs.supplement_text"
        rows="3"
        maxlength="50000"
        placeholder="例如：订单数量以上传详单为上限；优先减少借款。"
        @input="markInputsDirty"
      />
    </div>
    <div class="input-save">
      <span class="muted">{{ inputsDirty ? '输入待保存，读取时会自动保存' : '输入已保存' }}</span
      ><button
        class="review-action"
        :disabled="!inputsDirty || operations.has('inputs')"
        @click="
          run('inputs', async () => {
            await saveInputs();
          })
        "
      >
        保存输入
      </button>
    </div>
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
      <span class="muted"
        >已选择 {{ selected.length }} 个文件{{
          inputs.problem_text.trim() ? ' · 含手动题目' : ''
        }}</span
      ><button
        class="primary"
        :disabled="!canRead || operations.has('read')"
        @click="run('read', () => startRead())"
      >
        {{ activeRead ? '正在读题…' : snapshot ? '追加读取新文件 →' : '读取题目与空表 →' }}
      </button>
      <button
        v-if="snapshot"
        class="review-action"
        :disabled="!canRead || operations.has('read')"
        @click="run('read', () => startRead(false))"
      >
        重新读取所选资料
      </button>
    </footer>
    <DocumentPreview
      :document="preview"
      @close="preview = null"
    />
    <BudgetTemplatePreview
      :template="templatePreview"
      @close="templatePreview = null"
    />
  </section>
</template>
