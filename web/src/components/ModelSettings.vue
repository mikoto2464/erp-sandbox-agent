<script setup lang="ts">
import { computed, ref } from 'vue';
import AppModal from './AppModal.vue';
import AppSelect from './AppSelect.vue';
import { useWorkspaceContext } from '../composables/context';
import { useModelPresets } from '../composables/useModelPresets';
const workspace = useWorkspaceContext(),
  models = useModelPresets(workspace);
const { open, draft, selectedId, dirty, current, testResult } = models;
const { catalog, run, operations } = workspace;
const deleting = ref(false);
const presets = computed(
  () =>
    catalog.value?.presets.map((preset) => ({
      value: preset.id,
      label: `${preset.name}${preset.id === catalog.value!.active_id ? ' · 当前启用' : ''}`,
    })) || [],
);
const providers = computed(
  () =>
    catalog.value?.providers.map((provider) => ({ value: provider.id, label: provider.name })) ||
    [],
);
const providerHelp: Record<string, string> = {
  openai_chat: 'OpenAI、DeepSeek 等兼容接口。',
  openai_responses: 'OpenAI Responses 格式。',
  anthropic: 'Anthropic Messages 兼容接口。',
  gemini: 'Google Gemini 原生接口。',
  ollama: '本机 Ollama 服务可不填写密钥。',
};
const busy = computed(() => operations.value.has('preset'));
const formats = [
  { value: 'json_schema', label: '结构化 Schema' },
  { value: 'json_object', label: 'JSON 对象' },
  { value: 'text', label: '提示词约束 JSON' },
];
defineExpose({
  show: () => run('settings', models.show),
  activate: (id: string) => run('preset', () => models.activate(id)),
});
</script>
<template>
  <AppModal
    :open="open"
    title="模型预设"
    wide
    @close="models.close"
    ><template v-if="draft"
      ><div class="settings-selection">
        <label
          >已保存预设<AppSelect
            :model-value="selectedId"
            label="选择预设"
            :options="presets"
            @update:model-value="models.select" /></label
        ><button
          class="secondary"
          :disabled="busy"
          @click="models.create"
        >
          新增预设
        </button>
      </div>
      <div class="settings-toolbar">
        <span
          class="badge"
          :class="dirty ? 'assumed' : 'known'"
          >{{
            dirty
              ? '有未保存修改'
              : !selectedId
                ? '新预设'
                : selectedId === catalog?.active_id
                  ? '当前启用'
                  : current?.configured
                    ? '可启用'
                    : '配置未完成'
          }}</span
        >
        <div class="review-actions">
          <button
            class="review-action"
            :disabled="!current?.configured || dirty || selectedId === catalog?.active_id || busy"
            @click="run('preset', () => models.activate(selectedId))"
          >
            启用</button
          ><button
            class="review-action"
            :disabled="!current?.configured || dirty || busy"
            @click="run('preset', models.test)"
          >
            测试连接</button
          ><button
            v-if="dirty"
            class="review-action"
            @click="models.reset"
          >
            撤销修改</button
          ><button
            class="review-action danger-text"
            :disabled="!current || selectedId === catalog?.active_id || busy"
            @click="deleting = true"
          >
            删除
          </button>
        </div>
      </div>
      <p
        v-if="testResult"
        class="notice"
        role="status"
      >
        {{ testResult }}
      </p>
      <form
        class="settings-form"
        @submit.prevent="run('preset', models.save)"
        @input="dirty = true"
      >
        <div class="form-grid">
          <label
            >预设名称<input
              v-model="draft.name"
              required
              maxlength="80"
              placeholder="生产主模型 / 备用模型" /></label
          ><label
            >接口类型<AppSelect
              :model-value="draft.provider"
              label="接口类型"
              :options="providers"
              @update:model-value="models.changeProvider" /></label
          ><label class="full-field"
            >接口地址<input
              v-model="draft.base_url"
              type="url"
            /><small class="muted">{{ providerHelp[draft.provider] }}</small></label
          ><label
            >模型名称<input
              v-model="draft.model"
              maxlength="200"
              placeholder="供应商提供的模型名称" /></label
          ><label
            >API 密钥<input
              v-model="draft.api_key"
              type="password"
              autocomplete="new-password"
              :placeholder="draft.api_key_set ? '已保存，留空保留' : '输入 API 密钥'"
              maxlength="2048"
          /></label>
        </div>
        <label class="check"
          ><input
            v-model="draft.clear_api_key"
            type="checkbox"
          />清除已保存的 API 密钥</label
        >
        <fieldset class="model-scheduling">
          <legend>任务分配</legend>
          <div class="form-grid">
            <label
              >优先级<input
                v-model.number="draft.priority"
                type="number"
                min="0"
                max="1000"
                required
              />
              <small class="muted">数字越大越优先；最高优先级模型担任主 Agent。</small></label
            >
            <label
              >最大并行请求<input
                v-model.number="draft.max_parallel"
                type="number"
                min="1"
                max="16"
                required
              />
              <small class="muted">同一预设在全部任务间共享额度。</small></label
            >
          </div>
          <label class="full-field"
            >能力说明（选填）
            <textarea
              v-model="draft.capabilities"
              rows="2"
              maxlength="500"
              placeholder="例如：适合规则推理和跨表关联；或擅长格式稳定的表格提取。"
            />
            <small class="muted">帮助主 Agent 判断任务适配度；优先级表示偏好，不代表能力。</small>
          </label>
          <label class="check"
            ><input
              v-model="draft.scheduling_enabled"
              type="checkbox"
            />参与多模型协作</label
          >
          <p class="muted">
            当前启用预设始终参与。主 Agent
            根据具体任务难度、能力说明、优先级及剩余额度选择执行模型。
          </p>
        </fieldset>
        <details class="advanced">
          <summary>高级参数</summary>
          <div class="form-grid">
            <label
              >输出格式<AppSelect
                v-model="draft.format"
                label="输出格式"
                :options="formats"
                @update:model-value="dirty = true" /></label
            ><label v-if="draft.provider === 'openai_chat'"
              >Token 上限参数<AppSelect
                v-model="draft.token_parameter"
                label="Token 参数"
                :options="[
                  { value: 'max_completion_tokens', label: 'max_completion_tokens' },
                  { value: 'max_tokens', label: 'max_tokens' },
                ]"
                @update:model-value="dirty = true" /></label
            ><label v-if="['openai_chat', 'anthropic', 'ollama'].includes(draft.provider)"
              >思考模式<AppSelect
                v-model="draft.thinking_mode"
                label="思考模式"
                :options="[
                  { value: 'default', label: '接口默认' },
                  { value: 'disabled', label: '关闭思考' },
                  { value: 'enabled', label: '开启思考' },
                ]"
                @update:model-value="dirty = true" /></label
            ><label v-if="draft.provider === 'anthropic' && draft.thinking_mode === 'enabled'"
              >思考 Token 预算<input
                v-model.number="draft.thinking_budget"
                type="number"
                min="1024"
                max="393215" /></label
            ><label
              >超时（秒）<input
                v-model.number="draft.timeout"
                type="number"
                min="5"
                max="1800" /></label
            ><label
              >输入字符上限<input
                v-model.number="draft.max_input_chars"
                type="number"
                min="10000"
                max="8000000" /></label
            ><label
              >输出 Token 上限<input
                v-model.number="draft.max_output_tokens"
                type="number"
                min="256"
                max="393216"
            /></label>
          </div>
        </details>
        <p class="muted settings-note">
          各预设独立保存密钥，修改用于新任务。同一接口留空保留密钥，换接口或主机需重填。
        </p>
        <footer class="dialog-footer">
          <button
            type="button"
            class="secondary"
            @click="models.close"
          >
            取消</button
          ><button
            class="primary"
            :disabled="busy"
          >
            {{ selectedId ? '保存修改' : '保存新预设' }}
          </button>
        </footer>
      </form></template
    ></AppModal
  ><AppModal
    :open="deleting"
    title="删除模型预设"
    @close="deleting = false"
    ><p>删除“{{ current?.name }}”及其密钥？</p>
    <footer class="dialog-footer">
      <button
        class="secondary"
        @click="deleting = false"
      >
        取消</button
      ><button
        class="danger"
        :disabled="busy"
        @click="
          run('preset', async () => {
            await models.remove();
            deleting = false;
          })
        "
      >
        删除
      </button>
    </footer></AppModal
  >
</template>
