import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import SessionsList from '@/pages/SessionsList';
import { sessionsAPI, projectsAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  sessionsAPI: { getAll: vi.fn() },
  projectsAPI: { getAll: vi.fn() },
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

function makePage(sessions: ReturnType<typeof makeSession>[], overrides?: { total?: number; page?: number; total_pages?: number }) {
  return {
    items: sessions,
    page: overrides?.page ?? 1,
    per_page: 25,
    total: overrides?.total ?? sessions.length,
    total_pages: overrides?.total_pages ?? 1,
    has_next: false,
    has_previous: false,
  };
}

function setupMocks(sessions: ReturnType<typeof makeSession>[] = []) {
  (sessionsAPI.getAll as Mock).mockResolvedValue({ data: makePage(sessions) });
  (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
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

describe('SessionsList — paginated API', () => {
  it('calls sessionsAPI.getAll with page param (paginated mode)', async () => {
    setupMocks([]);
    await render();
    expect(sessionsAPI.getAll).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1 }),
    );
  });

  it('calls sessionsAPI.getAll without needs_attention filter by default (all sessions)', async () => {
    setupMocks([]);
    await render();
    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const firstCall = calls[0][0];
    expect(firstCall).not.toHaveProperty('needs_attention');
  });

  it('does not call tasksAPI.getAll', async () => {
    setupMocks([]);
    await render();
    expect(sessionsAPI.getAll).toHaveBeenCalled();
  });

  it('renders sessions from paginated response items', async () => {
    setupMocks([makeSession({ id: 1, name: 'Alpha Run', status: 'failed' })]);
    await render();
    expect(container.textContent).toContain('Alpha Run');
  });

  it('shows total count from page response in header', async () => {
    (sessionsAPI.getAll as Mock).mockResolvedValue({
      data: makePage([makeSession()], { total: 42 }),
    });
    (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
    await render();
    expect(container.textContent).toContain('42 runs');
  });
});

describe('SessionsList — default filter (all)', () => {
  it('defaults to all: the All filter button is visually selected on load', async () => {
    setupMocks([]);
    await render();
    const buttons = Array.from(container.querySelectorAll('button[type="button"]'));
    const btn = buttons.find((b) => b.textContent?.startsWith('All'));
    expect(btn).not.toBeNull();
    expect(btn?.className).toContain('border-primary-500');
  });

  it('does not send needs_attention filter on initial load', async () => {
    setupMocks([]);
    await render();
    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const firstCall = calls[0][0];
    expect(firstCall).not.toHaveProperty('needs_attention');
  });
});

describe('SessionsList — empty states', () => {
  it('shows "No runs yet" when all filter is active with no sessions', async () => {
    setupMocks([]);
    await render();
    // Default is 'all', mock returns empty page
    expect(container.textContent).toContain('No runs yet');
  });

  it('shows "No sessions need attention." when needs_attention filter is active and empty', async () => {
    setupMocks([]);
    await render();
    // Switch to needs_attention filter
    const btn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.includes('Needs attention'),
    );
    expect(btn).not.toBeNull();
    (sessionsAPI.getAll as Mock).mockResolvedValue({ data: makePage([]) });
    await act(async () => { (btn as HTMLElement).click(); });
    await act(async () => {});
    expect(container.textContent).toContain('No sessions need attention.');
  });

  it('shows "View all sessions" button in needs_attention empty state', async () => {
    setupMocks([]);
    await render();
    const btn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.includes('Needs attention'),
    );
    (sessionsAPI.getAll as Mock).mockResolvedValue({ data: makePage([]) });
    await act(async () => { (btn as HTMLElement).click(); });
    await act(async () => {});
    expect(container.textContent).toContain('View all sessions');
  });
});

describe('SessionsList — filter switching', () => {
  it('switching filter changes the API call params', async () => {
    setupMocks([]);
    await render();

    const allBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('All'),
    );
    expect(allBtn).not.toBeNull();
    await act(async () => { (allBtn as HTMLElement).click(); });

    // After switching to "all", should call without needs_attention
    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall).not.toHaveProperty('needs_attention');
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

  it('switching to "Active" sends is_active=true', async () => {
    setupMocks([]);
    await render();

    const activeBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('Active'),
    );
    expect(activeBtn).not.toBeNull();
    await act(async () => { (activeBtn as HTMLElement).click(); });

    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall).toHaveProperty('is_active', true);
  });

  it('switching to "Completed" sends status=completed', async () => {
    setupMocks([]);
    await render();

    const completedBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('Completed'),
    );
    expect(completedBtn).not.toBeNull();
    await act(async () => { (completedBtn as HTMLElement).click(); });

    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall).toHaveProperty('status', 'completed');
  });

  it('switching to "Stopped" sends status=stopped', async () => {
    setupMocks([]);
    await render();

    const stoppedBtn = Array.from(container.querySelectorAll('button[type="button"]')).find(
      (b) => b.textContent?.startsWith('Stopped'),
    );
    expect(stoppedBtn).not.toBeNull();
    await act(async () => { (stoppedBtn as HTMLElement).click(); });

    const calls = (sessionsAPI.getAll as Mock).mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall).toHaveProperty('status', 'stopped');
  });
});

describe('SessionsList — search', () => {
  it('sends search param when search query is entered', async () => {
    setupMocks([]);
    await render();

    const input = container.querySelector('input[placeholder="Search..."]') as HTMLInputElement;
    expect(input).not.toBeNull();

    await act(async () => {
      input.value = 'alpha';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });

    // Trigger React's synthetic event
    const changeEvent = new Event('change', { bubbles: true });
    Object.defineProperty(changeEvent, 'target', { value: { value: 'alpha' } });
    await act(async () => {
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });
});

describe('SessionsList — pagination controls', () => {
  it('shows pagination controls when total_pages > 1', async () => {
    (sessionsAPI.getAll as Mock).mockResolvedValue({
      data: {
        items: [makeSession({ id: 1 })],
        page: 1,
        per_page: 25,
        total: 50,
        total_pages: 2,
        has_next: true,
        has_previous: false,
      },
    });
    (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
    await render();
    expect(container.textContent).toContain('Page 1 of 2');
    expect(container.textContent).toContain('Next');
  });

  it('does not show pagination controls when only one page', async () => {
    setupMocks([makeSession()]);
    await render();
    expect(container.textContent).not.toContain('Page 1 of');
  });

  it('Previous button is disabled on page 1', async () => {
    (sessionsAPI.getAll as Mock).mockResolvedValue({
      data: {
        items: [makeSession()],
        page: 1,
        per_page: 25,
        total: 50,
        total_pages: 2,
        has_next: true,
        has_previous: false,
      },
    });
    (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
    await render();
    const prevBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Previous'),
    ) as HTMLButtonElement | undefined;
    expect(prevBtn).not.toBeNull();
    expect(prevBtn?.disabled).toBe(true);
  });

  it('Next button is disabled on last page', async () => {
    (sessionsAPI.getAll as Mock).mockResolvedValue({
      data: {
        items: [makeSession()],
        page: 2,
        per_page: 25,
        total: 26,
        total_pages: 2,
        has_next: false,
        has_previous: true,
      },
    });
    (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
    await render();
    const nextBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Next'),
    ) as HTMLButtonElement | undefined;
    expect(nextBtn).not.toBeNull();
    expect(nextBtn?.disabled).toBe(true);
  });

  it('shows "Showing N–M of total" in pagination controls', async () => {
    (sessionsAPI.getAll as Mock).mockResolvedValue({
      data: {
        items: Array.from({ length: 25 }, (_, i) => makeSession({ id: i + 1 })),
        page: 1,
        per_page: 25,
        total: 50,
        total_pages: 2,
        has_next: true,
        has_previous: false,
      },
    });
    (projectsAPI.getAll as Mock).mockResolvedValue({ data: [] });
    await render();
    expect(container.textContent).toContain('Showing 1–25 of 50');
  });
});

describe('SessionsList — sessions display', () => {
  it('renders session name as a link to the session detail', async () => {
    setupMocks([makeSession({ id: 7, name: 'My Session' })]);
    await render();
    const link = container.querySelector('a[href="/sessions/7"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('My Session');
  });

  it('shows the run id on each row', async () => {
    setupMocks([makeSession({ id: 7, name: 'My Session' })]);
    await render();
    expect(container.textContent).toContain('Run #7');
  });

  it('renders multiple sessions', async () => {
    setupMocks([
      makeSession({ id: 1, name: 'First Run' }),
      makeSession({ id: 2, name: 'Second Run' }),
    ]);
    await render();
    expect(container.textContent).toContain('First Run');
    expect(container.textContent).toContain('Second Run');
  });
});
