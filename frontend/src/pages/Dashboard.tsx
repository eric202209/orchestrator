import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { projectsAPI, authAPI, tasksAPI, adminAPI } from '../api/client';
import type { Project, User, Task } from '../types/api';
import {
  GitBranch,
  LogOut,
  Activity,
  CheckCircle2,
  FileText,
  Search,
  Terminal,
  Trash2,
  ExternalLink,
  ShieldCheck
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { StatusBadge, EmptyState, Skeleton } from '../components/ui';

function Dashboard() {
  const navigate = useNavigate();
  const [user, setUser] = useState<User | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'overview' | 'projects' | 'tasks'>('overview');
  const [showCreateProject, setShowCreateProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState('');
  const [creatingProject, setCreatingProject] = useState(false);
  const [isAuthChecked, setIsAuthChecked] = useState(false);
  const [projectSearchQuery, setProjectSearchQuery] = useState('');
  const [outcomeRates, setOutcomeRates] = useState<{
    gate_pass: boolean;
    sessions_analyzed: number;
    classified_sessions?: number;
    outcome_rates: Record<string, number>;
    outcome_counts: Record<string, number>;
    task_outcomes?: {
      total: number;
      counts: Record<string, number>;
      rates: Record<string, number>;
      execution_attempts: number;
      execution_attempts_done: number;
      first_pass_task_ids: number[];
      recovered_task_ids: number[];
    };
    operator_review_count: number;
  } | null>(null);

  const checkAuth = useCallback(async () => {
    try {
      const response = await authAPI.getMe();
      setUser(response.data);
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        console.error('Failed to fetch user:', axiosError.message || error);
      }
      navigate('/login', { replace: true });
      setIsAuthChecked(true);
      return;
    }
    setIsAuthChecked(true);
  }, [navigate]);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const fetchOutcomeRates = useCallback(async () => {
    try {
      const response = await adminAPI.getOutcomeRates(50);
      setOutcomeRates(response.data);
    } catch {
      // non-critical — silently ignore
    }
  }, []);

  const fetchTasks = useCallback(async () => {
    try {
      const tasksResponse = await tasksAPI.getAll({ limit: 500 });
      setTasks(tasksResponse.data);
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        console.error('Failed to fetch tasks:', axiosError.message || error);
      }
    }
  }, []);

  const fetchProjects = useCallback(async () => {
    try {
      const [projectsResponse] = await Promise.all([
        projectsAPI.getAll({ limit: 500 }),
      ]);
      setProjects(projectsResponse.data);
      await fetchTasks();
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        console.error('Failed to fetch projects:', axiosError.message || error);
      }
    } finally {
      setLoading(false);
    }
  }, [fetchTasks]);

  // Only fetch projects after auth is confirmed
  useEffect(() => {
    if (isAuthChecked && user) {
      fetchProjects();
      fetchOutcomeRates();
    } else if (isAuthChecked && !user) {
      setLoading(false);
    }
  }, [isAuthChecked, user, fetchProjects, fetchOutcomeRates]);

  // Refresh tasks every 30s so running status stays current
  useEffect(() => {
    if (!isAuthChecked || !user) return;
    const id = setInterval(fetchTasks, 30_000);
    return () => clearInterval(id);
  }, [isAuthChecked, user, fetchTasks]);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = newProjectName.trim();
    if (!trimmedName) {
      return;
    }

    setCreatingProject(true);
    const tempId = -Date.now();
    const now = new Date().toISOString();
    const optimisticProject: Project = {
      id: tempId,
      name: trimmedName,
      description: null,
      github_url: null,
      branch: 'main',
      created_at: now,
      updated_at: now,
    };
    setProjects((current) => [optimisticProject, ...current]);
    setNewProjectName('');
    setShowCreateProject(false);

    try {
      const response = await projectsAPI.create({ 
        name: trimmedName,
      });
      setProjects((current) =>
        current.map((project) => (project.id === tempId ? response.data : project))
      );
    } catch (error) {
      setProjects((current) => current.filter((project) => project.id !== tempId));
      setNewProjectName(trimmedName);
      setShowCreateProject(true);
      console.error('Failed to create project:', error);
      alert('Failed to create project. Please try again.');
    } finally {
      setCreatingProject(false);
    }
  };

  const handleDeleteProject = async (projectId: number) => {
    if (!confirm('Are you sure you want to delete this project? This cannot be undone.')) {
      return;
    }

    const previousProjects = projects;
    const previousTasks = tasks;
    setProjects((current) => current.filter((project) => project.id !== projectId));
    setTasks((current) => current.filter((task) => task.project_id !== projectId));

    try {
      await projectsAPI.delete(projectId);
    } catch (error) {
      setProjects(previousProjects);
      setTasks(previousTasks);
      console.error('Failed to delete project:', error);
      alert('Failed to delete project. Please try again.');
    }
  };

  const handleLogout = async () => {
    try {
      await authAPI.logout();
    } catch (error) {
      console.error('Failed to logout cleanly:', error);
    } finally {
      window.location.href = '/login';
    }
  };

  // Use StatusBadge component instead of custom status rendering

  if (loading) {
    return (
      <div className="space-y-5">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
        <div className="space-y-3">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      </div>
    );
  }

  const stats = {
    totalProjects: projects.length,
    totalTasks: tasks.length,
    activeTasks: tasks.filter(t => t.status === 'running').length,
    completedTasks: tasks.filter(t => t.status === 'done').length,
  };
  const taskOutcomeMetrics = outcomeRates?.task_outcomes;
  const showTaskOutcomes = Boolean(taskOutcomeMetrics && taskOutcomeMetrics.total > 0);
  const healthMetrics = showTaskOutcomes
    ? [
        {
          label: 'First-Pass',
          key: 'first_pass_success',
          color: 'text-emerald-400',
          unit: 'tasks',
        },
        {
          label: 'Recovered',
          key: 'recovered_success',
          color: 'text-sky-400',
          unit: 'tasks',
        },
        {
          label: 'Failed',
          key: 'final_failed',
          color: 'text-red-400',
          unit: 'tasks',
        },
        {
          label: 'In Progress',
          key: 'in_progress',
          color: 'text-amber-400',
          unit: 'tasks',
        },
      ]
    : [
        {
          label: 'First-Pass',
          key: 'first_pass_success',
          color: 'text-emerald-400',
          unit: 'sessions',
        },
        {
          label: 'Recovered',
          key: 'recovered_success',
          color: 'text-sky-400',
          unit: 'sessions',
        },
        {
          label: 'Actionable',
          key: 'failed_but_actionable',
          color: 'text-amber-400',
          unit: 'sessions',
        },
        {
          label: 'Stuck',
          key: 'stuck_or_manual_db_cleanup',
          color: 'text-red-400',
          unit: 'sessions',
        },
      ];
  const accountLabel = user?.name?.trim() || user?.email || '';
  const filteredProjects = projects.filter((project) => {
    const query = projectSearchQuery.trim().toLowerCase();
    if (!query) return true;

    return (
      project.name.toLowerCase().includes(query) ||
      project.description?.toLowerCase().includes(query) ||
      project.branch?.toLowerCase().includes(query) ||
      project.github_url?.toLowerCase().includes(query)
    );
  });

  return (
    <div>
      {/* Top bar */}
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

      {/* Stats Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Projects</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-white">{stats.totalProjects}</p>
            <GitBranch className="h-5 w-5 text-primary-500/60" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Tasks</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-white">{stats.totalTasks}</p>
            <FileText className="h-5 w-5 text-slate-400" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Running</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-primary-300">{stats.activeTasks}</p>
            <Activity className="h-5 w-5 text-primary-400" />
          </div>
        </div>

        <div className="bg-[color:var(--oc-surface)] rounded-lg p-4 border border-[color:var(--oc-border-soft)]">
          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-2">Completed</p>
          <div className="flex items-end justify-between">
            <p className="text-2xl font-semibold text-emerald-400">{stats.completedTasks}</p>
            <CheckCircle2 className="h-5 w-5 text-emerald-500" />
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-0 mb-5 border-b border-[color:var(--oc-border-soft)]">
        <button
          onClick={() => setActiveTab('overview')}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === 'overview'
              ? 'text-white border-primary-500'
              : 'text-slate-400 border-transparent hover:text-slate-200'
          }`}
        >
          Overview
        </button>
        <button
          onClick={() => setActiveTab('projects')}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === 'projects'
              ? 'text-white border-primary-500'
              : 'text-slate-400 border-transparent hover:text-slate-200'
          }`}
        >
          Projects
        </button>
        <button
          onClick={() => setActiveTab('tasks')}
          className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
            activeTab === 'tasks'
              ? 'text-white border-primary-500'
              : 'text-slate-400 border-transparent hover:text-slate-200'
          }`}
        >
          Tasks
        </button>
      </div>

        {/* Content */}
        {activeTab === 'overview' && (
          <div className="space-y-4">
            {/* System Health panel */}
            {outcomeRates && (
              <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)]">
                <div className="px-5 py-3 border-b border-[color:var(--oc-border-soft)] flex items-center justify-between">
                  <h2 className="text-sm font-medium text-white flex items-center gap-2">
                    <ShieldCheck className="h-4 w-4 text-slate-400" />
                    System Health
                  </h2>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                    outcomeRates.gate_pass
                      ? 'bg-emerald-900/40 text-emerald-400'
                      : 'bg-red-900/40 text-red-400'
                  }`}>
                    {outcomeRates.gate_pass ? 'Gate: PASS' : 'Gate: FAIL'}
                  </span>
                </div>
                <div className="px-5 py-4">
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    {healthMetrics.map(({ label, key, color, unit }) => {
                      const sourceRates = showTaskOutcomes
                        ? taskOutcomeMetrics?.rates
                        : outcomeRates.outcome_rates;
                      const sourceCounts = showTaskOutcomes
                        ? taskOutcomeMetrics?.counts
                        : outcomeRates.outcome_counts;
                      const rate = sourceRates?.[`${key}_rate`] ?? 0;
                      const count = sourceCounts?.[key] ?? 0;
                      return (
                        <div key={key} className="flex flex-col gap-0.5">
                          <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400">{label}</p>
                          <p className={`text-xl font-semibold ${color}`}>
                            {(rate * 100).toFixed(0)}%
                          </p>
                          <p className="text-xs text-slate-500">{count} {unit}</p>
                        </div>
                      );
                    })}
                  </div>
                  <p className="text-[10px] text-slate-500 mt-3">
                    {showTaskOutcomes
                      ? `${taskOutcomeMetrics?.total ?? 0} tasks analyzed · ${outcomeRates.sessions_analyzed} sessions sampled · ${outcomeRates.operator_review_count} operator reviews`
                      : `${outcomeRates.sessions_analyzed} sessions analyzed · ${outcomeRates.classified_sessions ?? 0} classified · ${outcomeRates.operator_review_count} operator reviews`}
                  </p>
                </div>
              </div>
            )}

            <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)]">
              <div className="px-5 py-3 border-b border-[color:var(--oc-border-soft)]">
                <h2 className="text-sm font-medium text-white">Recent Activity</h2>
              </div>
              <div className="px-5 py-3">
                {tasks.length === 0 ? (
                  <EmptyState
                    icon={Terminal}
                    title="No tasks yet"
                    description="Create a project to start orchestrating AI development tasks"
                  />
                ) : (
                  <div className="divide-y divide-[color:var(--oc-border-soft)]">
                    {tasks.slice(-5).reverse().map((task) => (
                      <div key={task.id} className="flex items-center justify-between py-3">
                        <div>
                          <p className="text-sm font-medium text-slate-200">{task.title}</p>
                          <p className="text-xs text-slate-400 mt-0.5">
                            {formatDistanceToNow(new Date(task.updated_at || task.created_at), { addSuffix: true })}
                          </p>
                        </div>
                        <StatusBadge status={task.status} />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {activeTab === 'projects' && (
          <div className="space-y-4">
            {projects.length > 0 && (
              <div className="flex justify-end">
                <div className="relative">
                  <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
                  <input
                    type="text"
                    placeholder="Search..."
                    value={projectSearchQuery}
                    onChange={(e) => setProjectSearchQuery(e.target.value)}
                    className="w-44 bg-[color:var(--oc-surface)] border border-[color:var(--oc-border-soft)] rounded-md py-1.5 pl-8 pr-3 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-[color:var(--oc-border)] hover:border-[color:var(--oc-border)]"
                  />
                </div>
              </div>
            )}
            {projects.length === 0 ? (
              <EmptyState
                icon={GitBranch}
                title="No projects yet"
                description="Create your first project to start orchestrating AI development tasks"
              />
            ) : filteredProjects.length === 0 ? (
              <EmptyState
                icon={GitBranch}
                title="No matching projects"
                description="Try adjusting your search query"
              />
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {filteredProjects.map((project) => (
                  <div key={project.id} className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-4 hover:border-[color:var(--oc-border)] transition-colors">
                    <div className="flex items-start justify-between mb-3">
                      <GitBranch className="h-4 w-4 text-primary-500/70 mt-0.5" />
                      <div className="flex gap-1.5">
                        {project.github_url && (
                          <a
                            href={project.github_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-slate-500 hover:text-slate-300 transition-colors"
                            title="View GitHub"
                          >
                            <ExternalLink className="h-3.5 w-3.5" />
                          </a>
                        )}
                        <button
                          onClick={() => handleDeleteProject(project.id)}
                          className="text-slate-500 hover:text-red-400 transition-colors"
                          title="Delete project"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                    <h3 className="text-sm font-semibold text-white mb-1">{project.name}</h3>
                    <div className="flex items-center justify-between text-xs text-slate-400">
                      <span>{project.branch}</span>
                      <span>{formatDistanceToNow(new Date(project.created_at), { addSuffix: true })}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {activeTab === 'tasks' && (
          <div>
            {tasks.length === 0 ? (
              <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-10 text-center">
                <FileText className="h-10 w-10 mx-auto mb-3 text-slate-500" />
                <p className="text-sm font-medium text-slate-300">No tasks yet</p>
                <p className="text-xs text-slate-500 mt-1">Create a project and add tasks to get started</p>
              </div>
            ) : (
              <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] divide-y divide-[color:var(--oc-border-soft)]">
                {tasks.map((task) => (
                  <div key={task.id} className="flex items-center justify-between gap-4 px-4 py-3">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-slate-200 truncate">{task.title}</p>
                      {task.description && (
                        <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">{task.description}</p>
                      )}
                      <p className="text-xs text-slate-500 mt-0.5">
                        {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                      </p>
                    </div>
                    <StatusBadge status={task.status} size="sm" />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      {/* Create Project Modal */}
      {showCreateProject && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-[color:var(--oc-shell)] rounded-lg border border-[color:var(--oc-border-soft)] p-5 w-full max-w-sm mx-4 shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">New Project</h3>
            <form onSubmit={handleCreateProject}>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Project Name
                  </label>
                  <input
                    type="text"
                    value={newProjectName}
                    onChange={(e) => {
                      setNewProjectName(e.target.value);
                    }}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border-soft)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 focus:border-primary-500"
                    placeholder="My Project"
                    autoFocus
                  />
                </div>
                <div className="flex gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setShowCreateProject(false)}
                    className="flex-1 bg-[color:var(--oc-surface)] hover:bg-[color:var(--oc-surface-raised)] text-slate-300 text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newProjectName.trim() || creatingProject}
                    className="flex-1 border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {creatingProject ? (
                      <>
                        <div className="h-3.5 w-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Creating...
                      </>
                    ) : (
                      'Create'
                    )}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default Dashboard;
