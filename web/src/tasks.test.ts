import { describe, expect, it } from 'vitest';
import { readingProgress } from './lib/tasks';
import { distinctIssues } from './lib/review';
import type { AgentTask, Issue, Job } from './lib/types';

describe('reading progress and review summaries', () => {
  it('counts extraction status changes independently of coordination, review and split parents', () => {
    const task = (id: string, status: string): AgentTask => ({
      id,
      status,
      agent: 'Reader',
      role: '整理参数与规则',
      parent_id: 'reader',
      object: id,
    });
    const job = {
      kind: 'read',
      tasks: [
        task('chapter', 'running'),
        task('table', 'waiting'),
        task('old', 'split'),
        { ...task('main', 'succeeded'), agent: '主 Agent' },
        { ...task('review', 'running'), role: '关联复核' },
      ],
    } as Job;
    expect(readingProgress(job)).toEqual({
      total: 2,
      completed: 0,
      processed: 0,
      active: 1,
      waiting: 1,
      unresolved: 0,
      failed: 0,
    });
    job.tasks![0].status = 'cached';
    job.tasks![1].status = 'needs_input';
    expect(readingProgress(job)).toEqual({
      total: 2,
      completed: 1,
      processed: 2,
      active: 0,
      waiting: 0,
      unresolved: 1,
      failed: 0,
    });
  });

  it('includes template reading and solver roles while excluding model dispatch tasks', () => {
    const task = { agent: 'Reader', parent_id: 'reader', role: '空表理解', status: 'succeeded' };
    expect(readingProgress({ kind: 'read', tasks: [task] } as Job).total).toBe(1);
    const splitTemplate = {
      kind: 'read',
      tasks: [
        { ...task, id: 'template-read:sheet' },
        { ...task, id: 'child', parent_id: 'template-read:sheet' },
      ],
    } as Job;
    expect(readingProgress(splitTemplate).total).toBe(1);
    const roles = ['operations', 'finance', 'synthesis'].map((id, index) => ({
      id,
      agent: 'Solver',
      parent_id: 'solver',
      role: id,
      status: index === 0 ? 'succeeded' : 'queued',
    }));
    const job = { kind: 'solve', tasks: [...roles, { ...roles[0], agent: '主 Agent' }] } as Job;
    expect(readingProgress(job).total).toBe(3);
    expect(readingProgress(job).processed).toBe(1);
    expect(readingProgress(job).waiting).toBe(2);
  });

  it('shows each specific cause once while preserving separate genuine concerns', () => {
    const issue = (code: string, target: string, message: string): Issue => ({
      code,
      target,
      message,
      severity: 'warning',
      evidence_ids: [],
    });
    const specific = issue('validation.unsupported_value', 'name', '原文不支持。');
    const semantic = issue('review.timing', 'name', '何时付款？');
    const missing = issue('validation.value_missing', 'salary', '工资未提供。');
    const timing = issue('review.timing', 'salary', '何时支付工资？');
    expect(
      distinctIssues([
        specific,
        specific,
        issue('validation.uncertain', 'name', '仍有冲突。'),
        semantic,
        missing,
        timing,
      ]),
    ).toEqual([specific, semantic, missing, timing]);
  });
});
