import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import SessionsList from '@/pages/SessionsList';
import { sessionsAPI, projectsAPI, tasksAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  sessionsAPI: { getAll: vi.fn() },
  projectsAPI: { getAll: vi.fn() },
  tasksAPI: { getAll: vi.fn() },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

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
  model_lane_label: null,
  model_lane_metadata: null,
  created_at: '2026-06-27T00:00:00Z',
  updated_at: '2026-06-27T00:00:00Z',
  started_at: '2026-06-27T00:00:00Z',
  stopped_at: null,
  paused_at: null,
  resumed_at: null,
  last_alert_message: null,
  last_alert_at: null,
  ...overrides,
});

function setupMocks(sessions: ReturnType<typeof makeSession>[] = []) {
  (sessionsAPI.getAll as Mock).mockResolvedValue({ data: sessions });
  (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
  (tasksAPI.getAll as Mock).mockResolvedValue({ data: [] });
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
        <SessionsList />
      </MemoryRouter>,
    );
  });
}

// ── tests ─────────────────────────────────────────────────────────────────────

describe('SessionsList — default filter (all)', () => {
  it('defaults to all: shows failed and running sessions without switching filter', async () => {
    setupMocks([
      makeSession({ id: 1, name: 'Failed Run', status: 'failed' }),
      makeSession({ id: 2, name: 'Running Run', status: 'running' }),
    ]);
    await render();
    expect(container.textContent).toContain('Failed Run');
    expect(container.textContent).toContain('Running Run');
  });

  it('defaults to all: shows completed sessions with logs/checkpoints reachable', async () => {
    setupMocks([
      makeSession({ id: 3, name: 'Blocked Session', status: 'awaiting_input' }),
      makeSession({ id: 4, name: 'Completed Session', status: 'done' }),
    ]);
    await render();
    expect(container.textContent).toContain('Blocked Session');
    expect(container.textContent).toContain('Completed Session');
  });

  it('defaults to all: shows error sessions', async () => {
    setupMocks([
      makeSession({ id: 5, name: 'Error Run', status: 'error' }),
    ]);
    await render();
    expect(container.textContent).toContain('Error Run');
  });

  it('the all filter button is visually selected on load', async () => {
    setupMocks([]);
    await render();
    const buttons = Array.from(container.querySelectorAll('button[type="button"]'));
    const allBtn = buttons.find((b) => b.textContent?.startsWith('All'));
    expect(allBtn).not.toBeNull();
    expect(allBtn?.className).toContain('border-primary-500');
  });
});

describe('SessionsList — needs_attention empty state', () => {
  it('shows "No sessions need attention." when all sessions are healthy', async () => {
    setupMocks([
      makeSession({ id: 1, status: 'running' }),
      makeSession({ id: 2, status: 'done' }),
    ]);
    await render();
    const attentionBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.includes('Needs attention'),
    );
    expect(attentionBtn).not.toBeNull();
    await act(async () => { (attentionBtn as HTMLElement).click(); });
    expect(container.textContent).toContain('No sessions need attention.');
  });

  it('shows "View all sessions" in the needs_attention empty state', async () => {
    setupMocks([makeSession({ id: 1, status: 'running' })]);
    await render();
    const attentionBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.includes('Needs attention'),
    );
    expect(attentionBtn).not.toBeNull();
    await act(async () => { (attentionBtn as HTMLElement).click(); });
    expect(container.textContent).toContain('View all sessions');
  });

  it('shows "No runs yet" when there are no sessions at all (sessions-level empty state)', async () => {
    setupMocks([]);
    await render();
    expect(container.textContent).toContain('No runs yet');
  });

  it('clicking "View all sessions" switches to the all filter and shows all sessions', async () => {
    setupMocks([
      makeSession({ id: 1, name: 'Running Run', status: 'running' }),
    ]);
    await render();
    const attentionBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.includes('Needs attention'),
    );
    expect(attentionBtn).not.toBeNull();
    await act(async () => { (attentionBtn as HTMLElement).click(); });
    // confirm the needs_attention empty state is showing
    expect(container.textContent).toContain('No sessions need attention.');

    // click "View all sessions"
    const viewAllBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('View all sessions'),
    );
    expect(viewAllBtn).not.toBeNull();
    await act(async () => { (viewAllBtn as HTMLElement).click(); });

    // now "all" is active — the running session should be visible
    expect(container.textContent).toContain('Running Run');
    expect(container.textContent).not.toContain('No sessions need attention.');
  });
});

describe('SessionsList — filter switching', () => {
  it('switching to "All" filter shows all sessions including running', async () => {
    setupMocks([
      makeSession({ id: 1, name: 'Healthy Run', status: 'running' }),
      makeSession({ id: 2, name: 'Failed Run', status: 'failed' }),
    ]);
    await render();

    const allBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('All'),
    );
    expect(allBtn).not.toBeNull();
    await act(async () => { (allBtn as HTMLElement).click(); });

    expect(container.textContent).toContain('Healthy Run');
    expect(container.textContent).toContain('Failed Run');
  });

  it('switching to "Active" filter shows only active sessions', async () => {
    setupMocks([
      makeSession({ id: 1, name: 'Live Run', status: 'running' }),
      makeSession({ id: 2, name: 'Done Run', status: 'done' }),
    ]);
    await render();

    const activeBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('Active'),
    );
    expect(activeBtn).not.toBeNull();
    await act(async () => { (activeBtn as HTMLElement).click(); });

    expect(container.textContent).toContain('Live Run');
    expect(container.textContent).not.toContain('Done Run');
  });

  it('all filter options are present', async () => {
    setupMocks([]);
    await render();
    const filterText = container.textContent;
    expect(filterText).toContain('All');
    expect(filterText).toContain('Active');
    expect(filterText).toContain('Needs attention');
    expect(filterText).toContain('Completed');
    expect(filterText).toContain('Stopped');
  });
});

describe('SessionsList — no sessions at all', () => {
  it('shows "No runs yet" global empty state when no sessions exist regardless of filter', async () => {
    setupMocks([]);
    await render();
    expect(container.textContent).toContain('No runs yet');
    expect(container.textContent).not.toContain('No sessions need attention.');
  });
});
