<script setup lang="ts">
import { computed, ref } from 'vue';
import AppSelect from '../components/AppSelect.vue';
import MappingEditor from '../components/MappingEditor.vue';
import MappingReview from '../components/MappingReview.vue';
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
  confirmMapping,
  exportArtifact,
  setStep,
  project,
} = useWorkspaceContext();
const editing = ref<WorkbookMapping | null>(null),
  preview = ref<BudgetTemplate | null>(null),
  previewMapping = ref<WorkbookMapping | null>(null),
  editingIndex = ref(0);
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
const plannedTemplates = computed(() =>
  templates.value.filter((template) =>
    currentSolution.value?.generation?.template_ids.includes(template.source.id),
  ),
);
function openPreview(template: BudgetTemplate, filled = false) {
  preview.value = template;
  previewMapping.value = filled ? mappingFor(template.source.id) || null : null;
}
function editMapping(mapping: WorkbookMapping, index = 0) {
  editingIndex.value = index;
  editing.value = mapping;
}
const formulaNames: Record<string, string> = {
  pending: 'Excel 打开后自动计算',
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
            <p class="muted">使用生成方案时选定的空表，填写值与方案预算保持一致。</p>
          </div>
        </div>
        <p
          v-if="!plannedTemplates.length"
          class="muted"
        >
          这份方案未选择预算空表。请先在空表库解读、核对并导入，再选择空表生成经营方案。
        </p>
        <article
          v-for="template in plannedTemplates"
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
                      : '请确认填写结果'
                    : '按方案预算准备填写'
                }}
              </p>
            </div>
            <button
              class="review-action"
              @click="openPreview(template)"
            >
              预览空表
            </button>
          </div>
          <template v-if="mappingFor(template.source.id)">
            <MappingReview
              :mapping="mappingFor(template.source.id)!"
              @edit="editMapping(mappingFor(template.source.id)!, $event)"
            />
          </template>
          <footer class="panel-footer">
            <button
              class="review-action"
              :disabled="pending || operations.has(template.source.id)"
              @click="run(template.source.id, () => analyzeTemplate(template.source.id))"
            >
              {{ mappingFor(template.source.id) ? '重新核对方案预算' : '准备填写' }}
            </button>
            <div class="artifact-actions">
              <button
                v-if="mappingFor(template.source.id)"
                class="review-action"
                @click="openPreview(template, true)"
              >
                预览填写结果
              </button>
              <button
                v-if="mappingFor(template.source.id)"
                class="secondary"
                :disabled="
                  pending ||
                  operations.has('confirm-' + template.source.id) ||
                  mappingFor(template.source.id)!.issues.some((issue) => issue.severity === 'error')
                "
                @click="
                  run('confirm-' + template.source.id, () =>
                    confirmMapping(
                      mappingFor(template.source.id)!,
                      mappingFor(template.source.id)!.entries,
                    ),
                  )
                "
              >
                {{ mappingFor(template.source.id)?.confirmed ? '已确认' : '确认填写' }}</button
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
      :initial-index="editingIndex"
      @close="editing = null"
    /><BudgetTemplatePreview
      :template="preview"
      :mapping="previewMapping"
      @close="preview = null"
    />
  </section>
</template>
