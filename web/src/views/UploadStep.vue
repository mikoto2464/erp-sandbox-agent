<script setup lang="ts">
import { computed, ref } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import type { ParsedDocument } from '../lib/types';
import DocumentPreview from '../components/DocumentPreview.vue';
import UploadDeleteButton from '../components/UploadDeleteButton.vue';
import FileUploadZone from '../components/FileUploadZone.vue';
import FileEntry from '../components/FileEntry.vue';
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
  snapshot,
} = useWorkspaceContext();
defineEmits<{ settings: [] }>();
const preview = ref<ParsedDocument | null>(null);
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
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <h2>题目文件</h2>
      <span class="muted">单文件最大 {{ status?.max_upload_mb }} MB</span>
    </div>
    <FileUploadZone
      accept=".docx,.doc,.pdf,.xls,.xlsx"
      formats="DOCX / DOC / PDF / XLS / XLSX"
      :disabled="operations.has('upload')"
      @files="run('upload', () => upload($event))"
    />
    <div class="file-list">
      <FileEntry
        v-for="doc in documents"
        :key="doc.source.id"
        :filename="doc.source.filename"
        :size="doc.source.size"
        :metadata="`${doc.evidence.length} 处证据`"
      >
        <template #selection>
          <input
            v-model="selected"
            type="checkbox"
            :value="doc.source.id"
            :aria-label="`选择${doc.source.filename}用于读题`"
          />
        </template>
        <template #actions>
          <button
            v-if="!snapshot?.document_ids?.includes(doc.source.id)"
            class="review-action"
            :disabled="activeRead || operations.has('read')"
            @click="run('read', () => startRead(!!snapshot, [doc.source.id]))"
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
        </template>
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
      </FileEntry>
      <FileEntry
        v-for="job in pendingFiles"
        :key="job.metadata.document_id"
        :filename="job.metadata.filename || '题目文件'"
        :metadata="
          job.status === 'failed' ? '解析失败' : job.status === 'cancelled' ? '已取消' : '正在解析…'
        "
      >
        <template #actions>
          <UploadDeleteButton
            :id="job.metadata.document_id!"
            :filename="job.metadata.filename || '题目文件'"
            kind="document"
          />
        </template>
      </FileEntry>
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
        {{ activeRead ? '正在读题…' : snapshot ? '追加读取新文件 →' : '读取题目 →' }}
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
  </section>
</template>
