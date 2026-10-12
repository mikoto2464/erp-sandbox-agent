<script setup lang="ts">
import { computed, ref } from 'vue';
import { useWorkspaceContext } from '../composables/context';
import AppSelect from '../components/AppSelect.vue';
import FactEditor from '../components/FactEditor.vue';
import EvidencePreview from '../components/EvidencePreview.vue';
import IssueCard from '../components/IssueCard.vue';
import { labels, fieldNames, stateNames } from '../lib/labels';
import { confirmFact, factInputError, fieldConfirmation } from '../lib/review';
import type { EditableReading, Fact } from '../lib/types';
const {
  snapshot,
  editable,
  issues,
  errors,
  horizon,
  readingDirty,
  reviewTab,
  setReviewTab,
  canRead,
  activeRead,
  markReadingDirty,
  run,
  startRead,
  saveReading,
  continueToSolve,
  operations,
  notify,
} = useWorkspaceContext();
const search = ref(''),
  category = ref(''),
  state = ref(''),
  issueFilter = ref(''),
  editing = ref<Fact | null>(null);
const evidence = ref<{ ids: string[]; title: string; fact?: Fact } | null>(null),
  jsonText = ref('');
const states = Object.entries(stateNames).map(([value, label]) => ({ value, label }));
const categories = computed(() => [
  { value: '', label: '全部分类' },
  ...[...new Set(editable.value?.facts.map((fact) => fact.category) || [])].map((value) => ({
    value,
    label: labels[value] || value,
  })),
]);
const visibleFacts = computed(() => {
  const groups = new Map<string, Fact[]>();
  for (const fact of editable.value?.facts || []) {
    if (
      (category.value && fact.category !== category.value) ||
      (state.value && fact.state !== state.value) ||
      (search.value &&
        !`${fact.entity_name} ${fact.key} ${fieldNames[fact.key] || ''} ${fact.value ?? ''}`
          .toLowerCase()
          .includes(search.value.toLowerCase()))
    )
      continue;
    if (!groups.has(fact.category)) groups.set(fact.category, []);
    groups.get(fact.category)!.push(fact);
  }
  return [...groups.entries()];
});
const pendingIssues = computed(() =>
  issues.value
    .filter(
      (issue) =>
        issue.severity !== 'info' && (!issueFilter.value || issue.severity === issueFilter.value),
    )
    .sort((a, b) => Number(b.severity === 'error') - Number(a.severity === 'error')),
);
const warnings = computed(
  () => issues.value.filter((issue) => issue.severity === 'warning').length,
);
const heading = computed(() =>
  readingDirty.value
    ? '修改待校验'
    : errors.value
      ? `先处理 ${errors.value} 项错误`
      : !horizon.value
        ? '请修正经营周期'
        : '可以生成方案草稿',
);
const ruleConfirmed = ref(new Set<string>());
function showEvidence(ids: string[], title: string, fact?: Fact) {
  evidence.value = { ids, title, fact };
}
function confirm(fact: Fact) {
  const error = factInputError(fact);
  if (error) {
    notify(error, 'error');
    return;
  }
  confirmFact(editable.value!, fact);
  markReadingDirty();
  notify('字段已确认，保存后生效。');
}
function isConfirmed(fact: Fact) {
  return editable.value!.issues.some(
    (issue) =>
      issue.code === 'review.confirmed' &&
      issue.target === fact.id &&
      issue.message === fieldConfirmation(fact),
  );
}
function addRule() {
  let id = 'review_rule';
  while (editable.value!.rules.some((rule) => rule.id === id)) id += '_';
  editable.value!.rules.push({
    id,
    name: '待补充规则',
    expression: '',
    state: 'assumed',
    evidence_ids: [],
    explanation: '',
  });
  markReadingDirty();
}
function confirmRule(id: string) {
  const rule = editable.value!.rules.find((rule) => rule.id === id)!;
  if (!rule.expression.trim()) {
    notify('请先填写规则内容。');
    return;
  }
  rule.explanation = rule.explanation.trim() || '默认';
  if (rule.state !== 'known') rule.state = 'assumed';
  ruleConfirmed.value.add(id);
  markReadingDirty();
  notify('规则已确认，保存后生效。');
}
function openAdvanced() {
  jsonText.value = JSON.stringify(editable.value, null, 2);
}
function applyJSON() {
  try {
    const value = JSON.parse(jsonText.value) as EditableReading;
    if (
      !Array.isArray(value.facts) ||
      !Array.isArray(value.rules) ||
      !Array.isArray(value.issues) ||
      typeof value.title !== 'string'
    )
      throw new Error('必须包含 title、facts、rules、issues。');
    editable.value = value;
    markReadingDirty();
    notify('修改已应用，请保存校验。');
  } catch (error) {
    notify(error instanceof Error ? error.message : 'JSON 无效。', 'error');
  }
}
</script>
<template>
  <section>
    <div class="review-toolbar">
      <div>
        <h2>确认题目</h2>
        <span class="muted">{{ snapshot?.reading.problem.title }}</span>
      </div>
      <button
        class="secondary"
        :disabled="!canRead || readingDirty || operations.has('read')"
        @click="run('read', startRead)"
      >
        {{ activeRead ? '正在读题…' : '重新读取' }}
      </button>
    </div>
    <div
      v-if="!snapshot || !editable"
      class="card empty"
    >
      上传文件或输入题目后，点击“读取题目”。
    </div>
    <template v-else
      ><div class="card review-guide">
        <div class="section-heading">
          <div>
            <h2>{{ heading }}</h2>
            <p class="muted">
              {{
                readingDirty
                  ? '修改尚未保存，保存后重新校验。'
                  : errors
                    ? '错误会阻止方案生成，请修正后保存。'
                    : `${warnings} 项提示；方案为草稿，经营数值尚未验证。`
              }}
            </p>
          </div>
          <span
            class="badge"
            :class="errors || readingDirty || !horizon ? 'conflicted' : 'known'"
            >{{ readingDirty ? '待保存' : errors || !horizon ? '待处理' : '结构校验通过' }}</span
          >
        </div>
        <div class="review-shortcuts">
          <button
            class="review-action"
            @click="setReviewTab('issues')"
          >
            待确认事项 {{ issues.filter((item) => item.severity !== 'info').length }}</button
          ><button
            class="review-action"
            @click="setReviewTab('facts')"
          >
            题目数据 {{ editable.facts.length }}</button
          ><button
            class="review-action"
            @click="setReviewTab('rules')"
          >
            经营规则 {{ editable.rules.length }}
          </button>
        </div>
      </div>
      <div class="metrics">
        <div>
          <strong>{{ editable.facts.length }}</strong
          ><span>已提取数据</span>
        </div>
        <div>
          <strong>{{ errors }}</strong
          ><span>需要修正</span>
        </div>
        <div>
          <strong>{{ warnings }}</strong
          ><span>待确认数据</span>
        </div>
        <div>
          <strong>{{ editable.rules.length }}</strong
          ><span>规则</span>
        </div>
      </div>
      <div class="card review-card">
        <div class="review-tabs">
          <button
            :class="{ active: reviewTab === 'issues' }"
            @click="setReviewTab('issues')"
          >
            待确认</button
          ><button
            :class="{ active: reviewTab === 'facts' }"
            @click="setReviewTab('facts')"
          >
            题目数据</button
          ><button
            :class="{ active: reviewTab === 'rules' }"
            @click="setReviewTab('rules')"
          >
            经营规则
          </button>
        </div>
        <div v-if="reviewTab === 'issues'">
          <div class="filters">
            <AppSelect
              v-model="issueFilter"
              label="问题类型"
              :options="[
                { value: '', label: '全部待核对' },
                { value: 'error', label: '阻止生成' },
                { value: 'warning', label: '需核对 / 可作为假设' },
              ]"
            /><span class="muted">{{
              readingDirty ? '修改待保存' : '错误必须修正，提示需明确依据或假设'
            }}</span>
          </div>
          <IssueCard
            v-for="(issue, index) in pendingIssues"
            :key="`${issue.code}:${issue.target}:${index}`"
            :issue="issue"
            @edit="editing = $event"
            @evidence="showEvidence"
            @rule="setReviewTab('rules')"
          />
          <p
            v-if="!pendingIssues.length"
            class="empty"
          >
            没有待处理的问题，可继续生成方案。
          </p>
        </div>
        <div v-else-if="reviewTab === 'facts'">
          <div class="filters">
            <input
              v-model="search"
              type="search"
              placeholder="搜索对象或字段"
              aria-label="搜索字段"
            /><AppSelect
              v-model="category"
              label="字段分类"
              :options="categories"
            /><AppSelect
              v-model="state"
              label="字段状态"
              :options="[{ value: '', label: '全部状态' }, ...states]"
            /><button
              class="review-action"
              @click="search = category = state = ''"
            >
              清除筛选
            </button>
          </div>
          <p class="muted">
            默认经营 4 年、每年 4 季，无需确认；特殊周期可在数据中修改。百分数以比例保存，如 50% 为
            0.5。
          </p>
          <div class="table-wrap">
            <table class="fact-table">
              <thead>
                <tr>
                  <th>对象 / 字段</th>
                  <th>值</th>
                  <th>单位</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <template
                  v-for="[group, facts] in visibleFacts"
                  :key="group"
                  ><tr class="group-row">
                    <td colspan="5">{{ labels[group] || group }} · {{ facts.length }}</td>
                  </tr>
                  <tr
                    v-for="fact in facts"
                    :key="fact.id"
                  >
                    <td>
                      <strong>{{ fact.entity_name }}</strong
                      ><small>{{ fieldNames[fact.key] || fact.key }}</small
                      ><small v-if="fact.explanation">{{ fact.explanation }}</small>
                    </td>
                    <td>
                      <input
                        :value="fact.value ?? ''"
                        :aria-label="`${fact.entity_name} ${fact.key}的值`"
                        placeholder="未提供"
                        @input="
                          fact.value = ($event.target as HTMLInputElement).value || null;
                          if (fact.value === null) fact.state = 'missing';
                          markReadingDirty();
                        "
                      />
                    </td>
                    <td>
                      <input
                        v-model="fact.unit"
                        :aria-label="`${fact.key}单位`"
                        @input="markReadingDirty"
                      />
                    </td>
                    <td>
                      <AppSelect
                        v-model="fact.state"
                        :label="`${fact.key}状态`"
                        :options="states"
                        @update:model-value="markReadingDirty"
                      />
                    </td>
                    <td>
                      <div class="review-actions">
                        <button
                          class="review-action"
                          @click="editing = fact"
                        >
                          确认 / 修改</button
                        ><button
                          class="review-action"
                          @click="
                            showEvidence(
                              fact.evidence_ids,
                              `${fact.entity_name} · ${fieldNames[fact.key] || fact.key}`,
                              fact,
                            )
                          "
                        >
                          查看原文</button
                        ><button
                          v-if="
                            ['years', 'periods_per_year'].includes(fact.key) &&
                            !fact.explanation.startsWith('系统默认：')
                          "
                          class="review-action"
                          :disabled="isConfirmed(fact)"
                          @click="confirm(fact)"
                        >
                          {{ isConfirmed(fact) ? '已确认' : '确认' }}
                        </button>
                      </div>
                    </td>
                  </tr></template
                >
              </tbody>
            </table>
          </div>
          <p
            v-if="!visibleFacts.length"
            class="empty"
          >
            没有匹配字段。
          </p>
        </div>
        <div v-else>
          <div class="section-heading rules-heading">
            <p class="muted">规则已提取不代表经营仿真已经支持。</p>
            <button
              class="review-action"
              @click="addRule"
            >
              补充规则
            </button>
          </div>
          <article
            v-for="rule in editable.rules"
            :key="rule.id"
            class="rule-item"
          >
            <div class="rule-title">
              <input
                v-model="rule.name"
                aria-label="规则名称"
                @input="markReadingDirty"
              /><AppSelect
                v-model="rule.state"
                label="规则状态"
                :options="states"
                @update:model-value="markReadingDirty"
              /><button
                class="review-action"
                @click="showEvidence(rule.evidence_ids, rule.name)"
              >
                查看原文
              </button>
            </div>
            <textarea
              v-model="rule.expression"
              rows="2"
              aria-label="规则内容"
              @input="
                rule.state = 'assumed';
                ruleConfirmed.delete(rule.id);
                markReadingDirty();
              "
            /><textarea
              v-model="rule.explanation"
              rows="2"
              placeholder="说明与适用范围"
              aria-label="规则说明"
              @input="markReadingDirty"
            /><button
              class="review-action"
              @click="confirmRule(rule.id)"
            >
              {{ ruleConfirmed.has(rule.id) ? '已确认' : '确认规则' }}
            </button>
          </article>
          <p
            v-if="!editable.rules.length"
            class="empty"
          >
            未提取到独立规则，可补充或重新读题。
          </p>
        </div>
        <details
          class="advanced"
          @toggle="($event.target as HTMLDetailsElement).open && openAdvanced()"
        >
          <summary>高级编辑</summary>
          <textarea
            v-model="jsonText"
            rows="16"
            spellcheck="false"
            aria-label="结构化读题数据"
          /><button
            class="review-action"
            @click="applyJSON"
          >
            应用修改
          </button>
        </details>
        <footer class="panel-footer">
          <span class="muted">{{ readingDirty ? '有未保存修改' : '暂定内容会随方案保留' }}</span>
          <div class="actions">
            <button
              class="secondary"
              :disabled="!readingDirty || operations.has('revision')"
              @click="run('revision', saveReading)"
            >
              保存修改</button
            ><button
              class="primary"
              :disabled="operations.has('continue') || (!readingDirty && (!!errors || !horizon))"
              @click="run('continue', continueToSolve)"
            >
              {{ readingDirty ? '保存并继续 →' : '继续生成方案 →' }}
            </button>
          </div>
        </footer>
      </div></template
    ><FactEditor
      :fact="editing"
      @close="editing = null"
    /><EvidencePreview
      :open="!!evidence"
      :ids="evidence?.ids || []"
      :title="evidence?.title || '原文证据'"
      :fact="evidence?.fact"
      @close="evidence = null"
    />
  </section>
</template>
