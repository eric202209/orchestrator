import { useState, useEffect, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { authAPI, sessionsAPI, tasksAPI, projectsAPI } from '../api/client';
import type { Session, Task, Project, User } from '../types/api';
import {
  AlertTriangle,
  PauseCircle,
  ClipboardList,
  LogOut,
  CheckCircle2,
} from 'lucide-react';
import { Skeleton } from '../components/ui';

const terminalProblemStatuses = new Set(['failed', 'error']);

function Dashboard() {
  const navigate = useNavigate();
  const [user, setUser] = useState<User | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projectMap, setProjectMap] = useState<Record<number, Project>>({});
  const [loading, setLoading] = useState(true);
  const [isAuthChecked, setIsAuthChecked] = useState(false);

  useEffect(() => {
    authAPI
      .getMe()
      .then((r) => setUser(r.data))
      .catch(() => navigate('/login', { replace: true }))
      .finally(() => setIsAuthChecked(true));
  }, [navigate]);

  const fetchAttentionData = useCallback(async () => {
    const [sessionsRes, tasksRes, projectsRes] = await Promise.allSettled([
      sessionsAPI.getAll({ limit: 500 }),
      tasksAPI.getAll({ limit: 5000 }),
      projectsAPI.getAll({ limit: 500 }),
    ]);
    if (sessionsRes.status === 'fulfilled') setSessions(sessionsRes.value.data || []);
    if (tasksRes.status === 'fulfilled') setTasks(tasksRes.value.data || []);
    if (projectsRes.status === 'fulfilled') {
      const map: Record<number, Project> = {};
      (projectsRes.value.data || []).forEach((p) => { map[p.id] = p; });
      setProjectMap(map);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (!isAuthChecked) return;
    if (!user) { setLoading(false); return; }
    fetchAttentionData();
    const id = setInterval(fetchAttentionData, 30_000);
    return () => clearInterval(id);
  }, [isAuthChecked, user, fetchAttentionData]);

  const handleLogout = async () => {
    try { await authAPI.logout(); } catch { /* ignore */ }
    window.location.href = '/login';
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between mb-6">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-5 w-20" />
        </div>
        <Skeleton className="h-28 w-full" />
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }

  const interventionSessions = sessions.filter((s) => s.status === 'awaiting_input');
  const attentionSessions = sessions.filter(
    (s) => terminalProblemStatuses.has(s.status as string) || s.status === 'paused',
  );
  const reviewTasks = tasks.filter((t) => t.workspace_status === 'ready');

  const hasAnything =
    interventionSessions.length > 0 ||
    attentionSessions.length > 0 ||
    reviewTasks.length > 0;

  const accountLabel = user?.name?.trim() || user?.email || '';

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold text-white">Dashboard</h1>
          {accountLabel && (
            <p className="text-sm text-slate-400 mt-0.5">{accountLabel}</p>
          )}
        </div>
        <button
          onClick={handleLogout}
          className="flex items-center gap-1.5 text-sm text-slate-400 hover:text-slate-200 transition-colors"
        >
          <LogOut className="h-4 w-4" />
          Sign out
        </button>
      </div>

      {!hasAnything ? (
        <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] px-6 py-12 text-center">
          <CheckCircle2 className="h-8 w-8 mx-auto mb-3 text-emerald-500/50" />
          <p className="text-sm font-medium text-slate-300">Nothing requires attention.</p>
          <p className="text-xs text-slate-500 mt-1">All sessions are healthy.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {/* Pending interventions — listed individually, direct links */}
          {interventionSessions.length > 0 && (
            <div className="bg-[color:var(--oc-surface)] rounded-lg border border-amber-500/30">
              <div className="px-5 py-3 border-b border-[color:var(--oc-border-soft)] flex items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-amber-400 flex-shrink-0" />
                <h2 className="text-sm font-medium text-white">
                  {interventionSessions.length === 1
                    ? '1 Pending Intervention'
                    : `${interventionSessions.length} Pending Interventions`}
                </h2>
              </div>
              <div className="divide-y divide-[color:var(--oc-border-soft)]">
                {interventionSessions.map((session) => (
                  <Link
                    key={session.id}
                    to={`/sessions/${session.id}`}
                    className="flex items-center justify-between px-5 py-3 hover:bg-[color:var(--oc-surface-raised)] transition-colors group"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-slate-200 group-hover:text-white transition-colors truncate">
                        {session.name}
                      </p>
                      {projectMap[session.project_id] && (
                        <p className="text-xs text-slate-500 mt-0.5">
                          {projectMap[session.project_id].name}
                        </p>
                      )}
                    </div>
                    <span className="text-xs text-amber-400 font-medium flex-shrink-0 ml-3">
                      Respond →
                    </span>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {/* Sessions needing attention — count + link */}
          {attentionSessions.length > 0 && (
            <Link
              to="/sessions"
              className="flex items-center justify-between px-5 py-4 bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] hover:border-[color:var(--oc-border)] transition-colors group"
            >
              <div className="flex items-center gap-3">
                <PauseCircle className="h-5 w-5 text-slate-400 flex-shrink-0" />
                <div>
                  <p className="text-sm font-medium text-slate-200 group-hover:text-white transition-colors">
                    {attentionSessions.length === 1
                      ? '1 session needs attention'
                      : `${attentionSessions.length} sessions need attention`}
                  </p>
                  <p className="text-xs text-slate-500 mt-0.5">Paused or failed sessions</p>
                </div>
              </div>
              <span className="text-xs text-slate-400 group-hover:text-slate-200 transition-colors flex-shrink-0">
                View →
              </span>
            </Link>
          )}

          {/* Tasks pending review — count + link */}
          {reviewTasks.length > 0 && (
            <Link
              to="/tasks"
              className="flex items-center justify-between px-5 py-4 bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] hover:border-[color:var(--oc-border)] transition-colors group"
            >
              <div className="flex items-center gap-3">
                <ClipboardList className="h-5 w-5 text-slate-400 flex-shrink-0" />
                <div>
                  <p className="text-sm font-medium text-slate-200 group-hover:text-white transition-colors">
                    {reviewTasks.length === 1
                      ? '1 task pending review'
                      : `${reviewTasks.length} tasks pending review`}
                  </p>
                  <p className="text-xs text-slate-500 mt-0.5">Awaiting operator decision</p>
                </div>
              </div>
              <span className="text-xs text-slate-400 group-hover:text-slate-200 transition-colors flex-shrink-0">
                Review →
              </span>
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

export default Dashboard;
