<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import AppSelect from '../components/AppSelect.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import UploadDeleteButton from '../components/UploadDeleteButton.vue';
import type { BudgetTemplate, PlanTone } from '../lib/types';
const {
  snapshot,
  solutions,
  solverMode,
  seed,
  candidates,
  status,
  inputChanged,
  errors,
  activeSolve,
  operations,
  run,
  solve,
  setStep,
  planTone,
  customPrompt,
  templates,
  templateIds,
  templateParsing,
  generationReady,
  uploadTemplates,
  currentSolution,
  selectSolution,
  jobs,
} = useWorkspaceContext();
const tones: { value: PlanTone; label: string; description: string }[] = [
  { value: 'conservative', label: '保守', description: '控制投入，优先现金安全' },
  { value: 'balanced', label: '平稳', description: '兼顾周转与经营增长' },
  { value: 'aggressive', label: '激进', description: '提高投入，争取更高收益' },
  { value: 'custom', label: '其他', description: '自行描述经营偏好' },
];
const revisionPrompt = ref(''),
  preview = ref<BudgetTemplate | null>(null);
watch(
  () => currentSolution.value?.id,
  () => {
    revisionPrompt.value = '';
  },
);
const note = computed(() =>
  inputChanged.value
    ? '题目输入或说明已修改，请重新读取。'
    : errors.value
      ? '题目有错误，请修正并保存。'
      : solverMode.value === 'llm_monte_carlo'
        ? status.value?.simulation_ready
          ? '在仿真器中搜索并比较候选。'
          : '经营仿真内核尚未就绪，暂不能使用蒙特卡罗。'
        : '生成结果为草稿，产能、现金与履约尚未经过经营仿真验证。',
);
const periods = computed(() =>
  [...new Set(currentSolution.value?.plan.actions.map((action) => action.period) || [])].sort(
    (a, b) => a - b,
  ),
);
const previousLabel = computed(() => {
  const previousId = currentSolution.value?.generation?.previous_solution_id;
  return previousId
    ? solutions.value.find((item) => item.id === previousId)?.plan.title || '此前方案'
    : '';
});
const templateJobs = computed(() => {
  const entries = new Map<string, (typeof jobs.value)[number]>();
  for (const job of jobs.value)
    if (
      job.kind === 'template' &&
      job.metadata.template_id &&
      !job.metadata.upload_deleted &&
      !templates.value.some((item) => item.source.id === job.metadata.template_id)
    )
      entries.set(job.metadata.template_id, job);
  return [...entries.values()];
});
function upload(event: Event) {
  const input = event.target as HTMLInputElement,
    files = [...(input.files || [])];
  input.value = '';
  if (files.length) void run('upload-template', () => uploadTemplates(files));
}
function toggleTemplate(id: string) {
  if (templateIds.value.includes(id))
    templateIds.value = templateIds.value.filter((item) => item !== id);
  else if (templateIds.value.length < 3) templateIds.value.push(id);
}
function periodLabel(period: number) {
  const count = snapshot.value?.reading.problem.periods_per_year || 4;
  return `第 ${Math.floor((period - 1) / count) + 1} 年 · 第 ${((period - 1) % count) + 1} 季`;
}
async function revise() {
  const id = currentSolution.value?.id;
  if (id) await solve(id, revisionPrompt.value);
}
</script>
<template>
  <section class="solver-step">
    <div class="card">
      <div class="section-heading">
        <h2>生成经营方案</h2>
        <span class="muted">{{
          snapshot
            ? `${snapshot.reading.problem.years} 年 · 每年 ${snapshot.reading.problem.periods_per_year} 季`
            : '请先读取题目'
        }}</span>
      </div>
      <div class="strategy-options">
        <label class="strategy-option"
          ><input
            v-model="solverMode"
            type="radio"
            value="llm_only"
            :disabled="activeSolve"
          /><span><strong>直接生成</strong><small>单 LLM · 方案草稿</small></span></label
        >
        <label class="strategy-option"
          ><input
            v-model="solverMode"
            type="radio"
            value="llm_monte_carlo"
            :disabled="activeSolve"
          /><span><strong>搜索更优方案</strong><small>LLM + 蒙特卡罗</small></span></label
        >
      </div>
      <div class="generation-section">
        <h3>方案基调</h3>
        <div
          class="tone-options"
          role="radiogroup"
          aria-label="方案基调"
        >
          <label
            v-for="tone in tones"
            :key="tone.value"
            class="tone-option"
            ><input
              v-model="planTone"
              type="radio"
              :value="tone.value"
              :disabled="activeSolve"
            /><span
              ><strong>{{ tone.label }}</strong
              ><small>{{ tone.description }}</small></span
            ></label
          >
        </div>
        <label
          v-if="planTone === 'custom'"
          class="custom-tone"
          >自定义要求<textarea
            v-model="customPrompt"
            rows="3"
            maxlength="10000"
            placeholder="例如：尽量少借款，优先发展本地市场，前两年保留现金。"
            :disabled="activeSolve"
          />
        </label>
      </div>
      <div
        v-if="solverMode === 'llm_monte_carlo'"
        class="form-grid"
      >
        <label
          >随机种子<input
            v-model.number="seed"
            type="number"
            min="0"
            max="4294967295" /></label
        ><label
          >候选数量<input
            v-model.number="candidates"
            type="number"
            min="1"
            max="2000"
        /></label>
      </div>
      <div
        v-if="solverMode === 'llm_only'"
        class="generation-section"
      >
        <div class="section-heading">
          <div>
            <h3>预算空表 <span class="muted">选填</span></h3>
            <p class="muted">
              先选空表，再按表中的项目、期间和单位生成方案。更换空表需要重新生成。
            </p>
          </div>
          <label
            class="review-action template-upload"
            :class="{ disabled: activeSolve || operations.has('upload-template') }"
            >上传空表<input
              type="file"
              accept=".xlsx,.xls"
              multiple
              class="sr-only"
              :disabled="activeSolve || operations.has('upload-template')"
              @change="upload"
          /></label>
        </div>
        <div
          v-for="template in templates"
          :key="template.source.id"
          class="budget-template-row"
        >
          <label class="check"
            ><input
              type="checkbox"
              :checked="templateIds.includes(template.source.id)"
              :disabled="
                activeSolve ||
                (!templateIds.includes(template.source.id) && templateIds.length >= 3)
              "
              @change="toggleTemplate(template.source.id)"
            /><span
              ><strong>{{ template.source.filename }}</strong
              ><small>{{ template.sheets.map((sheet) => sheet.name).join(' · ') }}</small></span
            ></label
          ><button
            class="review-action"
            @click="preview = template"
          >
            预览
          </button>
          <UploadDeleteButton
            :id="template.source.id"
            :filename="template.source.filename"
            kind="template"
          />
        </div>
        <div
          v-for="job in templateJobs"
          :key="job.id"
          class="budget-template-row"
        >
          <span class="muted"
            >{{ job.metadata.filename }} ·
            {{
              job.status === 'failed'
                ? '解析失败，可在任务中重试'
                : job.status === 'cancelled'
                  ? '已取消'
                  : '正在解析…'
            }}</span
          ><button
            v-if="
              job.metadata.template_id &&
              templateIds.includes(job.metadata.template_id) &&
              ['failed', 'cancelled'].includes(job.status)
            "
            class="review-action"
            @click="toggleTemplate(job.metadata.template_id)"
          >
            不使用
          </button>
          <UploadDeleteButton
            v-if="job.metadata.template_id"
            :id="job.metadata.template_id"
            :filename="job.metadata.filename || '预算空表'"
            kind="template"
          />
        </div>
        <p
          v-if="!templates.length && !templateParsing"
          class="muted"
        >
          支持 XLSX / XLS，最多选择 3 份。不上传则按题目生成。
        </p>
      </div>
      <p class="notice">{{ note }}</p>
      <footer class="panel-footer">
        <button
          class="review-action"
          @click="setStep(2)"
        >
          ← 返回题目</button
        ><button
          class="primary"
          :disabled="!generationReady || operations.has('solve')"
          @click="run('solve', () => solve())"
        >
          {{ activeSolve ? '正在生成…' : solutions.length ? '重新生成方案' : '生成方案' }}
        </button>
      </footer>
    </div>
    <div class="section-heading result-heading">
      <h2>经营计划</h2>
      <button
        class="review-action"
        @click="setStep(4)"
      >
        方案与表格 →
      </button>
    </div>
    <div
      v-if="!currentSolution"
      class="card empty"
    >
      还没有经营计划。
    </div>
    <article
      v-else
      class="card solution-card"
    >
      <div class="section-heading">
        <h2>{{ currentSolution.plan.title }}</h2>
        <AppSelect
          v-if="solutions.length > 1"
          :model-value="currentSolution.id"
          label="选择经营方案"
          :options="
            [...solutions].reverse().map((solution, index) => ({
              value: solution.id,
              label: `${solution.plan.title}${index === 0 ? ' · 最新' : ''}`,
            }))
          "
          @update:model-value="selectSolution"
        />
      </div>
      <p class="muted">
        {{ currentSolution.mode === 'llm_only' ? '直接生成 · AI 草稿' : '搜索生成 · 候选比较' }} ·
        {{
          tones.find((tone) => tone.value === (currentSolution?.generation?.tone || 'balanced'))
            ?.label
        }}{{ previousLabel ? ` · 基于「${previousLabel}」调整` : '' }}
      </p>
      <p
        v-if="snapshot && currentSolution.problem_version !== snapshot.version"
        class="notice"
      >
        题目已调整，此计划基于旧数据，请重新生成。
      </p>
      <p class="notice">
        {{
          currentSolution.evaluation.verified
            ? `已完成计算核对 · 得分 ${currentSolution.evaluation.score}`
            : '草稿 · 经营数值尚未经过仿真核对'
        }}
      </p>
      <p>{{ currentSolution.plan.summary }}</p>
      <details
        class="period-plan"
        open
      >
        <summary>
          逐期操作 · {{ periods.length }} 期 / {{ currentSolution.plan.actions.length }} 项
        </summary>
        <section
          v-for="period in periods"
          :key="period"
          class="period-group"
        >
          <h3>{{ periodLabel(period) }}</h3>
          <div
            v-for="(action, index) in currentSolution.plan.actions.filter(
              (action) => action.period === period,
            )"
            :key="index"
            class="plan-action"
          >
            <p>{{ action.description }}</p>
            <span class="action-values"
              >{{ action.quantity !== null ? `数量 ${action.quantity}` : '' }}
              {{ action.amount !== null ? `金额 ${action.amount}` : '' }}</span
            >
          </div>
        </section>
      </details>
      <details
        v-if="currentSolution.plan.budget_items?.length"
        class="plan-notes budget-plan"
      >
        <summary>按空表规划的预算 · {{ currentSolution.plan.budget_items.length }} 项</summary>
        <p class="muted">
          {{
            templates
              .filter((template) =>
                currentSolution?.generation?.template_ids.includes(template.source.id),
              )
              .map((template) => template.source.filename)
              .join('、')
          }}。填写时使用这份方案的预算，未确定项目留空。
        </p>
        <div class="table-scroll">
          <table>
            <thead>
              <tr>
                <th>预算项目</th>
                <th>期间</th>
                <th>规划值</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(item, index) in currentSolution.plan.budget_items"
                :key="index"
              >
                <td>
                  <strong>{{ item.label }}</strong
                  ><small>{{ item.sheet }} · {{ item.cell }}</small>
                </td>
                <td>{{ item.period ? periodLabel(item.period) : '汇总 / 未确定' }}</td>
                <td>
                  {{ item.value ?? '待确定'
                  }}{{ item.value !== null && item.unit ? ` ${item.unit}` : '' }}
                </td>
                <td>{{ item.explanation }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      <details
        v-for="[title, values] in [
          ['默认与暂定条件', currentSolution.plan.assumptions],
          ['风险提醒', currentSolution.plan.risks],
          ['检查结果', currentSolution.evaluation.issues.map((item) => item.message)],
        ] as [string, string[]][]"
        :key="title"
        class="plan-notes"
      >
        <summary>{{ title }} · {{ values.length }}</summary>
        <ul>
          <li
            v-for="(text, index) in values"
            :key="index"
          >
            {{ text }}
          </li>
        </ul>
      </details>
      <div
        v-if="solverMode === 'llm_only'"
        class="generation-section revision-section"
      >
        <h3>调整这份方案</h3>
        <p class="muted">填写新的要求，保留当前方案并生成下一版。上方基调与所选预算表同样生效。</p>
        <label
          >修改要求<textarea
            v-model="revisionPrompt"
            rows="3"
            maxlength="10000"
            placeholder="例如：第一年暂不增加产线，减少借款，并重新安排后续生产与交付。"
            :disabled="activeSolve"
          />
        </label>
        <footer class="panel-footer">
          <span class="muted">{{
            currentSolution.generation?.revision_prompt
              ? `上次要求：${currentSolution.generation.revision_prompt}`
              : ''
          }}</span
          ><button
            class="primary"
            :disabled="
              !generationReady ||
              !revisionPrompt.trim() ||
              operations.has('solve') ||
              currentSolution.problem_version !== snapshot?.version
            "
            @click="run('solve', revise)"
          >
            {{ activeSolve ? '正在生成…' : '按要求调整方案' }}
          </button>
        </footer>
      </div>
    </article>
    <BudgetTemplatePreview
      :template="preview"
      @close="preview = null"
    />
  </section>
</template>
