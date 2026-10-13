import { ref } from 'vue';
import { request } from '../lib/api';
import type { Job } from '../lib/types';

export function useApplicationJobs(onFinished: (job: Job) => void) {
  const jobs = ref<Job[]>([]);
  const observed = new Map<string, string>();
  let initialized = false,
    epoch = 0;
  let pending: Promise<void> | null = null;

  function refresh(): Promise<void> {
    if (pending) return pending;
    const captured = epoch;
    const operation = (async () => {
      const values = await request<Job[]>('/api/jobs');
      if (captured !== epoch) return;
      jobs.value = values;
      for (const job of values) {
        const previous = observed.get(job.id);
        observed.set(job.id, job.status);
        if (
          initialized &&
          previous !== job.status &&
          !job.metadata.upload_deleted &&
          ['succeeded', 'needs_input', 'failed', 'cancelled'].includes(job.status)
        )
          onFinished(job);
      }
      initialized = true;
    })();
    pending = operation;
    void operation
      .finally(() => {
        if (pending === operation) pending = null;
      })
      .catch(() => {});
    return operation;
  }

  function clear() {
    epoch++;
    pending = null;
    initialized = false;
    observed.clear();
    jobs.value = [];
  }
  return { jobs, refresh, clear };
}
