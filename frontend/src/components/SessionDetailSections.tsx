import type { ReactNode } from 'react';
import type { Project, Session, Task } from '@/types/api';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { TerminalViewer } from '@/components/TerminalViewer';
import { StatusBadge } from '@/components/ui';
import {
  Activity,
  CheckCircle2,
  Clock,
  ExternalLink,
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
  displayLogs: TerminalLogEntry[];
  formatDateTime: (value?: string | null) => string;
  handleRefreshLogs: () => Promise<void>;
  logVerbosity: 'clean' | 'verbose';
  logViewMode: 'newest' | 'oldest' | 'success' | 'errors' | 'all';
  onLogVerbosityChange: (mode: 'clean' | 'verbose') => void;
  onLogViewModeChange: (mode: 'newest' | 'oldest' | 'success' | 'errors' | 'all') => void;
  timelineEvents: TimelineEvent[];
  wsConnected: boolean;
}

export function SessionLogsPanel({
  displayLogs,
  formatDateTime,
  handleRefreshLogs,
  logVerbosity,
  logViewMode,
  onLogVerbosityChange,
  onLogViewModeChange,
  timelineEvents,
  wsConnected,
}: SessionLogsPanelProps) {
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
  formatDateTime: (value?: string | null) => string;
  onModeChange?: (mode: 'automatic' | 'manual') => void;
  onRefreshTasks?: () => void;
  session: Session;
}

export function SessionSettingsPanel({
  formatDateTime,
  onModeChange,
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
    </div>
  );
}
