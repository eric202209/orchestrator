import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { projectsAPI, authAPI, tasksAPI } from '../api/client';
import type { Project, User, Task } from '../types/api';
import { 
  GitBranch, 
  LogOut, 
  Activity, 
  CheckCircle2,
  FileText,
  Terminal,
  Trash2
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

  const checkAuth = useCallback(async () => {
    const token = localStorage.getItem('access_token');
    if (!token) {
      console.log('No access token, redirecting to login');
      navigate('/login', { replace: true });
      setIsAuthChecked(true);
      return;
    }

    try {
      const response = await authAPI.getMe();
      setUser(response.data);
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      // Suppress timeout errors - they're expected during slow network
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        console.error('Failed to fetch user:', axiosError.message || error);
      }
      // Token might be expired, let the interceptor handle it
      localStorage.removeItem('access_token');
      localStorage.removeItem('refresh_token');
      navigate('/login', { replace: true });
      setIsAuthChecked(true);
      return;
    }
    setIsAuthChecked(true);
  }, [navigate]);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  // Only fetch projects after auth is confirmed
  useEffect(() => {
    if (isAuthChecked && user) {
      fetchProjects();
    } else if (isAuthChecked && !user) {
      setLoading(false);
    }
  }, [isAuthChecked, user]);

  const fetchProjects = async () => {
    try {
      const response = await projectsAPI.getAll();
      const projectsData = response.data;
      setProjects(projectsData);
      
      // Fetch tasks for all projects
      const allTasks: Task[] = [];
      for (const project of projectsData) {
        const tasksResponse = await tasksAPI.getByProject(project.id);
        allTasks.push(...tasksResponse.data);
      }
      setTasks(allTasks);
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      // Suppress timeout errors - they're expected during slow network
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        console.error('Failed to fetch projects:', axiosError.message || error);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) {
      return;
    }

    setCreatingProject(true);
    try {
      await projectsAPI.create({ 
        name: newProjectName,
        branch: 'main'
      });
      setNewProjectName('');
      setShowCreateProject(false);
      await fetchProjects();
    } catch (error) {
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

    try {
      await projectsAPI.delete(projectId);
      await fetchProjects();
    } catch (error) {
      console.error('Failed to delete project:', error);
      alert('Failed to delete project. Please try again.');
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    window.location.href = '/login';
  };

  // Use StatusBadge component instead of custom status rendering

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-900">
        <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex items-center justify-between h-16">
              <div className="flex items-center gap-2">
                <Skeleton className="h-6 w-6" />
                <Skeleton className="h-6 w-32" />
              </div>
              <div className="flex items-center gap-4">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 w-20" />
              </div>
            </div>
          </div>
        </nav>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
          <div className="space-y-4">
            <Skeleton className="h-8 w-48" />
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
            <Skeleton className="h-32 w-full" />
          </div>
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
  const accountLabel = user?.name?.trim() || user?.email || '';

  return (
    <div className="min-h-screen bg-slate-900">
      {/* Navbar */}
      <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-2">
              <Activity className="h-6 w-6 text-primary-500" />
              <span className="text-xl font-bold text-white">Orchestrator</span>
            </div>
            
            <div className="flex items-center gap-4">
              {accountLabel && (
                <span className="text-sm text-slate-400">{accountLabel}</span>
              )}
              <button
                onClick={handleLogout}
                className="flex items-center gap-2 text-sm text-slate-400 hover:text-white transition-colors"
              >
                <LogOut className="h-4 w-4" />
                Logout
              </button>
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Stats Grid */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
          <div className="bg-slate-800/50 backdrop-blur rounded-xl p-6 border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400 mb-1">Total Projects</p>
                <p className="text-3xl font-bold text-white">{stats.totalProjects}</p>
              </div>
              <GitBranch className="h-8 w-8 text-primary-500" />
            </div>
          </div>

          <div className="bg-slate-800/50 backdrop-blur rounded-xl p-6 border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400 mb-1">Total Tasks</p>
                <p className="text-3xl font-bold text-white">{stats.totalTasks}</p>
              </div>
              <FileText className="h-8 w-8 text-blue-500" />
            </div>
          </div>

          <div className="bg-slate-800/50 backdrop-blur rounded-xl p-6 border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400 mb-1">Active Tasks</p>
                <p className="text-3xl font-bold text-blue-400">{stats.activeTasks}</p>
              </div>
              <Activity className="h-8 w-8 text-blue-400 animate-pulse" />
            </div>
          </div>

          <div className="bg-slate-800/50 backdrop-blur rounded-xl p-6 border border-slate-700">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-slate-400 mb-1">Completed</p>
                <p className="text-3xl font-bold text-green-400">{stats.completedTasks}</p>
              </div>
              <CheckCircle2 className="h-8 w-8 text-green-400" />
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 mb-6 border-b border-slate-700">
          <button
            onClick={() => setActiveTab('overview')}
            className={`px-4 py-2 font-medium transition-colors ${
              activeTab === 'overview'
                ? 'text-primary-400 border-b-2 border-primary-400'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            Overview
          </button>
          <button
            onClick={() => setActiveTab('projects')}
            className={`px-4 py-2 font-medium transition-colors ${
              activeTab === 'projects'
                ? 'text-primary-400 border-b-2 border-primary-400'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            Projects
          </button>
          <button
            onClick={() => setActiveTab('tasks')}
            className={`px-4 py-2 font-medium transition-colors ${
              activeTab === 'tasks'
                ? 'text-primary-400 border-b-2 border-primary-400'
                : 'text-slate-400 hover:text-white'
            }`}
          >
            Tasks
          </button>
        </div>

        {/* Content */}
        {activeTab === 'overview' && (
          <div className="space-y-6">
            {/* Recent Activity */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700">
              <div className="p-6 border-b border-slate-700">
                <h2 className="text-lg font-semibold text-white">Recent Activity</h2>
              </div>
              <div className="p-6">
                {tasks.length === 0 ? (
                  <EmptyState
                    icon={Terminal}
                    title="No tasks yet"
                    description="Create a project to start orchestrating AI development tasks"
                  />
                ) : (
                  <div className="space-y-4">
                    {tasks.slice(-5).reverse().map((task) => (
                      <div key={task.id} className="flex items-center justify-between py-3">
                        <div className="flex items-center gap-3">
                          <div>
                            <p className="font-medium text-white">{task.title}</p>
                            <p className="text-sm text-slate-400">
                              {formatDistanceToNow(new Date(task.updated_at || task.created_at), { addSuffix: true })}
                            </p>
                          </div>
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
            <h2 className="text-lg font-semibold text-white">Projects</h2>

            {projects.length === 0 ? (
              <EmptyState
                icon={GitBranch}
                title="No projects yet"
                description="Create your first project to start orchestrating AI development tasks"
              />
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {projects.map((project) => (
                  <div key={project.id} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-primary-500/50 transition-all">
                    <div className="flex items-start justify-between mb-4">
                      <GitBranch className="h-6 w-6 text-primary-500" />
                      <div className="flex gap-2">
                        {project.github_url && (
                          <a
                            href={project.github_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-slate-400 hover:text-primary-400 transition-colors"
                            title="View GitHub"
                          >
                            <ExternalLink className="h-4 w-4" />
                          </a>
                        )}
                        <button
                          onClick={() => handleDeleteProject(project.id)}
                          className="text-slate-400 hover:text-red-400 transition-colors"
                          title="Delete project"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </div>
                    <h3 className="text-lg font-semibold text-white mb-2">{project.name}</h3>
                    {project.description && (
                      <p className="text-sm text-slate-400 mb-4 line-clamp-2">{project.description}</p>
                    )}
                    <div className="flex items-center justify-between text-sm text-slate-400">
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
          <div className="space-y-4">
            <h2 className="text-lg font-semibold text-white">All Tasks</h2>
            
            {tasks.length === 0 ? (
              <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-12 text-center">
                <FileText className="h-16 w-16 mx-auto mb-4 text-slate-600" />
                <h3 className="text-xl font-semibold text-white mb-2">No tasks yet</h3>
                <p className="text-slate-400">Create a project and add tasks to get started</p>
              </div>
            ) : (
              <div className="space-y-2">
                {tasks.map((task) => (
                  <div key={task.id} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-4 hover:border-slate-600 transition-all">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex min-w-0 flex-1 items-start gap-3">
                        <div className="p-2 rounded-lg text-blue-400 bg-blue-400/10">
                          <Activity className="h-5 w-5" />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="font-medium text-white">{task.title}</p>
                          {task.description && (
                            <p className="text-sm text-slate-400 mt-1 line-clamp-1">{task.description}</p>
                          )}
                          <p className="text-xs text-slate-500 mt-1">
                            {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                          </p>
                        </div>
                      </div>
                      <div className="shrink-0 pt-0.5">
                        <StatusBadge status={task.status} size="sm" />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Create Project Modal */}
      {showCreateProject && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 w-full max-w-md mx-4">
            <h3 className="text-lg font-semibold text-white mb-4">Create New Project</h3>
            <form onSubmit={handleCreateProject}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Project Name
                  </label>
                  <input
                    type="text"
                    value={newProjectName}
                    onChange={(e) => {
                      console.log('Input changed:', e.target.value);
                      setNewProjectName(e.target.value);
                    }}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
                    placeholder="My Awesome Project"
                    autoFocus
                  />
                </div>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => setShowCreateProject(false)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newProjectName.trim() || creatingProject}
                    className="flex-1 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {creatingProject ? (
                      <>
                        <div className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
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

function ExternalLink({ className }: { className?: string }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
    </svg>
  );
}

export default Dashboard;
