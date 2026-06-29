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
  DecisionAnalytics,
} from '@/types/api';

vi.mock('@/api/client', () => ({
  analyticsAPI: {
    getOperational: vi.fn(),
    getFailures: vi.fn(),
    getKnowledge: vi.fn(),
    getExecution: vi.fn(),
    getOperators: vi.fn(),
    getDecision: vi.fn(),
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
  phase_utilization: {},
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
  low_effectiveness_items: [],
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
  phase_duration_seconds: {},
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

const decisionWindow = {
  successful_recovery_strategies: [],
  repeated_failures: [],
  knowledge_effectiveness: [],
  coordinator_reliability: [],
  project_reliability: [],
  improvement_opportunities: [],
};

const decision: DecisionAnalytics = {
  windows: { '7d': decisionWindow, '30d': decisionWindow, all_time: decisionWindow },
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
  (analyticsAPI.getDecision as Mock).mockResolvedValue({ data: decision });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  (analyticsAPI.getDecision as Mock).mockResolvedValue({ data: decision });
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

// ── metric hint text ──────────────────────────────────────────────────────────

describe('metric hint text', () => {
  it('shows hint for session success rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Sessions that reached a successful outcome');
  });

  it('shows hint for first attempt success rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('No repair or retry was needed');
  });

  it('shows hint for recovery success rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Failed sessions recovered by the repair system');
  });

  it('shows hint for budget exhaustions', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Sessions where all repair attempts were used up');
  });

  it('shows hint for repair churn', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Sessions stopped by the churn guard');
  });

  it('shows hint for knowledge hit rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Retrievals included in a prompt');
  });

  it('shows hint for knowledge effectiveness when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Retrieved items that aided task completion');
  });

  it('shows hint for queue P50 when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Half of jobs waited less than this');
  });

  it('shows hint for queue P95 when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('95% of jobs waited less than this');
  });

  it('shows hint for autonomy rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Sessions completed without operator action');
  });

  it('shows hint for intervention rate when data is present', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Sessions that needed at least one operator action');
  });
});

// ── null value explanation ────────────────────────────────────────────────────

describe('null value explanation', () => {
  it('shows "Not enough data yet" for null session success rate', async () => {
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
    expect(container.textContent).toContain('Not enough data yet');
  });

  it('shows "Not enough data yet" for null recovery success rate', async () => {
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
    expect(container.textContent).toContain('Not enough data yet');
  });

  it('shows "Not enough data yet" for null knowledge hit rate', async () => {
    const nullKn: KnowledgeAnalytics = {
      ...knowledge,
      windows: {
        '7d': { ...knowledgeWindow, knowledge_hit_rate: null, effectiveness_rate: null },
        '30d': { ...knowledgeWindow, knowledge_hit_rate: null, effectiveness_rate: null },
        all_time: { ...knowledgeWindow, knowledge_hit_rate: null, effectiveness_rate: null },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: nullKn });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await render();
    expect(container.textContent).toContain('Not enough data yet');
  });

  it('shows "Not enough data yet" for null queue latency', async () => {
    const nullEx: ExecutionAnalytics = {
      ...execution,
      windows: {
        '7d': { ...executionWindow, queue_latency_p50_seconds: null, queue_latency_p95_seconds: null },
        '30d': { ...executionWindow, queue_latency_p50_seconds: null, queue_latency_p95_seconds: null },
        all_time: { ...executionWindow, queue_latency_p50_seconds: null, queue_latency_p95_seconds: null },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: nullEx });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await render();
    expect(container.textContent).toContain('Not enough data yet');
  });

  it('shows "Not enough data yet" for null autonomy rate', async () => {
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
    expect(container.textContent).toContain('Not enough data yet');
  });
});

// ── section summaries ─────────────────────────────────────────────────────────

describe('section summaries', () => {
  it('shows summary in operational section for high success rate', async () => {
    const highOp: OperationalAnalytics = {
      ...operational,
      windows: {
        '7d': { ...operationalWindow, session_success_rate: 0.95 },
        '30d': { ...operationalWindow, session_success_rate: 0.95 },
        all_time: { ...operationalWindow, session_success_rate: 0.95 },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: highOp });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await render();
    expect(container.textContent).toContain('Session reliability is high');
  });

  it('shows summary in operational section for low success rate', async () => {
    const lowOp: OperationalAnalytics = {
      ...operational,
      windows: {
        '7d': { ...operationalWindow, session_success_rate: 0.3 },
        '30d': { ...operationalWindow, session_success_rate: 0.3 },
        all_time: { ...operationalWindow, session_success_rate: 0.3 },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: lowOp });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await render();
    expect(container.textContent).toContain('Session failure rate is elevated');
  });

  it('shows "No sessions recorded" summary when operational rate is null', async () => {
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
    expect(container.textContent).toContain('No sessions recorded in this window');
  });

  it('shows failure section summary', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Recovery is performing well');
  });

  it('shows knowledge section summary', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('actively used and frequently included');
  });

  it('shows execution section summary', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Queue latency is');
  });

  it('shows operator section summary', async () => {
    setupAllMocks();
    await render();
    expect(container.textContent).toContain('Moderate operator involvement');
  });

  it('shows high autonomy summary when autonomy_rate >= 0.8', async () => {
    const highOps: OperatorAnalytics = {
      ...operators,
      windows: {
        '7d': { ...operatorWindow, autonomy_rate: 0.9 },
        '30d': { ...operatorWindow, autonomy_rate: 0.9 },
        all_time: { ...operatorWindow, autonomy_rate: 0.9 },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: highOps });
    await render();
    expect(container.textContent).toContain('System is operating with high autonomy');
  });

  it('shows elevated failure summary when session success is below threshold', async () => {
    const lowFail: FailureAnalytics = {
      ...failures,
      windows: {
        '7d': { ...failureWindow, recovery_success_rate: 0.2 },
        '30d': { ...failureWindow, recovery_success_rate: 0.2 },
        all_time: { ...failureWindow, recovery_success_rate: 0.2 },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: lowFail });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await render();
    expect(container.textContent).toContain('Low recovery success rate');
  });
});

// ── drilldown links ───────────────────────────────────────────────────────────

describe('drilldown links', () => {
  it('renders a "View sessions" link in the failure section', async () => {
    setupAllMocks();
    await render();
    const links = container.querySelectorAll('a');
    const sessionLink = Array.from(links).find(
      (a) => a.textContent?.includes('View sessions'),
    );
    expect(sessionLink).toBeTruthy();
  });

  it('View sessions link points to /sessions', async () => {
    setupAllMocks();
    await render();
    const links = container.querySelectorAll('a');
    const sessionLink = Array.from(links).find(
      (a) => a.textContent?.includes('View sessions'),
    );
    expect(sessionLink?.getAttribute('href')).toBe('/sessions');
  });

  it('does not have a knowledge drilldown link (page does not exist)', async () => {
    setupAllMocks();
    await render();
    const links = Array.from(container.querySelectorAll('a'));
    const knowledgeLink = links.find(
      (a) => a.textContent?.toLowerCase().includes('knowledge base'),
    );
    expect(knowledgeLink).toBeFalsy();
  });

  it('does not have an interventions drilldown link (page does not exist)', async () => {
    setupAllMocks();
    await render();
    const links = Array.from(container.querySelectorAll('a'));
    const interventionLink = links.find(
      (a) => a.getAttribute('href')?.includes('intervention') || a.getAttribute('href')?.includes('guidance'),
    );
    expect(interventionLink).toBeFalsy();
  });
});

// ── empty state copy ──────────────────────────────────────────────────────────

describe('improved empty state copy', () => {
  it('shows window-specific copy for empty knowledge top-items chart', async () => {
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
    expect(container.textContent).toContain('No knowledge retrievals in this window');
  });

  it('shows "No knowledge retrievals" for empty top items table', async () => {
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
    // The TopItemsTable emptyText has been improved
    const occurrences = (container.textContent ?? '').split('No knowledge retrievals in this window').length - 1;
    expect(occurrences).toBeGreaterThanOrEqual(1);
  });
});

// ── footer metadata ───────────────────────────────────────────────────────────

describe('footer metadata', () => {
  it('shows generated_at timestamp in operational section', async () => {
    setupAllMocks();
    await render();
    // generated_at: '2026-06-27T00:00:00Z' → 'Jun 27 at 00:00 UTC'
    expect(container.textContent).toContain('Jun 27 at 00:00 UTC');
  });

  it('shows "Data as of" prefix in section footers', async () => {
    setupAllMocks();
    await render();
    const occurrences = (container.textContent ?? '').split('Data as of').length - 1;
    // Six sections each get a footer
    expect(occurrences).toBe(6);
  });
});

// ── window switching preserves polish ─────────────────────────────────────────

describe('window switching preserves explanations', () => {
  it('section summaries still render after window switch', async () => {
    setupAllMocks();
    await render();
    const btn30d = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === '30d',
    );
    await act(async () => { btn30d?.click(); });
    // Summaries should still be present
    expect(container.textContent).toContain('Session reliability');
    expect(container.textContent).toContain('Recovery is performing well');
  });

  it('metric hints still render after window switch', async () => {
    setupAllMocks();
    await render();
    const btnAll = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'All Time',
    );
    await act(async () => { btnAll?.click(); });
    expect(container.textContent).toContain('Sessions that reached a successful outcome');
  });

  it('footer timestamps still render after window switch', async () => {
    setupAllMocks();
    await render();
    const btn30d = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === '30d',
    );
    await act(async () => { btn30d?.click(); });
    expect(container.textContent).toContain('Data as of');
  });
});

// ── no extra backend calls ────────────────────────────────────────────────────

describe('no extra backend calls from polish additions', () => {
  it('calls each endpoint exactly once on mount', async () => {
    setupAllMocks();
    await render();
    expect((analyticsAPI.getOperational as Mock).mock.calls.length).toBe(1);
    expect((analyticsAPI.getFailures as Mock).mock.calls.length).toBe(1);
    expect((analyticsAPI.getKnowledge as Mock).mock.calls.length).toBe(1);
    expect((analyticsAPI.getExecution as Mock).mock.calls.length).toBe(1);
    expect((analyticsAPI.getOperators as Mock).mock.calls.length).toBe(1);
    expect((analyticsAPI.getDecision as Mock).mock.calls.length).toBe(1);
  });
});
