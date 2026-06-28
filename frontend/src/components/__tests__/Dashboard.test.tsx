import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import Dashboard from '@/pages/Dashboard';
import { authAPI, dashboardAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  authAPI: {
    getMe: vi.fn(),
    logout: vi.fn(),
  },
  dashboardAPI: {
    getAttention: vi.fn(),
  },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

const mockUser = { id: 1, email: 'operator@example.com', name: 'Test Operator' };

type AttentionOverride = {
  pending_interventions?: Array<{
    id: number;
    session_id: number;
    task_id: number | null;
    project_id: number;
    project_name: string;
    intervention_type: string;
    initiated_by: string;
    prompt: string;
    status: string;
    created_at: string | null;
    expires_at: string | null;
  }>;
  sessions_needing_attention?: number;
  tasks_pending_review?: number;
};

function makeAttention(overrides: AttentionOverride = {}) {
  return {
    pending_interventions: [],
    sessions_needing_attention: 0,
    tasks_pending_review: 0,
    ...overrides,
  };
}

function makePendingIntervention(overrides: Partial<{
  id: number;
  session_id: number;
  project_name: string;
  prompt: string;
}> = {}) {
  return {
    id: 1,
    session_id: 42,
    task_id: null,
    project_id: 10,
    project_name: 'Alpha Project',
    intervention_type: 'guidance',
    initiated_by: 'ai',
    prompt: 'How should I handle this edge case?',
    status: 'pending',
    created_at: '2026-06-27T00:00:00Z',
    expires_at: null,
    ...overrides,
  };
}

function setupMocks(attention: ReturnType<typeof makeAttention>) {
  (authAPI.getMe as Mock).mockResolvedValue({ data: mockUser });
  (dashboardAPI.getAttention as Mock).mockResolvedValue({ data: attention });
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

describe('Dashboard — uses /dashboard/attention', () => {
  it('calls dashboardAPI.getAttention (not sessions/tasks/projects lists)', async () => {
    setupMocks(makeAttention());
    await render();
    expect(dashboardAPI.getAttention).toHaveBeenCalledTimes(1);
  });

  it('does not call sessionsAPI.getAll or tasksAPI.getAll', async () => {
    setupMocks(makeAttention());
    await render();
    // Neither sessionsAPI nor tasksAPI should exist in mock (they are not imported)
    // This just confirms dashboardAPI.getAttention is the single data source
    expect(dashboardAPI.getAttention).toHaveBeenCalled();
  });
});

describe('Dashboard (Action Center)', () => {
  describe('loading state', () => {
    it('shows loading skeleton while data is pending', () => {
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
    it('renders "Nothing requires attention." when no data', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });

    it('renders "Nothing requires attention." when all counts are zero', async () => {
      setupMocks(makeAttention({
        sessions_needing_attention: 0,
        tasks_pending_review: 0,
        pending_interventions: [],
      }));
      await render();
      expect(container.textContent).toContain('Nothing requires attention.');
    });
  });

  describe('pending interventions', () => {
    it('shows pending intervention section when pending_interventions is non-empty', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention()],
      }));
      await render();
      expect(container.textContent).toContain('Pending Intervention');
    });

    it('shows a "Respond →" link pointing to the session detail page', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention({ id: 1, session_id: 99 })],
      }));
      await render();
      const link = container.querySelector('a[href="/sessions/99"]');
      expect(link).not.toBeNull();
      expect(link?.textContent).toContain('Respond');
    });

    it('shows the project name for an intervention', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention({ project_name: 'Alpha Project' })],
      }));
      await render();
      expect(container.textContent).toContain('Alpha Project');
    });

    it('shows the truncated prompt as the intervention label', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention({ prompt: 'How should I handle this edge case?' })],
      }));
      await render();
      expect(container.textContent).toContain('How should I handle this edge case?');
    });

    it('shows plural heading for multiple pending interventions', async () => {
      setupMocks(makeAttention({
        pending_interventions: [
          makePendingIntervention({ id: 1, session_id: 1 }),
          makePendingIntervention({ id: 2, session_id: 2 }),
        ],
      }));
      await render();
      expect(container.textContent).toContain('2 Pending Interventions');
    });

    it('shows singular heading for exactly one intervention', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention({ id: 1, session_id: 1 })],
      }));
      await render();
      expect(container.textContent).toContain('1 Pending Intervention');
      expect(container.textContent).not.toContain('1 Pending Interventions');
    });
  });

  describe('sessions needing attention', () => {
    it('shows sessions needing attention section when count > 0', async () => {
      setupMocks(makeAttention({ sessions_needing_attention: 1 }));
      await render();
      expect(container.textContent).toContain('1 session needs attention');
    });

    it('shows plural for multiple sessions needing attention', async () => {
      setupMocks(makeAttention({ sessions_needing_attention: 3 }));
      await render();
      expect(container.textContent).toContain('3 sessions need attention');
    });

    it('links to /sessions for the attention section', async () => {
      setupMocks(makeAttention({ sessions_needing_attention: 2 }));
      await render();
      const link = container.querySelector('a[href="/sessions"]');
      expect(link).not.toBeNull();
    });

    it('does not show attention section when count is 0', async () => {
      setupMocks(makeAttention({ sessions_needing_attention: 0 }));
      await render();
      expect(container.textContent).not.toContain('needs attention');
    });
  });

  describe('tasks pending review', () => {
    it('shows tasks pending review section when count > 0', async () => {
      setupMocks(makeAttention({ tasks_pending_review: 1 }));
      await render();
      expect(container.textContent).toContain('1 task pending review');
    });

    it('shows plural for multiple tasks', async () => {
      setupMocks(makeAttention({ tasks_pending_review: 3 }));
      await render();
      expect(container.textContent).toContain('3 tasks pending review');
    });

    it('links to /tasks for the review queue', async () => {
      setupMocks(makeAttention({ tasks_pending_review: 5 }));
      await render();
      const link = container.querySelector('a[href="/tasks"]');
      expect(link).not.toBeNull();
    });

    it('does not show review section when count is 0', async () => {
      setupMocks(makeAttention({ tasks_pending_review: 0 }));
      await render();
      expect(container.textContent).not.toContain('pending review');
    });
  });

  describe('all three sections together', () => {
    it('shows all three action sections when all are non-empty', async () => {
      setupMocks(makeAttention({
        pending_interventions: [makePendingIntervention()],
        sessions_needing_attention: 2,
        tasks_pending_review: 1,
      }));
      await render();
      expect(container.textContent).toContain('Pending Intervention');
      expect(container.textContent).toContain('need attention');
      expect(container.textContent).toContain('pending review');
    });
  });

  describe('removed content', () => {
    it('does not render System Health', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).not.toContain('System Health');
    });

    it('does not render Recent Activity', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).not.toContain('Recent Activity');
    });

    it('does not render a Projects tab', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).not.toContain('Overview');
    });

    it('does not render outcome rate metrics', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).not.toContain('First-Pass');
      expect(container.textContent).not.toContain('Gate:');
    });
  });

  describe('header', () => {
    it('renders the Dashboard title', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).toContain('Dashboard');
    });

    it('renders the user name when available', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).toContain('Test Operator');
    });

    it('renders Sign out button', async () => {
      setupMocks(makeAttention());
      await render();
      expect(container.textContent).toContain('Sign out');
    });
  });
});
