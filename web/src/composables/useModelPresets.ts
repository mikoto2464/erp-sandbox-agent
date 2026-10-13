import { computed, ref } from 'vue';
import { post, request } from '../lib/api';
import type { ModelCatalog, ModelPreset } from '../lib/types';
import type { Workspace } from './context';
export function useModelPresets(workspace: Workspace) {
  const open = ref(false),
    selectedId = ref(''),
    dirty = ref(false),
    testResult = ref('');
  const draft = ref<ModelPreset & { api_key: string; clear_api_key: boolean }>();
  const current = computed(() =>
    workspace.catalog.value?.presets.find((preset) => preset.id === selectedId.value),
  );
  function fill(preset: ModelPreset) {
    selectedId.value = preset.id;
    draft.value = {
      ...preset,
      priority: preset.priority ?? 100,
      max_parallel: preset.max_parallel ?? 2,
      scheduling_enabled: preset.scheduling_enabled ?? false,
      api_key: '',
      clear_api_key: false,
    };
    dirty.value = false;
    testResult.value = '';
  }
  async function show() {
    workspace.catalog.value = await request<ModelCatalog>('/api/model-presets');
    fill(
      workspace.catalog.value.presets.find(
        (preset) => preset.id === workspace.catalog.value!.active_id,
      )!,
    );
    open.value = true;
  }
  function select(id: string) {
    if (dirty.value) {
      workspace.notify('请先保存或撤销预设修改。');
      return;
    }
    fill(workspace.catalog.value!.presets.find((preset) => preset.id === id)!);
  }
  function create() {
    if (dirty.value) {
      workspace.notify('请先保存或撤销预设修改。');
      return;
    }
    fill({
      id: '',
      revision: 0,
      name: '',
      configured: false,
      priority: 100,
      max_parallel: 2,
      scheduling_enabled: false,
      provider: 'openai_chat',
      base_url: workspace.catalog.value!.providers.find(
        (provider) => provider.id === 'openai_chat',
      )!.base_url,
      model: '',
      api_key_set: false,
      format: 'json_schema',
      timeout: 90,
      max_input_chars: 100000,
      max_output_tokens: 12000,
      token_parameter: 'max_completion_tokens',
      thinking_mode: 'default',
      thinking_budget: 8192,
    });
  }
  function reset() {
    fill(
      current.value ||
        workspace.catalog.value!.presets.find(
          (preset) => preset.id === workspace.catalog.value!.active_id,
        )!,
    );
  }
  function changeProvider(value: string) {
    if (!draft.value) return;
    draft.value.provider = value;
    draft.value.base_url = workspace.catalog.value!.providers.find(
      (provider) => provider.id === value,
    )!.base_url;
    draft.value.model = '';
    draft.value.api_key = '';
    draft.value.api_key_set = false;
    draft.value.clear_api_key = false;
    draft.value.thinking_mode = 'default';
    dirty.value = true;
  }
  async function activate(id: string) {
    if (dirty.value && open.value) throw new Error('请先保存或撤销预设修改。');
    const preset = workspace.catalog.value!.presets.find((preset) => preset.id === id)!;
    await post(`/api/model-presets/${id}/activate`, { expected_revision: preset.revision });
    await workspace.loadStatus();
    if (open.value) fill(workspace.catalog.value!.presets.find((preset) => preset.id === id)!);
    workspace.notify(`已启用 ${preset.name}，新任务按协作模型优先级分配。`, 'success');
  }
  async function save() {
    if (!draft.value) return;
    const {
      name,
      provider,
      base_url,
      model,
      api_key,
      clear_api_key,
      format,
      timeout,
      max_input_chars,
      max_output_tokens,
      token_parameter,
      thinking_mode,
      thinking_budget,
      priority,
      max_parallel,
      scheduling_enabled,
    } = draft.value;
    const saved = await post<ModelPreset>(
      selectedId.value ? `/api/model-presets/${selectedId.value}` : '/api/model-presets',
      {
        name,
        provider,
        base_url,
        model,
        api_key,
        clear_api_key,
        format,
        timeout,
        max_input_chars,
        max_output_tokens,
        token_parameter,
        thinking_mode,
        thinking_budget,
        priority,
        max_parallel,
        scheduling_enabled,
        ...(selectedId.value ? { expected_revision: current.value!.revision } : {}),
      },
    );
    draft.value.api_key = '';
    await workspace.loadStatus();
    fill(saved);
    workspace.notify('模型预设已保存。', 'success');
  }
  async function test() {
    const preset = current.value!;
    testResult.value = '正在测试接口与 JSON 输出…';
    try {
      const value = await post<{ elapsed_ms: number }>(`/api/model-presets/${preset.id}/test`, {
        expected_revision: preset.revision,
      });
      testResult.value = `测试通过 · ${value.elapsed_ms} ms`;
    } catch (error) {
      testResult.value = `测试失败：${error instanceof Error ? error.message : '请求失败'}`;
      throw error;
    }
  }
  async function remove() {
    const preset = current.value!;
    await request(`/api/model-presets/${preset.id}?revision=${preset.revision}`, {
      method: 'DELETE',
    });
    await workspace.loadStatus();
    fill(
      workspace.catalog.value!.presets.find(
        (preset) => preset.id === workspace.catalog.value!.active_id,
      )!,
    );
    workspace.notify('预设已删除。', 'success');
  }
  function close() {
    open.value = false;
    if (draft.value) draft.value.api_key = '';
    dirty.value = false;
  }
  return {
    open,
    selectedId,
    dirty,
    testResult,
    draft,
    current,
    show,
    select,
    create,
    reset,
    changeProvider,
    activate,
    save,
    test,
    remove,
    close,
  };
}
