import type { AgentTask, Job } from './types';

export function isExtraction(task: AgentTask): boolean {
  return (
    task.task_kind === 'extraction' ||
    (task.agent === 'Reader' &&
      (task.parent_id === 'reader' ||
        (task.role === '空表理解' && !!task.parent_id?.startsWith('template-read:'))) &&
      ['整理参数与规则', '参数提取', '规则整理', '空表理解'].includes(task.role))
  );
}

export function readingProgress(job: Job) {
  const candidates = (job.tasks || []).filter(
    (task) =>
      (['artifact', 'mapping'].includes(job.kind)
        ? !!task.parent_id && ['Answerer', 'Validator'].includes(task.agent)
        : ['solve', 'budget'].includes(job.kind)
          ? task.agent === 'Solver' &&
            !!task.parent_id &&
            (task.parent_id === 'solver' || task.parent_id.startsWith('solver:'))
          : isExtraction(task)) && task.status !== 'split',
  );
  const parents = new Set(candidates.map((task) => task.parent_id).filter(Boolean));
  const tasks = candidates.filter((task) => !parents.has(task.id));
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
