import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import {
  ReviewReadyBlock,
  SessionCompleteBlock,
  SessionTabs,
  SessionTasksPanel,
} from '../SessionDetailSections';
import type { Session, Task } from '@/types/api';

// ── shared helpers ────────────────────────────────────────────────────────────

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

const baseSession = (overrides: Partial<Session> = {}): Session => ({
  id: 1,
  project_id: 10,
  name: 'Test Session',
  description: null,
  is_active: true,
  status: 'running',
  execution_mode: 'automatic',
  default_execution_profile: 'full_lifecycle',
  created_at: '2026-06-27T00:00:00Z',
  updated_at: null,
  started_at: null,
  stopped_at: null,
  paused_at: null,
  resumed_at: null,
  ...overrides,
});

const baseTask = (overrides: Partial<Task> = {}): Task => ({
  id: 1,
  project_id: 10,
  session_id: 1,
  title: 'Add feature',
  description: null,
  status: 'done',
  workspace_status: null,
  execution_profile: 'full_lifecycle',
  priority: 1,
  plan_position: 1,
  steps: null,
  current_step: 0,
  error_message: null,
  created_at: '2026-06-27T00:00:00Z',
  updated_at: null,
  started_at: null,
  completed_at: null,
  ...overrides,
});

// ── ReviewReadyBlock ──────────────────────────────────────────────────────────

describe('ReviewReadyBlock', () => {
  const render = (count: number) => {
    act(() => {
      root.render(
        <MemoryRouter>
          <ReviewReadyBlock count={count} />
        </MemoryRouter>,
      );
    });
  };

  it('shows singular label for count=1', () => {
    render(1);
    expect(container.textContent).toContain('1 task ready for review');
    expect(container.textContent).not.toContain('tasks ready for review');
  });

  it('shows plural label for count>1', () => {
    render(3);
    expect(container.textContent).toContain('3 tasks ready for review');
  });

  it('shows singular description for count=1', () => {
    render(1);
    expect(container.textContent).toContain('This task output is waiting');
  });

  it('shows plural description for count>1', () => {
    render(2);
    expect(container.textContent).toContain('These task outputs are waiting');
  });

  it('renders an Open Review Queue link pointing to /tasks', () => {
    render(2);
    const link = container.querySelector('a[href="/tasks"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('Open Review Queue');
  });
});

// ── SessionCompleteBlock ──────────────────────────────────────────────────────

describe('SessionCompleteBlock', () => {
  const render = (reviewCount: number, projectId?: number | null, sessionStatus = 'completed') => {
    act(() => {
      root.render(
        <MemoryRouter>
          <SessionCompleteBlock reviewCount={reviewCount} projectId={projectId} sessionStatus={sessionStatus} />
        </MemoryRouter>,
      );
    });
  };

  it('renders the "What next?" heading', () => {
    render(0);
    expect(container.textContent?.toLowerCase()).toContain('what next');
  });

  it('always shows Check system health link to /analytics', () => {
    render(0);
    const link = container.querySelector('a[href="/analytics"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('Check system health');
  });

  it('shows review link when reviewCount > 0', () => {
    render(3);
    const links = Array.from(container.querySelectorAll('a[href="/tasks"]'));
    expect(links.length).toBeGreaterThan(0);
    expect(container.textContent).toContain('Review Queue');
    expect(container.textContent).toContain('3 task outputs');
  });

  it('shows singular "output" for reviewCount=1', () => {
    render(1);
    expect(container.textContent).toContain('Review Queue');
    expect(container.textContent).toContain('1 task output');
    expect(container.textContent).not.toContain('1 task outputs');
  });

  it('hides review link when reviewCount=0', () => {
    render(0);
    // /tasks link must not appear when nothing to review
    const tasksLink = container.querySelector('a[href="/tasks"]');
    expect(tasksLink).toBeNull();
  });

  it('shows Start next session link to project when projectId provided', () => {
    render(0, 42);
    const link = container.querySelector('a[href="/projects/42"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('Start next session');
  });

  it('shows Return to Sessions fallback link when projectId is null', () => {
    render(0, null);
    const link = container.querySelector('a[href="/sessions"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('Return to Sessions');
  });

  it('shows Return to Sessions fallback link when projectId is undefined', () => {
    render(0, undefined);
    const link = container.querySelector('a[href="/sessions"]');
    expect(link).not.toBeNull();
  });

  it('does not show Start next session when no projectId', () => {
    render(0, null);
    const projectLink = container.querySelector('a[href^="/projects/"]');
    expect(projectLink).toBeNull();
  });
});

// ── SessionTabs — intervention badge ─────────────────────────────────────────

describe('SessionTabs — intervention badge', () => {
  const render = (interventionCount: number) => {
    act(() => {
      root.render(
        <MemoryRouter>
          <SessionTabs
            activeTab="logs"
            interventionCount={interventionCount}
            onChange={() => {}}
            tasksCount={2}
          />
        </MemoryRouter>,
      );
    });
  };

  it('shows badge on Summary tab when interventionCount > 0', () => {
    render(2);
    expect(container.textContent).toContain('Summary');
    expect(container.textContent).toContain('2');
    // Badge is a span with the count inside the Summary button
    const summaryBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Summary'),
    );
    expect(summaryBtn?.textContent).toContain('2');
  });

  it('hides badge when interventionCount is 0 (default)', () => {
    render(0);
    const summaryBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Summary'),
    );
    // Badge span has class including rounded-full — check it's absent
    expect(summaryBtn?.querySelector('[class*="rounded-full"]')).toBeNull();
  });

  it('hides badge when interventionCount is omitted', () => {
    act(() => {
      root.render(
        <MemoryRouter>
          <SessionTabs activeTab="summary" onChange={() => {}} tasksCount={0} />
        </MemoryRouter>,
      );
    });
    const summaryBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Summary'),
    );
    expect(summaryBtn?.querySelector('[class*="rounded-full"]')).toBeNull();
  });

  it('renders the correct badge count for count=1', () => {
    render(1);
    const summaryBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Summary'),
    );
    const badge = summaryBtn?.querySelector('[class*="rounded-full"]');
    expect(badge?.textContent).toBe('1');
  });
});

// ── SessionTasksPanel — review links on task rows ─────────────────────────────

describe('SessionTasksPanel — task row review links', () => {
  const render = (tasks: Task[], session: Session) => {
    act(() => {
      root.render(
        <MemoryRouter>
          <SessionTasksPanel
            actionButtons={null}
            formatDateTime={(v) => v ?? ''}
            session={session}
            tasks={tasks}
          />
        </MemoryRouter>,
      );
    });
  };

  it('shows "Ready for review" label for workspace_status=ready task', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'ready' })],
      baseSession({ project_id: 10 }),
    );
    expect(container.textContent).toContain('Ready for review');
  });

  it('links task title to Task Detail when workspace_status=ready and project_id available', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'ready' })],
      baseSession({ project_id: 10 }),
    );
    const link = container.querySelector('a[href="/projects/10/tasks/5"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('Add feature');
  });

  it('does not link task title when workspace_status is null', () => {
    render(
      [baseTask({ id: 5, workspace_status: null })],
      baseSession({ project_id: 10 }),
    );
    const link = container.querySelector('a[href="/projects/10/tasks/5"]');
    expect(link).toBeNull();
  });

  it('does not link task title when workspace_status is accepted', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'accepted' })],
      baseSession({ project_id: 10 }),
    );
    const link = container.querySelector('a[href="/projects/10/tasks/5"]');
    expect(link).toBeNull();
  });

  it('does not link task title when project_id is null (no link target)', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'ready', project_id: 0 })],
      baseSession({ project_id: 0 }),
    );
    // No project_id means no link
    const link = container.querySelector('a[href^="/projects/"]');
    expect(link).toBeNull();
  });

  it('shows task title as plain text when not linkable', () => {
    render(
      [baseTask({ id: 5, title: 'My plain task', workspace_status: null })],
      baseSession({ project_id: 10 }),
    );
    expect(container.textContent).toContain('My plain task');
  });

  it('does not show "Ready for review" for tasks with other workspace_status', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'accepted' })],
      baseSession({ project_id: 10 }),
    );
    expect(container.textContent).not.toContain('Ready for review');
  });

  it('shows raw workspace_status (humanized) for non-ready statuses', () => {
    render(
      [baseTask({ id: 5, workspace_status: 'changes_requested' })],
      baseSession({ project_id: 10 }),
    );
    expect(container.textContent).toContain('changes requested');
  });

  it('no reviewable block when all tasks have null workspace_status', () => {
    render(
      [baseTask({ id: 1, workspace_status: null }), baseTask({ id: 2, workspace_status: null })],
      baseSession({ project_id: 10 }),
    );
    expect(container.textContent).not.toContain('Ready for review');
  });
});
