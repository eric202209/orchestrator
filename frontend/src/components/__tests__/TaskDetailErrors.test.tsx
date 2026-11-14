import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import TaskDetail from '@/pages/TaskDetail';
import { projectsAPI, tasksAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  tasksAPI: { getById: vi.fn(), getChangeSet: vi.fn(), retry: vi.fn() },
  projectsAPI: { getById: vi.fn() },
}));

afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); });

describe('TaskDetail Run feedback', () => {
  it.each([
    ['Readable refusal', 'Readable refusal'],
    [{ code: 'DEVELOPMENT_EXECUTION_DISABLED', message: 'Execution is disabled in this development mode.' }, 'Execution is disabled in this development mode.'],
    [{ code: 'EXECUTION_DISABLED' }, 'EXECUTION_DISABLED'],
    [null, 'Failed to queue the task for another run'],
    ['success', null],
  ])('renders response %j', async (detail, expected) => {
    vi.mocked(tasksAPI.getById).mockResolvedValue({ data: {
      id: 1, project_id: 1, title: 'Example task', status: 'pending',
      current_step: 0, workspace_status: 'not_created',
      created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
    } } as Awaited<ReturnType<typeof tasksAPI.getById>>);
    vi.mocked(tasksAPI.getChangeSet).mockRejectedValue(new Error('No change set'));
    vi.mocked(projectsAPI.getById).mockResolvedValue({ data: { id: 1, name: 'Example project' } } as Awaited<ReturnType<typeof projectsAPI.getById>>);
    if (detail === 'success') vi.mocked(tasksAPI.retry).mockResolvedValue({ data: {} } as Awaited<ReturnType<typeof tasksAPI.retry>>);
    else vi.mocked(tasksAPI.retry).mockRejectedValue(detail === null ? new Error('Network Error') : { response: { data: { detail } } });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    try {
      await act(async () => { root.render(<MemoryRouter initialEntries={['/projects/1/tasks/1']}><Routes><Route path="/projects/:projectId/tasks/:taskId" element={<TaskDetail />} /></Routes></MemoryRouter>); });
      const run = Array.from(container.querySelectorAll('button')).find(button => button.textContent === 'Run');
      expect(run).toBeDefined();
      await act(async () => { run!.click(); });
      expect(tasksAPI.retry).toHaveBeenCalledWith(1, { retry_intent: 'ordinary' });
      if (expected) expect(container.textContent).toContain(expected);
      else expect(tasksAPI.getById).toHaveBeenCalledTimes(2);
      expect(container.textContent).toContain('Example task');
      expect(container.textContent).not.toContain('[object Object]');
    } finally {
      await act(async () => { root.unmount(); });
      container.remove();
    }
  });
});
