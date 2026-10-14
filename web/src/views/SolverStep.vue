<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import AppSelect from '../components/AppSelect.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import UploadDeleteButton from '../components/UploadDeleteButton.vue';
import TemplateLibraryPicker from '../components/TemplateLibraryPicker.vue';
import type { BudgetTemplate, PlanTone } from '../lib/types';
import { actionKindNames, amountMeaningNames } from '../lib/labels';
const {
  snapshot,
  solutions,
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
  generationReady,
  currentSolution,
  selectSolution,
  retryBudget,
} = useWorkspaceContext();
const availableTemplates = computed(() =>
  templates.value.filter((template) =>
    snapshot.value?.template_readings?.some(
      (reading) =>
        reading.template_id === template.source.id &&
        reading.reviewed &&
        reading.sha256 === template.source.sha256,
    ),
  ),
);
const tones: { value: PlanTone; label: string; description: string }[] = [
  { value: 'conservative', label: '保守', description: '控制投入，优先现金安全' },
  { value: 'balanced', label: '平稳', description: '兼顾周转与经营增长' },
  { value: 'aggressive', label: '激进', description: '提高投入，争取更高收益' },
  { value: 'custom', label: '其他', description: '自行描述经营偏好' },
];
const revisionDrafts = new Map<string, string>();
const revisionPrompt = ref(''),
  preview = ref<BudgetTemplate | null>(null);
watch(
  () => currentSolution.value?.id,
  (id, previous) => {
    if (previous) revisionDrafts.set(previous, revisionPrompt.value);
    revisionPrompt.value = id ? revisionDrafts.get(id) || '' : '';
  },
);
const note = computed(() =>
  inputChanged.value
    ? '题目输入或说明已修改，请重新读取。'
    : errors.value
      ? '题目有错误，请修正并保存。'
      : '生成结果为协作规划草稿，产能、现金与履约尚未经过经营仿真验证。',
);
const stageNames: Record<string, string> = {
  not_started: '尚未生成',
  not_applicable: '未选空表',
  queued: '等待处理',
  running: '处理中',
  partial: '部分预算可用',
  succeeded: '已完成',
  failed: '处理失败',
  cancelled: '已取消',
  draft: '行动草稿',
};
const budgetStage = computed(() => currentSolution.value?.stages?.budget);
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
      <div class="generation-section">
        <div class="section-heading">
          <div>
            <h3>预算空表 <span class="muted">选填</span></h3>
            <p class="muted">选择已有空表，或从空表库导入已处理的表格。</p>
          </div>
          <TemplateLibraryPicker />
        </div>
        <div
          v-for="template in availableTemplates"
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
        <p
          v-if="!availableTemplates.length"
          class="muted"
        >
          在空表库完成上传、解读和核对后导入；不使用空表也可生成方案。
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
        协作生成 · AI 草稿 ·
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
      <div
        v-if="currentSolution.stages"
        class="generation-section"
      >
        <h3>处理状态</h3>
        <p class="muted">
          行动草稿已保存 · 预算：{{
            stageNames[budgetStage?.status || 'not_started'] || budgetStage?.status
          }}
          · 报告：{{
            stageNames[currentSolution.stages.report.status] || currentSolution.stages.report.status
          }}
        </p>
        <p
          v-if="budgetStage?.error"
          class="notice"
        >
          {{ budgetStage.error.message }} 已完成的行动与预算仍保留。
        </p>
        <button
          v-if="['failed', 'cancelled'].includes(budgetStage?.status || '')"
          class="secondary"
          :disabled="operations.has('retry-budget')"
          @click="run('retry-budget', retryBudget)"
        >
          继续预算处理
        </button>
        <details
          v-if="budgetStage?.result?.gaps?.length"
          class="plan-notes"
        >
          <summary>预算缺口 · {{ budgetStage.result.gaps.length }} 项</summary>
          <p
            v-for="gap in budgetStage.result.gaps"
            :key="gap.id"
          >
            {{ gap.message
            }}<small
              >影响 {{ gap.targets.length }} 格：{{
                gap.targets.map((t) => `${t.sheet} · ${t.cell}`).join('、')
              }}</small
            >
          </p>
        </details>
      </div>
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
            v-for="action in currentSolution.plan.actions.filter(
              (action) => action.period === period,
            )"
            :key="action.action_id || action.description"
            class="plan-action"
          >
            <p>
              <small
                >{{ action.action_id }} ·
                {{ actionKindNames[action.kind || ''] || action.kind }}</small
              >{{ action.description }}
            </p>
            <p
              v-if="action.depends_on?.length"
              class="muted"
            >
              前置行动：{{ action.depends_on.join('、') }}
            </p>
            <p
              v-if="action.completion_period || action.payment_period"
              class="muted"
            >
              {{ action.completion_period ? `完成：${periodLabel(action.completion_period)}` : '' }}
              {{
                action.payment_period ? `支付 / 收款：${periodLabel(action.payment_period)}` : ''
              }}
            </p>
            <p
              v-if="action.missing_reason"
              class="muted"
            >
              未确定：{{ action.missing_reason }}
            </p>
            <span class="action-values"
              >{{
                action.quantity !== null
                  ? `数量 ${action.quantity} ${action.quantity_unit || ''}`
                  : ''
              }}
              {{
                action.amount !== null
                  ? `金额 ${action.amount} ${action.amount_unit || ''} · ${action.amount_meaning || ''}`
                  : ''
              }}</span
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
      <div class="generation-section revision-section">
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
