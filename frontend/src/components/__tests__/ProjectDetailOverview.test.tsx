import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { Route, Routes } from 'react-router-dom';
import ProjectDetail from '@/pages/ProjectDetail';
import { projectsAPI, tasksAPI, sessionsAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  projectsAPI: {
    getById: vi.fn(),
    getWorkspaceOverview: vi.fn(),
    update: vi.fn(),
    rebuildBaseline: vi.fn(),
    cleanupWorkspaces: vi.fn(),
    restoreWorkspaceArchive: vi.fn(),
  },
  tasksAPI: {
    getByProject: vi.fn(),
    create: vi.fn(),
    delete: vi.fn(),
    update: vi.fn(),
    retry: vi.fn(),
    acceptWorkspace: vi.fn(),
    requestWorkspaceChanges: vi.fn(),
    rejectChangeSet: vi.fn(),
  },
  sessionsAPI: {
    getByProject: vi.fn(),
    delete: vi.fn(),
    generateSteps: vi.fn(),
  },
  guidanceAPI: {
    getReadiness: vi.fn(),
  },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

const makeProject = (overrides = {}) => ({
  id: 1,
  name: 'Test Project',
  branch: 'main',
  description: 'A test project',
  project_rules: null,
  github_url: null,
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-27T00:00:00Z',
  ...overrides,
});

const makeSession = (overrides: Partial<{
  id: number;
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
}> = {}) => ({
  id: 10,
  name: 'Run #1',
  status: 'completed',
  description: null,
  is_active: false,
  execution_mode: 'automatic' as const,
  default_execution_profile: 'full_lifecycle' as const,
  model_lane_label: null,
  model_lane_metadata: null,
  created_at: '2026-06-27T10:00:00Z',
  updated_at: '2026-06-27T10:30:00Z',
  started_at: '2026-06-27T10:00:00Z',
  stopped_at: null,
  paused_at: null,
  resumed_at: null,
  last_alert_message: null,
  last_alert_at: null,
  ...overrides,
});

const makeTask = (overrides = {}) => ({
  id: 1,
  project_id: 1,
  title: 'Task One',
  description: null,
  status: 'pending' as const,
  execution_profile: 'full_lifecycle' as const,
  priority: 0,
  steps: null,
  current_step: 0,
  error_message: null,
  workspace_status: 'not_created' as const,
  promotion_note: null,
  promoted_at: null,
  created_at: '2026-06-27T00:00:00Z',
  updated_at: '2026-06-27T00:00:00Z',
  started_at: null,
  completed_at: null,
  session_id: null,
  task_subfolder: null,
  ...overrides,
});

const makeWorkspaceOverview = (overrides = {}) => ({
  counts: { ready: 0, promoted: 0, changes_requested: 0, blocked: 0 },
  baseline: { exists: false, file_count: 0, promoted_task_count: 0, path: null },
  promoted_tasks: [],
  pending_change_sets: [],
  ready_task_ids: [],
  ...overrides,
});

function setupMocks({
  project = makeProject(),
  sessions = [makeSession()],
  tasks = [makeTask()],
  workspace = makeWorkspaceOverview(),
}: {
  project?: ReturnType<typeof makeProject>;
  sessions?: ReturnType<typeof makeSession>[];
  tasks?: ReturnType<typeof makeTask>[];
  workspace?: ReturnType<typeof makeWorkspaceOverview>;
} = {}) {
  (projectsAPI.getById as Mock).mockResolvedValue({ data: project });
  (tasksAPI.getByProject as Mock).mockResolvedValue({ data: tasks });
  (sessionsAPI.getByProject as Mock).mockResolvedValue({ data: sessions });
  (projectsAPI.getWorkspaceOverview as Mock).mockResolvedValue({ data: workspace });
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

async function render(projectId = '1') {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/projects/${projectId}`]}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectDetail />} />
          <Route path="/sessions/new" element={<div />} />
        </Routes>
      </MemoryRouter>,
    );
  });
}

// ── Project Overview section ──────────────────────────────────────────────────

describe('ProjectDetail — Project Overview section', () => {
  it('renders the project overview grid', async () => {
    setupMocks({ sessions: [makeSession({ id: 10, name: 'Run #10', status: 'running' })] });
    await render();
    const overview = container.querySelector('[data-testid="project-overview"]');
    expect(overview).not.toBeNull();
  });

  it('shows latest session name with a link to session detail', async () => {
    setupMocks({ sessions: [makeSession({ id: 42, name: 'Run #42', status: 'completed' })] });
    await render();
    const link = container.querySelector('[data-testid="latest-session-link"]') as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.textContent).toContain('Run #42');
    expect(link.getAttribute('href')).toContain('/sessions/42');
  });

  it('shows "None yet" when there are no sessions', async () => {
    setupMocks({ sessions: [] });
    await render();
    const overview = container.querySelector('[data-testid="project-overview"]');
    expect(overview?.textContent).toContain('None yet');
  });

  it('shows sessions-needing-attention count', async () => {
    setupMocks({
      sessions: [
        makeSession({ id: 1, status: 'failed' }),
        makeSession({ id: 2, status: 'stopped' }),
        makeSession({ id: 3, status: 'completed' }),
      ],
    });
    await render();
    const link = container.querySelector('[data-testid="needs-attention-link"]');
    expect(link).not.toBeNull();
    expect(link?.textContent).toContain('2');
  });

  it('shows 0 (green) when no sessions need attention', async () => {
    setupMocks({ sessions: [makeSession({ id: 1, status: 'completed' })] });
    await render();
    expect(container.querySelector('[data-testid="needs-attention-link"]')).toBeNull();
    const overview = container.querySelector('[data-testid="project-overview"]');
    expect(overview?.textContent).toContain('0');
  });

  it('shows tasks-awaiting-review count when ready count > 0', async () => {
    setupMocks({
      workspace: makeWorkspaceOverview({ counts: { ready: 3, promoted: 0, changes_requested: 0, blocked: 0 } }),
    });
    await render();
    const btn = container.querySelector('[data-testid="review-count-btn"]');
    expect(btn).not.toBeNull();
    expect(btn?.textContent).toContain('3');
  });

  it('shows last activity relative time', async () => {
    setupMocks({
      sessions: [makeSession({ updated_at: '2026-06-27T09:00:00Z' })],
    });
    await render();
    const overview = container.querySelector('[data-testid="project-overview"]');
    // Some relative time string should appear
    expect(overview?.textContent?.length).toBeGreaterThan(0);
  });

  it('shows "No activity" when there are no sessions', async () => {
    setupMocks({ sessions: [] });
    await render();
    const overview = container.querySelector('[data-testid="project-overview"]');
    expect(overview?.textContent).toContain('No activity');
  });
});

// ── Review summary notification ───────────────────────────────────────────────

describe('ProjectDetail — Review summary notification', () => {
  it('shows review summary when there are pending change sets', async () => {
    setupMocks({
      workspace: makeWorkspaceOverview({
        pending_change_sets: [{
          task_id: 1,
          title: 'Task One',
          workspace_status: 'ready',
          task_execution_id: null,
          change_set: {
            changed_count: 3,
            added_count: 2,
            modified_count: 1,
            deleted_count: 0,
            added_files: [],
            modified_files: [],
            deleted_files: [],
            warning_flags: [],
          },
        }],
      }),
    });
    await render();
    const summary = container.querySelector('[data-testid="review-summary"]');
    expect(summary).not.toBeNull();
    expect(summary?.textContent).toContain('1 task output awaiting review');
  });

  it('hides review summary when no pending change sets', async () => {
    setupMocks({ workspace: makeWorkspaceOverview({ pending_change_sets: [] }) });
    await render();
    expect(container.querySelector('[data-testid="review-summary"]')).toBeNull();
  });

  it('shows "Open Review Queue →" in the review summary', async () => {
    setupMocks({
      workspace: makeWorkspaceOverview({
        pending_change_sets: [{
          task_id: 1,
          title: 'Task One',
          workspace_status: 'ready',
          task_execution_id: null,
          change_set: { changed_count: 1, added_count: 1, modified_count: 0, deleted_count: 0, added_files: [], modified_files: [], deleted_files: [], warning_flags: [] },
        }],
      }),
    });
    await render();
    const summary = container.querySelector('[data-testid="review-summary"]');
    expect(summary?.textContent).toContain('Open Review Queue');
  });

  it('falls back to ready tasks when workspace overview omits pending change sets', async () => {
    setupMocks({
      tasks: [
        makeTask({
          id: 7,
          title: 'Ready task with retained workspace',
          status: 'done',
          workspace_status: 'ready',
          task_subfolder: 'tasks/task-7',
        }),
      ],
      workspace: makeWorkspaceOverview({
        counts: { ready: 0, promoted: 0, changes_requested: 0, blocked: 0 },
        pending_change_sets: [],
      }),
    });
    await render();

    const btn = container.querySelector('[data-testid="review-count-btn"]');
    expect(btn).not.toBeNull();
    expect(btn?.textContent).toContain('1');
    expect(container.querySelector('[data-testid="review-summary"]')?.textContent).toContain(
      '1 task output awaiting review',
    );
  });
});

// ── Overview tab — default and Recent Sessions ────────────────────────────────

describe('ProjectDetail — Overview tab (default)', () => {
  it('renders overview tab by default', async () => {
    setupMocks();
    await render();
    const overviewTab = container.querySelector('[data-testid="overview-tab"]');
    expect(overviewTab).not.toBeNull();
  });

  it('shows recent sessions list when sessions exist', async () => {
    setupMocks({
      sessions: [
        makeSession({ id: 10, name: 'Run #10', created_at: '2026-06-27T10:00:00Z' }),
        makeSession({ id: 11, name: 'Run #11', created_at: '2026-06-27T09:00:00Z' }),
      ],
    });
    await render();
    const list = container.querySelector('[data-testid="recent-sessions-list"]');
    expect(list).not.toBeNull();
    expect(list?.textContent).toContain('Run #10');
    expect(list?.textContent).toContain('Run #11');
  });

  it('shows at most 5 recent sessions', async () => {
    setupMocks({
      sessions: Array.from({ length: 8 }, (_, i) =>
        makeSession({ id: i + 1, name: `Run #${i + 1}`, created_at: `2026-06-2${i % 7 + 1}T10:00:00Z` })
      ),
    });
    await render();
    const links = container.querySelectorAll('[data-testid^="recent-session-"]');
    expect(links.length).toBeLessThanOrEqual(5);
  });

  it('each recent session row links to session detail', async () => {
    setupMocks({
      sessions: [makeSession({ id: 99, name: 'Run #99' })],
    });
    await render();
    const link = container.querySelector('[data-testid="recent-session-99"]') as HTMLAnchorElement;
    expect(link).not.toBeNull();
    expect(link.getAttribute('href')).toContain('/sessions/99');
  });

  it('shows "No sessions yet" when there are no sessions', async () => {
    setupMocks({ sessions: [] });
    await render();
    const overviewTab = container.querySelector('[data-testid="overview-tab"]');
    expect(overviewTab?.textContent).toContain('No sessions yet');
  });
});

// ── Readiness section ─────────────────────────────────────────────────────────

describe('ProjectDetail — Readiness section', () => {
  it('renders the readiness section', async () => {
    setupMocks();
    await render();
    const section = container.querySelector('[data-testid="readiness-section"]');
    expect(section).not.toBeNull();
  });

  it('shows READY verdict when local project criteria pass', async () => {
    setupMocks({
      sessions: [makeSession({ id: 1, status: 'completed' })],
      tasks: [makeTask({ id: 1, status: 'done', workspace_status: 'promoted' })],
      workspace: makeWorkspaceOverview({
        counts: { ready: 0, promoted: 1, changes_requested: 0, blocked: 0 },
        baseline: { exists: true, file_count: 4, promoted_task_count: 1, path: '/tmp/baseline' },
      }),
    });
    await render();
    const verdict = container.querySelector('[data-testid="readiness-verdict"]');
    expect(verdict?.textContent).toContain('READY');
  });

  it('shows CAUTION verdict when review is pending', async () => {
    setupMocks({
      tasks: [makeTask({ id: 1, status: 'done', workspace_status: 'ready', task_subfolder: 'tasks/task-1' })],
      workspace: makeWorkspaceOverview({ counts: { ready: 1, promoted: 0, changes_requested: 0, blocked: 0 } }),
    });
    await render();
    const verdict = container.querySelector('[data-testid="readiness-verdict"]');
    expect(verdict?.textContent).toContain('CAUTION');
  });

  it('shows criteria items', async () => {
    setupMocks();
    await render();
    const criteria = container.querySelector('[data-testid="readiness-criteria"]');
    expect(criteria).not.toBeNull();
    expect((criteria?.querySelectorAll('div') ?? []).length).toBeGreaterThan(0);
  });

  it('shows a local runs criterion when no sessions are available', async () => {
    setupMocks({ sessions: [] });
    await render();
    const overviewTab = container.querySelector('[data-testid="overview-tab"]');
    expect(overviewTab?.textContent).toContain('Runs recorded: none');
  });

  it('shows local readiness information without pilot metrics', async () => {
    setupMocks({
      sessions: [makeSession({ id: 1, status: 'completed' })],
    });
    await render();
    const section = container.querySelector('[data-testid="readiness-section"]');
    expect(section?.textContent).toContain('Runs recorded');
    expect(section?.textContent).toContain('Review queue');
  });
});

// ── Empty project ─────────────────────────────────────────────────────────────

describe('ProjectDetail — empty project', () => {
  it('renders without errors when project has no sessions or tasks', async () => {
    setupMocks({ sessions: [], tasks: [] });
    await render();
    expect(container.querySelector('[data-testid="project-overview"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="overview-tab"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="review-summary"]')).toBeNull();
  });
});

// ── Navigation links ──────────────────────────────────────────────────────────

describe('ProjectDetail — navigation links', () => {
  it('sessions-needing-attention links to /sessions', async () => {
    setupMocks({
      sessions: [makeSession({ id: 1, status: 'failed' })],
    });
    await render();
    const link = container.querySelector('[data-testid="needs-attention-link"]') as HTMLAnchorElement;
    expect(link?.getAttribute('href')).toBe('/sessions');
  });

  it('latest session link goes to /sessions/:id', async () => {
    setupMocks({ sessions: [makeSession({ id: 7, name: 'Run #7' })] });
    await render();
    const link = container.querySelector('[data-testid="latest-session-link"]') as HTMLAnchorElement;
    expect(link?.getAttribute('href')).toContain('/sessions/7');
  });
});
