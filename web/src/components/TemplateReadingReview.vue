<script setup lang="ts">
import { computed, ref } from 'vue';
import AppSelect from './AppSelect.vue';
import TemplateIssueCard from './TemplateIssueCard.vue';
import type { BudgetTemplate, Issue, TemplateReading } from '../lib/types';
const props = defineProps<{ template: BudgetTemplate; reading: TemplateReading; busy: boolean }>();
const emit = defineEmits<{
  confirm: [explanation: string];
  confirmIssue: [issue: Issue, answer: string];
  preview: [];
}>();
const pending = computed(() => props.reading.issues.filter((issue) => issue.severity !== 'info'));
function issueKey(issue: Issue) {
  return JSON.stringify([
    issue.code,
    issue.severity,
    issue.target,
    issue.message,
    issue.evidence_ids,
  ]);
}
function reviewFor(issue: Issue) {
  return props.reading.issue_reviews?.find((review) => issueKey(review.issue) === issueKey(issue));
}
const changed = ref<Record<string, boolean>>({});
const unresolved = computed(() =>
  pending.value.filter(
    (issue) => !reviewFor(issue)?.answer.trim() || changed.value[issueKey(issue)],
  ),
);
const tab = ref(unresolved.value.length ? 'issues' : 'data'),
  search = ref(''),
  sheetName = ref(''),
  explanation = ref(props.reading.explanation);
const groups = computed(() =>
  props.reading.sheets.flatMap((sheet) =>
    sheet.input_groups.map((group) => ({
      ...group,
      sheet: sheet.name,
      periods: sheet.periods,
      purpose: sheet.purpose,
    })),
  ),
);
const visible = computed(() =>
  groups.value.filter(
    (group) =>
      (!sheetName.value || sheetName.value === group.sheet) &&
      `${group.sheet} ${group.label} ${group.range} ${group.unit} ${group.description} ${group.periods} ${group.purpose}`
        .toLowerCase()
        .includes(search.value.trim().toLowerCase()),
  ),
);
const visibleSheets = computed(() =>
  props.reading.sheets.filter(
    (sheet) =>
      (!sheetName.value || sheetName.value === sheet.name) &&
      (!search.value.trim() ||
        visible.value.some((group) => group.sheet === sheet.name) ||
        `${sheet.name} ${sheet.purpose} ${sheet.periods}`
          .toLowerCase()
          .includes(search.value.trim().toLowerCase())),
  ),
);
function issueTitle(message: string, target: string) {
  return (
    props.reading.sheets.find(
      (sheet) => target === sheet.name || message.startsWith(sheet.name + '：'),
    )?.name || props.template.source.filename
  );
}
</script>
<template>
  <section class="template-reading">
    <div class="review-toolbar">
      <div>
        <h2>确认空表</h2>
        <span class="muted">{{ template.source.filename }}</span>
      </div>
      <button
        class="review-action"
        @click="emit('preview')"
      >
        查看原表
      </button>
    </div>
    <div class="card review-guide">
      <div class="section-heading">
        <div>
          <h2>
            {{
              reading.reviewed && !unresolved.length
                ? '空表已确认'
                : unresolved.length
                  ? `还有 ${unresolved.length} 项疑点待确认`
                  : '解读完成，可以确认'
            }}
          </h2>
          <p class="muted">
            {{ reading.sheets.length }} 张工作表 ·
            {{ groups.length }} 个输入区域；公式与已有数据保留。
          </p>
        </div>
        <span
          class="badge"
          :class="reading.reviewed && !unresolved.length ? 'known' : 'assumed'"
          >{{ reading.reviewed && !unresolved.length ? '已确认' : '待确认' }}</span
        >
      </div>
    </div>
    <div class="card review-card">
      <div class="review-tabs">
        <button
          :class="{ active: tab === 'issues' }"
          @click="tab = 'issues'"
        >
          待确认 {{ unresolved.length }} / {{ pending.length }}
        </button>
        <button
          :class="{ active: tab === 'data' }"
          @click="tab = 'data'"
        >
          空表数据 {{ groups.length }}
        </button>
      </div>
      <div v-if="tab === 'issues'">
        <p
          v-if="pending.length && reading.explanation && !reading.issue_reviews?.length"
          class="notice"
        >
          旧版整体处理说明保留在下方，不会自动作为每条疑点的回答；请逐条补充确认。
        </p>
        <TemplateIssueCard
          v-for="issue in pending"
          :key="issueKey(issue)"
          :issue="issue"
          :title="issueTitle(issue.message, issue.target)"
          :saved="reviewFor(issue)"
          :busy="busy"
          @confirm="emit('confirmIssue', issue, $event)"
          @dirty="changed[issueKey(issue)] = $event"
          @preview="emit('preview')"
        />
        <p
          v-if="!pending.length"
          class="empty"
        >
          没有需要核对的疑点，可查看空表数据后确认。
        </p>
      </div>
      <div v-else>
        <div class="fact-search">
          <div class="fact-search-input">
            <svg
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle
                cx="10.5"
                cy="10.5"
                r="6.5"
              />
              <path d="m16 16 4 4" />
            </svg>
            <input
              v-model="search"
              type="search"
              aria-label="搜索空表数据"
              placeholder="搜索条目、位置或说明"
            />
            <button
              v-if="search"
              class="icon-button"
              aria-label="清空搜索"
              @click="search = ''"
            >
              ×
            </button>
          </div>
          <div class="fact-search-filters">
            <AppSelect
              v-model="sheetName"
              label="工作表筛选"
              :options="[
                { value: '', label: '全部工作表' },
                ...reading.sheets.map((sheet) => ({ value: sheet.name, label: sheet.name })),
              ]"
            />
            <button
              class="review-action"
              :disabled="!search && !sheetName"
              @click="search = sheetName = ''"
            >
              重置筛选
            </button>
            <span
              class="muted fact-search-count"
              role="status"
              >显示 {{ visible.length }} / {{ groups.length }} 条</span
            >
          </div>
        </div>
        <div class="table-wrap">
          <table class="fact-table">
            <thead>
              <tr>
                <th>工作表 / 条目</th>
                <th>位置</th>
                <th>期间 / 单位</th>
                <th>含义与用途</th>
              </tr>
            </thead>
            <tbody>
              <template
                v-for="sheet in visibleSheets"
                :key="sheet.name"
              >
                <tr class="group-row">
                  <td colspan="4">
                    {{ sheet.name }} · {{ sheet.purpose || '用途未注明' }} ·
                    {{ sheet.periods || '期间未注明' }}
                  </td>
                </tr>
                <tr
                  v-for="(group, index) in visible.filter((group) => group.sheet === sheet.name)"
                  :key="index"
                >
                  <td>
                    <strong>{{ group.label }}</strong
                    ><small class="muted">{{ group.sheet }}</small>
                  </td>
                  <td>{{ group.range }}</td>
                  <td>
                    {{ group.periods || '未注明期间'
                    }}<small class="muted template-unit">{{ group.unit || '无单位' }}</small>
                  </td>
                  <td>{{ group.description || group.purpose || '未提供说明' }}</td>
                </tr>
                <tr v-if="!groups.some((group) => group.sheet === sheet.name)">
                  <td
                    colspan="4"
                    class="muted"
                  >
                    无待填输入区域。
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </div>
        <p
          v-if="!visibleSheets.length"
          class="empty"
        >
          {{ reading.sheets.length ? '没有匹配条目。' : '未发现可展示的工作表。' }}
        </p>
      </div>
      <label class="issue-explanation">
        空表补充说明（选填）
        <textarea
          v-model="explanation"
          rows="3"
          :disabled="busy"
          placeholder="仅填写整份空表的补充信息，疑点请在对应问题下回答。"
        />
      </label>
      <footer class="panel-footer">
        <span class="muted">{{
          unresolved.length
            ? `还有 ${unresolved.length} 条疑点未确认`
            : reading.reviewed
              ? '已确认结构可供各项目导入'
              : '各条疑点已保存，可确认整份空表'
        }}</span>
        <button
          class="primary"
          :disabled="busy || !!unresolved.length"
          @click="emit('confirm', explanation)"
        >
          {{ busy ? '正在保存…' : reading.reviewed ? '保存处理说明' : '确认解读' }}
        </button>
      </footer>
    </div>
  </section>
</template>
