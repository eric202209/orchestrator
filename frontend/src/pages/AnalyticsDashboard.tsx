import { useState, useEffect, useCallback, useRef } from 'react';
import { BarChart2, RefreshCw } from 'lucide-react';
import { analyticsAPI } from '@/api/client';
import type {
  OperationalAnalytics,
  FailureAnalytics,
  KnowledgeAnalytics,
  ExecutionAnalytics,
  OperatorAnalytics,
  AnalyticsWindow,
} from '@/types/api';
import {
  AnalyticsCard,
  MetricCard,
  MetricGrid,
  DistributionTable,
  TopItemsTable,
  WindowSelector,
  LoadingPanel,
  ErrorPanel,
  SimpleBarChart,
  DistributionBarChart,
  RateComparisonChart,
} from '@/components/analytics';

// ── formatters ────────────────────────────────────────────────────────────────

function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

function fmtSec(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v < 60) return `${v.toFixed(1)}s`;
  const m = Math.floor(v / 60);
  const s = Math.round(v % 60);
  return `${m}m ${s}s`;
}

function fmtNum(v: number | null | undefined): string {
  if (v == null) return '—';
  return v.toLocaleString();
}

function secsAgo(iso: string): string {
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  return `${Math.floor(diff / 60)}m ago`;
}

// ── Section: Operational Health ───────────────────────────────────────────────

function OperationalSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: OperationalAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load operational data" />;
  if (!data) return null;

  const w = data.windows[win];
  const fmtRate = (v: number | null) => fmtPct(v);
  return (
    <div className="space-y-4">
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard label="Session Success Rate" value={fmtPct(w.session_success_rate)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="First Attempt Success" value={fmtPct(w.first_attempt_success_rate)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Sessions Started" value={fmtNum(w.sessions_started)} />
        </div>
        <div className="pl-4">
          <MetricCard label="Sessions Failed" value={fmtNum(w.sessions_failed)} />
        </div>
      </MetricGrid>
      <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)] grid grid-cols-1 sm:grid-cols-2 gap-6">
        <SimpleBarChart
          title="Session Success Rate by Window"
          bars={[
            { label: '7d', value: data.windows['7d'].session_success_rate },
            { label: '30d', value: data.windows['30d'].session_success_rate },
            { label: 'All', value: data.windows['all_time'].session_success_rate },
          ]}
          max={1}
          formatValue={fmtRate}
        />
        <SimpleBarChart
          title="First-Attempt Success Rate by Window"
          bars={[
            { label: '7d', value: data.windows['7d'].first_attempt_success_rate },
            { label: '30d', value: data.windows['30d'].first_attempt_success_rate },
            { label: 'All', value: data.windows['all_time'].first_attempt_success_rate },
          ]}
          max={1}
          formatValue={fmtRate}
        />
      </div>
      {Object.keys(w.failure_category_distribution).length > 0 && (
        <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <DistributionTable
            title="Failure Category Distribution"
            data={w.failure_category_distribution}
          />
        </div>
      )}
    </div>
  );
}

// ── Section: Failure Analytics ────────────────────────────────────────────────

function FailureSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: FailureAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load failure data" />;
  if (!data) return null;

  const w = data.windows[win];
  return (
    <div className="space-y-4">
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard label="Recovery Success Rate" value={fmtPct(w.recovery_success_rate)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Recovery Attempts" value={fmtNum(w.recovery_attempts)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Budget Exhaustions" value={fmtNum(w.budget_exhaustion_count)} />
        </div>
        <div className="pl-4">
          <MetricCard label="Repair Churn" value={fmtNum(w.churn_guard_activations)} />
        </div>
      </MetricGrid>
      <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)] grid grid-cols-1 sm:grid-cols-2 gap-6">
        <SimpleBarChart
          title="Recovery Success Rate by Window"
          bars={[
            { label: '7d', value: data.windows['7d'].recovery_success_rate },
            { label: '30d', value: data.windows['30d'].recovery_success_rate },
            { label: 'All', value: data.windows['all_time'].recovery_success_rate },
          ]}
          max={1}
          formatValue={fmtPct}
        />
        {Object.keys(w.failure_category_distribution).length > 0 && (
          <DistributionBarChart
            title="Failure Category Distribution"
            data={w.failure_category_distribution}
          />
        )}
      </div>
      {Object.keys(w.failure_category_distribution).length > 0 && (
        <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <DistributionTable
            title="Failure Category Distribution"
            data={w.failure_category_distribution}
          />
        </div>
      )}
    </div>
  );
}

// ── Section: Knowledge Analytics ──────────────────────────────────────────────

function KnowledgeSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: KnowledgeAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load knowledge data" />;
  if (!data) return null;

  const w = data.windows[win];
  return (
    <div className="space-y-4">
      <MetricGrid cols={2}>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Knowledge Hit Rate"
            value={fmtPct(w.knowledge_hit_rate)}
            sub={`${fmtNum(w.used_in_prompt_count)} / ${fmtNum(w.retrieval_count)} retrievals`}
          />
        </div>
        <div className="pl-4">
          <MetricCard label="Knowledge Effectiveness" value={fmtPct(w.effectiveness_rate)} />
        </div>
      </MetricGrid>
      <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)] grid grid-cols-1 sm:grid-cols-2 gap-6">
        <SimpleBarChart
          title="Knowledge Hit Rate by Window"
          bars={[
            { label: '7d', value: data.windows['7d'].knowledge_hit_rate },
            { label: '30d', value: data.windows['30d'].knowledge_hit_rate },
            { label: 'All', value: data.windows['all_time'].knowledge_hit_rate },
          ]}
          max={1}
          formatValue={fmtPct}
        />
        <SimpleBarChart
          title="Top Items by Retrieval Count"
          bars={w.top_items.map((item) => ({
            label: (item.title || item.knowledge_item_id).slice(0, 16),
            value: item.retrieval_count,
          }))}
          formatValue={fmtNum}
          emptyText="No knowledge items"
        />
      </div>
      {(w.top_items.length > 0 || w.low_effectiveness_items.length > 0) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <TopItemsTable
            title="Top Knowledge Items"
            items={w.top_items}
            emptyText="No knowledge items"
          />
          <TopItemsTable
            title="Low Effectiveness Items"
            items={w.low_effectiveness_items}
            emptyText="No low-effectiveness items"
          />
        </div>
      )}
    </div>
  );
}

// ── Section: Execution Analytics ──────────────────────────────────────────────

function ExecutionSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: ExecutionAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load execution data" />;
  if (!data) return null;

  const w = data.windows[win];
  const totalTokens = (w.tokens_in_total ?? 0) + (w.tokens_out_total ?? 0);
  return (
    <div className="space-y-4">
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Mean Runtime"
            value={fmtSec(w.mean_execution_duration_seconds)}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Queue P50" value={fmtSec(w.queue_latency_p50_seconds)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Queue P95" value={fmtSec(w.queue_latency_p95_seconds)} />
        </div>
        <div className="pl-4">
          <MetricCard
            label="Total Tokens"
            value={fmtNum(totalTokens)}
            sub={`${fmtNum(w.tokens_in_total)} in / ${fmtNum(w.tokens_out_total)} out`}
          />
        </div>
      </MetricGrid>
      <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)] grid grid-cols-1 sm:grid-cols-2 gap-6">
        <RateComparisonChart
          title="Queue Latency: P50 vs P95 by Window"
          groups={[
            {
              label: '7d',
              a: data.windows['7d'].queue_latency_p50_seconds,
              b: data.windows['7d'].queue_latency_p95_seconds,
            },
            {
              label: '30d',
              a: data.windows['30d'].queue_latency_p50_seconds,
              b: data.windows['30d'].queue_latency_p95_seconds,
            },
            {
              label: 'All',
              a: data.windows['all_time'].queue_latency_p50_seconds,
              b: data.windows['all_time'].queue_latency_p95_seconds,
            },
          ]}
          labelA="P50"
          labelB="P95"
          formatValue={fmtSec}
        />
        {Object.keys(w.backend_distribution).length > 0 && (
          <DistributionBarChart
            title="Backend Distribution"
            data={w.backend_distribution}
          />
        )}
      </div>
      {Object.keys(w.backend_distribution).length > 0 && (
        <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <DistributionTable title="Backend Distribution" data={w.backend_distribution} />
        </div>
      )}
    </div>
  );
}

// ── Section: Operator Analytics ───────────────────────────────────────────────

function OperatorSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: OperatorAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load operator data" />;
  if (!data) return null;

  const w = data.windows[win];
  return (
    <div className="space-y-4">
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard label="Autonomy Rate" value={fmtPct(w.autonomy_rate)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="Intervention Rate"
            value={fmtPct(
              w.autonomy_rate != null ? 1 - w.autonomy_rate : null,
            )}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Mean Response Time" value={fmtSec(w.mean_response_seconds)} />
        </div>
        <div className="pl-4">
          <MetricCard
            label="Pause / Resume / Stop"
            value={`${w.pause_count} / ${w.resume_count} / ${w.stop_count}`}
          />
        </div>
      </MetricGrid>
      <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)] grid grid-cols-1 sm:grid-cols-2 gap-6">
        <SimpleBarChart
          title="Autonomy Rate by Window"
          bars={[
            { label: '7d', value: data.windows['7d'].autonomy_rate },
            { label: '30d', value: data.windows['30d'].autonomy_rate },
            { label: 'All', value: data.windows['all_time'].autonomy_rate },
          ]}
          max={1}
          formatValue={fmtPct}
        />
        {Object.keys(w.intervention_type_distribution).length > 0 && (
          <DistributionBarChart
            title="Intervention Type Distribution"
            data={w.intervention_type_distribution}
          />
        )}
      </div>
      {Object.keys(w.intervention_type_distribution).length > 0 && (
        <div className="mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <DistributionTable
            title="Intervention Types"
            data={w.intervention_type_distribution}
          />
        </div>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

const AUTO_REFRESH_MS = 60_000;

export default function AnalyticsDashboard() {
  const [window, setWindow] = useState<AnalyticsWindow>('7d');
  const [lastRefreshed, setLastRefreshed] = useState<string | null>(null);

  const [operational, setOperational] = useState<OperationalAnalytics | null>(null);
  const [operationalLoading, setOperationalLoading] = useState(true);
  const [operationalError, setOperationalError] = useState(false);

  const [failures, setFailures] = useState<FailureAnalytics | null>(null);
  const [failuresLoading, setFailuresLoading] = useState(true);
  const [failuresError, setFailuresError] = useState(false);

  const [knowledge, setKnowledge] = useState<KnowledgeAnalytics | null>(null);
  const [knowledgeLoading, setKnowledgeLoading] = useState(true);
  const [knowledgeError, setKnowledgeError] = useState(false);

  const [execution, setExecution] = useState<ExecutionAnalytics | null>(null);
  const [executionLoading, setExecutionLoading] = useState(true);
  const [executionError, setExecutionError] = useState(false);

  const [operators, setOperators] = useState<OperatorAnalytics | null>(null);
  const [operatorsLoading, setOperatorsLoading] = useState(true);
  const [operatorsError, setOperatorsError] = useState(false);

  const anyLoading =
    operationalLoading ||
    failuresLoading ||
    knowledgeLoading ||
    executionLoading ||
    operatorsLoading;

  const fetchAll = useCallback(async () => {
    setOperationalLoading(true);
    setFailuresLoading(true);
    setKnowledgeLoading(true);
    setExecutionLoading(true);
    setOperatorsLoading(true);

    const [op, fa, kn, ex, oa] = await Promise.allSettled([
      analyticsAPI.getOperational(),
      analyticsAPI.getFailures(),
      analyticsAPI.getKnowledge(),
      analyticsAPI.getExecution(),
      analyticsAPI.getOperators(),
    ]);

    if (op.status === 'fulfilled') {
      setOperational(op.value.data);
      setOperationalError(false);
    } else {
      setOperationalError(true);
    }
    setOperationalLoading(false);

    if (fa.status === 'fulfilled') {
      setFailures(fa.value.data);
      setFailuresError(false);
    } else {
      setFailuresError(true);
    }
    setFailuresLoading(false);

    if (kn.status === 'fulfilled') {
      setKnowledge(kn.value.data);
      setKnowledgeError(false);
    } else {
      setKnowledgeError(true);
    }
    setKnowledgeLoading(false);

    if (ex.status === 'fulfilled') {
      setExecution(ex.value.data);
      setExecutionError(false);
    } else {
      setExecutionError(true);
    }
    setExecutionLoading(false);

    if (oa.status === 'fulfilled') {
      setOperators(oa.value.data);
      setOperatorsError(false);
    } else {
      setOperatorsError(true);
    }
    setOperatorsLoading(false);

    setLastRefreshed(new Date().toISOString());
  }, []);

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetchAll();
    timerRef.current = setInterval(fetchAll, AUTO_REFRESH_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [fetchAll]);

  return (
    <div className="bg-[color:var(--oc-canvas)] min-h-screen space-y-4">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-2">
        <div className="flex items-center gap-2">
          <BarChart2 className="h-5 w-5 text-[color:var(--oc-accent)]" />
          <h1 className="text-lg font-semibold text-white">Analytics Dashboard</h1>
        </div>
        <div className="flex items-center gap-3">
          <WindowSelector value={window} onChange={setWindow} />
          <button
            onClick={fetchAll}
            disabled={anyLoading}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
            aria-label="Refresh analytics"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${anyLoading ? 'animate-spin' : ''}`} />
            {lastRefreshed ? secsAgo(lastRefreshed) : 'Refresh'}
          </button>
        </div>
      </div>

      {/* Operational Health */}
      <AnalyticsCard title="Operational Health">
        <OperationalSection
          data={operational}
          loading={operationalLoading}
          error={operationalError}
          window={window}
        />
      </AnalyticsCard>

      {/* Failure Analytics */}
      <AnalyticsCard title="Failure Analytics">
        <FailureSection
          data={failures}
          loading={failuresLoading}
          error={failuresError}
          window={window}
        />
      </AnalyticsCard>

      {/* Knowledge Analytics */}
      <AnalyticsCard title="Knowledge Analytics">
        <KnowledgeSection
          data={knowledge}
          loading={knowledgeLoading}
          error={knowledgeError}
          window={window}
        />
      </AnalyticsCard>

      {/* Execution Analytics */}
      <AnalyticsCard title="Execution Analytics">
        <ExecutionSection
          data={execution}
          loading={executionLoading}
          error={executionError}
          window={window}
        />
      </AnalyticsCard>

      {/* Operator Analytics */}
      <AnalyticsCard title="Operator Analytics">
        <OperatorSection
          data={operators}
          loading={operatorsLoading}
          error={operatorsError}
          window={window}
        />
      </AnalyticsCard>
    </div>
  );
}
