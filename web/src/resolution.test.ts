import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { ref } from 'vue';
import JobResolution from './components/JobResolution.vue';
import { workspaceKey, type Workspace } from './composables/context';
import type { Job } from './lib/types';

function setup() {
  const pending: Job = {
    id: 'job',
    kind: 'solve',
    status: 'needs_input',
    stage: 'awaiting_resolution',
    metadata: {},
    scope_name: '测试项目',
    intervention: {
      id: 'question-1',
      task_id: 'solver:synthesis:window:2',
      title: '方案生成需要你的处理',
      code: 'planning.cash_unit',
      message: '第5–8期现金单位需要核对。',
      details: {
        cash_unit: '元',
        cash_limit: '100',
        actions: [
          { action_id: 'fee', period: 5, description: '支付管理费', amount: '10', unit: '元/季' },
        ],
      },
      options: [
        { id: 'use_unit', label: '确认金额以元计量', description: '保持数值，再校验。' },
        { id: 'retry', label: '按修正说明继续', description: '只重新处理本段。' },
        { id: 'cancel', label: '取消本次生成', description: '保留历史方案。' },
      ],
    },
  };
  const allJobs = ref<Job[]>([pending]),
    resolutionJobId = ref<string | null>(null),
    authenticated = ref(true);
  const answerJob = vi.fn().mockResolvedValue(undefined);
  const wrapper = mount(JobResolution, {
    attachTo: document.body,
    global: {
      provide: {
        [workspaceKey as symbol]: {
          allJobs,
          resolutionJobId,
          authenticated,
          answerJob,
        } as unknown as Workspace,
      },
    },
  });
  return { wrapper, allJobs, resolutionJobId, authenticated, answerJob, pending };
}
afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
});
describe('方案问题处理窗口', () => {
  it('自动展示具体金额和选项，不自动提交，关闭后仍可从任务记录打开', async () => {
    const t = setup();
    await flushPromises();
    expect(t.wrapper.text()).toContain('支付管理费');
    expect(t.wrapper.text()).toContain('元/季');
    expect(t.answerJob).not.toHaveBeenCalled();
    await t.wrapper.find('[aria-label="关闭窗口"]').trigger('click');
    expect(t.resolutionJobId.value).toBeNull();
    t.allJobs.value = [...t.allJobs.value];
    await flushPromises();
    expect(t.resolutionJobId.value).toBeNull();
    t.resolutionJobId.value = 'job';
    await flushPromises();
    expect(t.wrapper.find('form').exists()).toBe(true);
    t.wrapper.unmount();
  });
  it('提交用户选择和自由说明；失败保留窗口、显示诊断', async () => {
    const t = setup();
    await flushPromises();
    t.answerJob.mockRejectedValueOnce(new Error('问题已更新，请刷新。'));
    await t.wrapper.find('input[value="retry"]').setValue(true);
    await t.wrapper.find('textarea').setValue('按季度付款总额，以元表示。');
    await t.wrapper.find('form').trigger('submit');
    await flushPromises();
    expect(t.answerJob).toHaveBeenCalledWith(
      t.pending,
      'question-1',
      'retry',
      '按季度付款总额，以元表示。',
    );
    expect(t.wrapper.find('[role="alert"]').text()).toContain('问题已更新');
    expect(t.resolutionJobId.value).toBe('job');
    t.wrapper.unmount();
  });
  it('刷新后发现待处理问题，新问题重新提示，退出登录关闭窗口', async () => {
    const t = setup();
    await flushPromises();
    t.resolutionJobId.value = null;
    t.allJobs.value = [
      {
        ...t.pending,
        intervention: { ...t.pending.intervention!, id: 'question-2', message: '新的问题' },
      },
    ];
    await flushPromises();
    expect(t.wrapper.text()).toContain('新的问题');
    t.authenticated.value = false;
    await flushPromises();
    expect(t.resolutionJobId.value).toBeNull();
    t.wrapper.unmount();
  });
});
