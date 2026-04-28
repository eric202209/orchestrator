import { useState } from 'react';
import type { ReactNode } from 'react';
import type {
  Checkpoint,
  CheckpointInspection,
  InterventionRequest,
  Project,
  Session,
  SessionDivergenceCompareResponse,
  SessionStateDiffResponse,
  Task,
} from '@/types/api';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { TerminalViewer } from '@/components/TerminalViewer';
import { StatusBadge } from '@/components/ui';
import {
  Activity,
  CheckCircle2,
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

export interface TimelineEvent {
  id: string;
  at: string;
  type: TimelineEventType;
  title: string;
  detail: string;
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
              className="ml-2 text-primary-400 hover:text-primary-300"
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
            Alert{session.last_alert_at ? ` • ${new Date(session.last_alert_at).toLocaleString()}` : ''}: {session.last_alert_message}
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
    <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 flex items-center gap-2 text-sm text-slate-400">
          <Activity className="h-4 w-4" />
          Status
        </p>
        <p className="font-semibold capitalize text-white">{session.status}</p>
        <p className="mt-1 text-xs uppercase tracking-wide text-slate-500">
          {session.execution_mode} mode
        </p>
      </div>
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 flex items-center gap-2 text-sm text-slate-400">
          <TerminalIcon className="h-4 w-4" />
          Tasks
        </p>
        <p className="font-semibold text-white">{tasksCount}</p>
      </div>
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 flex items-center gap-2 text-sm text-slate-400">
          <Clock className="h-4 w-4" />
          Created
        </p>
        <p className="font-semibold text-white">{formatDateTime(session.created_at)}</p>
      </div>
      {session.started_at && (
        <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
          <p className="mb-1 flex items-center gap-2 text-sm text-slate-400">
            <CheckCircle2 className="h-4 w-4" />
            Started
          </p>
          <p className="font-semibold text-white">
            {formatDateTime(session.started_at)}
          </p>
        </div>
      )}
    </div>
  );
}

interface SessionTabsProps {
  activeTab: 'logs' | 'tasks' | 'settings';
  onChange: (tab: 'logs' | 'tasks' | 'settings') => void;
  tasksCount: number;
}

export function SessionTabs({
  activeTab,
  onChange,
  tasksCount,
}: SessionTabsProps) {
  return (
    <div className="border-b border-slate-700">
      <nav className="flex gap-4">
        <button
          onClick={() => onChange('logs')}
          className={cn(
            'flex items-center gap-2 px-2 pb-2 text-sm font-medium',
            activeTab === 'logs'
              ? 'border-b-2 border-blue-400 text-blue-400'
              : 'text-slate-400 hover:text-slate-200'
          )}
        >
          <TerminalIcon className="h-4 w-4" />
          Logs
        </button>
        <button
          onClick={() => onChange('tasks')}
          className={cn(
            'px-2 pb-2 text-sm font-medium',
            activeTab === 'tasks'
              ? 'border-b-2 border-blue-400 text-blue-400'
              : 'text-slate-400 hover:text-slate-200'
          )}
        >
          Tasks ({tasksCount})
        </button>
        <button
          onClick={() => onChange('settings')}
          className={cn(
            'flex items-center gap-2 px-2 pb-2 text-sm font-medium',
            activeTab === 'settings'
              ? 'border-b-2 border-blue-400 text-blue-400'
              : 'text-slate-400 hover:text-slate-200'
          )}
        >
          <Settings className="h-4 w-4" />
          Settings
        </button>
      </nav>
    </div>
  );
}

interface SessionLogsPanelProps {
  anomalyEvents?: Array<{ title: string; detail: string; at: string }>;
  compareMatches?: SessionDivergenceCompareResponse | null;
  displayLogs: TerminalLogEntry[];
  formatDateTime: (value?: string | null) => string;
  handleRefreshLogs: () => Promise<void>;
  healthEvents?: Array<{ timestamp: string; score: number; slope?: number | null }>;
  logVerbosity: 'clean' | 'verbose';
  logViewMode: 'newest' | 'oldest' | 'success' | 'errors' | 'all';
  onLogVerbosityChange: (mode: 'clean' | 'verbose') => void;
  onLogViewModeChange: (mode: 'newest' | 'oldest' | 'success' | 'errors' | 'all') => void;
  timelineSpans?: TimelineSpan[];
  stateDiff?: SessionStateDiffResponse | null;
  timelineEvents: TimelineEvent[];
  wsConnected: boolean;
}

export function SessionLogsPanel({
  anomalyEvents = [],
  compareMatches,
  displayLogs,
  formatDateTime,
  handleRefreshLogs,
  healthEvents = [],
  logVerbosity,
  logViewMode,
  onLogVerbosityChange,
  onLogViewModeChange,
  timelineSpans = [],
  stateDiff,
  timelineEvents,
  wsConnected,
}: SessionLogsPanelProps) {
  const latestHealth = healthEvents[healthEvents.length - 1] || null;

  return (
    <div className="space-y-4">
      <TerminalViewer
        logs={displayLogs}
        autoScroll={true}
        className="h-[500px] bg-slate-900"
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
            className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm text-white transition-colors hover:bg-slate-600"
          >
            <option value="clean">Clean Logs</option>
            <option value="verbose">Verbose Logs</option>
          </select>
          <button
            onClick={handleRefreshLogs}
            className="flex items-center gap-2 rounded-lg bg-slate-700 px-3 py-1.5 text-sm text-white transition-colors hover:bg-slate-600"
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </button>
          <select
            value={logViewMode}
            onChange={(e) =>
              onLogViewModeChange(
                e.target.value as 'newest' | 'oldest' | 'success' | 'errors' | 'all'
              )
            }
            className="rounded-lg bg-slate-700 px-3 py-1.5 text-sm text-white transition-colors hover:bg-slate-600"
          >
            <option value="newest">Sort: Newest First</option>
            <option value="oldest">Sort: Oldest First</option>
            <option value="success">Filter: Success Only</option>
            <option value="errors">Filter: Errors Only</option>
            <option value="all">Show All</option>
          </select>
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200">Health Score</h3>
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
              <span className="text-xs text-slate-500">No score yet</span>
            )}
          </div>
          {healthEvents.length === 0 ? (
            <p className="text-sm text-slate-500">
              Health history appears after orchestration events start landing.
            </p>
          ) : (
            <div className="max-h-56 space-y-2 overflow-y-auto">
              {healthEvents.slice(-5).reverse().map((event) => (
                <div
                  key={`${event.timestamp}-${event.score}`}
                  className="flex items-center justify-between rounded-md border border-slate-800 px-3 py-2 text-sm"
                >
                  <div>
                    <p className="font-medium text-slate-200">{event.score}/100</p>
                    <p className="text-xs text-slate-500">{formatDateTime(event.timestamp)}</p>
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

        <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200">Latest State Diff</h3>
            <span className="text-xs text-slate-500">
              {stateDiff
                ? `Snapshots ${stateDiff.from_checkpoint} → ${stateDiff.to_checkpoint}`
                : 'Unavailable'}
            </span>
          </div>
          {!stateDiff || !stateDiff.delta ? (
            <p className="text-sm text-slate-500">
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
                  <span className="text-xs text-slate-500">{formatDateTime(event.at)}</span>
                </div>
                <p className="mt-1 text-sm text-slate-200">{event.detail}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {compareMatches && compareMatches.matches.length > 0 && (
        <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-200">Similar Failed Sessions</h3>
            <span className="text-xs text-slate-400">{compareMatches.matches.length} matches</span>
          </div>
          <div className="space-y-2">
            {compareMatches.matches.slice(0, 3).map((match) => (
              <div key={match.session_id} className="rounded-md border border-slate-800 p-3">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-medium text-slate-200">
                    #{match.session_id} {match.session_name}
                  </p>
                  <span className="text-xs text-sky-400">
                    {(match.similarity_score * 100).toFixed(0)}% similar
                  </span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
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

      <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-200">Causal Spans</h3>
          <span className="text-xs text-slate-400">{timelineSpans.length} spans</span>
        </div>
        {timelineSpans.length === 0 ? (
          <p className="text-sm text-slate-500">
            Span grouping appears when parent-linked orchestration events are present.
          </p>
        ) : (
          <div className="max-h-56 space-y-2 overflow-y-auto">
            {timelineSpans.slice().reverse().map((span) => (
              <div key={span.id} className="rounded-md border border-slate-800 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <span
                      className={cn(
                        'text-xs font-medium uppercase',
                        span.lane === 'reasoning' && 'text-violet-400',
                        span.lane === 'tool' && 'text-sky-400',
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
                  <span className="text-xs text-slate-500">{formatDateTime(span.started_at)}</span>
                </div>
                <p className="mt-2 text-sm font-medium text-slate-200">{span.title}</p>
                <p className="mt-1 text-sm text-slate-400">{span.summary}</p>
                <p className="mt-1 text-xs text-slate-500">{span.event_count} linked events</p>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-slate-700 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-200">Execution Timeline</h3>
          <span className="text-xs text-slate-400">{timelineEvents.length} events</span>
        </div>
        <div className="max-h-56 space-y-2 overflow-y-auto text-sm">
          {timelineEvents.length === 0 ? (
            <p className="text-slate-500">
              No timeline events yet. Start/execute a task to see progress milestones.
            </p>
          ) : (
            timelineEvents
              .slice()
              .reverse()
              .map((event) => (
                <div key={event.id} className="rounded-md border border-slate-800 p-2">
                  <div className="flex items-center justify-between">
                    <span
                      className={cn(
                        'text-xs font-medium uppercase',
                        event.type === 'error' && 'text-red-400',
                        event.type === 'planning' && 'text-violet-400',
                        event.type === 'executing' && 'text-blue-400',
                        event.type === 'debugging' && 'text-amber-400',
                        event.type === 'revising_plan' && 'text-fuchsia-400',
                        event.type === 'summarizing' && 'text-teal-400',
                        event.type === 'checkpoint' && 'text-emerald-400',
                        event.type === 'validation' && 'text-lime-400',
                        event.type === 'repair' && 'text-orange-400',
                        event.type === 'task' && 'text-sky-400',
                        event.type === 'status' && 'text-cyan-400',
                        event.type === 'info' && 'text-slate-300'
                      )}
                    >
                      {event.title}
                    </span>
                    <span className="text-xs text-slate-500">
                      {formatDateTime(event.at)}
                    </span>
                  </div>
                  <p className="mt-1 break-words text-slate-300">{event.detail}</p>
                </div>
              ))
          )}
        </div>
      </div>
    </div>
  );
}

interface SessionTasksPanelProps {
  actionButtons: ReactNode;
  formatDateTime: (value?: string | null) => string;
  onExecuteTask?: (task: Task) => void;
  onRefreshTasks?: () => void;
  session: Session;
  tasks: Task[];
}

export function SessionTasksPanel({
  actionButtons,
  formatDateTime,
  onExecuteTask,
  onRefreshTasks,
  session,
  tasks,
}: SessionTasksPanelProps) {
  return (
    <div className="space-y-4">
      {actionButtons && session.status !== 'running' && (
        <div className="mb-4 rounded-lg border border-blue-700/50 bg-blue-900/20 p-4">
          <p className="mb-2 text-sm text-blue-400">
            Session is not running. Start the session to execute tasks automatically or enter manual mode and run tasks one by one.
          </p>
          {onRefreshTasks && (
            <button
              onClick={onRefreshTasks}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-blue-700"
            >
              Refresh Task View
            </button>
          )}
        </div>
      )}

      {tasks.length === 0 ? (
        <div className="py-12 text-center">
          <TerminalIcon className="mx-auto mb-4 h-12 w-12 text-slate-500" />
          <p className="text-slate-400">No tasks yet</p>
          {actionButtons && (
            <p className="mt-2 text-sm text-slate-500">
              Start the session to automatically execute tasks from your project
            </p>
          )}
        </div>
      ) : (
        tasks.map((task) => (
          <div
            key={task.id}
            className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur transition-colors hover:border-slate-600"
          >
            <div className="mb-2 flex items-start justify-between">
              <div>
                <h3 className="font-semibold text-white">{task.title}</h3>
                <p className="mt-1 text-xs text-slate-500">
                  Order: {task.plan_position ?? 'manual'} • Priority: {task.priority ?? 0}
                </p>
                {task.workspace_status && (
                  <p className="mt-1 text-xs capitalize text-slate-500">
                    Workspace: {task.workspace_status.replace(/_/g, ' ')}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2">
                <StatusBadge status={task.status} size="sm" />
                {onExecuteTask && (
                  session.execution_mode === 'manual' ||
                  task.status === 'pending' ||
                  task.status === 'failed' ||
                  task.status === 'cancelled' ||
                  task.status === 'done'
                ) && (
                  <button
                    onClick={() => onExecuteTask(task)}
                    disabled={task.status === 'running'}
                    className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs text-white transition-colors hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {task.status === 'done' ? 'Run Again' : 'Run Task'}
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
            <div className="mt-3 flex items-center gap-4 text-xs text-slate-500">
              {task.created_at && <span>Created: {formatDateTime(task.created_at)}</span>}
              {task.started_at && <span>Started: {formatDateTime(task.started_at)}</span>}
              {task.completed_at && (
                <span className="text-emerald-400">
                  Completed: {formatDateTime(task.completed_at)}
                </span>
              )}
            </div>
          </div>
        ))
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
  onRefreshTasks?: () => void;
  session: Session;
}

export function SessionSettingsPanel({
  checkpoints = [],
  checkpointInspection,
  formatDateTime,
  onInspectCheckpoint,
  onModeChange,
  onReplayCheckpoint,
  onRefreshTasks,
  session,
}: SessionSettingsPanelProps) {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-2 text-sm text-slate-400">Execution Mode</p>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onModeChange?.('automatic')}
            className={cn(
              'rounded-lg px-3 py-2 text-sm transition-colors',
              session.execution_mode === 'automatic'
                ? 'bg-primary-600 text-white'
                : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
            )}
          >
            Automatic
          </button>
          <button
            onClick={() => onModeChange?.('manual')}
            className={cn(
              'rounded-lg px-3 py-2 text-sm transition-colors',
              session.execution_mode === 'manual'
                ? 'bg-primary-600 text-white'
                : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
            )}
          >
            Manual
          </button>
          {onRefreshTasks && (
            <button
              onClick={onRefreshTasks}
              className="ml-auto rounded-lg bg-slate-700 px-3 py-2 text-sm text-white transition-colors hover:bg-slate-600"
            >
              Refresh Tasks
            </button>
          )}
        </div>
      </div>
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 text-sm text-slate-400">Session ID</p>
        <p className="font-mono text-sm text-white">{session.id}</p>
      </div>
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 text-sm text-slate-400">Project ID</p>
        <p className="font-mono text-sm text-white">{session.project_id}</p>
      </div>
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <p className="mb-1 text-sm text-slate-400">Created At</p>
        <p className="text-white">{formatDateTime(session.created_at)}</p>
      </div>
      {session.started_at && (
        <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
          <p className="mb-1 text-sm text-slate-400">Started At</p>
          <p className="text-white">{formatDateTime(session.started_at)}</p>
        </div>
      )}
      {session.stopped_at && (
        <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
          <p className="mb-1 text-sm text-slate-400">Stopped At</p>
          <p className="text-white">{formatDateTime(session.stopped_at)}</p>
        </div>
      )}
      <div className="rounded-xl border border-slate-700 bg-slate-800/50 p-4 backdrop-blur">
        <div className="mb-3 flex items-center justify-between">
          <p className="text-sm text-slate-400">Checkpoint Inspector</p>
          <span className="text-xs text-slate-500">{checkpoints.length} stored</span>
        </div>
        {checkpoints.length === 0 ? (
          <p className="text-sm text-slate-500">No checkpoints recorded for this session yet.</p>
        ) : (
          <div className="max-h-56 space-y-2 overflow-y-auto">
            {checkpoints.map((checkpoint) => (
              <div
                key={checkpoint.name}
                className="rounded-lg border border-slate-700 bg-slate-900/70 p-3"
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
                    <p className="text-xs text-slate-500">
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
                      className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-600"
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
                          ? 'cursor-not-allowed bg-slate-700/60 text-slate-400'
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
          <div className="mt-4 rounded-lg border border-cyan-800/60 bg-cyan-950/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium text-cyan-200">
                {checkpointInspection.checkpoint_name}
              </p>
              <span className="text-xs text-cyan-400">
                {checkpointInspection.summary.status || 'unknown'}
              </span>
            </div>
            {checkpointInspection.resume_readiness ? (
              <p
                className={cn(
                  'mt-2 text-xs',
                  checkpointInspection.resume_readiness.resumable
                    ? 'text-cyan-300'
                    : 'text-amber-300'
                )}
              >
                {checkpointInspection.resume_readiness.resume_reason}
              </p>
            ) : null}
            <p className="mt-2 text-xs text-slate-300">
              Plan steps {checkpointInspection.summary.plan_step_count} • Completed {checkpointInspection.summary.completed_step_count} • Repairs {checkpointInspection.summary.completion_repair_attempts}
            </p>
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
            {checkpointInspection.latest_validation && (
              <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-950/80 p-3 text-xs text-slate-300">
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
                <div className="max-w-[78%] rounded-2xl rounded-br-md border border-slate-700 bg-slate-800/90 px-4 py-3 text-sm text-slate-200">
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
            <div className="mb-3 rounded-md border border-slate-700 bg-slate-900/60 p-3">
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
              <pre className="mt-2 overflow-x-auto rounded bg-slate-950/80 p-3 text-xs text-slate-300">
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
                  className="flex-1 rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-amber-500 focus:outline-none"
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
                className="w-full resize-none rounded-lg border border-slate-600 bg-slate-800 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-amber-500 focus:outline-none"
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
