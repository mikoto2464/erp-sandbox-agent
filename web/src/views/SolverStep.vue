<script setup lang="ts">
import { computed } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import type { Solution } from '../lib/types';
const {
  snapshot,
  solutions,
  solverMode,
  seed,
  candidates,
  status,
  inputChanged,
  errors,
  canSolve,
  activeSolve,
  operations,
  run,
  solve,
  setStep,
} = useWorkspaceContext();
const note = computed(() =>
  inputChanged.value
    ? '题目输入或说明已修改，请重新读取。'
    : errors.value
      ? '题目有错误，请修正并保存。'
      : solverMode.value === 'llm_monte_carlo'
        ? status.value?.simulation_ready
          ? '在仿真器中搜索并比较候选。'
          : '经营仿真内核尚未就绪，暂不能使用蒙特卡罗。'
        : '当前为方案草稿，产能、现金与履约尚未经过经营仿真验证。',
);
function periods(solution: Solution) {
  return [...new Set(solution.plan.actions.map((action) => action.period))].sort((a, b) => a - b);
}
</script>
<template>
  <section>
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
          /><span><strong>直接生成</strong><small>单 LLM · AI 计划草稿</small></span></label
        ><label class="strategy-option"
          ><input
            v-model="solverMode"
            type="radio"
            value="llm_monte_carlo"
          /><span><strong>搜索更优方案</strong><small>LLM + 蒙特卡罗</small></span></label
        >
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
      <p class="notice">{{ note }}</p>
      <footer class="panel-footer">
        <button
          class="review-action"
          @click="setStep(2)"
        >
          ← 返回题目</button
        ><button
          class="primary"
          :disabled="!canSolve || operations.has('solve')"
          @click="run('solve', solve)"
        >
          {{ activeSolve ? '正在生成…' : '生成方案草稿' }}
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
      v-if="!solutions.length"
      class="card empty"
    >
      还没有经营计划。
    </div>
    <article
      v-for="(solution, index) in [...solutions].reverse()"
      :key="solution.id"
      class="card solution-card"
    >
      <h2>{{ solution.plan.title }}</h2>
      <p class="muted">
        {{ solution.mode === 'llm_only' ? '直接生成 · AI 草稿' : '搜索生成 · 候选比较' }}
      </p>
      <p
        v-if="snapshot && solution.problem_version !== snapshot.version"
        class="notice"
      >
        题目已调整，此计划基于调整前的数据，建议重新生成。
      </p>
      <p class="notice">
        {{
          solution.evaluation.verified
            ? `已完成计算核对 · 得分 ${solution.evaluation.score}`
            : '草稿 · 经营数值尚未经过仿真核对'
        }}
      </p>
      <p>{{ solution.plan.summary }}</p>
      <details
        class="period-plan"
        :open="index === 0"
      >
        <summary>
          逐期操作 · {{ periods(solution).length }} 期 / {{ solution.plan.actions.length }} 项
        </summary>
        <section
          v-for="period in periods(solution)"
          :key="period"
          class="period-group"
        >
          <h3>第 {{ period }} 期</h3>
          <div
            v-for="(action, i) in solution.plan.actions.filter(
              (action) => action.period === period,
            )"
            :key="i"
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
        v-for="[title, values] in [
          ['默认与暂定条件', solution.plan.assumptions],
          ['风险提醒', solution.plan.risks],
          ['检查结果', solution.evaluation.issues.map((item) => item.message)],
        ] as [string, string[]][]"
        :key="title"
        class="plan-notes"
      >
        <summary>{{ title }} · {{ values.length }}</summary>
        <ul>
          <li
            v-for="(text, i) in values"
            :key="i"
          >
            {{ text }}
          </li>
        </ul>
      </details>
    </article>
  </section>
</template>
