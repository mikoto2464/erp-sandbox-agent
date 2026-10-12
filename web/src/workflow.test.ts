import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';
import { flushPromises, mount } from '@vue/test-utils';
import App from './App.vue';
import ProjectsPage from './views/ProjectsPage.vue';
import WorkflowPage from './views/WorkflowPage.vue';
import AppModal from './components/AppModal.vue';
import AppSelect from './components/AppSelect.vue';
import IssueCard from './components/IssueCard.vue';
import SolverStep from './views/SolverStep.vue';
import ArtifactsStep from './views/ArtifactsStep.vue';
import FactEditor from './components/FactEditor.vue';
import { workspaceKey } from './composables/context';
import {
  issueAnswer,
  issueFact,
  resolveIssue,
  setIssueAnswer,
  TERM_TOTAL_ANSWER,
  UNKNOWN_RATE_ANSWER,
} from './lib/review';
import { useWorkspace } from './composables/useWorkspace';
import { useModelPresets } from './composables/useModelPresets';
import { evidenceTables } from './lib/evidence';
import type {
  EditableReading,
  BudgetTemplate,
  Fact,
  Issue,
  Job,
  ModelPreset,
  ParsedDocument,
  Snapshot,
  Solution,
  WorkbookMapping,
  Artifact,
} from './lib/types';
const projectId = 'a'.repeat(32);
const preset: ModelPreset = {
  id: 'model',
  revision: 1,
  name: '测试模型',
  configured: true,
  provider: 'openai_chat',
  base_url: 'https://example.com/v1',
  model: 'test',
  api_key_set: true,
  format: 'json_schema',
  timeout: 90,
  max_input_chars: 100000,
  max_output_tokens: 12000,
  token_parameter: 'max_completion_tokens',
  thinking_mode: 'default',
  thinking_budget: 8192,
};
function setup() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/projects', name: 'projects', component: ProjectsPage },
      { path: '/projects/:projectId/steps/:step', name: 'workflow', component: WorkflowPage },
    ],
  });
  const jobs: Job[] = [
    {
      id: 'old',
      kind: 'read',
      status: 'succeeded',
      stage: 'done',
      metadata: {},
      result: { version: 1 },
    },
  ];
  const inputs = { revision: 0, problem_text: '', supplement_text: '', annotations: [] };
  const solutions: Solution[] = [];
  const templates: BudgetTemplate[] = [];
  const mappings: WorkbookMapping[] = [],
    artifacts: Artifact[] = [];
  let snapshot: Snapshot = {
    version: 1,
    inputs,
    additional_sources: [],
    reading: {
      problem: { title: '测试赛题', years: 4, periods_per_year: 4, facts: [] },
      rules: { rules: [] },
      issues: [],
    },
  };
  const fetcher = vi.fn(async (url: string, options: RequestInit = {}) => {
    let data: unknown;
    if (url === '/api/auth/session') data = { authenticated: true, configured: true };
    else if (url === '/api/status')
      data = {
        llm_configured: true,
        simulation_ready: false,
        default_solver: 'llm_only',
        max_upload_mb: 20,
      };
    else if (url === '/api/model-presets')
      data = {
        active_id: 'model',
        presets: [preset],
        providers: [{ id: 'openai_chat', name: 'OpenAI', base_url: preset.base_url }],
      };
    else if (url === '/api/projects')
      data = [{ id: projectId, name: '项目', created_at: '2026-10-12' }];
    else if (url.endsWith('/inputs'))
      data = options.method ? { ...JSON.parse(String(options.body)), revision: 1 } : inputs;
    else if (url.endsWith('/documents')) data = [];
    else if (url.endsWith('/solutions')) data = solutions;
    else if (url.endsWith('/mappings')) data = options.method ? {} : mappings;
    else if (url.endsWith('/artifacts')) data = options.method ? {} : artifacts;
    else if (url.includes('/mappings/') && options.method) {
      const payload = JSON.parse(String(options.body));
      const mapping = mappings.find((item) => url.endsWith(item.id))!;
      Object.assign(mapping, {
        entries: payload.entries,
        confirmed: true,
        revision: mapping.revision + 1,
      });
      data = mapping;
    } else if (url.endsWith('/templates')) {
      if (options.method) {
        data = {
          id: 'template-job',
          kind: 'template',
          status: 'queued',
          stage: 'queued',
          metadata: { template_id: 'template', filename: 'budget.xlsx' },
        };
        jobs.push(data as Job);
      } else data = templates;
    } else if (url.endsWith('/jobs')) data = structuredClone(jobs);
    else if (url.endsWith('/problem')) data = { snapshot };
    else if (url.endsWith('/revisions')) {
      const reading = JSON.parse(String(options.body)).reading as EditableReading;
      snapshot = {
        ...snapshot,
        version: snapshot.version + 1,
        reading: {
          problem: { ...snapshot.reading.problem, title: reading.title, facts: reading.facts },
          rules: { rules: reading.rules },
          issues: reading.issues,
        },
      };
      data = snapshot;
    } else if (url === '/api/model-presets/model')
      data = { ...preset, ...JSON.parse(String(options.body)), revision: 2 };
    else data = {};
    return { ok: true, status: 200, json: async () => structuredClone(data) };
  });
  vi.stubGlobal('fetch', fetcher);
  return { router, jobs, fetcher, solutions, templates, mappings, artifacts };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe('Vue migration', () => {
  it('reviews template sources before filling and shows downloads for the selected solution', async () => {
    const { router, solutions, templates, mappings, artifacts, fetcher } = setup();
    solutions.push({
      id: 'plan',
      mode: 'llm_only',
      problem_version: 1,
      plan: { title: '经营计划', summary: '', actions: [], assumptions: [], risks: [] },
      evaluation: { verified: false, score: null, issues: [] },
    });
    templates.push({
      source: { id: 'template', filename: '预算.xlsx', size: 1, sha256: '', format: 'xlsx' },
      sheets: [],
      warnings: [],
    });
    mappings.push({
      id: 'mapping',
      solution_id: 'plan',
      template_id: 'template',
      revision: 1,
      confirmed: false,
      entries: [
        {
          sheet: '预算',
          cell: 'B2',
          label: '借款',
          source_ids: ['action:0:amount'],
          conversion: 'identity',
          explanation: '第一期金额',
        },
      ],
      issues: [],
      values: ['10'],
      sources: [
        { id: 'action:0:amount', label: '借款', value: '10', numeric: true, period: 1, unit: '元' },
      ],
    });
    artifacts.push({
      id: 'file',
      solution_id: 'plan',
      problem_version: 1,
      kind: 'docx',
      filename: '经营方案_草稿.docx',
      created_at: '',
      draft: true,
      formula_status: 'not_applicable',
      warnings: [],
      template_id: null,
      mapping_id: null,
      mapping_revision: null,
    });
    await router.push(`/projects/${projectId}/steps/4`);
    const workspace = useWorkspace(router);
    await workspace.initialize();
    const wrapper = mount(ArtifactsStep, {
      global: { provide: { [workspaceKey as symbol]: workspace } },
    });
    const fill = () =>
      wrapper.findAll('button').find((button) => button.text() === '填写并生成 Excel')!;
    expect(fill().attributes('disabled')).toBeDefined();
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '核对 / 修改')!
      .trigger('click');
    await flushPromises();
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存并确认映射')!
      .trigger('click');
    await flushPromises();
    expect(fill().attributes('disabled')).toBeUndefined();
    await fill().trigger('click');
    await flushPromises();
    const call = fetcher.mock.calls.find(
      ([url, options]) => url.endsWith('/artifacts') && options?.method === 'POST',
    )!;
    expect(JSON.parse(String(call[1]!.body))).toEqual({
      solution_id: 'plan',
      kind: 'xlsx',
      mapping_id: 'mapping',
    });
    expect(wrapper.find('a[download]').attributes('href')).toContain('/artifacts/file/download');
    wrapper.unmount();
    workspace.dispose();
  });
  it('generates with tone and a parsed budget, then revises a selected plan without replacing it', async () => {
    const { router, fetcher, jobs, solutions, templates } = setup();
    await router.push(`/projects/${projectId}/steps/3`);
    const workspace = useWorkspace(router);
    await workspace.initialize();
    const wrapper = mount(SolverStep, {
      global: { provide: { [workspaceKey as symbol]: workspace } },
    });
    expect(workspace.planTone.value).toBe('balanced');
    await wrapper.find('input[value="custom"]').setValue();
    expect(workspace.generationReady.value).toBe(false);
    await wrapper.find('.custom-tone textarea').setValue('减少借款');
    await workspace.uploadTemplates([new File(['fixture'], 'budget.xlsx')]);
    expect(workspace.generationReady.value).toBe(false);
    templates.push({
      source: { id: 'template', filename: 'budget.xlsx', size: 7, sha256: 'hash', format: 'xlsx' },
      sheets: [
        {
          name: '现金预算',
          rows: 2,
          columns: 2,
          cells: [],
          merged_ranges: [],
          hidden: false,
          protected: false,
        },
      ],
      warnings: [],
    });
    jobs.at(-1)!.status = 'succeeded';
    await workspace.refresh();
    await flushPromises();
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '生成方案')!
      .trigger('click');
    await flushPromises();
    const requests = () =>
      fetcher.mock.calls
        .filter(([url]) => url.endsWith('/solve'))
        .map(([, options]) => JSON.parse(String(options!.body)));
    expect(requests()[0].generation).toEqual({
      tone: 'custom',
      custom_prompt: '减少借款',
      template_ids: ['template'],
      previous_solution_id: null,
      revision_prompt: '',
    });
    const first: Solution = {
      id: 'first',
      mode: 'llm_only',
      problem_version: 1,
      generation: requests()[0].generation,
      plan: {
        title: '第一版方案',
        summary: '保持现金',
        actions: [{ period: 1, description: '第一季', quantity: null, amount: '100' }],
        assumptions: [],
        risks: [],
        budget_items: [
          {
            template_id: 'template',
            sheet: '现金预算',
            cell: 'B2',
            label: '借款预算',
            period: 1,
            unit: '元',
            value: '100',
            action_indices: [0],
            explanation: '暂定借款',
          },
        ],
      },
      evaluation: { verified: false, score: null, issues: [] },
    };
    solutions.push(first);
    await workspace.refresh();
    await flushPromises();
    expect(wrapper.find('.budget-plan').text()).toContain('借款预算');
    await wrapper.find('.revision-section textarea').setValue('第一年不新增产线');
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '按要求调整方案')!
      .trigger('click');
    await flushPromises();
    expect(requests()[1].generation).toMatchObject({
      previous_solution_id: 'first',
      revision_prompt: '第一年不新增产线',
      template_ids: ['template'],
    });
    solutions.push({
      ...first,
      id: 'second',
      plan: { ...first.plan, title: '第二版方案' },
      generation: requests()[1].generation,
    });
    await workspace.refresh();
    await flushPromises();
    expect(workspace.currentSolution.value?.id).toBe('second');
    expect((wrapper.find('.revision-section textarea').element as HTMLTextAreaElement).value).toBe(
      '',
    );
    expect(workspace.solutions.value[0].plan.title).toBe('第一版方案');
    await wrapper.find('.budget-template-row button').trigger('click');
    await flushPromises();
    expect(wrapper.find('dialog').text()).toContain('现金预算');
    wrapper.findComponent(AppModal).vm.$emit('close');
    workspace.selectSolution('first');
    expect(workspace.currentSolution.value?.id).toBe('first');
    wrapper.unmount();
    workspace.dispose();
  });
  it('keeps clarification separate from extraction and confirms term rate without changing numeric evidence', async () => {
    const { router, fetcher } = setup();
    await router.push(`/projects/${projectId}/steps/2`);
    const workspace = useWorkspace(router);
    await workspace.initialize();
    const fact: Fact = {
      id: 'direct_rate',
      category: 'finance',
      entity_id: 'direct',
      entity_name: '直接融资',
      key: 'interest_rate',
      value: '0.05',
      value_type: 'number',
      unit: '比例',
      state: 'known',
      evidence_ids: ['rate_cell'],
      conversion: 'percent_to_ratio',
      explanation: '直接融资利率5%转为0.05',
    };
    const issue: Issue = {
      code: 'rate_period',
      target: fact.id,
      severity: 'warning',
      message: '利率周期（年/季/期）未明确，影响利息计算。',
      evidence_ids: ['rate_cell'],
    };
    const other = { ...issue, code: 'payment_timing', message: '利息何时支付？' };
    const reading = workspace.editable.value!;
    reading.facts = [fact];
    reading.issues = [issue, other];
    setIssueAnswer(reading, other, '到期结算');
    const wrapper = mount(IssueCard, {
      props: { issue },
      global: { provide: { [workspaceKey as symbol]: workspace } },
    });
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('');
    expect(wrapper.find('.issue-extraction').text()).toContain('5%转为0.05');
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '整个期限总利率')!
      .trigger('click');
    expect(issueAnswer(reading, issue)).toBe(TERM_TOTAL_ANSWER);
    expect(issueAnswer(reading, other)).toBe('到期结算');
    expect(reading.facts[0].explanation).toBe('直接融资利率5%转为0.05');
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '确认处理')!
      .trigger('click');
    expect(reading.facts[0]).toMatchObject({
      value: '0.05',
      state: 'known',
      evidence_ids: ['rate_cell'],
      conversion: 'percent_to_ratio',
    });
    expect(reading.facts[0].explanation).toContain(TERM_TOTAL_ANSWER);
    expect(reading.facts.find((item) => item.key === 'interest_rate_basis')).toMatchObject({
      value: 'term_total',
      state: 'assumed',
      entity_id: 'direct',
    });
    expect(reading.issues.some((item) => item.code === 'review.resolved.rate_period')).toBe(true);
    const uncertain = { ...issue, code: 'rate_uncertain', message: '计息口径是否已经确定？' };
    reading.issues.push(uncertain);
    resolveIssue(reading, uncertain, UNKNOWN_RATE_ANSWER);
    expect(reading.facts.find((item) => item.key === 'interest_rate_basis')).toMatchObject({
      value: null,
      state: 'missing',
    });
    reading.facts.push({ ...fact, id: 'other_rate', entity_id: 'other' });
    expect(issueFact(reading, { ...issue, target: 'interest_rate' })).toBeUndefined();
    const editor = mount(FactEditor, {
      props: { fact: reading.facts[0] },
      global: { provide: { [workspaceKey as symbol]: workspace } },
    });
    await editor.find('input').setValue('0.06');
    expect(reading.facts[0].value).toBe('0.05');
    await editor.find('form').trigger('submit');
    expect(editor.emitted('close')).toHaveLength(1);
    expect(reading.facts[0].value).toBe('0.06');
    await workspace.saveReading();
    const payload = JSON.parse(
      String(fetcher.mock.calls.find(([url]) => url.endsWith('/revisions'))![1]!.body),
    );
    expect(payload).toMatchObject({ expected_version: 1, reading: { facts: reading.facts } });
    expect(payload.reading.issues).toEqual(reading.issues);
    expect(workspace.snapshot.value!.reading.problem.facts[0]).toMatchObject({
      value: '0.06',
      evidence_ids: ['rate_cell'],
      conversion: 'percent_to_ratio',
    });
    expect(workspace.snapshot.value!.reading.issues).toContainEqual(
      expect.objectContaining({ code: 'review.confirmed', evidence_ids: ['rate_cell'] }),
    );
    expect(workspace.readingDirty.value).toBe(false);
    workspace.editable.value!.facts[0].evidence_ids.push('draft_only');
    expect(workspace.snapshot.value!.reading.problem.facts[0].evidence_ids).toEqual(['rate_cell']);
    editor.unmount();
    wrapper.unmount();
    workspace.dispose();
  });
  it('restores project and step, handles polling once, saves inputs and protects old solves', async () => {
    const { router, jobs } = setup();
    await router.push(`/projects/${projectId}/steps/4?review=rules`);
    const workspace = useWorkspace(router);
    await workspace.initialize();
    expect(workspace.currentStep.value).toBe(4);
    expect(workspace.reviewTab.value).toBe('rules');
    expect(workspace.notifications.messages.value).toHaveLength(0);
    await workspace.logout();
    expect(workspace.project.value).toBeNull();
    await workspace.login('access-key', true);
    expect(workspace.snapshot.value?.version).toBe(1);
    expect(workspace.currentStep.value).toBe(4);
    jobs.push({ id: 'new', kind: 'read', status: 'running', stage: 'reading', metadata: {} });
    await workspace.refresh();
    jobs[1].status = 'needs_input';
    jobs[1].result = { version: 2 };
    await workspace.refresh();
    await workspace.refresh();
    expect(workspace.currentStep.value).toBe(4);
    expect(workspace.notifications.messages.value).toHaveLength(1);
    workspace.inputs.value.problem_text = '手动题目';
    workspace.setAnnotation('doc', '数量上限');
    workspace.setAnnotation('supplement', '补充规则');
    workspace.setAnnotation('doc', '订单数量上限');
    workspace.markInputsDirty();
    await workspace.saveInputs();
    expect(workspace.inputs.value.annotations).toEqual([
      { document_id: 'supplement', text: '补充规则' },
      { document_id: 'doc', text: '订单数量上限' },
    ]);
    expect(workspace.inputsDirty.value).toBe(false);
    expect(workspace.inputChanged.value).toBe(true);
    expect(workspace.canSolve.value).toBe(false);
    workspace.markReadingDirty();
    await router.push('/projects');
    expect(router.currentRoute.value.params.projectId).toBe(projectId);
    expect(workspace.pendingNavigation.value).not.toBeNull();
    workspace.discardAndNavigate();
    await flushPromises();
    expect(router.currentRoute.value.path).toBe('/projects');
    workspace.dispose();
  });
  it('renders app with restored route, opens settings, drawer and background dismissal', async () => {
    const { router } = setup();
    await router.push(`/projects/${projectId}/steps/3`);
    const wrapper = mount(App, { attachTo: document.body, global: { plugins: [router] } });
    await flushPromises();
    expect(wrapper.text()).toContain('生成经营方案');
    expect(wrapper.find('.step.active').text()).toContain('经营方案');
    expect(wrapper.find('.nav-home').text()).toBe('项目列表');
    await wrapper
      .findAll('.topbar-actions button')
      .find((button) => button.text() === '模型预设')!
      .trigger('click');
    await flushPromises();
    expect(
      wrapper
        .findAll('dialog')
        .some(
          (dialog) =>
            (dialog.element as HTMLDialogElement).open && dialog.text().includes('预设名称'),
        ),
    ).toBe(true);
    const settings = wrapper
      .findAllComponents(AppModal)
      .find((modal) => modal.props('title') === '模型预设')!;
    await settings.find('.select-trigger').trigger('click');
    await flushPromises();
    expect(settings.find('.select-menu[role="listbox"]').exists()).toBe(true);
    await settings.find('.select-menu button').trigger('click');
    await flushPromises();
    settings.vm.$emit('close');
    await flushPromises();
    await wrapper
      .findAll('.topbar-actions button')
      .find((button) => button.text() === '任务')!
      .trigger('click');
    await flushPromises();
    expect((wrapper.find('.drawer-modal').element as HTMLDialogElement).open).toBe(true);
    await wrapper.find('.drawer-modal').trigger('pointerdown', { clientX: -1, clientY: -1 });
    await wrapper.find('.drawer-modal').trigger('click', { clientX: -1, clientY: -1 });
    await flushPromises();
    await vi.waitFor(() =>
      expect((wrapper.find('.drawer-modal').element as HTMLDialogElement).open).toBe(false),
    );
    wrapper.unmount();
  });
  it('keeps model revision and independent key semantics when saving', async () => {
    const { router, fetcher } = setup();
    await router.push('/projects');
    const workspace = useWorkspace(router);
    await workspace.initialize();
    const models = useModelPresets(workspace);
    await models.show();
    models.draft.value!.api_key = 'private-key';
    await models.save();
    const call = fetcher.mock.calls.find(([url]) => url === '/api/model-presets/model')!;
    expect(JSON.parse(String(call[1]!.body))).toMatchObject({
      expected_revision: 1,
      api_key: 'private-key',
      clear_api_key: false,
    });
    expect(models.draft.value!.api_key).toBe('');
    workspace.dispose();
  });
  it('groups a complete source table and highlights data separately from headers', () => {
    const document = {
      source: { id: 'doc', filename: '赛题.docx' },
      blocks: [
        {
          index: 0,
          kind: 'table',
          cells: [
            { evidence_id: 'head', row: 1, text: '利率%' },
            { evidence_id: 'object', row: 2, text: '直接融资' },
            { evidence_id: 'value', row: 2, text: '5%' },
            { evidence_id: 'other', row: 3, text: '2%' },
          ],
        },
      ],
      evidence: [
        {
          id: 'object',
          document_id: 'doc',
          text: '直接融资',
          table_context: {
            block_index: 0,
            row: 2,
            column: 1,
            column_header_ids: [],
            row_ids: ['object', 'value'],
            heading_ids: [],
          },
        },
        {
          id: 'head',
          document_id: 'doc',
          text: '利率%',
          table_context: {
            block_index: 0,
            row: 1,
            column: 4,
            column_header_ids: [],
            row_ids: [],
            heading_ids: [],
          },
        },
        {
          id: 'value',
          document_id: 'doc',
          text: '5%',
          table_context: {
            block_index: 0,
            row: 2,
            column: 4,
            column_header_ids: ['head'],
            row_ids: ['object', 'value'],
            heading_ids: [],
          },
        },
      ],
    } as unknown as ParsedDocument;
    const tables = evidenceTables([document], ['head', 'object', 'value']);
    expect(tables).toHaveLength(1);
    expect(tables[0].block.cells).toHaveLength(4);
    expect(tables[0].primary).toEqual(['value']);
  });
  it('selects styled options without exposing a native dropdown', async () => {
    const wrapper = mount(AppSelect, {
      props: {
        modelValue: 'a',
        options: [
          { value: 'a', label: 'A' },
          { value: 'b', label: 'B' },
        ],
        label: '模型',
      },
      attachTo: document.body,
    });
    await wrapper.find('.select-trigger').trigger('click');
    await flushPromises();
    const option = [...document.querySelectorAll<HTMLButtonElement>('.select-menu button')].find(
      (option) => option.textContent === 'B',
    )!;
    option.click();
    await flushPromises();
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['b']);
    wrapper.unmount();
  });
});
