import { useState, useEffect, useCallback } from 'react';
import { FlaskConical, RefreshCw } from 'lucide-react';
import { Skeleton, EmptyState } from '@/components/ui';
import { projectsAPI } from '@/api/client';
import {
  pilotAPI,
  type PilotSummary,
  type PilotGuidanceStats,
  type PilotTokenStats,
  type PilotPermissionStats,
  type QueueLatencyStats,
  type AuditEventsResponse,
} from '@/api/client';
import type { Page, Project } from '@/types/api';

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtPct(v: number | null): string {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

function fmtNum(v: number | null | undefined): string {
  if (v == null) return '—';
  return v.toLocaleString();
}

function fmtSec(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v < 60) return `${v.toFixed(1)}s`;
  const m = Math.floor(v / 60);
  const s = Math.round(v % 60);
  return `${m}m ${s}s`;
}

function secsAgo(iso: string): string {
  const diff = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  return `${Math.floor(diff / 60)}m ago`;
}

const pageItems = <T,>(data: Page<T> | T[] | null | undefined): T[] =>
  Array.isArray(data) ? data : data?.items ?? [];

// ── Decision Card ─────────────────────────────────────────────────────────────

type Verdict = 'READY' | 'CAUTION' | 'NOT_READY' | 'LOADING';

interface Criterion {
  label: string;
  state: 'pass' | 'warn' | 'fail' | 'unknown';
}

function computeVerdict(
  summary: PilotSummary | null,
  guidance: PilotGuidanceStats | null,
  tokens: PilotTokenStats | null,
  perms: PilotPermissionStats | null,
  audit: AuditEventsResponse | null,
): { verdict: Verdict; criteria: Criterion[] } {
  if (!summary && !guidance && !tokens && !perms && !audit) {
    return { verdict: 'LOADING', criteria: [] };
  }

  const criteria: Criterion[] = [];

  const successRate = summary?.rates.success_rate ?? null;
  criteria.push({
    label: `Success rate: ${fmtPct(successRate)}`,
    state:
      successRate == null ? 'unknown'
      : successRate >= 0.7 ? 'pass'
      : successRate >= 0.5 ? 'warn'
      : 'fail',
  });

  const conflictRate = guidance?.conflicts.conflict_rate ?? null;
  criteria.push({
    label: `Guidance conflict rate: ${fmtPct(conflictRate)}`,
    state:
      conflictRate == null ? 'unknown'
      : conflictRate < 0.3 ? 'pass'
      : conflictRate < 0.5 ? 'warn'
      : 'fail',
  });

  const auditTotal = audit?.total ?? null;
  criteria.push({
    label: `Audit trail: ${auditTotal == null ? '—' : auditTotal > 0 ? `${auditTotal} events` : 'empty'}`,
    state: auditTotal == null ? 'unknown' : auditTotal > 0 ? 'pass' : 'warn',
  });

  const tokenRate = tokens?.token_availability_rate ?? null;
  criteria.push({
    label: `Token data coverage: ${fmtPct(tokenRate)}`,
    state:
      tokenRate == null ? 'unknown'
      : tokenRate >= 0.5 ? 'pass'
      : 'warn',
  });

  const pending = perms?.pending ?? 0;
  const maxResp = perms?.max_response_seconds ?? 0;
  const permState =
    perms == null ? 'unknown'
    : pending > 0 && maxResp > 300 ? 'fail'
    : 'pass';
  criteria.push({
    label: `Permission path: ${perms == null ? '—' : pending === 0 ? 'clear' : `${pending} pending`}`,
    state: permState,
  });

  const hasNotReady = criteria.some((c) => c.state === 'fail');
  const hasCaution = criteria.some((c) => c.state === 'warn' || c.state === 'unknown');

  const verdict: Verdict =
    hasNotReady ? 'NOT_READY'
    : hasCaution ? 'CAUTION'
    : 'READY';

  return { verdict, criteria };
}

function DecisionCard({
  summary, guidance, tokens, perms, audit, loading,
}: {
  summary: PilotSummary | null;
  guidance: PilotGuidanceStats | null;
  tokens: PilotTokenStats | null;
  perms: PilotPermissionStats | null;
  audit: AuditEventsResponse | null;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-28 w-full" />;
  }

  const { verdict, criteria } = computeVerdict(summary, guidance, tokens, perms, audit);

  const colors: Record<Verdict, string> = {
    READY: 'border-emerald-500/30 bg-emerald-500/10',
    CAUTION: 'border-amber-500/30 bg-amber-500/10',
    NOT_READY: 'border-red-500/30 bg-red-500/10',
    LOADING: 'border-slate-500/30 bg-slate-500/10',
  };
  const textColors: Record<Verdict, string> = {
    READY: 'text-emerald-400',
    CAUTION: 'text-amber-400',
    NOT_READY: 'text-red-400',
    LOADING: 'text-slate-400',
  };
  const labels: Record<Verdict, string> = {
    READY: '● READY',
    CAUTION: '⚠ CAUTION',
    NOT_READY: '✗ NOT READY',
    LOADING: '… LOADING',
  };

  return (
    <div
      className={`rounded-lg border p-4 mb-5 ${colors[verdict]}`}
    >
      <p className={`text-base font-semibold mb-3 ${textColors[verdict]}`}>
        {labels[verdict]}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
        {criteria.map((c) => (
          <div key={c.label} className="flex items-center gap-2 text-xs">
            <span className={
              c.state === 'pass' ? 'text-emerald-400'
              : c.state === 'warn' ? 'text-amber-400'
              : c.state === 'fail' ? 'text-red-400'
              : 'text-slate-500'
            }>
              {c.state === 'pass' ? '✓'
               : c.state === 'warn' ? '⚠'
               : c.state === 'fail' ? '✗'
               : '—'}
            </span>
            <span className="text-slate-300">{c.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Card shell ────────────────────────────────────────────────────────────────

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-4">
      <p className="text-sm font-medium text-slate-300 mb-3">{title}</p>
      {children}
    </div>
  );
}

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between py-1 border-b border-[color:var(--oc-border-soft)] last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <span className="text-sm font-semibold text-white tabular-nums">{value}</span>
    </div>
  );
}

// ── Section cards ─────────────────────────────────────────────────────────────

function PilotSummaryCard({ data, loading }: { data: PilotSummary | null; loading: boolean }) {
  return (
    <Card title="Pilot Summary">
      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      ) : !data ? (
        <EmptyState title="No data" description="Select a project to view summary." />
      ) : (
        <div>
          <div className="grid grid-cols-5 gap-1 mb-3 text-center">
            {[
              ['total', data.task_executions.total],
              ['done', data.task_executions.done],
              ['fail', data.task_executions.failed],
              ['pend', data.task_executions.pending],
              ['run', data.task_executions.running],
            ].map(([label, val]) => (
              <div key={String(label)}>
                <p className="text-lg font-semibold text-white">{val}</p>
                <p className="text-[10px] text-slate-500">{label}</p>
              </div>
            ))}
          </div>
          <StatRow label="Success rate" value={fmtPct(data.rates.success_rate)} />
          <StatRow label="Rejection rate" value={fmtPct(data.rates.rejection_rate)} />
          <StatRow label="Timeout rate" value={fmtPct(data.rates.timeout_rate)} />
        </div>
      )}
    </Card>
  );
}

function GuidanceCard({ data, loading }: { data: PilotGuidanceStats | null; loading: boolean }) {
  return (
    <Card title="Human Guidance">
      {loading ? (
        <div className="space-y-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </div>
      ) : !data ? (
        <EmptyState title="No data" description="No guidance data for this project." />
      ) : (
        <div>
          <div className="grid grid-cols-3 gap-1 mb-3 text-center">
            <div>
              <p className="text-lg font-semibold text-white">{data.usage.total_injections}</p>
              <p className="text-[10px] text-slate-500">injections</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">{data.conflicts.total}</p>
              <p className="text-[10px] text-slate-500">conflicts</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">{data.conflicts.resolved}</p>
              <p className="text-[10px] text-slate-500">resolved</p>
            </div>
          </div>
          <StatRow label="Conflict rate" value={fmtPct(data.conflicts.conflict_rate)} />
          {data.usage.top_entries.length > 0 && (
            <div className="mt-2">
              <p className="text-[10px] text-slate-500 mb-1">Top entries</p>
              {data.usage.top_entries.map((e) => (
                <div key={e.guidance_id} className="flex items-center justify-between py-0.5">
                  <span className="text-xs text-slate-400 truncate max-w-[75%]">
                    · {e.message_preview || `#${e.guidance_id}`}
                  </span>
                  <span className="text-xs text-slate-300 tabular-nums ml-2">
                    ×{e.injection_count}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function VerificationCard({ data, loading }: { data: PilotSummary | null; loading: boolean }) {
  return (
    <Card title="Completion Verification">
      {loading ? (
        <Skeleton className="h-20 w-full" />
      ) : !data ? (
        <EmptyState title="No data" description="Select a project." />
      ) : (
        <div>
          <div className="grid grid-cols-3 gap-1 mb-3 text-center">
            <div>
              <p className="text-lg font-semibold text-white">
                {data.symbol_verification.passed ?? '—'}
              </p>
              <p className="text-[10px] text-slate-500">pass</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">
                {data.symbol_verification.failed}
              </p>
              <p className="text-[10px] text-slate-500">fail</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">—</p>
              <p className="text-[10px] text-slate-500">n/a</p>
            </div>
          </div>
          {data.symbol_verification.passed === null && (
            <p className="text-[11px] text-slate-500 italic">
              Pass count unavailable — Gap 1 not yet resolved.
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

function QueueCard({ data, loading }: { data: QueueLatencyStats | null; loading: boolean }) {
  return (
    <Card title="Queue Observability">
      {loading ? (
        <Skeleton className="h-20 w-full" />
      ) : !data ? (
        <EmptyState title="No data" description="No queue latency data." />
      ) : (
        <div>
          <div className="grid grid-cols-3 gap-1 mb-3 text-center">
            <div>
              <p className="text-lg font-semibold text-white">
                {fmtSec(data.avg_queue_latency_seconds)}
              </p>
              <p className="text-[10px] text-slate-500">avg</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">
                {fmtSec(data.max_queue_latency_seconds)}
              </p>
              <p className="text-[10px] text-slate-500">max</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">
                {fmtSec(data.p95_queue_latency_seconds)}
              </p>
              <p className="text-[10px] text-slate-500">p95</p>
            </div>
          </div>
          <StatRow
            label="Executions with latency"
            value={String(data.executions_with_latency)}
          />
        </div>
      )}
    </Card>
  );
}

function TokenCard({ data, loading }: { data: PilotTokenStats | null; loading: boolean }) {
  return (
    <Card title="Token Usage">
      {loading ? (
        <Skeleton className="h-20 w-full" />
      ) : !data ? (
        <EmptyState title="No data" description="No token data for this project." />
      ) : (
        <div>
          <div className="grid grid-cols-3 gap-1 mb-3 text-center">
            <div>
              <p className="text-lg font-semibold text-white">{data.tasks_with_tokens}</p>
              <p className="text-[10px] text-slate-500">with data</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">
                {fmtNum(data.avg_tokens_in)}
              </p>
              <p className="text-[10px] text-slate-500">avg in</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-white">
                {fmtNum(data.avg_tokens_out)}
              </p>
              <p className="text-[10px] text-slate-500">avg out</p>
            </div>
          </div>
          {data.top_consumers.length > 0 && (
            <div className="mt-1">
              <p className="text-[10px] text-slate-500 mb-1">Top consumers</p>
              {data.top_consumers.map((c, i) => (
                <div key={i} className="flex items-center justify-between py-0.5">
                  <span className="text-xs text-slate-400 truncate max-w-[60%]">
                    · {c.task_title || `Task ${c.task_id}`}
                  </span>
                  <span className="text-xs text-slate-300 tabular-nums ml-2">
                    {fmtNum(c.tokens_in)} / {fmtNum(c.tokens_out)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function PermissionCard({
  data, loading,
}: { data: PilotPermissionStats | null; loading: boolean }) {
  return (
    <Card title="Permission Activity">
      {loading ? (
        <Skeleton className="h-20 w-full" />
      ) : !data ? (
        <EmptyState title="No data" description="No permission data for this project." />
      ) : (
        <div>
          <div className="grid grid-cols-3 gap-1 mb-3 text-center">
            <div>
              <p className="text-lg font-semibold text-emerald-400">{data.approvals}</p>
              <p className="text-[10px] text-slate-500">approved</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-red-400">{data.denials}</p>
              <p className="text-[10px] text-slate-500">denied</p>
            </div>
            <div>
              <p className="text-lg font-semibold text-amber-400">{data.pending}</p>
              <p className="text-[10px] text-slate-500">pending</p>
            </div>
          </div>
          <StatRow label="Avg response" value={fmtSec(data.avg_response_seconds)} />
          <StatRow label="Max response" value={fmtSec(data.max_response_seconds)} />
        </div>
      )}
    </Card>
  );
}

const EVENT_TYPE_OPTIONS = [
  { value: '', label: 'All events' },
  { value: 'PERMISSION_APPROVED', label: 'Permission approved' },
  { value: 'PERMISSION_DENIED', label: 'Permission denied' },
  { value: 'PERMISSION_REQUIRED', label: 'Permission required' },
  { value: 'TOKEN_USAGE_RECORDED', label: 'Token usage' },
  { value: 'COMPLETION_SYMBOL_VERIFICATION_FAILED', label: 'Symbol verification failed' },
  { value: 'GUIDANCE_CONFLICT_WARNING', label: 'Guidance conflict' },
];

function AuditTimelineCard({
  projectId,
  loading,
}: { projectId: number | null; loading: boolean }) {
  const [events, setEvents] = useState<AuditEventsResponse | null>(null);
  const [eventType, setEventType] = useState('');
  const [fetching, setFetching] = useState(false);
  const [offset, setOffset] = useState(0);
  const LIMIT = 20;

  const fetch = useCallback(async () => {
    if (!projectId) return;
    setFetching(true);
    try {
      const resp = await pilotAPI.getAuditEvents({
        project_id: projectId,
        event_type: eventType || undefined,
        limit: LIMIT,
        offset,
        order: 'desc',
      });
      setEvents(resp.data);
    } catch {
      // leave previous data
    } finally {
      setFetching(false);
    }
  }, [projectId, eventType, offset]);

  useEffect(() => {
    fetch();
  }, [fetch]);

  useEffect(() => {
    setOffset(0);
  }, [projectId, eventType]);

  return (
    <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-4">
      <div className="flex items-center justify-between mb-3">
        <p className="text-sm font-medium text-slate-300">Audit Timeline</p>
        <select
          value={eventType}
          onChange={(e) => setEventType(e.target.value)}
          className="text-xs bg-[color:var(--oc-surface-raised)] border border-[color:var(--oc-border-soft)] text-slate-300 rounded px-2 py-1"
        >
          {EVENT_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      {loading || fetching ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-8 w-full" />
          ))}
        </div>
      ) : !events || events.items.length === 0 ? (
        <EmptyState
          title="No audit events"
          description="No structured events found for this project and filter."
        />
      ) : (
        <>
          <div className="space-y-1 font-mono text-xs">
            {events.items.map((e) => (
              <div key={e.id} className="flex gap-3 py-1 border-b border-[color:var(--oc-border-soft)] last:border-0">
                <span className="text-slate-500 flex-shrink-0 w-14 text-right">
                  {e.created_at ? new Date(e.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'}
                </span>
                <span className={`flex-shrink-0 w-12 ${e.level === 'WARNING' ? 'text-amber-400' : e.level === 'ERROR' ? 'text-red-400' : 'text-sky-400'}`}>
                  {e.level?.slice(0, 4)}
                </span>
                <span className="text-slate-300 truncate">{e.message}</span>
              </div>
            ))}
          </div>
          <div className="flex items-center justify-between mt-3 text-xs text-slate-500">
            <span>Showing {offset + 1}–{Math.min(offset + LIMIT, events.total)} of {events.total}</span>
            <div className="flex gap-2">
              {offset > 0 && (
                <button
                  onClick={() => setOffset(Math.max(0, offset - LIMIT))}
                  className="text-sky-400 hover:text-sky-300"
                >
                  ← Prev
                </button>
              )}
              {offset + LIMIT < events.total && (
                <button
                  onClick={() => setOffset(offset + LIMIT)}
                  className="text-sky-400 hover:text-sky-300"
                >
                  Next →
                </button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function AdminPilotDashboard() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<string | null>(null);

  const [summary, setSummary] = useState<PilotSummary | null>(null);
  const [guidance, setGuidance] = useState<PilotGuidanceStats | null>(null);
  const [tokens, setTokens] = useState<PilotTokenStats | null>(null);
  const [perms, setPerms] = useState<PilotPermissionStats | null>(null);
  const [queue, setQueue] = useState<QueueLatencyStats | null>(null);
  const [audit, setAudit] = useState<AuditEventsResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    projectsAPI.getAll({ page: 1, per_page: 100, order_by: 'name', order_dir: 'asc' }).then((r) => {
      if (cancelled) return;
      const projectItems = pageItems(r.data);
      setProjects(projectItems);
      setSelectedProjectId((prev) => prev ?? projectItems[0]?.id ?? null);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const fetchAll = useCallback(async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    const [s, g, t, p, q, a] = await Promise.allSettled([
      pilotAPI.getSummary(selectedProjectId),
      pilotAPI.getGuidanceStats(selectedProjectId),
      pilotAPI.getTokenStats(selectedProjectId),
      pilotAPI.getPermissionStats(selectedProjectId),
      pilotAPI.getQueueLatency(7),
      pilotAPI.getAuditEvents({ project_id: selectedProjectId, limit: 20, order: 'desc' }),
    ]);
    if (s.status === 'fulfilled') setSummary(s.value.data);
    if (g.status === 'fulfilled') setGuidance(g.value.data);
    if (t.status === 'fulfilled') setTokens(t.value.data);
    if (p.status === 'fulfilled') setPerms(p.value.data);
    if (q.status === 'fulfilled') setQueue(q.value.data);
    if (a.status === 'fulfilled') setAudit(a.value.data);
    setLastRefreshed(new Date().toISOString());
    setLoading(false);
  }, [selectedProjectId]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 30_000);
    return () => clearInterval(id);
  }, [fetchAll]);

  return (
    <div className="bg-[color:var(--oc-canvas)] min-h-screen">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
        <div className="flex items-center gap-2">
          <FlaskConical className="h-5 w-5 text-[color:var(--oc-accent)]" />
          <h1 className="text-lg font-semibold text-white">Pilot Evidence Dashboard</h1>
        </div>
        <div className="flex items-center gap-3">
          {projects.length > 0 && (
            <select
              value={selectedProjectId ?? ''}
              onChange={(e) => setSelectedProjectId(Number(e.target.value))}
              className="text-sm bg-[color:var(--oc-surface)] border border-[color:var(--oc-border-soft)] text-slate-300 rounded px-2 py-1.5"
            >
              {projects.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          )}
          <button
            onClick={fetchAll}
            disabled={loading}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            {lastRefreshed ? secsAgo(lastRefreshed) : 'Refresh'}
          </button>
        </div>
      </div>

      {!selectedProjectId ? (
        <EmptyState
          icon={FlaskConical}
          title="No project selected"
          description="Select a project from the dropdown to view pilot evidence."
        />
      ) : (
        <>
          {/* No-evidence banner — distinguishes "never ran" from "pipeline broken" */}
          {!loading && summary && summary.task_executions.total === 0 && (
            <div className="mb-5 rounded-lg border border-sky-500/20 bg-sky-500/5 p-4">
              <p className="text-sm font-medium text-sky-300 mb-1">
                No orchestration evidence found for this project.
              </p>
              <p className="text-xs text-slate-400 mb-2">
                The selected project has 0 TaskExecution rows. All metrics below will show empty or zero.
              </p>
              <ul className="text-xs text-slate-500 space-y-0.5 list-none">
                <li>• Run at least one orchestrator session against this project to populate:</li>
                <li className="pl-3">Human Guidance metrics · Token metrics · Queue latency · Audit events · Permission activity</li>
              </ul>
            </div>
          )}

          {/* Decision Card */}
          <DecisionCard
            summary={summary}
            guidance={guidance}
            tokens={tokens}
            perms={perms}
            audit={audit}
            loading={loading}
          />

          {/* 2-column grid */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
            <PilotSummaryCard data={summary} loading={loading} />
            <GuidanceCard data={guidance} loading={loading} />
            <VerificationCard data={summary} loading={loading} />
            <QueueCard data={queue} loading={loading} />
            <TokenCard data={tokens} loading={loading} />
            <PermissionCard data={perms} loading={loading} />
          </div>

          {/* Audit Timeline — full width */}
          <AuditTimelineCard projectId={selectedProjectId} loading={loading} />
        </>
      )}
    </div>
  );
}
