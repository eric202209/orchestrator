import { useState, useEffect, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { authAPI, dashboardAPI } from '../api/client';
import type { DashboardAttention, User } from '../types/api';
import {
  AlertTriangle,
  LogOut,
  CheckCircle2,
  Activity,
  GitBranch,
  FileText,
} from 'lucide-react';
import { Skeleton } from '../components/ui';

const truncatePrompt = (prompt: string, max = 70): string =>
  prompt.length > max ? `${prompt.slice(0, max - 1)}…` : prompt;

function Dashboard() {
  const navigate = useNavigate();
  const [user, setUser] = useState<User | null>(null);
  const [attention, setAttention] = useState<DashboardAttention | null>(null);
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
    try {
      const res = await dashboardAPI.getAttention();
      setAttention(res.data);
    } catch {
      // ignore — keep last known state
    } finally {
      setLoading(false);
    }
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
      <div className="space-y-5">
        <div className="flex items-center justify-between mb-6">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-5 w-20" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
        <Skeleton className="h-28 w-full" />
      </div>
    );
  }

  const pendingInterventions = attention?.pending_interventions ?? [];
  const totalProjects = attention?.total_projects ?? 0;
  const totalTasks = attention?.total_tasks ?? 0;
  const runningSessions = attention?.running_sessions ?? 0;
  const completedTasks = attention?.completed_tasks ?? 0;

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

      {/* System overview counts */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Projects</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-white">{totalProjects}</p>
            <GitBranch className="h-5 w-5 text-primary-500/60" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Tasks</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-white">{totalTasks}</p>
            <FileText className="h-5 w-5 text-slate-400" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Running</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-primary-300">{runningSessions}</p>
            <Activity className="h-5 w-5 text-primary-400" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Completed</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-emerald-400">{completedTasks}</p>
            <CheckCircle2 className="h-5 w-5 text-emerald-500" />
          </div>
        </div>
      </div>

      {/* Pending interventions */}
      {pendingInterventions.length === 0 ? (
        <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] px-6 py-12 text-center">
          <CheckCircle2 className="h-8 w-8 mx-auto mb-3 text-emerald-500/50" />
          <p className="text-sm font-medium text-slate-300">Nothing requires attention.</p>
          <p className="text-xs text-slate-500 mt-1">All sessions are healthy.</p>
        </div>
      ) : (
        <div className="bg-[color:var(--oc-surface)] rounded-lg border border-amber-500/30">
          <div className="px-5 py-3 border-b border-[color:var(--oc-border-soft)] flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-400 flex-shrink-0" />
            <h2 className="text-sm font-medium text-white">
              {pendingInterventions.length === 1
                ? '1 Pending Intervention'
                : `${pendingInterventions.length} Pending Interventions`}
            </h2>
          </div>
          <div className="divide-y divide-[color:var(--oc-border-soft)]">
            {pendingInterventions.map((item) => (
              <Link
                key={item.id}
                to={`/sessions/${item.session_id}`}
                className="flex items-center justify-between px-5 py-3 hover:bg-[color:var(--oc-surface-raised)] transition-colors group"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-200 group-hover:text-white transition-colors truncate">
                    {truncatePrompt(item.prompt)}
                  </p>
                  <p className="text-xs text-slate-500 mt-0.5">
                    {item.project_name}
                  </p>
                </div>
                <span className="text-xs text-amber-400 font-medium flex-shrink-0 ml-3">
                  Respond →
                </span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default Dashboard;
