<script setup lang="ts">
import { ref } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import type { ParsedDocument } from '../lib/types';
import DocumentPreview from '../components/DocumentPreview.vue';
const {
  inputs,
  inputsDirty,
  documents,
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
} = useWorkspaceContext();
defineEmits<{ settings: [] }>();
const preview = ref<ParsedDocument | null>(null),
  dragging = ref(false),
  fileInput = ref<HTMLInputElement>();
function files(values: FileList | null) {
  if (values) void run('upload', () => upload([...values]));
  if (fileInput.value) fileInput.value.value = '';
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
      ><span class="muted">DOCX / DOC / PDF / XLS</span
      ><input
        ref="fileInput"
        type="file"
        accept=".docx,.doc,.pdf,.xls"
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
            <span class="badge known">已解析</span
            ><button
              class="review-action"
              @click="preview = doc"
            >
              预览
            </button>
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
        @click="run('read', startRead)"
      >
        {{ activeRead ? '正在读题…' : '读取题目 →' }}
      </button>
    </footer>
    <DocumentPreview
      :document="preview"
      @close="preview = null"
    />
  </section>
</template>
