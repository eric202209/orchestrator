import { useState } from 'react';
import type { ReactNode } from 'react';
import type {
  Checkpoint,
  CheckpointInspection,
  ExecutionFailureSummary,
  FailureDiagnostics,
  InterventionRequest,
  KnowledgeUsageEntry,
  Project,
  Session,
  SessionDecisionEvent,
  SessionDispatchWatchdogResponse,
  SessionDivergenceCompareResponse,
  SessionReplayResponse,
  SessionStateDiffResponse,
  Task,
} from '@/types/api';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { TerminalViewer } from '@/components/TerminalViewer';
import { StatusBadge } from '@/components/ui';
import { deriveRunStateFromTask, getRunStateDisplay } from '@/lib/runState';
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  Clock,
  ExternalLink,
  MessageCircle,
  RefreshCw,
  Settings,
  Terminal as TerminalIcon,
  XCircle,
} from 'lucide-react';
import { cn } from '@/lib/utils';

type TimelineEventType =
  | 'planning'
  | 'executing'
  | 'debugging'
  | 'revising_plan'
  | 'summarizing'
  | 'checkpoint'
  | 'validation'
  | 'repair'
  | 'task'
  | 'error'
  | 'status'
  | 'info';
type TimelineEventImportance = 'primary' | 'secondary';

export interface TimelineEvent {
  id: string;
  at: string;
  type: TimelineEventType;
  title: string;
  detail: string;
  importance?: TimelineEventImportance;
}

export interface TimelineSpan {
  id: string;
  title: string;
  lane: 'reasoning' | 'tool' | 'workspace' | 'validation' | 'system';
  status: 'healthy' | 'warning' | 'error';
  started_at: string;
  event_count: number;
  summary: string;
}

export interface OffTrackMoment {
  id: string;
  timestamp: string;
  phase: string;
  reason: string;
  trigger: 'health_threshold' | 'accepted_after_rejection' | 'divergence';
  health_score?: number | null;
  event_type: string;
  event_id?: string | null;
}

export interface RepairGenealogyNode {
  id: string;
  parent_id?: string | null;
  timestamp: string;
  event_type: string;
  title: string;
  status: 'original' | 'repair' | 'accepted' | 'rejected' | 'abandoned';
  validator?: string | null;
  reason?: string | null;
  event_id?: string | null;
  details?: Record<string, unknown>;
}

const getStringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value
        .map((item) => String(item || '').trim())
        .filter((item) => item.length > 0)
    : [];

const getNumberList = (value: unknown): number[] =>
  Array.isArray(value)
    ? value
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item))
    : [];

const getStepDetailEntries = (
  value: unknown
): Array<{ step: string; codes: string[] }> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return [];
  }
  return Object.entries(value as Record<string, unknown>)
    .map(([step, codes]) => ({ step, codes: getStringList(codes) }))
    .filter((entry) => entry.codes.length > 0);
};

const humanizeSeconds = (totalSeconds: number): string => {
  if (totalSeconds >= 86400) return `${(totalSeconds / 86400).toFixed(1)} days`;
  if (totalSeconds >= 3600) {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  }
  if (totalSeconds >= 60) {
    const m = Math.floor(totalSeconds / 60);
    const s = Math.floor(totalSeconds % 60);
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  }
  return `${Math.round(totalSeconds)}s`;
};

const humanizeAlertMessage = (message: string): string =>
  message.replace(/\b(\d+(?:\.\d+)?)s\b/g, (_, n: string) => humanizeSeconds(Number(n)));

const splitReasonIntoBullets = (reason: string): string[] => {
  const parts = reason.split(/,\s+(?=[A-Z])/);
  return parts.length >= 2 ? parts.map((p) => p.trim()).filter(Boolean) : [];
};

const getDiagnosticBadges = (
  diagnostics?: FailureDiagnostics | Record<string, unknown> | null
): string[] => {
  if (!diagnostics) return [];
  const badges: string[] = [];
  const subcodes = getStringList(diagnostics.brittle_command_subcodes);
  if (subcodes.length > 0) {
    badges.push(...subcodes.slice(0, 3));
  }
  const stepDetails = getStepDetailEntries(diagnostics.brittle_command_step_details);
  if (stepDetails.length > 0) {
    badges.push(
      ...stepDetails.slice(0, 3).map((entry) => `step ${entry.step}: ${entry.codes.join(', ')}`)
    );
  }
  const weakSteps = getNumberList(diagnostics.weak_verification_steps);
  if (weakSteps.length > 0) {
    badges.push(`weak verification steps ${weakSteps.join(', ')}`);
  }
  const missingSteps = getNumberList(diagnostics.missing_verification_steps);
  if (missingSteps.length > 0) {
    badges.push(`missing verification steps ${missingSteps.join(', ')}`);
  }
  if (typeof diagnostics.max_command_length === 'number') {
    badges.push(`max command ${diagnostics.max_command_length} chars`);
  }
  if (typeof diagnostics.command_total_chars === 'number') {
    badges.push(`total commands ${diagnostics.command_total_chars} chars`);
  }
  if (typeof diagnostics.heredoc_command_count === 'number') {
    badges.push(`${diagnostics.heredoc_command_count} heredocs`);
  }
  return Array.from(new Set(badges)).slice(0, 8);
};

const getDiagnosticReasons = (
  diagnostics?: FailureDiagnostics | Record<string, unknown> | null
): string[] => {
  if (!diagnostics) return [];
  const reasons = [
    ...getStringList(diagnostics.validation_reasons),
    ...getStringList(diagnostics.contract_violations),
  ];
  return Array.from(new Set(reasons)).slice(0, 5);
};

interface OperatorEvidence {
  boundary: string;
  operation?: string;
  operations: OperationEvidence[];
  targetPath?: string;
  outcome: string;
  reason?: string;
  nextAction: string;
}

interface OperationEvidence {
  op: string;
  path?: string;
  outcome?: string;
}

const FILE_OP_NAMES = ['replace_in_file', 'append_file', 'write_file', 'delete_file', 'mkdir'];

const getStringValue = (
  source: Record<string, unknown>,
  keys: string[]
): string | undefined => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return undefined;
};

const humanizeEvidenceValue = (value: string): string =>
  value.replace(/[_-]/g, ' ').replace(/\s+/g, ' ').trim();

const titleCaseEvidenceValue = (value: string): string =>
  humanizeEvidenceValue(value).replace(/\b\w/g, (letter) => letter.toUpperCase());

const detectOperation = (diagnostics: Record<string, unknown>, evidenceText: string) => {
  const explicitOperation = getStringValue(diagnostics, [
    'operation',
    'op',
    'op_name',
    'structured_op',
    'failed_op',
  ]);
  if (explicitOperation) return explicitOperation;
  return FILE_OP_NAMES.find((name) => evidenceText.includes(name));
};

const detectBoundary = (diagnostics: Record<string, unknown>, evidenceText: string): string => {
  const explicitBoundary = getStringValue(diagnostics, [
    'boundary',
    'failure_boundary',
    'failure_class',
    'contract_violation_type',
  ]);
  if (explicitBoundary) {
    const normalizedBoundary = explicitBoundary.toLowerCase();
    if (normalizedBoundary.includes('planning') && normalizedBoundary.includes('validation')) {
      return 'Planning Validation';
    }
    if (normalizedBoundary.includes('completion') && normalizedBoundary.includes('repair')) {
      return 'Completion Repair';
    }
    if (normalizedBoundary.includes('structured') || FILE_OP_NAMES.some((name) => normalizedBoundary.includes(name))) {
      return 'Structured Operation';
    }
    if (normalizedBoundary.includes('workspace')) return 'Workspace Guard';
    return titleCaseEvidenceValue(explicitBoundary);
  }
  if (evidenceText.includes('planning validation')) return 'Planning Validation';
  if (evidenceText.includes('completion repair')) return 'Completion Repair';
  if (evidenceText.includes('workspace_guard') || evidenceText.includes('workspace guard')) {
    return 'Workspace Guard';
  }
  if (FILE_OP_NAMES.some((name) => evidenceText.includes(name))) {
    return 'Structured Operation';
  }
  return 'Stopped Session Recovery';
};

const detectOutcome = (diagnostics: Record<string, unknown>, evidenceText: string): string => {
  const explicitOutcome = getStringValue(diagnostics, ['outcome']);
  if (explicitOutcome) return titleCaseEvidenceValue(explicitOutcome);
  if (diagnostics.workspace_guard_blocked || evidenceText.includes('workspace guard')) {
    return 'Blocked by workspace guard';
  }
  if (diagnostics.regex_fallback_applied || evidenceText.includes('regex replacement')) {
    return 'Regex fallback applied';
  }
  if (diagnostics.already_applied || diagnostics.applied || evidenceText.includes('already applied')) {
    return 'Already applied';
  }
  if (evidenceText.includes('repair_applied') || evidenceText.includes('replaced by repair')) {
    return 'Replaced by repair';
  }
  if (evidenceText.includes('failed') || evidenceText.includes('not found')) {
    return 'Failed';
  }
  return 'Needs operator decision';
};

const detectTargetPath = (diagnostics: Record<string, unknown>, evidenceText: string) => {
  const explicitPath = getStringValue(diagnostics, ['target_path', 'path', 'file_path']);
  if (explicitPath) return explicitPath;
  const pathMatch =
    evidenceText.match(/\bin\s+([A-Za-z0-9_./-]+\.[A-Za-z0-9_-]+)/) ||
    evidenceText.match(/\bpath[:=]\s*["']?([A-Za-z0-9_./-]+)/);
  return pathMatch?.[1];
};

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const extractOperationEvidence = (
  details: Record<string, unknown>,
  fallbackOperation?: string,
  fallbackPath?: string,
  fallbackOutcome?: string
): OperationEvidence[] => {
  const rawOperations = [
    details.ops,
    details.operations,
    details.replacement_ops,
    details.failed_ops,
  ].find((value) => Array.isArray(value)) as unknown[] | undefined;
  const singleOperation =
    asRecord(details.failed_op) ||
    asRecord(details.operation_detail) ||
    asRecord(details.operation);
  const operationRecords = rawOperations
    ? rawOperations.map(asRecord).filter((value): value is Record<string, unknown> => Boolean(value))
    : singleOperation
      ? [singleOperation]
      : [];

  const operations: OperationEvidence[] = [];
  operationRecords.forEach((operation) => {
      const op = getStringValue(operation, ['op', 'operation', 'op_name', 'name']);
      if (!op) return;
      operations.push({
        op,
        path: getStringValue(operation, ['path', 'target_path', 'file_path']),
        outcome: getStringValue(operation, ['outcome', 'status', 'result']),
      });
    });

  if (operations.length > 0) {
    return operations.slice(0, 3);
  }
  return fallbackOperation
    ? [{ op: fallbackOperation, path: fallbackPath, outcome: fallbackOutcome }]
    : [];
};

const getOperatorEvidence = (
  summary: ExecutionFailureSummary,
  canStartReplan: boolean,
  replanStillOwnsFlow: boolean
): OperatorEvidence => {
  const diagnostics = (summary.diagnostics || {}) as Record<string, unknown>;
  const reason = getStringValue(diagnostics, ['reason', 'message']) || summary.message;
  const evidenceText = [
    summary.summary,
    reason,
    JSON.stringify(diagnostics),
  ].join(' ').toLowerCase();
  const boundary = detectBoundary(diagnostics, evidenceText);
  const operation = detectOperation(diagnostics, evidenceText);
  const targetPath = detectTargetPath(diagnostics, evidenceText);
  const outcome = detectOutcome(diagnostics, evidenceText);
  const operations = extractOperationEvidence(diagnostics, operation, targetPath, outcome);
  const nextAction = replanStillOwnsFlow
    ? 'Open Project Architect'
    : boundary === 'Workspace Guard'
      ? 'Inspect workspace'
      : canStartReplan
        ? 'Send to Project Architect'
        : 'Retry or inspect workspace';

  return {
    boundary,
    operation,
    operations,
    targetPath,
    outcome,
    reason,
    nextAction,
  };
};

const getDecisionEventEvidence = (event: SessionDecisionEvent): OperatorEvidence | null => {
  const details = event.details || {};
  const evidenceText = [
    event.title,
    event.summary,
    event.phase,
    event.event_type,
    event.status,
    JSON.stringify(details),
  ].join(' ').toLowerCase();

  if (
    event.severity !== 'error' &&
    event.severity !== 'warning' &&
    !FILE_OP_NAMES.some((name) => evidenceText.includes(name)) &&
    !evidenceText.includes('planning validation') &&
    !evidenceText.includes('completion repair') &&
    !evidenceText.includes('workspace guard') &&
    !evidenceText.includes('workspace_guard')
  ) {
    return null;
  }

  const boundary = detectBoundary(details, evidenceText);
  const operation = detectOperation(details, evidenceText);
  const targetPath = detectTargetPath(details, evidenceText);
  const outcome = detectOutcome(details, evidenceText);

  return {
    boundary,
    operation,
    operations: extractOperationEvidence(details, operation, targetPath, outcome),
    targetPath,
    outcome,
    reason: getStringValue(details, ['reason', 'message', 'error']) || event.summary,
    nextAction: boundary === 'Workspace Guard'
      ? 'Inspect workspace'
      : 'Review recovery path',
  };
};

const formatRunDuration = (session: Session): string => {
  if (!session.started_at) return 'Not started';
  const startedAt = new Date(session.started_at);
  const endedAt = session.stopped_at ? new Date(session.stopped_at) : new Date();

  if (Number.isNaN(startedAt.getTime()) || Number.isNaN(endedAt.getTime())) {
    return 'Unknown';
  }

  const totalSeconds = Math.max(0, Math.round((endedAt.getTime() - startedAt.getTime()) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
};

const renderMarkdownSummary = (markdown: string) => {
  const lines = markdown.split('\n');
  const blocks: ReactNode[] = [];
  let listItems: string[] = [];
  let codeLines: string[] = [];
  let inCodeBlock = false;

  const flushList = () => {
    if (listItems.length === 0) return;
    const items = listItems;
    listItems = [];
    blocks.push(
      <ul key={`list-${blocks.length}`} className="list-disc space-y-1 pl-5 text-sm text-slate-200">
        {items.map((item, index) => (
          <li key={`${item}-${index}`}>{item}</li>
        ))}
      </ul>
    );
  };

  const flushCode = () => {
    if (codeLines.length === 0) return;
    const content = codeLines.join('\n');
    codeLines = [];
    blocks.push(
      <pre key={`code-${blocks.length}`} className="overflow-x-auto rounded-md bg-[color:var(--oc-surface-deep)] p-3 text-xs text-slate-300">
        {content}
      </pre>
    );
  };

  lines.forEach((line) => {
    const trimmed = line.trim();

    if (trimmed.startsWith('```')) {
      if (inCodeBlock) {
        inCodeBlock = false;
        flushCode();
      } else {
        flushList();
        inCodeBlock = true;
      }
      return;
    }

    if (inCodeBlock) {
      codeLines.push(line);
      return;
    }

    if (!trimmed) {
      flushList();
      return;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushList();
      const [, level, text] = heading;
      const className =
        level.length <= 2
          ? 'text-sm font-semibold text-red-300'
          : 'text-xs font-semibold uppercase text-red-300';
      blocks.push(
        <p key={`heading-${blocks.length}`} className={className}>
          {text}
        </p>
      );
      return;
    }

    const listItem = trimmed.match(/^[-*]\s+(.+)$/);
    if (listItem) {
      listItems.push(listItem[1]);
      return;
    }

    flushList();
    blocks.push(
      <p key={`p-${blocks.length}`} className="text-sm leading-6 text-slate-200">
        {trimmed}
      </p>
    );
  });

  flushList();
  flushCode();

  return blocks;
};

interface SessionHeaderProps {
  project: Project | null;
  session: Session;
  wsConnected: boolean;
  actionButtons: ReactNode;
}

export function SessionHeader({
  project,
  session,
  wsConnected,
  actionButtons,
}: SessionHeaderProps) {
  return (
    <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div className="mb-2 flex items-center gap-3">
          <h1 className="text-2xl font-bold text-slate-100">{session.name}</h1>
          <StatusBadge status={session.status} />
          {wsConnected && (
            <div className="flex items-center gap-1 text-sm text-emerald-400">
              <Activity className="h-4 w-4 animate-pulse" />
              <span>Live</span>
            </div>
          )}
        </div>
        <p className="text-sm text-slate-400">
          ID: {session.id} • Project: {project?.name || 'Unknown'}
          {project?.github_url && (
            <a
              href={project.github_url}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-2 text-primary-300 hover:text-primary-300"
            >
              <ExternalLink className="inline h-4 w-4" />
            </a>
          )}
        </p>
      </div>
      {actionButtons}
    </div>
  );
}

interface SessionConnectionProps {
  checkpointCount: number;
  session: Session;
  wsConnected: boolean;
}

export function SessionConnectionNotice({
  checkpointCount,
  session,
  wsConnected,
}: SessionConnectionProps) {
  return (
    <>
      <div className="flex items-center gap-2 text-sm">
        <div
          className={cn(
            'h-2 w-2 rounded-full',
            wsConnected ? 'bg-emerald-500 animate-pulse' : 'bg-slate-500'
          )}
        />
        <span className={cn(wsConnected ? 'text-emerald-400' : 'text-slate-500')}>
          {wsConnected ? 'WebSocket Connected - Live Logs' : 'WebSocket Disconnected'}
        </span>
      </div>

      {session.status === 'stopped' && checkpointCount > 0 && (
        <div className="rounded-lg border border-emerald-700/50 bg-emerald-900/20 p-4">
          <p className="text-sm text-emerald-300">
            Resume is available for this stopped session. {checkpointCount}{' '}
            checkpoint{checkpointCount === 1 ? '' : 's'} detected.
          </p>
        </div>
      )}

      {session.last_alert_message && (
        <div className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4">
          <p className="text-sm text-amber-300">
            Alert{session.last_alert_at ? ` • ${new Date(session.last_alert_at).toLocaleString()}` : ''}: {humanizeAlertMessage(session.last_alert_message)}
          </p>
        </div>
      )}
    </>
  );
}

interface SessionStatsProps {
  formatDateTime: (value?: string | null) => string;
  session: Session;
  tasksCount: number;
}

export function SessionStats({
  formatDateTime,
  session,
  tasksCount,
}: SessionStatsProps) {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <p className="mb-1.5 text-xs text-slate-400">Tasks</p>
        <p className="text-sm font-medium text-white">{tasksCount}</p>
      </div>
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <p className="mb-1.5 text-xs text-slate-400">Duration</p>
        <p className="text-sm font-medium text-white">{formatRunDuration(session)}</p>
        <p className="mt-0.5 text-xs capitalize text-slate-400">{session.execution_mode} mode</p>
      </div>
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <p className="mb-1.5 text-xs text-slate-400">Started</p>
        <p className="text-sm font-medium text-white font-mono">
          {session.started_at ? formatDateTime(session.started_at) : 'N/A'}
        </p>
        {session.stopped_at && (
          <>
            <p className="mt-2 mb-1 text-xs text-slate-400">Stopped</p>
            <p className="text-sm font-medium text-slate-300 font-mono">
              {formatDateTime(session.stopped_at)}
            </p>
          </>
        )}
      </div>
    </div>
  );
}

export type SessionDetailTab = 'timeline' | 'tasks' | 'logs' | 'settings';

interface SessionTabsProps {
  activeTab: SessionDetailTab;
  onChange: (tab: SessionDetailTab) => void;
  tasksCount: number;
}

export function SessionTabs({
  activeTab,
  onChange,
  tasksCount,
}: SessionTabsProps) {
  return (
    <div className="border-b border-[color:var(--oc-border-soft)]">
      <nav className="flex gap-0">
        <button
          onClick={() => onChange('timeline')}
          className={cn(
            'flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
            activeTab === 'timeline'
              ? 'border-primary-500 text-white'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          )}
        >
          <Clock className="h-3.5 w-3.5" />
          Timeline
        </button>
        <button
          onClick={() => onChange('tasks')}
          className={cn(
            'px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
            activeTab === 'tasks'
              ? 'border-primary-500 text-white'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          )}
        >
          Tasks {tasksCount > 0 && <span className="ml-1 text-xs text-slate-500">({tasksCount})</span>}
        </button>
        <button
          onClick={() => onChange('logs')}
          className={cn(
            'flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
            activeTab === 'logs'
              ? 'border-primary-500 text-white'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          )}
        >
          <TerminalIcon className="h-3.5 w-3.5" />
          Logs
        </button>
        <button
          onClick={() => onChange('settings')}
          className={cn(
            'flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
            activeTab === 'settings'
              ? 'border-primary-500 text-white'
              : 'border-transparent text-slate-500 hover:text-slate-300'
          )}
        >
          <Settings className="h-3.5 w-3.5" />
          Settings
        </button>
      </nav>
    </div>
  );
}

interface SessionLogsPanelProps {
  displayLogs: TerminalLogEntry[];
  handleRefreshLogs: () => Promise<void>;
  logVerbosity: 'clean' | 'verbose';
  logViewMode: 'newest' | 'oldest' | 'success' | 'errors' | 'all';
  onLogVerbosityChange: (mode: 'clean' | 'verbose') => void;
  onLogViewModeChange: (mode: 'newest' | 'oldest' | 'success' | 'errors' | 'all') => void;
  wsConnected: boolean;
}

interface SessionTimelinePanelProps {
  decisionEvents?: SessionDecisionEvent[];
  formatDateTime: (value?: string | null) => string;
  timelineSpans?: TimelineSpan[];
  timelineEvents: TimelineEvent[];
  offTrackMoment?: OffTrackMoment | null;
  repairGenealogy?: RepairGenealogyNode[];
}

interface SessionDiagnosticsPanelProps {
  anomalyEvents?: Array<{ title: string; detail: string; at: string }>;
  compareMatches?: SessionDivergenceCompareResponse | null;
  dispatchWatchdog?: SessionDispatchWatchdogResponse | null;
  formatDateTime: (value?: string | null) => string;
  healthEvents?: Array<{ timestamp: string; score: number; slope?: number | null }>;
  decisionEvents?: SessionDecisionEvent[];
  replayInvestigation?: SessionReplayResponse | null;
  stateDiff?: SessionStateDiffResponse | null;
}

export function SessionLogsPanel({
  displayLogs,
  handleRefreshLogs,
  logVerbosity,
  logViewMode,
  onLogVerbosityChange,
  onLogViewModeChange,
  wsConnected,
}: SessionLogsPanelProps) {
  return (
    <div className="space-y-4">
      <TerminalViewer
        logs={displayLogs}
        autoScroll={true}
        className="h-[500px] bg-[color:var(--oc-shell)]"
      />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm">
          <div
            className={cn(
              'h-2 w-2 rounded-full',
              wsConnected ? 'bg-emerald-500 animate-pulse' : 'bg-slate-500'
            )}
          />
          <span className={cn(wsConnected ? 'text-emerald-400' : 'text-slate-500')}>
            {displayLogs.length} logs loaded
          </span>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={logVerbosity}
            onChange={(e) => onLogVerbosityChange(e.target.value as 'clean' | 'verbose')}
            className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-2.5 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] focus:outline-none"
          >
            <option value="clean">Clean</option>
            <option value="verbose">Verbose</option>
          </select>
          <button
            onClick={handleRefreshLogs}
            className="flex items-center gap-1.5 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-2.5 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
          <select
            value={logViewMode}
            onChange={(e) =>
              onLogViewModeChange(
                e.target.value as 'newest' | 'oldest' | 'success' | 'errors' | 'all'
              )
            }
            className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-2.5 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] focus:outline-none"
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="success">Success only</option>
            <option value="errors">Errors only</option>
            <option value="all">All</option>
          </select>
        </div>
      </div>
    </div>
  );
}

export function SessionDiagnosticsPanel({
  anomalyEvents = [],
  compareMatches,
  dispatchWatchdog,
  formatDateTime,
  healthEvents = [],
  replayInvestigation,
  stateDiff,
}: SessionDiagnosticsPanelProps) {
  const latestHealth = healthEvents[healthEvents.length - 1] || null;
  const staleDispatch = dispatchWatchdog?.stale_tasks?.[0] || null;
  const queuedDispatches =
    dispatchWatchdog?.tasks?.filter((task) => task.dispatch_state === 'queued') || [];
  const visibleMatches = (compareMatches?.matches || []).filter(
    (match) => match.similarity_score > 0
  );

  return (
    <details className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-4">
      <summary className="cursor-pointer text-sm font-semibold text-slate-200 hover:text-white">
        Diagnostics
      </summary>
      <div className="mt-4 space-y-4">
        <div className="grid gap-3 lg:grid-cols-2">
          <div
          className={cn(
            'rounded-lg border p-4',
            staleDispatch
              ? 'border-amber-800/60 bg-amber-950/20'
              : 'border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)]'
          )}
          >
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">Dispatch Watchdog</h3>
            <span
              className={cn(
                'text-xs font-medium',
                staleDispatch ? 'text-amber-300' : 'text-emerald-400'
              )}
            >
              {dispatchWatchdog
                ? staleDispatch
                  ? `${dispatchWatchdog.stale_task_count} stale`
                  : 'healthy'
                : 'Unavailable'}
            </span>
          </div>
          {!dispatchWatchdog ? (
            <p className="text-sm text-slate-400">
              Queue/claim watchdog data appears after session events are indexed.
            </p>
          ) : (
            <div className="space-y-2 text-sm">
              <p className="text-slate-300">
                SLA: <span className="font-medium text-white">{dispatchWatchdog.sla_seconds}s</span>
              </p>
              <p className="text-slate-300">
                Queued now: <span className="font-medium text-white">{queuedDispatches.length}</span>
              </p>
              {staleDispatch ? (
                <>
                  <p className="text-amber-200">
                    Stalled: <span className="font-medium">{staleDispatch.task_title}</span>
                  </p>
                  <p className="text-slate-300">
                    Queue age:{' '}
                    <span className="font-medium text-white">
                      {(staleDispatch.queue_age_seconds || 0).toFixed(1)}s
                    </span>
                  </p>
                  {staleDispatch.failure_root_cause && (
                    <p className="text-slate-300">
                      Last root cause:{' '}
                      <span className="font-medium text-white">
                        {staleDispatch.failure_root_cause}
                      </span>
                    </p>
                  )}
                </>
              ) : (
                <p className="text-sm text-slate-400">
                  No queued dispatch has exceeded the watchdog SLA.
                </p>
              )}
            </div>
          )}
          </div>

          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">Health Score</h3>
            {latestHealth ? (
              <span
                className={cn(
                  'text-xs font-medium',
                  latestHealth.score >= 80
                    ? 'text-emerald-400'
                    : latestHealth.score >= 50
                      ? 'text-amber-400'
                      : 'text-red-400'
                )}
              >
                {latestHealth.score}/100
              </span>
            ) : (
              <span className="text-xs text-slate-400">No score yet</span>
            )}
          </div>
          {healthEvents.length === 0 ? (
            <p className="text-sm text-slate-400">
              Health history appears after orchestration events start landing.
            </p>
          ) : (
            <div className="max-h-56 space-y-2 overflow-y-auto">
              {healthEvents.slice(-5).reverse().map((event) => (
                <div
                  key={`${event.timestamp}-${event.score}`}
                  className="flex items-center justify-between rounded-md border border-[color:var(--oc-border-soft)] px-3 py-2 text-sm"
                >
                  <div>
                    <p className="font-medium text-slate-200">{event.score}/100</p>
                    <p className="text-xs text-slate-400">{formatDateTime(event.timestamp)}</p>
                  </div>
                  <span
                    className={cn(
                      'text-xs font-medium',
                      typeof event.slope !== 'number' || event.slope === 0
                        ? 'text-slate-400'
                        : event.slope > 0
                          ? 'text-emerald-400'
                          : 'text-red-400'
                    )}
                  >
                    {typeof event.slope === 'number'
                      ? `${event.slope > 0 ? '+' : ''}${event.slope}`
                      : 'stable'}
                  </span>
                </div>
              ))}
            </div>
          )}
          </div>

          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-200">Latest State Diff</h3>
            <span className="text-xs text-slate-400">
              {stateDiff
                ? `Snapshots ${stateDiff.from_checkpoint} → ${stateDiff.to_checkpoint}`
                : 'Unavailable'}
            </span>
          </div>
          {!stateDiff || !stateDiff.delta ? (
            <p className="text-sm text-slate-400">
              Diff data appears after at least two state snapshots exist.
            </p>
          ) : (
            <div className="space-y-2 text-sm">
              <p className="text-slate-300">
                Step index change: <span className="font-medium text-white">{stateDiff.delta.current_step_index?.change ?? 'N/A'}</span>
              </p>
              <p className="text-slate-300">
                Retry budget: <span className="font-medium text-white">{stateDiff.delta.retry_budget_remaining.from}</span> →{' '}
                <span className="font-medium text-white">{stateDiff.delta.retry_budget_remaining.to}</span>
              </p>
              <p className="text-slate-300">
                Files added: <span className="font-medium text-white">{stateDiff.delta.files_touched.added.length}</span>
                {stateDiff.delta.files_touched.added.length > 0
                  ? ` • ${stateDiff.delta.files_touched.added.slice(0, 3).join(', ')}`
                  : ''}
              </p>
              <p className="text-slate-300">
                New validations: <span className="font-medium text-white">{stateDiff.delta.validation_verdicts.new_entries.length}</span>
              </p>
              <p className="text-slate-300">
                Workspace hash changed:{' '}
                <span className={cn('font-medium', stateDiff.delta.workspace_hash_changed ? 'text-amber-300' : 'text-emerald-400')}>
                  {stateDiff.delta.workspace_hash_changed ? 'yes' : 'no'}
                </span>
              </p>
            </div>
          )}
          </div>
      </div>

      {anomalyEvents.length > 0 && (
        <div className="rounded-lg border border-amber-800/70 bg-amber-950/20 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-amber-200">Anomaly Pins</h3>
            <span className="text-xs text-amber-300">{anomalyEvents.length}</span>
          </div>
          <div className="space-y-2">
            {anomalyEvents.slice(-3).reverse().map((event) => (
              <div key={`${event.at}-${event.title}`} className="rounded-md border border-amber-900/60 px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-xs font-medium uppercase text-amber-300">{event.title}</span>
                  <span className="text-xs text-slate-400">{formatDateTime(event.at)}</span>
                </div>
                <p className="mt-1 text-sm text-slate-200">{event.detail}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-medium text-slate-200">Replay Investigation</h3>
          <span className="text-xs text-slate-400">
            {replayInvestigation
              ? replayInvestigation.compatibility_version
              : 'Unavailable'}
          </span>
        </div>
        {!replayInvestigation ? (
          <p className="text-sm text-slate-400">
            Replay reconstruction appears after orchestration evidence exists.
          </p>
        ) : (
          <div className="space-y-3 text-sm">
            <div className="grid gap-2 sm:grid-cols-3">
              <div>
                <p className="text-xs text-slate-500">Integrity</p>
                <p
                  className={cn(
                    'font-medium capitalize',
                    replayInvestigation.integrity.confidence === 'high' &&
                      'text-emerald-400',
                    replayInvestigation.integrity.confidence === 'medium' &&
                      'text-amber-300',
                    replayInvestigation.integrity.confidence !== 'high' &&
                      replayInvestigation.integrity.confidence !== 'medium' &&
                      'text-red-400'
                  )}
                >
                  {replayInvestigation.integrity.confidence}
                </p>
              </div>
              <div>
                <p className="text-xs text-slate-500">Determinism</p>
                <p className="font-medium capitalize text-slate-200">
                  {replayInvestigation.determinism.level}
                </p>
              </div>
              <div>
                <p className="text-xs text-slate-500">Boundary</p>
                <p className="font-medium text-slate-200">
                  {String(replayInvestigation.boundary.mode || 'full')}
                </p>
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              <p className="text-slate-300">
                Phase:{' '}
                <span className="font-medium text-white">
                  {replayInvestigation.state.phase || 'unknown'}
                </span>
              </p>
              <p className="text-slate-300">
                Status:{' '}
                <span className="font-medium text-white">
                  {replayInvestigation.state.status || 'unknown'}
                </span>
              </p>
              <p className="text-slate-300">
                Step:{' '}
                <span className="font-medium text-white">
                  {replayInvestigation.state.current_step_index ?? 0}
                </span>
              </p>
            </div>
            <p className="text-xs text-slate-400">
              Events applied: {replayInvestigation.integrity.event_count_applied} /{' '}
              {replayInvestigation.integrity.event_count_read} • Workspace:{' '}
              {replayInvestigation.workspace_evidence.status}
            </p>
            {replayInvestigation.drift_findings.length > 0 && (
              <div className="space-y-1">
                {replayInvestigation.drift_findings.slice(0, 3).map((finding, idx) => (
                  <p
                    key={`${String(finding.type || 'finding')}-${idx}`}
                    className="rounded-md border border-[color:var(--oc-border-soft)] px-2 py-1 text-xs text-slate-300"
                  >
                    {String(finding.type || 'finding')}: {String(finding.summary || '')}
                  </p>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {visibleMatches.length > 0 && (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-medium text-slate-300">Similar Failed Runs</h3>
            <span className="text-xs text-slate-400">{visibleMatches.length} matches</span>
          </div>
          <div className="space-y-2">
            {visibleMatches.slice(0, 3).map((match) => (
              <div key={match.session_id} className="rounded-md border border-[color:var(--oc-border-soft)] p-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-medium text-slate-200">
                    #{match.session_id} {match.session_name}
                  </p>
                  <span className="text-xs text-primary-300">
                    {(match.similarity_score * 100).toFixed(0)}% similar
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-400">
                  Retries {match.retry_count} • Tool failures {match.tool_failure_count} • Intent gaps {match.intent_gap_count}
                </p>
                {match.shared_tags.length > 0 && (
                  <p className="mt-1 text-xs text-slate-400">
                    Shared: {match.shared_tags.slice(0, 4).join(', ')}
                  </p>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      </div>
    </details>
  );
}

export function SessionTimelinePanel({
  decisionEvents = [],
  formatDateTime,
  timelineSpans = [],
  timelineEvents,
  offTrackMoment = null,
  repairGenealogy = [],
}: SessionTimelinePanelProps) {
  const [showAllDecision, setShowAllDecision] = useState(false);
  const getTimelineImportance = (event: TimelineEvent): TimelineEventImportance => {
    if (event.importance) return event.importance;
    const text = `${event.title} ${event.detail}`.toLowerCase();
    if (
      event.type === 'error' ||
      text.includes('failed') ||
      text.includes('waiting for input') ||
      text.includes('off-track') ||
      text.includes('divergence') ||
      text.includes('intent gap') ||
      text.includes('plan revised') ||
      text.includes('retry entered')
    ) {
      return 'primary';
    }
    if (
      event.type === 'task' ||
      event.type === 'planning' ||
      event.type === 'summarizing' ||
      text.includes('phase started') ||
      text.includes('phase finished')
    ) {
      return 'primary';
    }
    if (
      text.includes('tool invoked') ||
      text.includes('checkpoint saved') ||
      text.includes('health score') ||
      text.includes('evaluator result') ||
      text.includes('reasoning artifact') ||
      text.includes('workspace preserved') ||
      text.includes('workspace restore skipped') ||
      event.type === 'info'
    ) {
      return 'secondary';
    }
    return 'secondary';
  };

  const orderedTimelineEvents = timelineEvents.slice().reverse();
  const primaryTimelineEvents = orderedTimelineEvents
    .filter((event) => getTimelineImportance(event) === 'primary')
    .slice(0, 12);
  const secondaryTimelineEvents = orderedTimelineEvents
    .filter((event) => getTimelineImportance(event) === 'secondary')
    .slice(0, 16);
  const renderTimelineEvent = (
    event: TimelineEvent,
    density: 'normal' | 'compact' = 'normal'
  ) => (
    <div
      key={event.id}
      className={cn(
        'min-w-0 overflow-hidden rounded-md border border-[color:var(--oc-border-soft)]',
        density === 'compact' ? 'p-2' : 'p-3'
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span
          className={cn(
            'break-words text-xs font-medium uppercase',
            event.type === 'error' && 'text-red-400',
            event.type === 'planning' && 'text-violet-400',
            event.type === 'executing' && 'text-blue-400',
            event.type === 'debugging' && 'text-amber-400',
            event.type === 'revising_plan' && 'text-fuchsia-400',
            event.type === 'summarizing' && 'text-teal-400',
            event.type === 'checkpoint' && 'text-emerald-400',
            event.type === 'validation' && 'text-lime-400',
            event.type === 'repair' && 'text-orange-400',
            event.type === 'task' && 'text-primary-300',
            event.type === 'status' && 'text-cyan-400',
            event.type === 'info' && 'text-slate-300'
          )}
        >
          {event.title}
        </span>
        <span className="text-xs text-slate-400">
          {formatDateTime(event.at)}
        </span>
      </div>
      <p
        className={cn(
          'mt-1 break-words text-slate-200',
          density === 'compact' && 'text-xs text-slate-300'
        )}
      >
        {event.detail}
      </p>
    </div>
  );

  return (
    <div className="min-w-0 space-y-4 overflow-x-hidden">
      {(offTrackMoment || repairGenealogy.length > 0) && (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <div className="min-w-0 overflow-hidden rounded-lg border border-amber-500/45 border-l-4 border-l-amber-400 bg-[color:var(--oc-surface)] p-4 shadow-sm shadow-black/20">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-slate-100">Where It Went Off Track</h3>
              {offTrackMoment && (
                <span className="rounded-sm border border-amber-400/50 bg-amber-400/15 px-1.5 py-0.5 text-xs uppercase text-amber-200">
                  {offTrackMoment.trigger.replace(/_/g, ' ')}
                </span>
              )}
            </div>
            {!offTrackMoment ? (
              <p className="text-sm text-slate-400">
                No off-track point detected from health, divergence, or repair acceptance signals.
              </p>
            ) : (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <span>{formatDateTime(offTrackMoment.timestamp)}</span>
                  <span className="rounded-sm border border-amber-400/35 bg-amber-400/10 px-1.5 py-0.5 uppercase text-amber-200">
                    {offTrackMoment.phase}
                  </span>
                  {typeof offTrackMoment.health_score === 'number' && (
                    <span className="text-amber-200">
                      Health {offTrackMoment.health_score}/100
                    </span>
                  )}
                </div>
                {(() => {
                  const bullets = splitReasonIntoBullets(offTrackMoment.reason);
                  return bullets.length >= 2 ? (
                    <ul className="space-y-1.5 text-sm text-slate-100">
                      {bullets.map((b, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400" />
                          <span>{b}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="break-words text-sm text-slate-100">{offTrackMoment.reason}</p>
                  );
                })()}
                <p className="break-words text-xs text-slate-400">
                  {offTrackMoment.event_type}
                  {offTrackMoment.event_id ? ` • ${offTrackMoment.event_id}` : ''}
                </p>
              </div>
            )}
          </div>

          <div className="min-w-0 overflow-hidden rounded-lg border border-orange-500/40 border-l-4 border-l-orange-400 bg-[color:var(--oc-surface)] p-4 shadow-sm shadow-black/20">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-100">Repair History</h3>
              <span className="text-xs text-slate-400">{repairGenealogy.length} events</span>
            </div>
            {repairGenealogy.length === 0 ? (
              <p className="text-sm text-slate-400">
                No repair attempts detected in the orchestration event journal.
              </p>
            ) : (
              <div className="max-h-72 space-y-2 overflow-y-auto">
                {repairGenealogy.map((node, index) => (
                  <div key={node.id} className="relative min-w-0 pl-5">
                    {index < repairGenealogy.length - 1 && (
                      <span className="absolute left-1.5 top-7 h-[calc(100%-0.25rem)] w-px bg-slate-600" />
                    )}
                    <span
                      className={cn(
                        'absolute left-0 top-3 h-3 w-3 rounded-full border',
                        node.status === 'original' &&
                          'border-slate-500 bg-[color:var(--oc-surface-raised)]',
                        node.status === 'repair' && 'border-orange-400 bg-orange-500/40',
                        node.status === 'accepted' && 'border-emerald-400 bg-emerald-500/40',
                        node.status === 'rejected' && 'border-red-400 bg-red-500/40',
                        node.status === 'abandoned' && 'border-amber-400 bg-amber-500/40'
                      )}
                    />
                    <div className="min-w-0 overflow-hidden rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className={cn(
                              'text-xs font-medium uppercase',
                              node.status === 'original' && 'text-slate-300',
                              node.status === 'repair' && 'text-orange-300',
                              node.status === 'accepted' && 'text-emerald-300',
                              node.status === 'rejected' && 'text-red-300',
                              node.status === 'abandoned' && 'text-amber-300'
                            )}
                          >
                            {node.title}
                          </span>
                          <span className="rounded-sm border border-orange-400/30 bg-orange-400/10 px-1.5 py-0.5 text-xs uppercase text-orange-200">
                            {node.status}
                          </span>
                        </div>
                        <span className="text-xs text-slate-400">
                          {formatDateTime(node.timestamp)}
                        </span>
                      </div>
                      {(node.validator || node.reason) && (
                        <p className="mt-1 break-words text-xs text-slate-300">
                          {[node.validator, node.reason].filter(Boolean).join(' | ')}
                        </p>
                      )}
                      {node.details && Object.keys(node.details).length > 0 && (
                        <details className="mt-2">
                          <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">
                            Raw event details
                          </summary>
                          <pre className="mt-2 max-h-36 overflow-auto rounded bg-[color:var(--oc-surface-deep)] p-2 text-xs text-slate-400">
                            {JSON.stringify(node.details, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="min-w-0 overflow-hidden rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">Decision Timeline</h3>
          <span className="text-xs text-slate-400">{decisionEvents.length} events</span>
        </div>
        {(() => {
          const allDecision = decisionEvents.slice().reverse();
          const visibleDecision = showAllDecision ? allDecision : allDecision.slice(0, 6);
          return (
            <div className="space-y-2 text-sm">
              {allDecision.length === 0 ? (
                <p className="text-slate-500">No decision timeline events yet.</p>
              ) : (
                <>
                  {visibleDecision.map((event) => {
                    const diagnosticBadges = getDiagnosticBadges(event.details);
                    const diagnosticReasons = getDiagnosticReasons(event.details);
                    const operatorEvidence = getDecisionEventEvidence(event);

                    return (
                      <div key={event.id} className="min-w-0 overflow-hidden rounded-md border border-[color:var(--oc-border-soft)] p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="flex flex-wrap items-center gap-2">
                            <span
                              className={cn(
                                'text-xs font-medium uppercase',
                                event.severity === 'error' && 'text-red-400',
                                event.severity === 'warning' && 'text-amber-400',
                                event.severity !== 'error' &&
                                  event.severity !== 'warning' &&
                                  'text-primary-300'
                              )}
                            >
                              {event.title}
                            </span>
                            <span className="rounded-sm border border-[color:var(--oc-border-soft)] px-1.5 py-0.5 text-xs uppercase text-slate-400">
                              {event.phase}
                            </span>
                            {event.task_id !== null && event.task_id !== undefined && (
                              <span className="text-xs text-slate-500">
                                Task {event.task_id}
                              </span>
                            )}
                          </div>
                          <span className="text-xs text-slate-400">
                            {formatDateTime(event.timestamp)}
                          </span>
                        </div>
                        <p className="mt-1 break-words text-slate-200">{event.summary}</p>
                        {operatorEvidence && (
                          <div className="mt-2 grid gap-2 rounded-md border border-orange-400/25 bg-orange-400/10 p-2 text-xs sm:grid-cols-2">
                            <p className="break-words text-slate-300">
                              <span className="text-slate-500">Boundary:</span>{' '}
                              <span className="font-medium text-slate-100">{operatorEvidence.boundary}</span>
                            </p>
                            <p className="break-words text-slate-300">
                              <span className="text-slate-500">Outcome:</span>{' '}
                              <span className="font-medium text-slate-100">{operatorEvidence.outcome}</span>
                            </p>
                            {operatorEvidence.operation && (
                              <p className="break-words font-mono text-slate-200">
                                {operatorEvidence.operation}
                              </p>
                            )}
                            {operatorEvidence.targetPath && (
                              <p className="break-words font-mono text-slate-200">
                                {operatorEvidence.targetPath}
                              </p>
                            )}
                            <p className="break-words text-orange-200 sm:col-span-2">
                              {operatorEvidence.nextAction}
                            </p>
                            {operatorEvidence.operations.length > 0 && (
                              <div className="flex flex-wrap gap-1.5 sm:col-span-2">
                                {operatorEvidence.operations.map((operation, index) => (
                                  <span
                                    key={`${operation.op}-${operation.path || 'no-path'}-${index}`}
                                    className="rounded-sm border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-1.5 py-0.5 font-mono text-xs text-slate-200"
                                  >
                                    {operation.op}
                                    {operation.path ? ` ${operation.path}` : ''}
                                    {operation.outcome ? ` (${humanizeEvidenceValue(operation.outcome)})` : ''}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                        {diagnosticBadges.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-1.5">
                            {diagnosticBadges.map((badge) => (
                              <span
                                key={badge}
                                className="rounded-sm border border-red-600/60 bg-red-950/40 px-1.5 py-0.5 text-xs text-red-300"
                              >
                                {badge}
                              </span>
                            ))}
                          </div>
                        )}
                        {diagnosticReasons.length > 0 && (
                          <div className="mt-2 space-y-1">
                            {diagnosticReasons.map((reason) => (
                              <p key={reason} className="break-words text-xs text-slate-400">
                                {reason}
                              </p>
                            ))}
                          </div>
                        )}
                        {(event.knowledge_usage_ids.length > 0 || event.intervention_id) && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            {event.knowledge_usage_ids.length > 0 && (
                              <span className="rounded-sm border border-violet-900/70 bg-violet-950/30 px-1.5 py-0.5 text-xs text-violet-300">
                                Knowledge phase context
                              </span>
                            )}
                            {event.intervention_id && (
                              <span className="rounded-sm border border-amber-900/70 bg-amber-950/30 px-1.5 py-0.5 text-xs text-amber-300">
                                Intervention #{event.intervention_id}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {allDecision.length > 6 && (
                    <button
                      onClick={() => setShowAllDecision((v) => !v)}
                      className="w-full rounded-md border border-[color:var(--oc-border-soft)] py-2 text-xs text-slate-400 transition-colors hover:border-[color:var(--oc-border)] hover:text-slate-200"
                    >
                      {showAllDecision
                        ? 'Show fewer'
                        : `Show all ${allDecision.length} events`}
                    </button>
                  )}
                </>
              )}
            </div>
          );
        })()}
      </div>

      <div className="min-w-0 overflow-hidden rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-white">Execution Timeline</h3>
            <p className="mt-0.5 text-xs text-slate-400">
              Major milestones first, work details second.
            </p>
          </div>
          <span className="text-xs text-slate-400">{timelineEvents.length} events</span>
        </div>
        <div className="space-y-4 text-sm">
          {timelineEvents.length === 0 ? (
            <p className="text-slate-500">
              No timeline events yet. Start/execute a task to see progress milestones.
            </p>
          ) : (
            <>
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <h4 className="text-xs font-semibold uppercase text-slate-300">Milestones</h4>
                  <span className="text-xs text-slate-500">{primaryTimelineEvents.length}</span>
                </div>
                <div className="max-h-72 space-y-2 overflow-y-auto">
                  {primaryTimelineEvents.length === 0 ? (
                    <p className="text-xs text-slate-500">No major milestone events yet.</p>
                  ) : (
                    primaryTimelineEvents.map((event) => renderTimelineEvent(event))
                  )}
                </div>
              </div>

              <details className="group">
                <summary className="mb-2 flex cursor-pointer list-none items-center justify-between">
                  <h4 className="text-xs font-semibold uppercase text-slate-400 group-open:text-slate-300">Work Details</h4>
                  <span className="text-xs text-slate-500">{secondaryTimelineEvents.length}</span>
                </summary>
                <div className="max-h-64 space-y-2 overflow-y-auto">
                  {secondaryTimelineEvents.length === 0 ? (
                    <p className="text-xs text-slate-500">No secondary work details yet.</p>
                  ) : (
                    secondaryTimelineEvents.map((event) =>
                      renderTimelineEvent(event, 'compact')
                    )
                  )}
                </div>
              </details>
            </>
          )}
        </div>
      </div>

      <details className="min-w-0 overflow-hidden rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <summary className="cursor-pointer text-sm font-semibold text-white hover:text-slate-200">
          Causal Spans <span className="text-xs font-normal text-slate-400">({timelineSpans.length})</span>
        </summary>
        <div className="mt-3">
          {timelineSpans.length === 0 ? (
            <p className="text-sm text-slate-400">
              Span grouping appears when parent-linked orchestration events are present.
            </p>
          ) : (
            <div className="max-h-56 space-y-2 overflow-y-auto">
              {timelineSpans.slice().reverse().map((span) => (
                <div key={span.id} className="min-w-0 overflow-hidden rounded-md border border-[color:var(--oc-border-soft)] p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          'text-xs font-medium uppercase',
                          span.lane === 'reasoning' && 'text-violet-400',
                          span.lane === 'tool' && 'text-primary-300',
                          span.lane === 'workspace' && 'text-emerald-400',
                          span.lane === 'validation' && 'text-lime-400',
                          span.lane === 'system' && 'text-cyan-400'
                        )}
                      >
                        {span.lane}
                      </span>
                      <span
                        className={cn(
                          'text-xs font-medium',
                          span.status === 'healthy' && 'text-emerald-400',
                          span.status === 'warning' && 'text-amber-400',
                          span.status === 'error' && 'text-red-400'
                        )}
                      >
                        {span.status}
                      </span>
                    </div>
                    <span className="text-xs text-slate-400">{formatDateTime(span.started_at)}</span>
                  </div>
                  <p className="mt-2 break-words text-sm font-medium text-slate-200">{span.title}</p>
                  <p className="mt-1 break-words text-sm text-slate-300">{span.summary}</p>
                  <p className="mt-1 text-xs text-slate-400">{span.event_count} linked event{span.event_count === 1 ? '' : 's'}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </details>
    </div>
  );
}

interface SessionTasksPanelProps {
  actionButtons: ReactNode;
  formatDateTime: (value?: string | null) => string;
  executionAction?: string | null;
  onExecuteTask?: (task: Task) => void;
  session: Session;
  tasks: Task[];
}

export function SessionTasksPanel({
  actionButtons,
  formatDateTime,
  executionAction,
  onExecuteTask,
  session,
  tasks,
}: SessionTasksPanelProps) {
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <p className="text-sm font-medium text-slate-200">Runs in this session</p>
        <p className="mt-1 text-sm text-slate-400">
          These runs belong to this workflow session. Running one again adds a new attempt here instead of creating a separate session.
        </p>
      </div>

      {actionButtons && session.status !== 'running' && (
        <div className="mb-4 rounded-lg border border-blue-700/50 bg-blue-900/20 p-4">
          <p className="mb-2 text-sm text-blue-400">
            Session is not running. Start the session to execute tasks automatically or enter manual mode and run tasks one by one.
          </p>
        </div>
      )}

      {tasks.length === 0 ? (
        <div className="py-12 text-center">
          <TerminalIcon className="mx-auto mb-4 h-12 w-12 text-slate-500" />
          <p className="text-slate-400">No tasks yet</p>
          {actionButtons && (
            <p className="mt-2 text-sm text-slate-400">
              Start the session to automatically execute tasks from your project
            </p>
          )}
        </div>
      ) : (
        tasks.map((task) => {
          const runDisplay = getRunStateDisplay(deriveRunStateFromTask(task));
          return (
          <div
            key={task.id}
            className="rounded-xl border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4 transition-colors hover:border-[color:var(--oc-border)]"
          >
            <div className="mb-2 flex items-start justify-between">
              <div>
                <h3 className="font-semibold text-white">{task.title}</h3>
                <p className="mt-1 text-xs text-slate-400">
                  Order: {task.plan_position ?? 'manual'} • Priority: {task.priority ?? 0}
                </p>
                {task.workspace_status && (
                  <p className="mt-1 text-xs capitalize text-slate-500">
                    Diagnostics: {task.workspace_status.replace(/_/g, ' ')}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <span
                  className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${runDisplay.badgeClass}`}
                  title={runDisplay.description}
                >
                  {runDisplay.label}
                </span>
                {onExecuteTask && (
                  session.execution_mode === 'manual' ||
                  task.status === 'pending' ||
                  task.status === 'failed' ||
                  task.status === 'cancelled' ||
                  task.status === 'done'
                ) && (
                  <button
                    onClick={() => onExecuteTask(task)}
                    disabled={task.status === 'running' || Boolean(executionAction)}
                    className="rounded-lg border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-[color:var(--oc-action-hover)] disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {executionAction === 'run-task'
                      ? 'Queueing...'
                      : task.status === 'done'
                      ? 'Run again in workflow session'
                      : 'Run in workflow session'}
                  </button>
                )}
              </div>
            </div>
            {task.description && (
              <p className="mt-1 text-sm text-slate-400">{task.description}</p>
            )}
            {task.error_message && (
              <div className="mt-2 rounded-lg border border-red-700/50 bg-red-900/20 p-2">
                <p className="flex items-center gap-2 text-sm text-red-400">
                  <XCircle className="h-4 w-4" />
                  Error: {task.error_message}
                </p>
              </div>
            )}
            <div className="mt-3 flex items-center gap-4 text-xs text-slate-400">
              {task.created_at && <span>Created: {formatDateTime(task.created_at)}</span>}
              {task.started_at && <span>Started: {formatDateTime(task.started_at)}</span>}
              {task.completed_at && (
                <span className="text-emerald-400">
                  Completed: {formatDateTime(task.completed_at)}
                </span>
              )}
            </div>
          </div>
          );
        })
      )}
    </div>
  );
}

interface SessionSettingsPanelProps {
  checkpoints?: Checkpoint[];
  checkpointInspection?: CheckpointInspection | null;
  formatDateTime: (value?: string | null) => string;
  onInspectCheckpoint?: (checkpointName: string) => void;
  onModeChange?: (mode: 'automatic' | 'manual') => void;
  onReplayCheckpoint?: (checkpointName: string) => void;
  session: Session;
}

export function SessionSettingsPanel({
  checkpoints = [],
  checkpointInspection,
  formatDateTime,
  onInspectCheckpoint,
  onModeChange,
  onReplayCheckpoint,
  session,
}: SessionSettingsPanelProps) {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <p className="mb-2 text-sm text-slate-400">Execution Mode</p>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onModeChange?.('automatic')}
            className={cn(
              'rounded-lg px-3 py-2 text-sm transition-colors',
              session.execution_mode === 'automatic'
                ? 'border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white'
                : 'border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
            )}
          >
            Automatic
          </button>
          <button
            onClick={() => onModeChange?.('manual')}
            className={cn(
              'rounded-lg px-3 py-2 text-sm transition-colors',
              session.execution_mode === 'manual'
                ? 'border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white'
                : 'border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
            )}
          >
            Manual
          </button>
        </div>
      </div>
      <div className="rounded-xl border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <p className="mb-3 text-sm text-slate-400">Session Metadata</p>
        <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <p className="text-xs text-slate-500">Session ID</p>
            <p className="font-mono text-white">{session.id}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Project ID</p>
            <p className="font-mono text-white">{session.project_id}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Created</p>
            <p className="text-white">{formatDateTime(session.created_at)}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Started</p>
            <p className="text-white">{session.started_at ? formatDateTime(session.started_at) : 'N/A'}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Stopped</p>
            <p className="text-white">{session.stopped_at ? formatDateTime(session.stopped_at) : 'N/A'}</p>
          </div>
        </div>
      </div>
      <div className="rounded-xl border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] p-4">
        <div className="mb-3 flex items-center justify-between">
          <p className="text-sm text-slate-400">Checkpoint Inspector</p>
          <span className="text-xs text-slate-400">{checkpoints.length} stored</span>
        </div>
        {checkpoints.length === 0 ? (
          <p className="text-sm text-slate-400">No checkpoints recorded for this session yet.</p>
        ) : (
          <div className="max-h-56 space-y-2 overflow-y-auto">
            {checkpoints.map((checkpoint) => (
              <div
                key={checkpoint.name}
                className="rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-raised)] p-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm font-medium text-white">
                      {checkpoint.name}
                      {checkpoint.recommended ? (
                        <span className="ml-2 text-xs text-emerald-400">Recommended</span>
                      ) : null}
                      {checkpoint.resumable === false ? (
                        <span className="ml-2 text-xs text-amber-400">Metadata Only</span>
                      ) : null}
                    </p>
                    <p className="text-xs text-slate-400">
                      {formatDateTime(checkpoint.created_at)} • Step {checkpoint.step_index ?? 0} • Completed {checkpoint.completed_steps ?? 0}
                    </p>
                    {checkpoint.restore_fidelity ? (
                      <p
                        className={cn(
                          'mt-1 text-xs',
                          checkpoint.restore_fidelity.status === 'high'
                            ? 'text-emerald-300'
                            : checkpoint.restore_fidelity.status === 'medium'
                              ? 'text-amber-300'
                              : 'text-red-300'
                        )}
                      >
                        Replay fidelity: {checkpoint.restore_fidelity.status} ({checkpoint.restore_fidelity.score}/100)
                      </p>
                    ) : null}
                    {checkpoint.resume_reason ? (
                      <p
                        className={cn(
                          'mt-1 text-xs',
                          checkpoint.resumable === false ? 'text-amber-300' : 'text-slate-400'
                        )}
                      >
                        {checkpoint.resume_reason}
                      </p>
                    ) : null}
                  </div>
                  <div className="flex gap-2">
                    <button
                      onClick={() => onInspectCheckpoint?.(checkpoint.name)}
                      className="rounded-lg rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white"
                    >
                      Inspect
                    </button>
                    <button
                      onClick={() => onReplayCheckpoint?.(checkpoint.name)}
                      disabled={checkpoint.resumable === false}
                      title={
                        checkpoint.resumable === false
                          ? checkpoint.resume_reason || 'This checkpoint is missing replay state'
                          : undefined
                      }
                      className={cn(
                        'rounded-lg px-3 py-1.5 text-xs text-white transition-colors',
                        checkpoint.resumable === false
                          ? 'cursor-not-allowed bg-[color:var(--oc-surface-raised)] text-slate-400'
                          : 'bg-emerald-700 hover:bg-emerald-600'
                      )}
                    >
                      Replay
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        {checkpointInspection && (
          <div className="mt-4 rounded-lg border border-blue-800/60 bg-blue-950/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium text-blue-200">
                {checkpointInspection.checkpoint_name}
              </p>
              <span className="text-xs text-blue-400">
                {checkpointInspection.summary.status || 'unknown'}
              </span>
            </div>
            {checkpointInspection.resume_readiness ? (
              <p
                className={cn(
                  'mt-2 text-xs',
                  checkpointInspection.resume_readiness.resumable
                    ? 'text-blue-300'
                    : 'text-amber-300'
                )}
              >
                {checkpointInspection.resume_readiness.resume_reason}
              </p>
            ) : null}
            <p className="mt-2 text-xs text-slate-300">
              Plan steps {checkpointInspection.summary.plan_step_count} • Completed {checkpointInspection.summary.completed_step_count} • Repairs {checkpointInspection.summary.completion_repair_attempts}
            </p>
            {checkpointInspection.reasoning_artifact ? (
              <div className="mt-3 rounded-lg border border-primary-700/60 bg-primary-500/10 p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-primary-300">
                  Reasoning Artifact
                </p>
                <p className="mt-1 text-sm text-primary-100">
                  {checkpointInspection.reasoning_artifact.intent}
                </p>
                <p className="mt-2 text-xs text-primary-200/90">
                  Workspace facts:{' '}
                  {checkpointInspection.reasoning_artifact.workspace_facts
                    .slice(0, 3)
                    .join(' • ')}
                </p>
                <p className="mt-1 text-xs text-primary-200/90">
                  Planned actions:{' '}
                  {checkpointInspection.reasoning_artifact.planned_actions
                    .slice(0, 3)
                    .join(' • ')}
                </p>
                <p className="mt-1 text-xs text-primary-200/90">
                  Verification:{' '}
                  {checkpointInspection.reasoning_artifact.verification_plan
                    .slice(0, 2)
                    .join(' • ')}
                </p>
              </div>
            ) : null}
            {checkpointInspection.restore_fidelity ? (
              <p
                className={cn(
                  'mt-2 text-xs',
                  checkpointInspection.restore_fidelity.status === 'high'
                    ? 'text-emerald-300'
                    : checkpointInspection.restore_fidelity.status === 'medium'
                      ? 'text-amber-300'
                      : 'text-red-300'
                )}
              >
                Replay fidelity {checkpointInspection.restore_fidelity.status} ({checkpointInspection.restore_fidelity.score}/100): {checkpointInspection.restore_fidelity.summary}
              </p>
            ) : null}
            {checkpointInspection.latest_failure ? (
              <div className="mt-3 rounded-lg border border-amber-800/60 bg-amber-950/30 p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-amber-300">
                  Latest Failure
                </p>
                <p className="mt-1 text-sm text-amber-100">
                  {checkpointInspection.latest_failure.root_cause || 'unknown'} {checkpointInspection.latest_failure.task_title ? `• ${checkpointInspection.latest_failure.task_title}` : ''}
                </p>
                <p className="mt-1 text-xs text-amber-200/80">
                  {checkpointInspection.latest_failure.phase || 'execution'} {typeof checkpointInspection.latest_failure.step_index === 'number' ? `• step ${checkpointInspection.latest_failure.step_index + 1}` : ''}
                  {checkpointInspection.latest_failure.timestamp ? ` • ${formatDateTime(checkpointInspection.latest_failure.timestamp)}` : ''}
                </p>
                {checkpointInspection.latest_failure.stderr_preview && (
                  <p className="mt-2 text-xs text-amber-100/90">
                    {checkpointInspection.latest_failure.stderr_preview}
                  </p>
                )}
              </div>
            ) : null}
            {checkpointInspection.failure_history_preview &&
            checkpointInspection.failure_history_preview.length > 1 ? (
              <p className="mt-2 text-xs text-slate-300">
                Recent failure roots:{' '}
                {checkpointInspection.failure_history_preview
                  .slice(0, 3)
                  .map((failure) => failure.root_cause || 'unknown')
                  .join(' • ')}
              </p>
            ) : null}
            {checkpointInspection.latest_validation && (
              <pre className="mt-3 overflow-x-auto rounded-lg bg-[color:var(--oc-surface-deep)] p-3 text-xs text-slate-300">
                {JSON.stringify(checkpointInspection.latest_validation, null, 2)}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

interface HumanInterventionPanelProps {
  interventions: InterventionRequest[];
  onApprove: (id: number) => Promise<void>;
  onDeny: (id: number, reason?: string) => Promise<void>;
  onReply: (id: number, reply: string) => Promise<void>;
  variant?: 'default' | 'chat';
}

export function HumanInterventionPanel({
  interventions,
  onApprove,
  onDeny,
  onReply,
  variant = 'default',
}: HumanInterventionPanelProps) {
  const [replyText, setReplyText] = useState<Record<number, string>>({});
  const [denyReason, setDenyReason] = useState<Record<number, string>>({});
  const [submitting, setSubmitting] = useState<Record<number, boolean>>({});

  const pending = interventions.filter((i) => i.status === 'pending');

  if (pending.length === 0) {
    return (
      <div className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4">
        <p className="flex items-center gap-2 text-sm text-amber-300">
          <MessageCircle className="h-4 w-4" />
          Session paused waiting for operator. No pending interventions found yet — check back shortly.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {pending.map((intervention) => {
        let snapshotData: Record<string, string> = {};
        try { snapshotData = JSON.parse(intervention.context_snapshot || '{}'); } catch { /* ignore */ }
        const isHumanInitiated = intervention.initiated_by === 'human';
        const aiResponse: string | null = snapshotData.ai_response || null;

        return (
        <div
          key={intervention.id}
          className="rounded-lg border border-amber-700/50 bg-amber-900/20 p-4"
        >
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <MessageCircle className="h-5 w-5 text-amber-400" />
              <span className="font-semibold text-amber-200">
                {isHumanInitiated ? 'Your Question to AI' : 'Operator Input Required'}
              </span>
              <span className="rounded bg-amber-800/50 px-2 py-0.5 text-xs uppercase text-amber-300">
                {intervention.intervention_type}
              </span>
            </div>
            {intervention.expires_at && (
              <span className="text-xs text-slate-400">
                Expires: {new Date(intervention.expires_at).toLocaleString()}
              </span>
            )}
          </div>

          {variant === 'chat' && !isHumanInitiated ? (
            <div className="mb-4 space-y-3">
              <div className="flex justify-start">
                <div className="max-w-[85%] rounded-2xl rounded-bl-md border border-amber-600/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-50 shadow-sm">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-amber-300">
                    OpenClaw
                  </p>
                  <p className="whitespace-pre-wrap leading-6">{intervention.prompt}</p>
                </div>
              </div>
              <div className="flex justify-end">
                <div className="max-w-[78%] rounded-2xl rounded-br-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-4 py-3 text-sm text-slate-200">
                  <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                    You
                  </p>
                  <p className="text-slate-400">
                    Reply below to approve, deny, or guide the next step.
                  </p>
                </div>
              </div>
            </div>
          ) : (
            <p className="mb-3 whitespace-pre-wrap text-sm text-slate-200">{intervention.prompt}</p>
          )}

          {isHumanInitiated && (
            <div className="mb-3 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-3">
              {aiResponse ? (
                <>
                  <p className="mb-1 text-xs font-medium text-emerald-400">AI Response</p>
                  <p className="whitespace-pre-wrap text-sm text-slate-200">{aiResponse}</p>
                </>
              ) : (
                <p className="flex items-center gap-2 text-sm text-slate-400">
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-amber-400 border-t-transparent" />
                  AI is processing your question…
                </p>
              )}
            </div>
          )}

          {!isHumanInitiated && intervention.context_snapshot && (
            <details className="mb-3">
              <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-300">
                Context snapshot
              </summary>
              <pre className="mt-2 overflow-x-auto rounded bg-[color:var(--oc-surface-deep)] p-3 text-xs text-slate-300">
                {intervention.context_snapshot}
              </pre>
            </details>
          )}

          {!isHumanInitiated && intervention.intervention_type === 'approval' ? (
            <div className="space-y-3">
              {variant === 'chat' && (
                <p className="text-xs text-amber-200/80">
                  Approve to let OpenClaw continue this action. Deny to stop this path and send correction context back.
                </p>
              )}
              <div className="flex items-start gap-3">
                <input
                  type="text"
                  placeholder="Why deny? Optional note back to OpenClaw"
                  value={denyReason[intervention.id] || ''}
                  onChange={(e) =>
                    setDenyReason((prev) => ({ ...prev, [intervention.id]: e.target.value }))
                  }
                  className="flex-1 rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-amber-500 focus:outline-none"
                />
                <button
                  disabled={submitting[intervention.id]}
                  onClick={async () => {
                    setSubmitting((prev) => ({ ...prev, [intervention.id]: true }));
                    try {
                      await onApprove(intervention.id);
                    } finally {
                      setSubmitting((prev) => ({ ...prev, [intervention.id]: false }));
                    }
                  }}
                  className="flex items-center gap-1.5 rounded-lg bg-emerald-600 px-4 py-2 text-sm text-white transition-colors hover:bg-emerald-700 disabled:opacity-50"
                >
                  Approve
                </button>
                <button
                  disabled={submitting[intervention.id]}
                  onClick={async () => {
                    setSubmitting((prev) => ({ ...prev, [intervention.id]: true }));
                    try {
                      await onDeny(intervention.id, denyReason[intervention.id]);
                    } finally {
                      setSubmitting((prev) => ({ ...prev, [intervention.id]: false }));
                    }
                  }}
                  className="flex items-center gap-1.5 rounded-lg bg-red-600 px-4 py-2 text-sm text-white transition-colors hover:bg-red-700 disabled:opacity-50"
                >
                  Deny
                </button>
              </div>
            </div>
          ) : !isHumanInitiated ? (
            <div className="space-y-2">
              {variant === 'chat' && (
                <p className="text-xs text-slate-400">
                  Your reply is added to the session context and OpenClaw resumes from the paused step.
                </p>
              )}
              <textarea
                rows={3}
                placeholder={`Your ${intervention.intervention_type} reply...`}
                value={replyText[intervention.id] || ''}
                onChange={(e) =>
                  setReplyText((prev) => ({ ...prev, [intervention.id]: e.target.value }))
                }
                className="w-full resize-none rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface)] px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-amber-500 focus:outline-none"
              />
              <button
                disabled={
                  submitting[intervention.id] || !(replyText[intervention.id] || '').trim()
                }
                onClick={async () => {
                  const reply = (replyText[intervention.id] || '').trim();
                  if (!reply) return;
                  setSubmitting((prev) => ({ ...prev, [intervention.id]: true }));
                  try {
                    await onReply(intervention.id, reply);
                    setReplyText((prev) => ({ ...prev, [intervention.id]: '' }));
                  } finally {
                    setSubmitting((prev) => ({ ...prev, [intervention.id]: false }));
                  }
                }}
                className="rounded-lg bg-amber-600 px-4 py-2 text-sm text-white transition-colors hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Submit Reply
              </button>
            </div>
          ) : null}
        </div>
        );
      })}
    </div>
  );
}

interface KnowledgeUsagePanelProps {
  phases: Record<string, KnowledgeUsageEntry[]>;
}

export function KnowledgeUsagePanel({ phases }: KnowledgeUsagePanelProps) {
  const phaseKeys = Object.keys(phases);
  if (phaseKeys.length === 0) return null;

  return (
    <details className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
      <summary className="cursor-pointer text-sm font-semibold text-slate-200 hover:text-white">
        Knowledge References Used <span className="text-xs font-normal text-slate-400">({phaseKeys.length} phases)</span>
      </summary>
      <div className="mt-4 space-y-4">
        {phaseKeys.map((phase) => (
          <div key={phase}>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-400 capitalize">
              {phase}
            </p>
            <div className="space-y-2">
              {phases[phase].map((entry, i) => (
                <div
                  key={`${entry.knowledge_item_id}-${entry.retrieval_reason}-${entry.used_in_prompt}-${i}`}
                  className="rounded-md border border-[color:var(--oc-border-soft)] px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium text-slate-200">{entry.title}</p>
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-xs text-slate-400">
                        {(entry.confidence_max * 100).toFixed(0)}%
                      </span>
                      <span
                        className={cn(
                          'text-xs font-medium',
                          entry.used_in_prompt ? 'text-emerald-400' : 'text-slate-500'
                        )}
                      >
                        {entry.used_in_prompt ? 'injected' : 'retrieved'}
                      </span>
                      {entry.usage_count > 1 ? (
                        <span className="text-xs text-slate-400">
                          used {entry.usage_count} times
                        </span>
                      ) : null}
                    </div>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {entry.knowledge_type} • {entry.retrieval_reason}
                  </p>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

interface FailureSummaryPanelProps {
  summary: ExecutionFailureSummary | null;
  loading: boolean;
  onFeedbackSubmit: (feedback: string) => Promise<void>;
  onOpenProjectArchitect?: () => void;
  onReplan: () => Promise<void>;
}

export function FailureSummaryPanel({
  summary,
  loading,
  onFeedbackSubmit,
  onOpenProjectArchitect,
  onReplan,
}: FailureSummaryPanelProps) {
  const [feedback, setFeedback] = useState('');
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [replanning, setReplanning] = useState(false);

  if (loading) {
    return (
      <div className="rounded-lg border border-orange-500/40 border-l-4 border-l-orange-400 bg-[color:var(--oc-surface)] p-4 shadow-sm shadow-black/20">
        <p className="flex items-center gap-2 text-sm text-slate-200">
          <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-orange-300 border-t-transparent" />
          Generating failure summary…
        </p>
      </div>
    );
  }

  if (!summary) return null;

  const replanStatus = summary.replan_planning_session_status || null;
  const hasPriorReplan = summary.replan_planning_session_id !== null;
  const replanStillOwnsFlow =
    hasPriorReplan &&
    (!replanStatus || ['active', 'waiting_for_input', 'completed'].includes(replanStatus));
  const canStartReplan =
    !hasPriorReplan || ['failed', 'cancelled', 'canceled'].includes(replanStatus || '');
  const diagnosticBadges = getDiagnosticBadges(summary.diagnostics);
  const diagnosticReasons = getDiagnosticReasons(summary.diagnostics);
  const operatorEvidence = getOperatorEvidence(summary, canStartReplan, replanStillOwnsFlow);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-orange-500/40 border-l-4 border-l-orange-400 bg-[color:var(--oc-surface)] p-4 shadow-sm shadow-black/20">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-orange-300" />
              <span className="font-semibold text-slate-100">Recovery needed</span>
              {hasPriorReplan && (
                <span className="rounded bg-primary-800/50 px-2 py-0.5 text-xs text-primary-300">
                  Replan {replanStatus ? replanStatus.replace(/_/g, ' ') : 'started'}
                </span>
              )}
            </div>
            <p className="text-sm text-slate-300">
              This session ended before all work completed. Review the summary only if
              you need details, then send recovery guidance to Project Architect.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setDetailsOpen((value) => !value)}
            className="flex items-center gap-1.5 rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-xs text-slate-200 transition-colors hover:border-orange-400/60 hover:text-white"
          >
            {detailsOpen ? 'Hide details' : 'Show details'}
            <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', detailsOpen && 'rotate-180')} />
          </button>
        </div>
        {detailsOpen && (
          <div className="mt-4 space-y-3 rounded border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-3">
            {renderMarkdownSummary(summary.summary)}
          </div>
        )}
        {detailsOpen && summary.diagnostics && (
          <div className="mt-3 rounded-md border border-red-500/35 border-l-4 border-l-red-400 bg-[color:var(--oc-surface-deep)] p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs font-semibold uppercase text-red-300">
                Failure diagnostics
              </span>
              {summary.diagnostics.task_execution_id && (
                <span className="text-xs text-slate-400">
                  TE {summary.diagnostics.task_execution_id}
                </span>
              )}
              {summary.diagnostics.reason && (
                <span className="text-xs text-slate-400">
                  {summary.diagnostics.reason}
                </span>
              )}
            </div>
            {diagnosticBadges.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {diagnosticBadges.map((badge) => (
                  <span
                    key={badge}
                    className="rounded-sm border border-red-400/50 bg-red-400/15 px-1.5 py-0.5 text-xs text-red-300"
                  >
                    {badge}
                  </span>
                ))}
              </div>
            )}
            {diagnosticReasons.length > 0 && (
              <div className="mt-2 space-y-1">
                {diagnosticReasons.map((reason) => (
                  <p key={reason} className="break-words text-xs text-slate-300">
                    {reason}
                  </p>
                ))}
              </div>
            )}
            {summary.diagnostics.message && (
              <p className="mt-2 break-words text-xs text-slate-400">
                {summary.diagnostics.message}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-semibold text-slate-200">Operator evidence</p>
          <span className="rounded-sm border border-orange-400/40 bg-orange-400/10 px-2 py-0.5 text-xs text-orange-200">
            {operatorEvidence.nextAction}
          </span>
        </div>
        <div className="grid gap-3 text-sm md:grid-cols-2">
          <div>
            <p className="text-xs text-slate-500">Boundary</p>
            <p className="mt-1 font-medium text-slate-100">{operatorEvidence.boundary}</p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Outcome</p>
            <p className="mt-1 font-medium text-slate-100">{operatorEvidence.outcome}</p>
          </div>
          {operatorEvidence.operation && (
            <div>
              <p className="text-xs text-slate-500">Structured operation</p>
              <p className="mt-1 font-mono text-sm text-slate-100">
                {operatorEvidence.operation}
              </p>
            </div>
          )}
          {operatorEvidence.targetPath && (
            <div>
              <p className="text-xs text-slate-500">Target</p>
              <p className="mt-1 break-words font-mono text-sm text-slate-100">
                {operatorEvidence.targetPath}
              </p>
            </div>
          )}
        </div>
        {operatorEvidence.operations.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {operatorEvidence.operations.map((operation, index) => (
              <span
                key={`${operation.op}-${operation.path || 'no-path'}-${index}`}
                className="rounded-sm border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-2 py-1 font-mono text-xs text-slate-200"
              >
                {operation.op}
                {operation.path ? ` ${operation.path}` : ''}
                {operation.outcome ? ` (${humanizeEvidenceValue(operation.outcome)})` : ''}
              </span>
            ))}
          </div>
        )}
        {operatorEvidence.reason && (
          <p className="mt-3 break-words rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-xs text-slate-300">
            {operatorEvidence.reason}
          </p>
        )}
      </div>

      {summary.operator_feedback && (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
          <p className="mb-1 text-xs font-medium text-slate-400">Saved Operator Feedback</p>
          <p className="whitespace-pre-wrap text-sm text-slate-200">{summary.operator_feedback}</p>
        </div>
      )}

      {canStartReplan && (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4 space-y-3">
          <p className="text-sm font-medium text-slate-300">Recovery guidance</p>
          <p className="text-xs text-slate-400">
            Add high-level direction before replanning. If you type guidance here and
            send to Project Architect, it is saved first and included with the failure
            summary.
            {hasPriorReplan && replanStatus
              ? ` Previous Project Architect run is ${replanStatus.replace(/_/g, ' ')}, so you can start another.`
              : ''}
          </p>
          <textarea
            rows={3}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            placeholder="e.g. Focus on fixing the database migration — the schema change was wrong."
            className="w-full rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-shell)] px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-primary-500 focus:outline-none resize-none"
          />
          <div className="flex items-center gap-3">
            {feedback.trim() && (
              <button
                disabled={feedbackSubmitting}
                onClick={async () => {
                  setFeedbackSubmitting(true);
                  try {
                    await onFeedbackSubmit(feedback.trim());
                    setFeedback('');
                  } finally {
                    setFeedbackSubmitting(false);
                  }
                }}
                className="rounded-lg border border-[color:var(--oc-border)] px-4 py-2 text-sm text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:opacity-50"
              >
                {feedbackSubmitting ? 'Saving…' : 'Save Feedback'}
              </button>
            )}
            <button
              disabled={replanning}
              onClick={async () => {
                setReplanning(true);
                try {
                  if (feedback.trim()) {
                    await onFeedbackSubmit(feedback.trim());
                    setFeedback('');
                  }
                  await onReplan();
                } finally {
                  setReplanning(false);
                }
              }}
              className="flex items-center gap-1.5 rounded-lg border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] px-4 py-2 text-sm text-white transition-colors hover:bg-[color:var(--oc-action-hover)] disabled:opacity-50"
            >
              {replanning
                ? 'Starting replan…'
                : feedback.trim()
                  ? 'Save and Send to Project Architect'
                  : hasPriorReplan
                    ? 'Send to Project Architect Again'
                    : 'Send to Project Architect'}
            </button>
          </div>
        </div>
      )}

      {replanStillOwnsFlow && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-primary-700/50 bg-primary-500/10 p-3">
          <p className="text-xs text-slate-300">
            Replan started as planning session #{summary.replan_planning_session_id}.
            Review and commit the revised plan in Project Architect.
          </p>
          {onOpenProjectArchitect && (
            <button
              type="button"
              onClick={onOpenProjectArchitect}
              className="rounded-md border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] px-3 py-1.5 text-xs text-white transition-colors hover:bg-[color:var(--oc-action-hover)]"
            >
              Open Project Architect
            </button>
          )}
        </div>
      )}
    </div>
  );
}
