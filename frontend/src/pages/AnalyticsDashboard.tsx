import { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import { BarChart2, ChevronDown, RefreshCw } from 'lucide-react';
import { analyticsAPI } from '@/api/client';
import type {
  OperationalAnalytics,
  OperationalWindow,
  FailureAnalytics,
  FailureWindow,
  KnowledgeAnalytics,
  KnowledgeWindow,
  ExecutionAnalytics,
  ExecutionWindow,
  OperatorAnalytics,
  OperatorWindow,
  DecisionAnalytics,
  AnalyticsWindow,
  DecisionImprovementOpportunity,
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

function fmtGeneratedAt(iso: string): string {
  const d = new Date(iso);
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const month = months[d.getUTCMonth()];
  const day = d.getUTCDate();
  const h = String(d.getUTCHours()).padStart(2, '0');
  const min = String(d.getUTCMinutes()).padStart(2, '0');
  return `${month} ${day} at ${h}:${min} UTC`;
}

// ── section summaries ─────────────────────────────────────────────────────────

function operationalSummary(w: OperationalWindow): string {
  if (w.session_success_rate == null) return 'No sessions recorded in this window.';
  if (w.session_success_rate >= 0.9) return 'Session reliability is high this window.';
  if (w.session_success_rate >= 0.7) return 'Session reliability is moderate — some sessions are failing.';
  return 'Session failure rate is elevated — review recent failures.';
}

function failureSummary(w: FailureWindow): string {
  if (w.recovery_success_rate == null) return 'No failure recovery data in this window.';
  if (w.recovery_success_rate >= 0.8) return 'Recovery is performing well this window.';
  if (w.recovery_success_rate >= 0.5) return 'Recovery success is moderate — some failures are unresolved.';
  return 'Low recovery success rate — failures are not being resolved.';
}

function knowledgeSummary(w: KnowledgeWindow): string {
  if (w.knowledge_hit_rate == null) return 'No knowledge retrievals in this window.';
  if (w.knowledge_hit_rate >= 0.7) return 'Knowledge base is actively used and frequently included in prompts.';
  if (w.knowledge_hit_rate >= 0.4) return 'Knowledge base is used, but many retrievals do not reach prompts.';
  return 'Knowledge base has low prompt inclusion — most retrievals are filtered out.';
}

function executionSummary(w: ExecutionWindow): string {
  if (w.queue_latency_p95_seconds == null) {
    if (w.execution_count === 0) return 'No executions in this window.';
    return 'Execution timing data unavailable for this window.';
  }
  if (w.queue_latency_p95_seconds <= 5) return 'Queue latency is low — jobs are starting quickly.';
  if (w.queue_latency_p95_seconds <= 30) return 'Queue latency is moderate.';
  return 'High P95 queue latency — some jobs are waiting a long time.';
}

function operatorSummary(w: OperatorWindow): string {
  if (w.autonomy_rate == null) return 'No operator session data in this window.';
  if (w.autonomy_rate >= 0.8) return 'System is operating with high autonomy.';
  if (w.autonomy_rate >= 0.5) return 'Moderate operator involvement — some sessions need guidance.';
  return 'High operator intervention rate — many sessions require attention.';
}

function fmtDecisionMetric(label: string, value: number | null | undefined): string {
  if (value == null) return '—';
  if (label.toLowerCase().includes('rate') || label.toLowerCase().includes('effectiveness')) {
    return fmtPct(value);
  }
  return fmtNum(value);
}

function decisionSummary(data: DecisionAnalytics, win: AnalyticsWindow): string {
  const w = data.windows[win];
  const count = w.improvement_opportunities.length;
  if (count === 0) return 'No recommendation candidates in this window.';
  return `${count} evidence-backed improvement ${count === 1 ? 'candidate' : 'candidates'} in this window.`;
}

function fmtUnknownMetric(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'number') {
    if (value >= 0 && value <= 1) return fmtPct(value);
    return fmtNum(value);
  }
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

function EvidencePanel({ item }: { item: DecisionImprovementOpportunity }) {
  const metrics = Object.entries(item.evidence.supporting_metrics);
  return (
    <div className="mt-3 rounded-md border border-[color:var(--oc-border-soft)] bg-black/10 p-3 space-y-3">
      <p className="text-xs text-slate-300">{item.rationale}</p>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <MetricCard label="Confidence" value={fmtPct(item.confidence)} />
        <MetricCard label="Sample Size" value={fmtNum(item.evidence.sample_size)} />
        <MetricCard label="Projects" value={fmtNum(item.evidence.affected_projects.length)} />
        <MetricCard label="Sessions" value={fmtNum(item.evidence.affected_sessions.length)} />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <p className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">Affected Projects</p>
          <p className="text-xs text-slate-300 break-words">
            {item.evidence.affected_projects.length > 0 ? item.evidence.affected_projects.join(', ') : 'None recorded'}
          </p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wide text-slate-500 mb-1">Affected Sessions</p>
          <p className="text-xs text-slate-300 break-words">
            {item.evidence.affected_sessions.length > 0 ? item.evidence.affected_sessions.join(', ') : 'None recorded'}
          </p>
        </div>
      </div>
      <div>
        <p className="text-[10px] uppercase tracking-wide text-slate-500 mb-2">Supporting Metrics</p>
        {metrics.length > 0 ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1">
            {metrics.map(([key, value]) => (
              <div key={key} className="flex items-center justify-between gap-3 text-xs">
                <span className="text-slate-500 truncate">{key.replace(/_/g, ' ')}</span>
                <span className="text-slate-300 tabular-nums">{fmtUnknownMetric(value)}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-slate-600">No supporting metrics recorded.</p>
        )}
      </div>
    </div>
  );
}

function DecisionSection({
  data,
  loading,
  error,
  window: win,
}: {
  data: DecisionAnalytics | null;
  loading: boolean;
  error: boolean;
  window: AnalyticsWindow;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (loading) return <LoadingPanel />;
  if (error) return <ErrorPanel message="Failed to load decision intelligence" />;
  if (!data) return null;

  const w = data.windows[win];
  const topOpportunities = w.improvement_opportunities.slice(0, 4);
  const topRecovery = w.successful_recovery_strategies.slice(0, 3);
  const topKnowledge = w.knowledge_effectiveness.slice(0, 3);

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">{decisionSummary(data, win)}</p>
      {topOpportunities.length > 0 ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {topOpportunities.map((item) => (
            <div
              key={`${item.kind}-${item.target}-${item.metric_label}`}
              className="border border-[color:var(--oc-border-soft)] rounded-md p-3 bg-[color:var(--oc-surface-muted)]"
            >
              {(() => {
                const itemKey = `${item.kind}-${item.target}-${item.metric_label}`;
                const isExpanded = expanded === itemKey;
                return (
                  <>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-200 truncate">{item.target}</p>
                  <p className="text-[10px] uppercase tracking-wide text-slate-500">{item.kind.replace('_', ' ')}</p>
                </div>
                <span className={`text-[10px] uppercase ${item.severity === 'high' ? 'text-red-300' : 'text-amber-300'}`}>
                  {item.severity}
                </span>
              </div>
              <div className="mt-3 grid grid-cols-[1fr_auto] items-end gap-3">
                <div>
                  <p className="text-xs text-slate-500">{item.metric_label}</p>
                  <p className="text-xl font-semibold text-white tabular-nums">
                    {fmtDecisionMetric(item.metric_label, item.metric_value)}
                  </p>
                </div>
                <p className="text-lg text-slate-500">↓</p>
              </div>
              <p className="mt-2 text-xs text-slate-300">{item.recommendation}</p>
              {item.kind === 'knowledge' && item.knowledge_item_id && (
                <Link
                  to={`/knowledge?item=${item.knowledge_item_id}&source=decision`}
                  className="mt-2 inline-block text-[11px] text-[color:var(--oc-accent)] hover:underline"
                >
                  Open in Knowledge Library →
                </Link>
              )}
              <div className="mt-2 flex items-center justify-between gap-3">
                <p className="text-[10px] text-slate-600">
                  {fmtNum(item.evidence.sample_size)} samples · {fmtPct(item.confidence)} confidence
                </p>
                <button
                  type="button"
                  onClick={() => setExpanded(isExpanded ? null : itemKey)}
                  className="inline-flex items-center gap-1 text-[10px] text-[color:var(--oc-accent)] hover:underline"
                  aria-expanded={isExpanded}
                >
                  Evidence
                  <ChevronDown className={`h-3 w-3 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
                </button>
              </div>
              {isExpanded && <EvidencePanel item={item} />}
                  </>
                );
              })()}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-xs text-slate-500">No improvement opportunities in this window.</p>
      )}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
        <div>
          <h3 className="text-xs font-semibold text-slate-300 mb-2">Recovery Strategies</h3>
          <div className="space-y-2">
            {topRecovery.map((item) => (
              <div key={item.repair_type} className="flex items-center justify-between gap-2 text-xs">
                <span className="text-slate-400 truncate">{item.repair_type}</span>
                <span className="text-slate-200 tabular-nums">{fmtPct(item.success_rate)}</span>
              </div>
            ))}
            {topRecovery.length === 0 && <p className="text-xs text-slate-600">No recovery evidence.</p>}
          </div>
        </div>
        <div>
          <h3 className="text-xs font-semibold text-slate-300 mb-2">Knowledge Leaderboard</h3>
          <div className="space-y-2">
            {topKnowledge.map((item) => (
              <div key={item.knowledge_item_id} className="flex items-center justify-between gap-2 text-xs">
                <span className="text-slate-400 truncate">{item.title || item.knowledge_item_id}</span>
                <span className="text-slate-200 tabular-nums">{fmtPct(item.effectiveness)}</span>
              </div>
            ))}
            {topKnowledge.length === 0 && <p className="text-xs text-slate-600">No knowledge evidence.</p>}
          </div>
        </div>
        <div>
          <h3 className="text-xs font-semibold text-slate-300 mb-2">Repeated Failures</h3>
          <div className="space-y-2">
            {w.repeated_failures.slice(0, 3).map((item) => (
              <div key={item.failure_signature} className="flex items-center justify-between gap-2 text-xs">
                <span className="text-slate-400 truncate">{item.failure_signature}</span>
                <span className="text-slate-200 tabular-nums">{fmtNum(item.occurrences)}</span>
              </div>
            ))}
            {w.repeated_failures.length === 0 && <p className="text-xs text-slate-600">No repeated failures.</p>}
          </div>
        </div>
      </div>
      <p className="text-[10px] text-slate-700 pt-2 border-t border-[color:var(--oc-border-soft)]">
        Data as of {fmtGeneratedAt(data.generated_at)}
      </p>
    </div>
  );
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
      <p className="text-xs text-slate-500">{operationalSummary(w)}</p>
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Session Success Rate"
            value={fmtPct(w.session_success_rate)}
            hint={w.session_success_rate == null ? 'Not enough data yet' : 'Sessions that reached a successful outcome'}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="First Attempt Success"
            value={fmtPct(w.first_attempt_success_rate)}
            hint={w.first_attempt_success_rate == null ? 'Not enough data yet' : 'No repair or retry was needed'}
          />
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
      <p className="text-[10px] text-slate-700 pt-2 border-t border-[color:var(--oc-border-soft)]">
        Data as of {fmtGeneratedAt(data.generated_at)}
      </p>
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
      <p className="text-xs text-slate-500">{failureSummary(w)}</p>
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Recovery Success Rate"
            value={fmtPct(w.recovery_success_rate)}
            hint={w.recovery_success_rate == null ? 'Not enough data yet' : 'Failed sessions recovered by the repair system'}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard label="Recovery Attempts" value={fmtNum(w.recovery_attempts)} />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="Budget Exhaustions"
            value={fmtNum(w.budget_exhaustion_count)}
            hint="Sessions where all repair attempts were used up"
          />
        </div>
        <div className="pl-4">
          <MetricCard
            label="Repair Churn"
            value={fmtNum(w.churn_guard_activations)}
            hint="Sessions stopped by the churn guard"
          />
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
            emptyText="No categorized failures in this window"
          />
        </div>
      )}
      <div className="flex items-center justify-between pt-2 border-t border-[color:var(--oc-border-soft)]">
        <p className="text-[10px] text-slate-700">Data as of {fmtGeneratedAt(data.generated_at)}</p>
        <Link
          to="/sessions"
          className="text-[10px] text-[color:var(--oc-accent)] hover:underline"
        >
          View sessions →
        </Link>
      </div>
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
      <p className="text-xs text-slate-500">{knowledgeSummary(w)}</p>
      <MetricGrid cols={2}>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Knowledge Hit Rate"
            value={fmtPct(w.knowledge_hit_rate)}
            sub={`${fmtNum(w.used_in_prompt_count)} / ${fmtNum(w.retrieval_count)} retrievals`}
            hint={w.knowledge_hit_rate == null ? 'Not enough data yet' : 'Retrievals included in a prompt'}
          />
        </div>
        <div className="pl-4">
          <MetricCard
            label="Knowledge Effectiveness"
            value={fmtPct(w.effectiveness_rate)}
            hint={w.effectiveness_rate == null ? 'Not enough data yet' : 'Retrieved items that aided task completion'}
          />
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
            label: item.title || item.knowledge_item_id,
            value: item.retrieval_count,
          }))}
          formatValue={fmtNum}
          emptyText="No knowledge retrievals in this window"
          labelClassName="w-40 text-left"
        />
      </div>
      {(w.top_items.length > 0 || w.low_effectiveness_items.length > 0) && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-4 pt-4 border-t border-[color:var(--oc-border-soft)]">
          <TopItemsTable
            title="Top Knowledge Items"
            items={w.top_items}
            emptyText="No knowledge retrievals in this window"
          />
          <TopItemsTable
            title="Low Effectiveness Items"
            items={w.low_effectiveness_items}
            emptyText="No low-effectiveness items in this window"
          />
        </div>
      )}
      <p className="text-[10px] text-slate-700 pt-2 border-t border-[color:var(--oc-border-soft)]">
        Data as of {fmtGeneratedAt(data.generated_at)}
      </p>
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
      <p className="text-xs text-slate-500">{executionSummary(w)}</p>
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Mean Runtime"
            value={fmtSec(w.mean_execution_duration_seconds)}
            hint={w.mean_execution_duration_seconds == null ? 'Not enough data yet' : undefined}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="Queue P50"
            value={fmtSec(w.queue_latency_p50_seconds)}
            hint={w.queue_latency_p50_seconds == null ? 'Not enough data yet' : 'Half of jobs waited less than this'}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="Queue P95"
            value={fmtSec(w.queue_latency_p95_seconds)}
            hint={w.queue_latency_p95_seconds == null ? 'Not enough data yet' : '95% of jobs waited less than this'}
          />
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
          <DistributionTable
            title="Backend Distribution"
            data={w.backend_distribution}
            emptyText="No executions in this window"
          />
        </div>
      )}
      <p className="text-[10px] text-slate-700 pt-2 border-t border-[color:var(--oc-border-soft)]">
        Data as of {fmtGeneratedAt(data.generated_at)}
      </p>
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
      <p className="text-xs text-slate-500">{operatorSummary(w)}</p>
      <MetricGrid>
        <div className="pl-0 pr-4">
          <MetricCard
            label="Autonomy Rate"
            value={fmtPct(w.autonomy_rate)}
            hint={w.autonomy_rate == null ? 'Not enough data yet' : 'Sessions completed without operator action'}
          />
        </div>
        <div className="pl-4 pr-4">
          <MetricCard
            label="Intervention Rate"
            value={fmtPct(w.autonomy_rate != null ? 1 - w.autonomy_rate : null)}
            hint={w.autonomy_rate == null ? 'Not enough data yet' : 'Sessions that needed at least one operator action'}
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
            emptyText="No operator interventions in this window"
          />
        </div>
      )}
      <p className="text-[10px] text-slate-700 pt-2 border-t border-[color:var(--oc-border-soft)]">
        Data as of {fmtGeneratedAt(data.generated_at)}
      </p>
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

  const [decision, setDecision] = useState<DecisionAnalytics | null>(null);
  const [decisionLoading, setDecisionLoading] = useState(true);
  const [decisionError, setDecisionError] = useState(false);

  const anyLoading =
    operationalLoading ||
    failuresLoading ||
    knowledgeLoading ||
    executionLoading ||
    operatorsLoading ||
    decisionLoading;

  const fetchAll = useCallback(async () => {
    setOperationalLoading(true);
    setFailuresLoading(true);
    setKnowledgeLoading(true);
    setExecutionLoading(true);
    setOperatorsLoading(true);
    setDecisionLoading(true);

    const loadSection = async <T,>(
      request: () => Promise<{ data: T }>,
      setData: (value: T) => void,
      setError: (value: boolean) => void,
      setLoading: (value: boolean) => void,
    ) => {
      try {
        const response = await request();
        setData(response.data);
        setError(false);
      } catch {
        setError(true);
      } finally {
        setLoading(false);
      }
    };

    await loadSection(
      analyticsAPI.getOperational,
      setOperational,
      setOperationalError,
      setOperationalLoading,
    );
    await loadSection(
      analyticsAPI.getFailures,
      setFailures,
      setFailuresError,
      setFailuresLoading,
    );
    await loadSection(
      analyticsAPI.getKnowledge,
      setKnowledge,
      setKnowledgeError,
      setKnowledgeLoading,
    );
    await loadSection(
      analyticsAPI.getExecution,
      setExecution,
      setExecutionError,
      setExecutionLoading,
    );
    await loadSection(
      analyticsAPI.getOperators,
      setOperators,
      setOperatorsError,
      setOperatorsLoading,
    );
    await loadSection(
      analyticsAPI.getDecision,
      setDecision,
      setDecisionError,
      setDecisionLoading,
    );

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

      {/* Decision Intelligence */}
      <AnalyticsCard title="Decision Intelligence">
        <DecisionSection
          data={decision}
          loading={decisionLoading}
          error={decisionError}
          window={window}
        />
      </AnalyticsCard>

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
