import { afterEach, describe, expect, it, vi } from 'vitest';
import { request } from './lib/api';
import { useApplicationJobs } from './composables/useApplicationJobs';
import type { Job } from './lib/types';

afterEach(() => vi.unstubAllGlobals());

describe('application jobs and API errors', () => {
  it('keeps all scopes, reports transitions once and ignores stale responses after logout', async () => {
    const finished = vi.fn();
    const tasks = useApplicationJobs(finished);
    const values: Job[] = [
      {
        id: 'project',
        project_id: 'project-a',
        kind: 'read',
        status: 'succeeded',
        stage: 'done',
        metadata: {},
      },
      {
        id: 'template',
        project_id: 'template-library',
        kind: 'read',
        status: 'running',
        stage: 'reading_template',
        metadata: {},
      },
    ];
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(values))),
    );
    await tasks.refresh();
    expect(tasks.jobs.value).toHaveLength(2);
    expect(finished).not.toHaveBeenCalled();
    values[1].status = 'needs_input';
    await tasks.refresh();
    await tasks.refresh();
    expect(finished).toHaveBeenCalledTimes(1);
    expect(finished.mock.calls[0][0].project_id).toBe('template-library');
    let resolve!: (value: Response) => void;
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((done) => {
            resolve = done;
          }),
      ),
    );
    const pending = tasks.refresh();
    expect(tasks.refresh()).toBe(pending);
    tasks.clear();
    resolve(new Response(JSON.stringify(values)));
    await pending;
    expect(tasks.jobs.value).toHaveLength(0);
  });

  it('turns HTML responses into useful errors and still expires unauthorized sessions', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('<!DOCTYPE html><h1>Bad request</h1>', { status: 400 })),
    );
    await expect(request('/api/template-library/test/read')).rejects.toMatchObject({
      code: 'invalid_api_response',
      status: 400,
      message: expect.stringContaining('/api'),
    });
    const expired = vi.fn();
    window.addEventListener('erp-session-expired', expired);
    try {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response('<!DOCTYPE html>', { status: 401 })),
      );
      await expect(request('/api/jobs')).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('erp-session-expired', expired);
    }
  });
});
