import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { sessionsAPI, projectsAPI, tasksAPI } from '../api/client';
import type { Session, Project, Task } from '../types/api';
import { isLegacyTaskExecutionSession } from '../lib/sessionIdentity';
import { deriveRunStateFromSession, getRunStateDisplay } from '../lib/runState';
import {
  Terminal,
  Clock,
  Search,
  Activity,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { EmptyState, Skeleton } from '../components/ui';

const sessionAccentClasses: Record<string, string> = {
  done: 'border-l-emerald-500/80',
  completed: 'border-l-emerald-500/80',
  failed: 'border-l-red-500/80',
  error: 'border-l-red-500/80',
  paused: 'border-l-amber-500/80',
  awaiting_input: 'border-l-amber-500/80',
  stopped: 'border-l-slate-600',
  cancelled: 'border-l-slate-600',
  canceled: 'border-l-slate-600',
  running: 'border-l-primary-500/80',
  pending: 'border-l-slate-500',
};

const mutedSessionStatuses = new Set(['stopped', 'cancelled', 'canceled']);
const activeSessionStatuses = new Set(['running', 'paused', 'awaiting_input', 'pending']);
const terminalProblemStatuses = new Set(['failed', 'error']);

type SessionFilter = 'all' | 'active' | 'needs_attention' | 'completed' | 'stopped';

const sessionFilterLabels: Array<{ key: SessionFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'active', label: 'Active' },
  { key: 'needs_attention', label: 'Needs attention' },
  { key: 'completed', label: 'Completed' },
  { key: 'stopped', label: 'Stopped' },
];

const getSessionTime = (session: Session): number => {
  const candidate =
    session.updated_at ||
    session.stopped_at ||
    session.paused_at ||
    session.resumed_at ||
    session.started_at ||
    session.created_at;
  return candidate ? new Date(candidate).getTime() : 0;
};

const activeSortRank = (session: Session, tasks: Task[]): number => {
  const statusKey = session.status?.toLowerCase() || '';
  const sessionTasks = tasks.filter((task) => task.session_id === session.id);
  if (sessionTasks.some((task) => task.status === 'running')) return 0;
  if (statusKey === 'running') return 1;
  if (statusKey === 'pending' || sessionTasks.some((task) => task.status === 'pending')) return 2;
  if (statusKey === 'awaiting_input') return 3;
  if (statusKey === 'paused') return 4;
  return 5;
};

const getSessionActivityDisplay = (session: Session, tasks: Task[]) => {
  const statusKey = session.status?.toLowerCase() || '';
  const sessionTasks = tasks.filter((task) => task.session_id === session.id);
  const runningTask = sessionTasks.find((task) => task.status === 'running');
  const queuedTask = sessionTasks.find((task) => task.status === 'pending');
  const baseRunState = deriveRunStateFromSession(session);
  const baseDisplay = getRunStateDisplay(baseRunState);

  if (runningTask) {
    return {
      ...baseDisplay,
      label: 'Running',
      description: runningTask.title,
    };
  }

  if (statusKey === 'running') {
    return {
      ...baseDisplay,
      label: queuedTask ? 'Queued' : 'Running',
      description: queuedTask?.title || 'Worker is preparing the next task.',
    };
  }

  if (statusKey === 'pending' || queuedTask) {
    return {
      ...baseDisplay,
      label: 'Queued',
      description: queuedTask?.title || 'Waiting for execution to start.',
    };
  }

  return baseDisplay;
};

const matchesSessionFilter = (session: Session, filter: SessionFilter): boolean => {
  const statusKey = session.status?.toLowerCase() || '';
  if (filter === 'active') return activeSessionStatuses.has(statusKey);
  if (filter === 'needs_attention') {
    return terminalProblemStatuses.has(statusKey) || statusKey === 'awaiting_input';
  }
  if (filter === 'completed') return ['done', 'completed'].includes(statusKey);
  if (filter === 'stopped') return mutedSessionStatuses.has(statusKey);
  return true;
};

function SessionsList() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Record<number, Project>>({});
  const [tasksByProject, setTasksByProject] = useState<Record<number, Task[]>>({});
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<SessionFilter>('all');
  const [query, setQuery] = useState('');

  useEffect(() => {
    const fetchSessions = async () => {
      try {
        const projectsResponse = await projectsAPI.getAll();
        const allProjects = projectsResponse.data || [];

        const projectMap: Record<number, Project> = {};
        allProjects.forEach(project => {
          projectMap[project.id] = project;
        });
        setProjects(projectMap);

        const sessionPromises = allProjects.map(async (project) => {
          try {
            const sessionsResponse = await sessionsAPI.getByProject(project.id);
            return sessionsResponse.data || [];
          } catch (error) {
            console.error(`Failed to fetch sessions for project ${project.id}:`, error);
            return [];
          }
        });

        const taskPromises = allProjects.map(async (project) => {
          try {
            const tasksResponse = await tasksAPI.getByProject(project.id);
            return [project.id, tasksResponse.data || []] as const;
          } catch (error) {
            console.error(`Failed to fetch tasks for project ${project.id}:`, error);
            return [project.id, []] as const;
          }
        });

        const [allSessionsArrays, taskEntries] = await Promise.all([
          Promise.all(sessionPromises),
          Promise.all(taskPromises),
        ]);
        setSessions(allSessionsArrays.flat());
        setTasksByProject(Object.fromEntries(taskEntries));
      } catch (error) {
        console.error('Failed to fetch sessions:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchSessions();
  }, []);

  const visibleSessions = sessions
    .filter((session) => {
      if (!matchesSessionFilter(session, filter)) return false;
      const project = projects[session.project_id || 0];
      const haystack =
        `${session.name || ''} ${session.description || ''} ${project?.name || ''}`.toLowerCase();
      return !query.trim() || haystack.includes(query.trim().toLowerCase());
    })
    .sort((a, b) => {
      const aTasks = tasksByProject[a.project_id] || [];
      const bTasks = tasksByProject[b.project_id] || [];
      const rankDelta = activeSortRank(a, aTasks) - activeSortRank(b, bTasks);
      if (rankDelta !== 0) return rankDelta;
      return getSessionTime(b) - getSessionTime(a);
    });

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-lg font-semibold text-white">Runs</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            {sessions.length} run{sessions.length !== 1 ? 's' : ''} · {Object.keys(projects).length} project{Object.keys(projects).length !== 1 ? 's' : ''}
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search..."
              className="w-full rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] py-1.5 pl-8 pr-3 text-xs text-white placeholder-slate-400 hover:border-[color:var(--oc-border)] focus:border-primary-500 focus:outline-none sm:w-44"
            />
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {sessionFilterLabels.map((item) => {
          const selected = filter === item.key;
          const count = sessions.filter((session) => matchesSessionFilter(session, item.key)).length;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => setFilter(item.key)}
              className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                selected
                  ? 'border-primary-500 bg-primary-500/10 text-white'
                  : 'border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
              }`}
            >
              {item.label}
              <span className={selected ? 'ml-1 text-primary-200/80' : 'ml-1 text-slate-400'}>{count}</span>
            </button>
          );
        })}
      </div>

      {/* Sessions List */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
              <Skeleton className="h-5 w-3/4 mb-2" />
              <Skeleton className="h-4 w-1/2 mb-3" />
              <Skeleton className="h-8 w-full" />
            </div>
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          icon={Terminal}
          title="No runs yet"
          description="Runs are created from a project so each execution stays tied to project context."
        />
      ) : visibleSessions.length === 0 ? (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-8 text-center">
          <Terminal className="mx-auto mb-3 h-8 w-8 text-slate-500" />
          <h2 className="text-sm font-medium text-white">No matching runs</h2>
          <p className="mt-1 text-sm text-slate-400">
            Adjust the status filter or search text to inspect another run.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] divide-y divide-[color:var(--oc-border-soft)]">
          {visibleSessions.map((session) => {
            const project = projects[session.project_id || 0];
            const isLegacySession = isLegacyTaskExecutionSession(
              session,
              (tasksByProject[session.project_id] || []).map((task) => task.title)
            );
            const statusKey = session.status?.toLowerCase() || '';
            const accentClass = sessionAccentClasses[statusKey] || 'border-l-slate-600';
            const isMuted = mutedSessionStatuses.has(statusKey);
            const sessionTasks = tasksByProject[session.project_id] || [];
            const runDisplay = getSessionActivityDisplay(session, sessionTasks);
            return (
              <Link
                key={session.id}
                to={`/sessions/${session.id}`}
                className={`group grid gap-3 border-l-[3px] px-4 py-3 transition-colors md:grid-cols-[minmax(0,1.4fr)_minmax(160px,0.8fr)_minmax(120px,0.5fr)] md:items-center ${accentClass} ${
                  isMuted
                    ? 'opacity-70 hover:bg-[color:var(--oc-surface-raised)] hover:opacity-100'
                    : 'hover:bg-[color:var(--oc-surface-raised)]'
                }`}
              >
                <div className="min-w-0">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${runDisplay.badgeClass}`}>
                      {runDisplay.label}
                    </span>
                    {runDisplay.label === 'Running' && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-sky-400/30 bg-sky-400/10 px-2 py-0.5 text-[11px] font-medium text-sky-200">
                        <Activity className="h-3 w-3" />
                        Current
                      </span>
                    )}
                    {isLegacySession && (
                      <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-300">
                        Diagnostics
                      </span>
                    )}
                  </div>
                  <h3 className="truncate text-sm font-medium text-slate-100 transition-colors group-hover:text-white">
                    {session.name}
                  </h3>
                  {session.description && (
                    <p className="mt-1 line-clamp-1 text-xs text-slate-400">
                      {session.description}
                    </p>
                  )}
                  {activeSessionStatuses.has(statusKey) && runDisplay.description && (
                    <p className="mt-1 truncate text-xs text-sky-200/80">
                      {runDisplay.description}
                    </p>
                  )}
                </div>

                <div className="min-w-0 text-xs text-slate-400">
                  <p className="truncate text-slate-300">{project?.name || 'Unknown project'}</p>
                  <p className="mt-1 text-slate-400">
                    {session.execution_mode ? `${session.execution_mode} mode` : 'workflow session'}
                  </p>
                </div>

                <div className="flex items-center justify-between gap-3 text-xs text-slate-400 md:justify-end">
                  <span>{formatDistanceToNow(new Date(getSessionTime(session) || session.created_at), { addSuffix: true })}</span>
                  {session.started_at && (
                    <span className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      Started
                    </span>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default SessionsList;
