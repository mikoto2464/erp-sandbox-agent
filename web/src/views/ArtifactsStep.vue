<script setup lang="ts">
import { computed, ref } from 'vue';
import AppSelect from '../components/AppSelect.vue';
import MappingEditor from '../components/MappingEditor.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import { useWorkspaceContext } from '../composables/context';
import type { BudgetTemplate, WorkbookMapping } from '../lib/types';
const {
  currentSolution,
  solutions,
  selectSolution,
  templates,
  mappings,
  artifacts,
  jobs,
  operations,
  run,
  analyzeTemplate,
  exportArtifact,
  uploadTemplates,
  setStep,
  project,
} = useWorkspaceContext();
const editing = ref<WorkbookMapping | null>(null),
  preview = ref<BudgetTemplate | null>(null);
const visibleArtifacts = computed(() =>
  artifacts.value.filter((item) => item.solution_id === currentSolution.value?.id),
);
const visibleMappings = computed(() =>
  mappings.value.filter((item) => item.solution_id === currentSolution.value?.id),
);
const pending = computed(() =>
  jobs.value.some(
    (job) =>
      ['mapping', 'artifact'].includes(job.kind) &&
      job.metadata.solution_id === currentSolution.value?.id &&
      ['queued', 'running'].includes(job.status),
  ),
);
function mappingFor(id: string) {
  return visibleMappings.value.filter((item) => item.template_id === id).at(-1);
}
function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = [...(input.files || [])];
  input.value = '';
  if (files.length) void run('upload-template', () => uploadTemplates(files));
}
const formulaNames: Record<string, string> = {
  pending: '公式待重算',
  not_needed: '无公式',
  not_applicable: '文档检查完成',
  recalculated: '公式已重算',
};
</script>
<template>
  <section>
    <div class="section-heading">
      <div>
        <h2>方案与表格</h2>
        <p class="muted">{{ currentSolution?.plan.title || '请先生成经营方案' }}</p>
      </div>
      <AppSelect
        v-if="solutions.length > 1 && currentSolution"
        :model-value="currentSolution.id"
        label="选择导出方案"
        :options="
          [...solutions].reverse().map((item) => ({ value: item.id, label: item.plan.title }))
        "
        @update:model-value="selectSolution"
      />
    </div>
    <div
      v-if="!currentSolution"
      class="card empty"
    >
      还没有可导出的方案。<button
        class="primary"
        @click="setStep(3)"
      >
        生成经营方案
      </button>
    </div>
    <template v-else>
      <p
        v-if="!currentSolution.evaluation.verified"
        class="notice"
      >
        导出内容为当前方案草稿。经营可行性与财务数值尚未核算，未确定项目会留空。
      </p>
      <div class="module-grid">
        <article class="card module-card">
          <h3>经营方案文档</h3>
          <p class="muted">策略、完整逐季操作、预算条目、风险与题目依据。</p>
          <button
            class="primary"
            :disabled="pending || operations.has('export-docx')"
            @click="run('export-docx', () => exportArtifact('docx'))"
          >
            生成 Word 方案
          </button>
        </article>
        <article class="card module-card">
          <h3>标准操作预算表</h3>
          <p class="muted">按经营期列出已有操作数量和金额，不生成缺少核算依据的报表。</p>
          <button
            class="secondary"
            :disabled="pending || operations.has('export-standard')"
            @click="run('export-standard', () => exportArtifact('xlsx'))"
          >
            生成标准 Excel
          </button>
        </article>
      </div>
      <div class="card artifact-templates">
        <div class="section-heading">
          <div>
            <h3>填写预算空表</h3>
            <p class="muted">AI 根据方案匹配空白输入格，已有公式和数据保持原样。</p>
          </div>
          <label class="review-action template-upload"
            >上传空表<input
              type="file"
              class="sr-only"
              accept=".xlsx,.xls"
              multiple
              :disabled="operations.has('upload-template')"
              @change="upload"
          /></label>
        </div>
        <p
          v-if="!templates.length"
          class="muted"
        >
          可使用经营方案页上传的空表，也可在这里上传新模板。
        </p>
        <article
          v-for="template in templates"
          :key="template.source.id"
          class="artifact-template"
        >
          <div class="section-heading">
            <div>
              <strong>{{ template.source.filename }}</strong>
              <p class="muted">
                {{
                  mappingFor(template.source.id)
                    ? mappingFor(template.source.id)?.confirmed
                      ? '对应关系已确认'
                      : '请核对条目与来源'
                    : '尚未分析条目'
                }}
              </p>
            </div>
            <button
              class="review-action"
              @click="preview = template"
            >
              预览空表
            </button>
          </div>
          <template v-if="mappingFor(template.source.id)">
            <div class="table-scroll mapping-table">
              <table>
                <thead>
                  <tr>
                    <th>项目与位置</th>
                    <th>引用值</th>
                    <th>对应依据 / 未确定原因</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(entry, index) in mappingFor(template.source.id)!.entries"
                    :key="index"
                  >
                    <td>
                      {{ entry.label }}<small>{{ entry.sheet }} · {{ entry.cell }}</small>
                    </td>
                    <td>{{ mappingFor(template.source.id)?.values[index] ?? '留空' }}</td>
                    <td>{{ entry.explanation }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p
              v-for="issue in mappingFor(template.source.id)!.issues"
              :key="issue.target + issue.code"
              :class="issue.severity === 'error' ? 'error' : 'muted'"
            >
              {{ issue.message }}
            </p>
          </template>
          <footer class="panel-footer">
            <button
              class="review-action"
              :disabled="pending || operations.has(template.source.id)"
              @click="run(template.source.id, () => analyzeTemplate(template.source.id))"
            >
              {{ mappingFor(template.source.id) ? '重新分析' : '分析条目对应关系' }}
            </button>
            <div class="artifact-actions">
              <button
                v-if="mappingFor(template.source.id)"
                class="secondary"
                @click="editing = mappingFor(template.source.id)!"
              >
                核对 / 修改</button
              ><button
                class="primary"
                :disabled="
                  pending ||
                  !mappingFor(template.source.id)?.confirmed ||
                  operations.has('fill-' + template.source.id)
                "
                @click="
                  run('fill-' + template.source.id, () =>
                    exportArtifact('xlsx', mappingFor(template.source.id)!.id),
                  )
                "
              >
                填写并生成 Excel
              </button>
            </div>
          </footer>
        </article>
      </div>
      <div class="card artifact-downloads">
        <h3>下载文件</h3>
        <p
          v-if="!visibleArtifacts.length"
          class="muted"
        >
          文件生成完成后显示在这里，任务中可查看进度。
        </p>
        <article
          v-for="artifact in [...visibleArtifacts].reverse()"
          :key="artifact.id"
          class="artifact-file"
        >
          <div>
            <strong>{{ artifact.filename }}</strong>
            <p class="muted">
              {{ artifact.draft ? '草稿' : '已验证方案' }} ·
              {{ formulaNames[artifact.formula_status] || artifact.formula_status }}
            </p>
            <p
              v-for="warning in artifact.warnings"
              :key="warning"
              class="muted"
            >
              {{ warning }}
            </p>
          </div>
          <a
            class="review-action"
            :href="`/api/projects/${project?.id}/artifacts/${artifact.id}/download`"
            download
            >下载</a
          >
        </article>
      </div>
    </template>
    <footer class="panel-footer">
      <button
        class="review-action"
        @click="setStep(3)"
      >
        ← 经营方案</button
      ><button
        class="review-action"
        @click="setStep(5)"
      >
        方案答疑 →
      </button>
    </footer>
    <MappingEditor
      :mapping="editing"
      @close="editing = null"
    /><BudgetTemplatePreview
      :template="preview"
      @close="preview = null"
    />
  </section>
</template>
