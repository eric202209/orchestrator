import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import AnalyticsDashboard from '@/pages/AnalyticsDashboard';
import { analyticsAPI } from '@/api/client';
import { SimpleBarChart } from '@/components/analytics/SimpleBarChart';
import { DistributionBarChart } from '@/components/analytics/DistributionBarChart';
import { RateComparisonChart } from '@/components/analytics/RateComparisonChart';

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
  failure_category_distribution: { timeout: 2, planning: 1 },
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

// ── helpers ───────────────────────────────────────────────────────────────────

function setupAllMocks() {
  (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
  (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
  (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
  (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
  (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
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

async function renderDashboard() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <AnalyticsDashboard />
      </MemoryRouter>,
    );
  });
}

// ── SimpleBarChart unit tests ─────────────────────────────────────────────────

describe('SimpleBarChart', () => {
  it('renders bars with data', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <SimpleBarChart
          title="Test"
          bars={[{ label: '7d', value: 0.8 }, { label: '30d', value: 0.6 }]}
          max={1}
          formatValue={(v) => (v == null ? '—' : `${Math.round(v * 100)}%`)}
        />,
      );
    });
    expect(div.textContent).toContain('Test');
    expect(div.textContent).toContain('7d');
    expect(div.textContent).toContain('80%');
    expect(div.textContent).toContain('60%');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('shows empty state when all values are null', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <SimpleBarChart
          bars={[{ label: '7d', value: null }, { label: '30d', value: null }]}
          emptyText="Nothing here"
        />,
      );
    });
    expect(div.textContent).toContain('Nothing here');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('shows empty state when bars array is empty', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(<SimpleBarChart bars={[]} emptyText="No bars" />);
    });
    expect(div.textContent).toContain('No bars');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('handles all-zero values without crashing', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <SimpleBarChart
          bars={[{ label: '7d', value: 0 }, { label: '30d', value: 0 }]}
          formatValue={(v) => String(v ?? '—')}
        />,
      );
    });
    expect(div.textContent).toContain('7d');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('auto-computes max from bars when max prop is not provided', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <SimpleBarChart
          bars={[{ label: 'a', value: 10 }, { label: 'b', value: 5 }]}
          formatValue={(v) => String(v ?? '—')}
        />,
      );
    });
    // Both bars rendered; a=10 is the max so bar width=100%, b=50%
    const bars = div.querySelectorAll('[role="presentation"]');
    expect(bars.length).toBe(2);
    act(() => { r.unmount(); });
    div.remove();
  });
});

// ── DistributionBarChart unit tests ───────────────────────────────────────────

describe('DistributionBarChart', () => {
  it('renders bars with data and shows percentage', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <DistributionBarChart
          title="Categories"
          data={{ timeout: 3, validation: 1 }}
        />,
      );
    });
    expect(div.textContent).toContain('Categories');
    expect(div.textContent).toContain('timeout');
    expect(div.textContent).toContain('validation');
    // timeout is 3/4 = 75%, validation is 1/4 = 25%
    expect(div.textContent).toContain('75%');
    expect(div.textContent).toContain('25%');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('shows empty state when data is empty', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(<DistributionBarChart title="Dist" data={{}} emptyText="Empty dist" />);
    });
    expect(div.textContent).toContain('Empty dist');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('sorts entries descending by count', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <DistributionBarChart
          title="Sorted"
          data={{ low: 1, high: 10, mid: 5 }}
        />,
      );
    });
    const text = div.textContent ?? '';
    // high should appear before low in sorted order
    expect(text.indexOf('high')).toBeLessThan(text.indexOf('low'));
    act(() => { r.unmount(); });
    div.remove();
  });
});

// ── RateComparisonChart unit tests ────────────────────────────────────────────

describe('RateComparisonChart', () => {
  it('renders groups with both values', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <RateComparisonChart
          title="Latency"
          groups={[{ label: '7d', a: 2.1, b: 8.7 }]}
          labelA="P50"
          labelB="P95"
          formatValue={(v) => (v == null ? '—' : `${v}s`)}
        />,
      );
    });
    expect(div.textContent).toContain('Latency');
    expect(div.textContent).toContain('P50');
    expect(div.textContent).toContain('P95');
    expect(div.textContent).toContain('7d');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('shows empty state when all values are null', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <RateComparisonChart
          groups={[{ label: '7d', a: null, b: null }]}
          labelA="P50"
          labelB="P95"
          emptyText="No latency data"
        />,
      );
    });
    expect(div.textContent).toContain('No latency data');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('shows empty state when groups is empty', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <RateComparisonChart groups={[]} labelA="P50" labelB="P95" emptyText="None" />,
      );
    });
    expect(div.textContent).toContain('None');
    act(() => { r.unmount(); });
    div.remove();
  });

  it('renders null individual values with — placeholder', () => {
    const div = document.createElement('div');
    document.body.appendChild(div);
    const r = createRoot(div);
    act(() => {
      r.render(
        <RateComparisonChart
          groups={[{ label: '7d', a: 2.1, b: null }]}
          labelA="P50"
          labelB="P95"
          formatValue={(v) => (v == null ? '—' : `${v}s`)}
        />,
      );
    });
    expect(div.textContent).toContain('—');
    act(() => { r.unmount(); });
    div.remove();
  });
});

// ── Dashboard integration: charts render ─────────────────────────────────────

describe('charts render in dashboard', () => {
  it('shows by-window rate chart titles in operational section', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Session Success Rate by Window');
    expect(container.textContent).toContain('First-Attempt Success Rate by Window');
  });

  it('shows by-window rate chart in failure section', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Recovery Success Rate by Window');
  });

  it('shows distribution bar chart in failure section', async () => {
    setupAllMocks();
    await renderDashboard();
    // DistributionBarChart renders percentages alongside DistributionTable counts
    expect(container.textContent).toContain('Failure Category Distribution');
  });

  it('shows knowledge hit rate by window chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Knowledge Hit Rate by Window');
  });

  it('shows top items by retrieval count chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Top Items by Retrieval Count');
  });

  it('shows queue latency P50 vs P95 comparison chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Queue Latency: P50 vs P95 by Window');
    expect(container.textContent).toContain('P50');
    expect(container.textContent).toContain('P95');
  });

  it('shows backend distribution chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Backend Distribution');
  });

  it('shows autonomy rate by window chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Autonomy Rate by Window');
  });

  it('shows intervention type distribution chart', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Intervention Type Distribution');
  });
});

// ── Dashboard integration: charts tolerate empty/null data ───────────────────

describe('charts tolerate empty and null data', () => {
  it('renders gracefully when distributions are empty', async () => {
    const emptyOp: OperationalAnalytics = {
      ...operational,
      windows: {
        '7d': { ...operationalWindow, failure_category_distribution: {} },
        '30d': { ...operationalWindow, failure_category_distribution: {} },
        all_time: { ...operationalWindow, failure_category_distribution: {} },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: emptyOp });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await renderDashboard();
    expect(container.textContent).toContain('Operational Health');
  });

  it('renders — for null rates in by-window charts', async () => {
    const nullOp: OperationalAnalytics = {
      ...operational,
      windows: {
        '7d': { ...operationalWindow, session_success_rate: null, first_attempt_success_rate: null },
        '30d': { ...operationalWindow, session_success_rate: null, first_attempt_success_rate: null },
        all_time: { ...operationalWindow, session_success_rate: null, first_attempt_success_rate: null },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: nullOp });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await renderDashboard();
    // Null rates render — placeholder
    expect(container.textContent).toContain('—');
  });

  it('renders gracefully when knowledge top_items is empty', async () => {
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
    await renderDashboard();
    // Chart shows empty state text
    expect(container.textContent).toContain('No knowledge items');
  });

  it('renders gracefully when queue latency values are null', async () => {
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
    await renderDashboard();
    // RateComparisonChart shows empty state or — for nulls
    expect(container.textContent).toContain('Execution Analytics');
  });

  it('renders gracefully when intervention_type_distribution is empty', async () => {
    const emptyOps: OperatorAnalytics = {
      ...operators,
      windows: {
        '7d': { ...operatorWindow, intervention_type_distribution: {} },
        '30d': { ...operatorWindow, intervention_type_distribution: {} },
        all_time: { ...operatorWindow, intervention_type_distribution: {} },
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: emptyOps });
    await renderDashboard();
    expect(container.textContent).toContain('Operator Analytics');
  });
});

// ── Dashboard integration: tables still render ───────────────────────────────

describe('existing tables still render alongside charts', () => {
  it('DistributionTable for failure categories still shows raw counts', async () => {
    setupAllMocks();
    await renderDashboard();
    // DistributionTable renders timeout count; DistributionBarChart renders %
    // Both should appear
    expect(container.textContent).toContain('timeout');
  });

  it('DistributionTable for backends still shows backend labels', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('openclaw');
  });

  it('DistributionTable for intervention types still shows type labels', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('guidance');
  });

  it('TopItemsTable for knowledge still renders', async () => {
    setupAllMocks();
    await renderDashboard();
    expect(container.textContent).toContain('Python typing guide');
  });
});

// ── Dashboard integration: window switching updates current-window charts ─────

describe('window switching updates current-window chart data', () => {
  it('distribution chart updates when window switches to different data', async () => {
    // Use different distribution data per window
    const multiWindowOps: OperatorAnalytics = {
      ...operators,
      windows: {
        '7d': { ...operatorWindow, intervention_type_distribution: { guidance: 5 } },
        '30d': { ...operatorWindow, intervention_type_distribution: { approval: 9 } },
        all_time: operatorWindow,
      },
    };
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: multiWindowOps });
    await renderDashboard();

    // On 7d window, 'guidance' should appear
    expect(container.textContent).toContain('guidance');

    // Switch to 30d
    const btn30d = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === '30d',
    );
    await act(async () => { btn30d?.click(); });

    // On 30d window, 'approval' should appear
    expect(container.textContent).toContain('approval');
  });
});

// ── Dashboard integration: five sections render independently ─────────────────

describe('all five sections render independently after chart additions', () => {
  it('knowledge section renders even if operational fails', async () => {
    (analyticsAPI.getOperational as Mock).mockRejectedValue(new Error('Network'));
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await renderDashboard();
    expect(container.textContent).toContain('Knowledge Hit Rate by Window');
    expect(container.textContent).toContain('Failed to load operational data');
  });

  it('execution section renders even if knowledge fails', async () => {
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockRejectedValue(new Error('Network'));
    (analyticsAPI.getExecution as Mock).mockResolvedValue({ data: execution });
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await renderDashboard();
    expect(container.textContent).toContain('Queue Latency: P50 vs P95 by Window');
  });

  it('operator section renders even if execution fails', async () => {
    (analyticsAPI.getOperational as Mock).mockResolvedValue({ data: operational });
    (analyticsAPI.getFailures as Mock).mockResolvedValue({ data: failures });
    (analyticsAPI.getKnowledge as Mock).mockResolvedValue({ data: knowledge });
    (analyticsAPI.getExecution as Mock).mockRejectedValue(new Error('Network'));
    (analyticsAPI.getOperators as Mock).mockResolvedValue({ data: operators });
    await renderDashboard();
    expect(container.textContent).toContain('Autonomy Rate by Window');
  });
});
