import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import Dashboard from '@/pages/Dashboard';
import { authAPI, sessionsAPI, tasksAPI, projectsAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  authAPI: {
    getMe: vi.fn(),
    logout: vi.fn(),
  },
  sessionsAPI: {
    getAll: vi.fn(),
  },
  tasksAPI: {
    getAll: vi.fn(),
  },
  projectsAPI: {
    getAll: vi.fn(),
  },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

const mockUser = { id: 1, email: 'operator@example.com', name: 'Test Operator' };

const makeSession = (overrides: Partial<{
  id: number;
  name: string;
  project_id: number;
  status: string;
}> = {}) => ({
  id: 1,
  name: 'Test Session',
  project_id: 10,
  status: 'running',
  is_active: true,
  description: null,
  execution_mode: 'automatic' as const,
  default_execution_profile: 'full_lifecycle' as const,
  created_at: '2026-06-27T00:00:00Z',
  updated_at: null,
  started_at: null,
  stopped_at: null,
  paused_at: null,
  resumed_at: null,
  ...overrides,
});

const makeTask = (overrides: Partial<{
  id: number;
  title: string;
  workspace_status: string | null;
  project_id: number;
  session_id: number | null;
}> = {}) => ({
  id: 1,
  title: 'Test Task',
  project_id: 10,
  session_id: null,
  status: 'done' as const,
  workspace_status: null,
  description: null,
  execution_profile: 'full_lifecycle' as const,
  priority: 1,
  plan_position: null,
  steps: null,
  current_step: 0,
  error_message: null,
  created_at: '2026-06-27T00:00:00Z',
  updated_at: null,
  started_at: null,
  completed_at: null,
  ...overrides,
});

const mockProject = { id: 10, name: 'Alpha Project', description: null, github_url: null, branch: 'main', created_at: '2026-06-27T00:00:00Z', updated_at: null };

function setupMocks({
  sessions = [] as ReturnType<typeof makeSession>[],
  tasks = [] as ReturnType<typeof makeTask>[],
} = {}) {
  (authAPI.getMe as Mock).mockResolvedValue({ data: mockUser });
  (sessionsAPI.getAll as Mock).mockResolvedValue({ data: sessions });
  (tasksAPI.getAll as Mock).mockResolvedValue({ data: tasks });
  (projectsAPI.getAll as Mock).mockResolvedValue({ data: [mockProject] });
}

// ── test harness ──────────────────────────────────────────────────────────────

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
  vi.clearAllMocks();
  vi.useRealTimers();
});

async function render() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
  });
}

// ── tests ─────────────────────────────────────────────────────────────────────

describe('Dashboard (Action Center)', () => {
  describe('loading state', () => {
    it('shows loading skeleton while data is pending', () => {
      // Auth is pending — never resolves in this tick
      (authAPI.getMe as Mock).mockReturnValue(new Promise(() => {}));
      act(() => {
        root.render(
          <MemoryRouter>
            <Dashboard />
          </MemoryRouter>,
        );
      });
      const skeletons = container.querySelectorAll('[class*="animate-pulse"]');
      expect(skeletons.length).toBeGreaterThan(0);
    });
  });

  describe('all-clear state', () => {
    it('renders "Nothing requires attention." when there is nothing actionable', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });

    it('renders "Nothing requires attention." when sessions are all running (not needing attention)', async () => {
      setupMocks({
        sessions: [makeSession({ status: 'running' })],
        tasks: [],
      });
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });

    it('renders "Nothing requires attention." when tasks have no pending review', async () => {
      setupMocks({
        sessions: [],
        tasks: [makeTask({ workspace_status: 'accepted' })],
      });
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });
  });

  describe('pending interventions', () => {
    it('shows a pending intervention when a session is awaiting_input', async () => {
      setupMocks({
        sessions: [makeSession({ id: 42, name: 'My Run', status: 'awaiting_input' })],
      });
      await render();
      expect(container.textContent).toContain('Pending Intervention');
      expect(container.textContent).toContain('My Run');
    });

    it('shows a "Respond →" link pointing to the session detail page', async () => {
      setupMocks({
        sessions: [makeSession({ id: 99, name: 'Blocked Session', status: 'awaiting_input' })],
      });
      await render();
      const link = container.querySelector('a[href="/sessions/99"]');
      expect(link).not.toBeNull();
      expect(link?.textContent).toContain('Respond');
    });

    it('shows the project name for an intervention session', async () => {
      setupMocks({
        sessions: [makeSession({ id: 5, name: 'Session X', project_id: 10, status: 'awaiting_input' })],
      });
      await render();
      expect(container.textContent).toContain('Alpha Project');
    });

    it('shows a plural heading for multiple pending interventions', async () => {
      setupMocks({
        sessions: [
          makeSession({ id: 1, name: 'Session A', status: 'awaiting_input' }),
          makeSession({ id: 2, name: 'Session B', status: 'awaiting_input' }),
        ],
      });
      await render();
      expect(container.textContent).toContain('2 Pending Interventions');
    });

    it('shows a singular heading for exactly one intervention', async () => {
      setupMocks({
        sessions: [makeSession({ id: 1, name: 'Only Session', status: 'awaiting_input' })],
      });
      await render();
      expect(container.textContent).toContain('1 Pending Intervention');
      expect(container.textContent).not.toContain('1 Pending Interventions');
    });
  });

  describe('sessions needing attention', () => {
    it('shows paused sessions count', async () => {
      setupMocks({
        sessions: [makeSession({ id: 3, name: 'Paused Run', status: 'paused' })],
      });
      await render();
      expect(container.textContent).toContain('1 session needs attention');
    });

    it('shows failed sessions count', async () => {
      setupMocks({
        sessions: [makeSession({ id: 4, name: 'Failed Run', status: 'failed' })],
      });
      await render();
      expect(container.textContent).toContain('1 session needs attention');
    });

    it('links to /sessions for attention sessions', async () => {
      setupMocks({
        sessions: [makeSession({ id: 7, status: 'paused' })],
      });
      await render();
      const link = container.querySelector('a[href="/sessions"]');
      expect(link).not.toBeNull();
    });

    it('shows plural count for multiple attention sessions', async () => {
      setupMocks({
        sessions: [
          makeSession({ id: 1, status: 'paused' }),
          makeSession({ id: 2, status: 'failed' }),
          makeSession({ id: 3, status: 'paused' }),
        ],
      });
      await render();
      expect(container.textContent).toContain('3 sessions need attention');
    });

    it('does not show attention sessions section for running sessions', async () => {
      setupMocks({
        sessions: [makeSession({ id: 1, status: 'running' })],
      });
      await render();
      expect(container.textContent).not.toContain('needs attention');
    });
  });

  describe('tasks pending review', () => {
    it('shows tasks with workspace_status=ready as pending review', async () => {
      setupMocks({
        tasks: [makeTask({ id: 11, title: 'Add feature', workspace_status: 'ready' })],
      });
      await render();
      expect(container.textContent).toContain('1 task pending review');
    });

    it('shows plural count for multiple review tasks', async () => {
      setupMocks({
        tasks: [
          makeTask({ id: 1, workspace_status: 'ready' }),
          makeTask({ id: 2, workspace_status: 'ready' }),
          makeTask({ id: 3, workspace_status: 'ready' }),
        ],
      });
      await render();
      expect(container.textContent).toContain('3 tasks pending review');
    });

    it('links to /tasks for the review queue', async () => {
      setupMocks({
        tasks: [makeTask({ id: 5, workspace_status: 'ready' })],
      });
      await render();
      const link = container.querySelector('a[href="/tasks"]');
      expect(link).not.toBeNull();
    });

    it('does not count tasks with accepted workspace_status', async () => {
      setupMocks({
        tasks: [makeTask({ id: 1, workspace_status: 'accepted' })],
      });
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });

    it('does not count tasks with null workspace_status', async () => {
      setupMocks({
        tasks: [makeTask({ id: 1, workspace_status: null })],
      });
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });
  });

  describe('all three sections together', () => {
    it('shows all three action sections when all are non-empty', async () => {
      setupMocks({
        sessions: [
          makeSession({ id: 1, name: 'Waiting Session', status: 'awaiting_input' }),
          makeSession({ id: 2, status: 'paused' }),
        ],
        tasks: [makeTask({ id: 10, workspace_status: 'ready' })],
      });
      await render();
      expect(container.textContent).toContain('Pending Intervention');
      expect(container.textContent).toContain('needs attention');
      expect(container.textContent).toContain('pending review');
    });
  });

  describe('removed content', () => {
    it('does not render System Health', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).not.toContain('System Health');
    });

    it('does not render Recent Activity', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).not.toContain('Recent Activity');
    });

    it('does not render a Projects tab', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).not.toContain('Overview');
    });

    it('does not render outcome rate metrics', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).not.toContain('First-Pass');
      expect(container.textContent).not.toContain('Gate:');
    });
  });

  describe('header', () => {
    it('renders the Dashboard title', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).toContain('Dashboard');
    });

    it('renders the user name when available', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).toContain('Test Operator');
    });

    it('renders Sign out button', async () => {
      setupMocks({ sessions: [], tasks: [] });
      await render();
      expect(container.textContent).toContain('Sign out');
    });
  });
});
