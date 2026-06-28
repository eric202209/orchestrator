import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import AnalyticsDashboard from '@/pages/AnalyticsDashboard';
import { analyticsAPI } from '@/api/client';

import type {
  OperationalAnalytics,
  FailureAnalytics,
  KnowledgeAnalytics,
  ExecutionAnalytics,
  OperatorAnalytics,
} from '@/types/api';

vi.mock('@/api/client', () => ({
  analyticsAPI: {
    getOperational: vi.fn(),
    getFailures: vi.fn(),
    getKnowledge: vi.fn(),
    getExecution: vi.fn(),
    getOperators: vi.fn(),
  },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

const operationalWindow = {
  session_success_rate: 0.85,
  first_attempt_success_rate: 0.72,
  failure_category_distribution: { timeout: 3, validation: 1 },
  sessions_started: 10,
  sessions_completed: 8,
  sessions_failed: 2,
};

const operational: OperationalAnalytics = {
  windows: { '7d': operationalWindow, '30d': operationalWindow, all_time: operationalWindow },
  generated_at: '2026-06-27T00:00:00Z',
  metrics_version: 1,
};

const failureWindow = {
  recovery_attempts: 5,
  recovery_successes: 4,
  recovery_failures: 1,
  recovery_success_rate: 0.8,
  budget_exhaustion_count: 2,
  churn_guard_activations: 1,
  failure_category_distribution: { timeout: 2 },
  failure_category_recovery: {},
};

const failures: FailureAnalytics = {
  windows: { '7d': failureWindow, '30d': failureWindow, all_time: failureWindow },
  generated_at: '2026-06-27T00:00:00Z',
  metrics_version: 1,
};

const knowledgeWindow = {
  retrieval_count: 20,
  used_in_prompt_count: 15,
  knowledge_hit_rate: 0.75,
  effectiveness_rate: 0.6,
  phase_utilization: { planning: 10, execution: 5 },
  top_items: [
    {
      knowledge_item_id: 'ki-1',
      title: 'Python typing guide',
      retrieval_count: 8,
      used_in_prompt_count: 6,
      hit_rate: 0.75,
      effectiveness_rate: 0.67,
      avg_confidence: 0.9,
    },
  ],
  low_effectiveness_items: [
    {
      knowledge_item_id: 'ki-2',
      title: 'Outdated guide',
      retrieval_count: 5,
      used_in_prompt_count: 4,
      effectiveness_rate: 0.1,
      avg_confidence: 0.5,
    },
  ],
};

const knowledge: KnowledgeAnalytics = {
  windows: { '7d': knowledgeWindow, '30d': knowledgeWindow, all_time: knowledgeWindow },
  generated_at: '2026-06-27T00:00:00Z',
  metrics_version: 1,
};

const executionWindow = {
  execution_count: 30,
  mean_execution_duration_seconds: 145.5,
  queue_latency_p50_seconds: 2.1,
  queue_latency_p95_seconds: 8.7,
  tokens_in_total: 120000,
  tokens_out_total: 45000,
  backend_distribution: { openclaw: 28, local: 2 },
  phase_duration_seconds: {
    planning: { count: 10, mean_seconds: 12.3 },
    execution: { count: 10, mean_seconds: 130.2 },
  },
};

const execution: ExecutionAnalytics = {
  windows: { '7d': executionWindow, '30d': executionWindow, all_time: executionWindow },
  generated_at: '2026-06-27T00:00:00Z',
  metrics_version: 1,
};

const operatorWindow = {
  intervention_requests: 8,
  intervention_responses: 7,
  intervention_response_rate: 0.875,
  mean_response_seconds: 42.5,
  median_response_seconds: 35.0,
  sessions_with_intervention: 3,
  sessions_without_intervention: 7,
  autonomy_rate: 0.7,
  pause_count: 2,
  resume_count: 2,
  stop_count: 1,
  intervention_type_distribution: { guidance: 5, approval: 3 },
  phase_intervention_distribution: {},
};

const operators: OperatorAnalytics = {
  windows: { '7d': operatorWindow, '30d': operatorWindow, all_time: operatorWindow },
  generated_at: '2026-06-27T00:00:00Z',
  metrics_version: 1,
};

// ── helpers ───────────────────────────────────────────────────────────────────

function setupAllMocks() {
  (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
  (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
  (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
  (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
  (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
}

function setupPendingMocks() {
  const pending = new Promise(() => {});
  (analyticsAPI.getOperational as Mock).mockReturnValue(pending);
  (analyticsAPI.getFailures as Mock).mockReturnValue(pending);
  (analyticsAPI.getKnowledge as Mock).mockReturnValue(pending);
  (analyticsAPI.getExecution as Mock).mockReturnValue(pending);
  (analyticsAPI.getOperators as Mock).mockReturnValue(pending);
}

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
        <AnalyticsDashboard />
      </MemoryRouter>,
    );
  });
}

// ── tests ─────────────────────────────────────────────────────────────────────

describe('AnalyticsDashboard', () => {
  describe('loading', () => {
    it('shows loading skeletons while data is pending', () => {
      setupPendingMocks();
      act(() => {
        root.render(
          <MemoryRouter>
            <AnalyticsDashboard />
          </MemoryRouter>,
        );
      });
      // Skeletons are rendered via LoadingPanel which emits divs with animate-pulse
      const skeletons = container.querySelectorAll('[class*="animate-pulse"]');
      expect(skeletons.length).toBeGreaterThan(0);
    });

    it('shows all five section headings after data loads', async () => {
      setupAllMocks();
      await render();
      const text = container.textContent ?? '';
      expect(text).toContain('Operational Health');
      expect(text).toContain('Failure Analytics');
      expect(text).toContain('Knowledge Analytics');
      expect(text).toContain('Execution Analytics');
      expect(text).toContain('Operator Analytics');
    });
  });

  describe('operational section', () => {
    it('renders session success rate', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('85%');
    });

    it('renders first attempt success rate', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('72%');
    });

    it('renders sessions started count', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('10');
    });

    it('renders sessions failed count', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('2');
    });

    it('renders failure category distribution', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('timeout');
    });

    it('shows error panel when operational endpoint fails', async () => {
      (analyticsAPI.getOperational as Mock).mockRejectedValue(new Error('Network'));
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('Failed to load operational data');
    });
  });

  describe('failure section', () => {
    it('renders recovery success rate', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('80%');
    });

    it('renders budget exhaustion count', async () => {
      setupAllMocks();
      await render();
      // budget_exhaustion_count = 2 — check label
      expect(container.textContent).toContain('Budget Exhaustions');
    });

    it('renders repair churn label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Repair Churn');
    });

    it('shows error panel when failures endpoint fails', async () => {
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockRejectedValue(new Error('Network'));
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('Failed to load failure data');
    });

    it('renders without breaking when other sections succeed', async () => {
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockRejectedValue(new Error('Network'));
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      // Other sections should still render
      expect(container.textContent).toContain('Operational Health');
      expect(container.textContent).toContain('Knowledge Analytics');
    });
  });

  describe('knowledge section', () => {
    it('renders knowledge hit rate', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('75%');
    });

    it('renders top knowledge items', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Python typing guide');
    });

    it('renders low effectiveness items', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Outdated guide');
    });

    it('renders effectiveness rate label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Knowledge Effectiveness');
    });

    it('shows error panel when knowledge endpoint fails', async () => {
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockRejectedValue(new Error('Network'));
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('Failed to load knowledge data');
    });
  });

  describe('execution section', () => {
    it('renders mean runtime label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Mean Runtime');
    });

    it('renders queue p50 label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Queue P50');
    });

    it('renders queue p95 label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Queue P95');
    });

    it('renders backend distribution', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('openclaw');
    });

    it('renders total tokens label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Total Tokens');
    });

    it('shows error panel when execution endpoint fails', async () => {
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockRejectedValue(new Error('Network'));
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('Failed to load execution data');
    });
  });

  describe('operator section', () => {
    it('renders autonomy rate', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('70%');
    });

    it('renders mean response time label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Mean Response Time');
    });

    it('renders pause / resume / stop label', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Pause / Resume / Stop');
    });

    it('renders intervention types', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('guidance');
    });

    it('shows error panel when operators endpoint fails', async () => {
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockRejectedValue(new Error('Network'));
      await render();
      expect(container.textContent).toContain('Failed to load operator data');
    });
  });

  describe('window switching', () => {
    it('renders 7d window button', async () => {
      setupAllMocks();
      await render();
      const btn7d = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === '7d',
      );
      expect(btn7d).toBeTruthy();
    });

    it('renders 30d window button', async () => {
      setupAllMocks();
      await render();
      const btn30d = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === '30d',
      );
      expect(btn30d).toBeTruthy();
    });

    it('renders All Time window button', async () => {
      setupAllMocks();
      await render();
      const btnAllTime = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === 'All Time',
      );
      expect(btnAllTime).toBeTruthy();
    });

    it('switches to 30d window on click without re-fetching', async () => {
      setupAllMocks();
      await render();
      const btn30d = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === '30d',
      );
      await act(async () => { btn30d?.click(); });
      // Still shows data (same fixture for all windows)
      expect(container.textContent).toContain('Session Success Rate');
      // Did not call APIs again for window change
      expect((analyticsAPI.getOperational as Mock).mock.calls.length).toBe(1);
    });

    it('switches to all_time window on click', async () => {
      setupAllMocks();
      await render();
      const btnAllTime = Array.from(container.querySelectorAll('button')).find(
        (b) => b.textContent?.trim() === 'All Time',
      );
      await act(async () => { btnAllTime?.click(); });
      expect(container.textContent).toContain('Operational Health');
    });
  });

  describe('refresh', () => {
    it('renders refresh button', async () => {
      setupAllMocks();
      await render();
      const refreshBtn = container.querySelector('[aria-label="Refresh analytics"]');
      expect(refreshBtn).toBeTruthy();
    });

    it('re-fetches all endpoints on refresh click', async () => {
      setupAllMocks();
      await render();
      const refreshBtn = container.querySelector('[aria-label="Refresh analytics"]') as HTMLButtonElement;
      await act(async () => { refreshBtn?.click(); });
      expect((analyticsAPI.getOperational as Mock).mock.calls.length).toBeGreaterThan(1);
    });

    it('auto-refreshes after 60 seconds', async () => {
      setupAllMocks();
      await render();
      const callsBefore = (analyticsAPI.getOperational as Mock).mock.calls.length;
      await act(async () => {
        vi.advanceTimersByTime(60_000);
      });
      // Wait for promises
      await act(async () => {});
      const callsAfter = (analyticsAPI.getOperational as Mock).mock.calls.length;
      expect(callsAfter).toBeGreaterThan(callsBefore);
    });
  });

  describe('null metric rendering', () => {
    it('renders — for null session success rate', async () => {
      const nullOp: OperationalAnalytics = {
        ...operational,
        windows: {
          '7d': { ...operationalWindow, session_success_rate: null },
          '30d': { ...operationalWindow, session_success_rate: null },
          all_time: { ...operationalWindow, session_success_rate: null },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: nullOp });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('—');
    });

    it('renders — for null recovery success rate', async () => {
      const nullFail: FailureAnalytics = {
        ...failures,
        windows: {
          '7d': { ...failureWindow, recovery_success_rate: null },
          '30d': { ...failureWindow, recovery_success_rate: null },
          all_time: { ...failureWindow, recovery_success_rate: null },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: nullFail });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('—');
    });

    it('renders — for null mean runtime', async () => {
      const nullEx: ExecutionAnalytics = {
        ...execution,
        windows: {
          '7d': { ...executionWindow, mean_execution_duration_seconds: null },
          '30d': { ...executionWindow, mean_execution_duration_seconds: null },
          all_time: { ...executionWindow, mean_execution_duration_seconds: null },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: nullEx });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      expect(container.textContent).toContain('—');
    });

    it('renders — for null autonomy rate', async () => {
      const nullOps: OperatorAnalytics = {
        ...operators,
        windows: {
          '7d': { ...operatorWindow, autonomy_rate: null },
          '30d': { ...operatorWindow, autonomy_rate: null },
          all_time: { ...operatorWindow, autonomy_rate: null },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: nullOps });
      await render();
      expect(container.textContent).toContain('—');
    });
  });

  describe('distribution rendering', () => {
    it('renders distribution table for failure categories', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Failure Category Distribution');
      expect(container.textContent).toContain('timeout');
    });

    it('renders distribution table for backends', async () => {
      setupAllMocks();
      await render();
      expect(container.textContent).toContain('Backend Distribution');
      expect(container.textContent).toContain('openclaw');
    });

    it('renders empty text for empty distributions', async () => {
      const emptyEx: ExecutionAnalytics = {
        ...execution,
        windows: {
          '7d': { ...executionWindow, backend_distribution: {} },
          '30d': { ...executionWindow, backend_distribution: {} },
          all_time: { ...executionWindow, backend_distribution: {} },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: emptyEx });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      // backend_distribution empty means no distribution table shown
      expect(container.textContent).not.toContain('Backend Distribution');
    });

    it('renders empty state for top items when list is empty', async () => {
      const emptyKn: KnowledgeAnalytics = {
        ...knowledge,
        windows: {
          '7d': { ...knowledgeWindow, top_items: [], low_effectiveness_items: [] },
          '30d': { ...knowledgeWindow, top_items: [], low_effectiveness_items: [] },
          all_time: { ...knowledgeWindow, top_items: [], low_effectiveness_items: [] },
        },
      };
      (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
      (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
      (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: emptyKn });
      (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
      (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
      await render();
      // Tables section not rendered when both lists are empty
      expect(container.textContent).not.toContain('Python typing guide');
    });
  });

  describe('all endpoints fail', () => {
    it('shows all five error panels', async () => {
      const err = new Error('Network');
      (analyticsAPI.getOperational as Mock).mockRejectedValue(err);
      (analyticsAPI.getFailures as Mock).mockRejectedValue(err);
      (analyticsAPI.getKnowledge as Mock).mockRejectedValue(err);
      (analyticsAPI.getExecution as Mock).mockRejectedValue(err);
      (analyticsAPI.getOperators as Mock).mockRejectedValue(err);
      await render();
      expect(container.textContent).toContain('Failed to load operational data');
      expect(container.textContent).toContain('Failed to load failure data');
      expect(container.textContent).toContain('Failed to load knowledge data');
      expect(container.textContent).toContain('Failed to load execution data');
      expect(container.textContent).toContain('Failed to load operator data');
    });
  });

  describe('API contract compatibility', () => {
    it('calls all five analytics endpoints on mount', async () => {
      setupAllMocks();
      await render();
      expect(analyticsAPI.getOperational).toHaveBeenCalledTimes(1);
      expect(analyticsAPI.getFailures).toHaveBeenCalledTimes(1);
      expect(analyticsAPI.getKnowledge).toHaveBeenCalledTimes(1);
      expect(analyticsAPI.getExecution).toHaveBeenCalledTimes(1);
      expect(analyticsAPI.getOperators).toHaveBeenCalledTimes(1);
    });

    it('calls endpoints with no arguments', async () => {
      setupAllMocks();
      await render();
      expect((analyticsAPI.getOperational as Mock).mock.calls[0]).toHaveLength(0);
      expect((analyticsAPI.getFailures as Mock).mock.calls[0]).toHaveLength(0);
      expect((analyticsAPI.getKnowledge as Mock).mock.calls[0]).toHaveLength(0);
      expect((analyticsAPI.getExecution as Mock).mock.calls[0]).toHaveLength(0);
      expect((analyticsAPI.getOperators as Mock).mock.calls[0]).toHaveLength(0);
    });
  });
});
