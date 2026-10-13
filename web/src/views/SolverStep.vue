<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import AppSelect from '../components/AppSelect.vue';
import BudgetTemplatePreview from '../components/BudgetTemplatePreview.vue';
import type { BudgetTemplate, PlanTone } from '../lib/types';
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
      : '生成结果为协作规划草稿，产能、现金与履约尚未经过经营仿真验证。',
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
            <p class="muted">这里只显示读题阶段已核对的空表。新增空表请返回第一步读取。</p>
          </div>
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
        </div>
        <p
          v-if="!availableTemplates.length"
          class="muted"
        >
          请在第一步上传空表，并在第二步核对后使用；不使用空表也可生成方案。
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
