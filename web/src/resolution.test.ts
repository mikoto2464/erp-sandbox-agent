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
  it('引用错误展示经营操作，将内部编号折叠，用户无需填写说明即可重查', async () => {
    const t = setup();
    t.allJobs.value = [
      {
        ...t.pending,
        intervention: {
          ...t.pending.intervention!,
          id: 'reference-question',
          title: 'AI生成的操作未能关联题目依据',
          message: '部分操作使用的依据编号无法匹配当前题目，系统需要重新核对其来源。',
          help_text: '你不需要查找或填写 fact 编号，直接选择重新核对即可。',
          input_placeholder: '可留空，系统会重新查询工资依据。',
          technical_details:
            'periods.6 · w2-p6-senior-wage-payment：fact_7a1114af2fbc26e28af6d 不存在',
          details: {
            affected_actions: [
              { period_label: '第 2 年 · 第 2 季', description: '发放高级技工季度工资' },
            ],
          },
          options: [
            { id: 'retry', label: '重新核对题目依据并修正（推荐）', description: '说明可留空。' },
            { id: 'cancel', label: '取消本次生成', description: '保留历史方案。' },
          ],
        },
      },
    ];
    await flushPromises();
    expect(t.wrapper.find('.resolution-message').text()).not.toContain('fact_');
    expect(t.wrapper.text()).toContain('你不需要查找或填写');
    expect(t.wrapper.find('table').text()).toContain('第 2 年 · 第 2 季');
    expect(t.wrapper.find('table').text()).toContain('高级技工季度工资');
    expect(t.wrapper.find('.technical-diagnostic').attributes('open')).toBeUndefined();
    expect(t.wrapper.find('textarea').attributes('placeholder')).toContain('可留空');
    await t.wrapper.find('form').trigger('submit');
    await flushPromises();
    expect(t.answerJob).toHaveBeenCalledWith(t.allJobs.value[0], 'reference-question', 'retry', '');
    t.wrapper.unmount();
  });
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
