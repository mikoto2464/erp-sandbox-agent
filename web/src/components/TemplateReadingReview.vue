<script setup lang="ts">
import { computed, ref } from 'vue';
import AppSelect from './AppSelect.vue';
import type { BudgetTemplate, TemplateReading } from '../lib/types';
const props = defineProps<{ template: BudgetTemplate; reading: TemplateReading; busy: boolean }>();
const emit = defineEmits<{ confirm: [explanation: string]; preview: [] }>();
const pending = computed(() => props.reading.issues.filter((issue) => issue.severity !== 'info'));
const tab = ref(pending.value.length && !props.reading.reviewed ? 'issues' : 'data'),
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
              reading.reviewed
                ? '空表已确认'
                : pending.length
                  ? `先核对 ${pending.length} 项疑点`
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
          :class="reading.reviewed ? 'known' : 'assumed'"
          >{{ reading.reviewed ? '已确认' : '待确认' }}</span
        >
      </div>
    </div>
    <div class="card review-card">
      <div class="review-tabs">
        <button
          :class="{ active: tab === 'issues' }"
          @click="tab = 'issues'"
        >
          {{ reading.reviewed ? '核对记录' : '待确认' }} {{ pending.length }}
        </button>
        <button
          :class="{ active: tab === 'data' }"
          @click="tab = 'data'"
        >
          空表数据 {{ groups.length }}
        </button>
      </div>
      <div v-if="tab === 'issues'">
        <article
          v-for="(issue, index) in pending"
          :key="index"
          class="issue-item"
          :class="issue.severity"
        >
          <div class="issue-heading">
            <span
              class="badge"
              :class="
                reading.reviewed ? 'known' : issue.severity === 'error' ? 'conflicted' : 'assumed'
              "
              >{{ reading.reviewed ? '已记录处理说明' : '需核对' }}</span
            >
            <strong>{{ issueTitle(issue.message, issue.target) }}</strong>
          </div>
          <p>{{ issue.message }}</p>
          <button
            class="review-action"
            @click="emit('preview')"
          >
            查看原表
          </button>
        </article>
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
        {{ pending.length ? '疑点处理说明' : '处理说明（选填）' }}
        <textarea
          v-model="explanation"
          rows="3"
          :required="!!pending.length"
          :disabled="busy"
          :placeholder="
            pending.length
              ? '请针对上方疑点说明正确的期间、用途或条目含义；多项疑点请分别说明。'
              : '可补充空表的用途或填表口径。'
          "
        />
      </label>
      <footer class="panel-footer">
        <span class="muted">{{
          reading.reviewed ? '已确认结构可供各项目导入' : '确认后可在经营方案中导入'
        }}</span>
        <button
          class="primary"
          :disabled="busy || (!!pending.length && !explanation.trim())"
          @click="emit('confirm', explanation)"
        >
          {{ busy ? '正在保存…' : reading.reviewed ? '保存处理说明' : '确认解读' }}
        </button>
      </footer>
    </div>
  </section>
</template>
