import type { AgentTask, Job } from './types';

export function isExtraction(task: AgentTask): boolean {
  return (
    task.task_kind === 'extraction' ||
    (task.agent === 'Reader' &&
      task.parent_id === 'reader' &&
      ['整理参数与规则', '参数提取', '规则整理'].includes(task.role))
  );
}

export function readingProgress(job: Job) {
  const tasks = (job.tasks || []).filter((task) => isExtraction(task) && task.status !== 'split');
  const count = (...states: string[]) =>
    tasks.filter((task) => states.includes(task.status)).length;
  const completed = count('succeeded', 'cached');
  const unresolved = count('needs_input');
  const failed = count('failed', 'cancelled');
  const processed = completed + unresolved + failed;
  return {
    total: tasks.length,
    completed,
    processed,
    unresolved,
    failed,
    active: count('running'),
    waiting: count('queued', 'waiting'),
  };
}
