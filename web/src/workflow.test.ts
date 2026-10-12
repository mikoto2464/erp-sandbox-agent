import { afterEach, describe, expect, it, vi } from 'vitest';
import { createMemoryHistory, createRouter } from 'vue-router';
import { flushPromises, mount } from '@vue/test-utils';
import App from './App.vue';
import ProjectsPage from './views/ProjectsPage.vue';
import WorkflowPage from './views/WorkflowPage.vue';
import AppModal from './components/AppModal.vue';
import AppSelect from './components/AppSelect.vue';
import { useWorkspace } from './composables/useWorkspace';
import { useModelPresets } from './composables/useModelPresets';
import { evidenceTables } from './lib/evidence';
import type { Job, ModelPreset, ParsedDocument } from './lib/types';
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
  const snapshot = {
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
    else if (url.endsWith('/documents') || url.endsWith('/solutions')) data = [];
    else if (url.endsWith('/jobs')) data = structuredClone(jobs);
    else if (url.endsWith('/problem')) data = { snapshot };
    else if (url.endsWith('/revisions'))
      data = {
        ...snapshot,
        version: 2,
        inputs: { revision: 1 },
        reading: {
          ...snapshot.reading,
          problem: { ...snapshot.reading.problem, ...JSON.parse(String(options.body)).reading },
        },
      };
    else if (url === '/api/model-presets/model')
      data = { ...preset, ...JSON.parse(String(options.body)), revision: 2 };
    else data = {};
    return { ok: true, status: 200, json: async () => structuredClone(data) };
  });
  vi.stubGlobal('fetch', fetcher);
  return { router, jobs, fetcher };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
describe('Vue migration', () => {
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
    workspace.markInputsDirty();
    await workspace.saveInputs();
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
