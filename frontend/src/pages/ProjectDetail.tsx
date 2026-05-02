import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { projectsAPI, tasksAPI, sessionsAPI } from '../api/client';
import type { Project, Task, Session } from '../types/api';
import { ProjectPlannerPanel } from '../components/ProjectPlannerPanel';
import {
  GitBranch,
  FileText,
  XCircle,
  ArrowLeft,
  ExternalLink,
  Trash2,
  Terminal,
  Activity,
  Clock,
  Plus
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { StatusBadge, EmptyState } from '../components/ui';

function ProjectDetail() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const id = projectId;
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [workspaceOverview, setWorkspaceOverview] = useState<{
    counts: Record<string, number>;
    baseline: {
      exists: boolean;
      path?: string | null;
      file_count: number;
      promoted_task_count: number;
    };
    promoted_tasks: Array<{ id: number; title: string; promoted_at?: string | null }>;
    ready_task_ids: number[];
  } | null>(null);
  const [activeTab, setActiveTab] = useState<'sessions' | 'tasks' | 'planner'>('tasks');
  const [loading, setLoading] = useState(true);
  const [showCreateTask, setShowCreateTask] = useState(false);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDescription, setTaskDescription] = useState('');
  const [taskSteps, setTaskSteps] = useState('');
  const [generatingSteps, setGeneratingSteps] = useState(false);
  const [creatingTask, setCreatingTask] = useState(false);
  const [editingTaskId, setEditingTaskId] = useState<number | null>(null);
  const [editTitle, setEditTitle] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editSteps, setEditSteps] = useState('');
  const [updatingTask, setUpdatingTask] = useState(false);
  const [savingGithubUrl, setSavingGithubUrl] = useState(false);
  const [editingProjectMeta, setEditingProjectMeta] = useState(false);
  const [projectDescriptionDraft, setProjectDescriptionDraft] = useState('');
  const [projectRulesDraft, setProjectRulesDraft] = useState('');
  const [savingProjectMeta, setSavingProjectMeta] = useState(false);
  const [rebuildingBaseline, setRebuildingBaseline] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    if (!id) {
      setError('Invalid project ID');
      setLoading(false);
      return;
    }

    const loadProjectData = async () => {
      try {
        const [projectRes, tasksRes, sessionsRes] = await Promise.all([
          projectsAPI.getById(Number(id)),
          tasksAPI.getByProject(Number(id)),
          sessionsAPI.getByProject(Number(id))
        ]);
        const workspaceRes = await projectsAPI.getWorkspaceOverview(Number(id));

        setProject(projectRes.data);
        setProjectDescriptionDraft(projectRes.data.description || '');
        setProjectRulesDraft(projectRes.data.project_rules || '');
        setTasks(tasksRes.data || []);
        setSessions(sessionsRes.data || []);
        setWorkspaceOverview(workspaceRes.data || null);
      } catch (err) {
        console.error('Failed to load project data:', err);
        setError(err instanceof Error ? err.message : 'Failed to load project data');
        navigate('/projects');
      } finally {
        setLoading(false);
      }
    };

    loadProjectData();
  }, [id, navigate]);

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="text-center">
          <XCircle className="h-10 w-10 text-red-500 mx-auto mb-3" />
          <h2 className="text-base font-semibold text-white mb-2">Error Loading Project</h2>
          <p className="text-sm text-slate-400 mb-4">{error}</p>
          <button
            onClick={() => navigate('/projects')}
            className="bg-sky-600 hover:bg-sky-500 text-white px-4 py-2 rounded-md text-sm transition-colors"
          >
            Back to Projects
          </button>
        </div>
      </div>
    );
  }

  const fetchTasks = async () => {
    if (!id) return;
    try {
      const [response, workspaceResponse] = await Promise.all([
        tasksAPI.getByProject(Number(id)),
        projectsAPI.getWorkspaceOverview(Number(id)),
      ]);
      setTasks(response.data || []);
      setWorkspaceOverview(workspaceResponse.data || null);
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    }
  };

  const getWorkspaceBadgeClass = (status?: string | null) => {
    switch (status) {
      case 'promoted':
        return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
      case 'ready':
        return 'border-sky-500/30 bg-sky-500/10 text-sky-300';
      case 'changes_requested':
        return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
      case 'blocked':
        return 'border-red-500/30 bg-red-500/10 text-red-300';
      case 'in_progress':
        return 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300';
      default:
        return 'border-slate-600 bg-slate-700/40 text-slate-300';
    }
  };

  const formatWorkspaceStatus = (status?: string | null) =>
    (status || 'not_created').replace(/_/g, ' ');

  const handlePromoteTask = async (task: Task) => {
    const note = window.prompt('Optional promotion note for this task workspace:', task.promotion_note || '');
    if (note === null) return;
    try {
      const response = await tasksAPI.promoteWorkspace(task.id, note || undefined);
      setTasks((current) => current.map((item) => (item.id === task.id ? response.data : item)));
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
    } catch (error) {
      console.error('Failed to promote task workspace:', error);
      alert('Failed to promote task workspace. Please try again.');
    }
  };

  const handleRequestChanges = async (task: Task) => {
    const note = window.prompt('Describe what still needs to change before promotion:', task.promotion_note || '');
    if (!note) return;
    try {
      const response = await tasksAPI.requestWorkspaceChanges(task.id, note);
      setTasks((current) => current.map((item) => (item.id === task.id ? response.data : item)));
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
    } catch (error) {
      console.error('Failed to mark task workspace for changes:', error);
      alert('Failed to update workspace review state. Please try again.');
    }
  };

  const handleRebuildBaseline = async () => {
    if (!id) return;
    if (!window.confirm('Rebuild the project baseline from all promoted task workspaces?')) {
      return;
    }

    try {
      setRebuildingBaseline(true);
      const result = await projectsAPI.rebuildBaseline(Number(id));
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
      alert(
        `Baseline rebuilt with ${result.data.files_copied} files from ${result.data.promoted_task_count} promoted task(s).`
      );
    } catch (error) {
      console.error('Failed to rebuild project baseline:', error);
      alert('Failed to rebuild the project baseline. Please try again.');
    } finally {
      setRebuildingBaseline(false);
    }
  };

  const generateStepsFromDescription = async (description: string) => {
    setGeneratingSteps(true);
    try {
      const response = await sessionsAPI.generateSteps({
        task_name: taskTitle || 'Task',
        description,
      });
      setTaskSteps(JSON.stringify(response.data, null, 2));
    } catch (error) {
      console.error('Failed to generate steps:', error);
      alert('Failed to auto-generate steps. You can manually edit the JSON below.');
    } finally {
      setGeneratingSteps(false);
    }
  };

  const handleCreateTask = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!taskTitle.trim() || !id) return;

    setCreatingTask(true);
    const tempId = -Date.now();
    const now = new Date().toISOString();
    const optimisticTask: Task = {
      id: tempId,
      project_id: Number(id),
      title: taskTitle.trim(),
      description: taskDescription || null,
      status: 'pending',
      execution_profile: 'full_lifecycle',
      priority: 0,
      steps: taskSteps.trim() ? taskSteps : null,
      current_step: 0,
      error_message: null,
      workspace_status: 'not_created',
      promotion_note: null,
      promoted_at: null,
      created_at: now,
      updated_at: now,
      started_at: null,
      completed_at: null,
      session_id: null,
      task_subfolder: null,
    };
    setTasks((current) => [optimisticTask, ...current]);
    setTaskTitle('');
    setTaskDescription('');
    setTaskSteps('');
    setShowCreateTask(false);

    try {
      const payload: {
        project_id: number;
        title: string;
        description?: string;
        steps?: string;
      } = {
        project_id: Number(id),
        title: taskTitle,
        description: taskDescription || undefined,
      };

      if (taskSteps.trim()) {
        payload.steps = taskSteps;
      }

      const response = await tasksAPI.create(payload);
      setTasks((current) =>
        current.map((task) => (task.id === tempId ? response.data : task))
      );
    } catch (error) {
      setTasks((current) => current.filter((task) => task.id !== tempId));
      setTaskTitle(optimisticTask.title);
      setTaskDescription(optimisticTask.description || '');
      setTaskSteps(optimisticTask.steps || '');
      setShowCreateTask(true);
      console.error('Failed to create task:', error);
      alert('Failed to create task. Please try again.');
    } finally {
      setCreatingTask(false);
    }
  };

  const handleDeleteTask = async (taskId: number) => {
    if (!confirm('Are you sure you want to delete this task? This cannot be undone.')) {
      return;
    }

    const previousTasks = tasks;
    setTasks((current) => current.filter((task) => task.id !== taskId));
    try {
      await tasksAPI.delete(taskId);
    } catch (error) {
      setTasks(previousTasks);
      console.error('Failed to delete task:', error);
      alert('Failed to delete task. Please try again.');
    }
  };

  const handleRerunTask = async (task: Task) => {
    if (task.status === 'running') return;
    try {
      await tasksAPI.retry(task.id);
      await fetchTasks();
      alert(task.status === 'done' ? 'Task queued to run again' : 'Task queued');
    } catch (error) {
      console.error('Failed to rerun task:', error);
      alert('Failed to queue the task. Please try again.');
    }
  };

  const startEditTask = (task: Task) => {
    setEditingTaskId(task.id);
    setEditTitle(task.title);
    setEditDescription(task.description || '');
    setEditSteps(task.steps || '');
  };

  const handleUpdateTask = async (taskId: number) => {
    const trimmedTitle = editTitle.trim();
    if (!trimmedTitle) return;

    setUpdatingTask(true);
    const previousTasks = tasks;
    setTasks((currentTasks) =>
      currentTasks.map((task) =>
        task.id === taskId
          ? {
              ...task,
              title: trimmedTitle,
              description: editDescription || null,
              steps: editSteps || null,
            }
          : task
      )
    );
    setEditingTaskId(null);

    try {
      const response = await tasksAPI.update(taskId, {
        title: trimmedTitle,
        description: editDescription,
        steps: editSteps,
      });

      setTasks((currentTasks) =>
        currentTasks.map((task) =>
          task.id === taskId ? response.data : task
        )
      );
    } catch (error) {
      setTasks(previousTasks);
      setEditingTaskId(taskId);
      console.error('Failed to update task:', error);
      alert('Failed to update task. Please try again.');
    } finally {
      setUpdatingTask(false);
    }
  };

  const handleDeleteSession = async (sessionId: number) => {
    if (!confirm('Delete this session? This cannot be undone.')) {
      return;
    }

    const previousSessions = sessions;
    setSessions((current) => current.filter((session) => session.id !== sessionId));
    try {
      await sessionsAPI.delete(sessionId);
      alert('Session deleted');
    } catch (error) {
      setSessions(previousSessions);
      console.error('Failed to delete session:', error);
      alert('Failed to delete session. Please try again.');
    }
  };

  const handleUpdateGithubUrl = async () => {
    if (!project) return;

    const currentValue = project.github_url || '';
    const nextValue = window.prompt(
      'Enter GitHub repository URL. Leave blank to remove the current link.',
      currentValue
    );

    if (nextValue === null) return;

    const trimmedValue = nextValue.trim();
    if (trimmedValue && !/^https?:\/\/.+/i.test(trimmedValue)) {
      alert('Please enter a valid GitHub URL starting with http:// or https://');
      return;
    }

    setSavingGithubUrl(true);
    try {
      const response = await projectsAPI.update(project.id, {
        github_url: trimmedValue || null,
      });
      setProject(response.data);
      alert(trimmedValue ? 'GitHub repository linked' : 'GitHub repository link removed');
    } catch (error) {
      console.error('Failed to update GitHub repository URL:', error);
      alert('Failed to update GitHub repository link. Please try again.');
    } finally {
      setSavingGithubUrl(false);
    }
  };

  const handleSaveProjectMeta = async () => {
    if (!project) return;

    setSavingProjectMeta(true);
    try {
      const response = await projectsAPI.update(project.id, {
        description: projectDescriptionDraft.trim() || null,
        project_rules: projectRulesDraft.trim() || null,
      });
      setProject(response.data);
      setProjectDescriptionDraft(response.data.description || '');
      setProjectRulesDraft(response.data.project_rules || '');
      setEditingProjectMeta(false);
    } catch (error) {
      console.error('Failed to update project metadata:', error);
      alert('Failed to update project brief/rules. Please try again.');
    } finally {
      setSavingProjectMeta(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="h-8 w-8 border-2 border-sky-500/30 border-t-sky-500 rounded-full animate-spin" />
      </div>
    );
  }

  if (!project) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <p className="text-sm text-white">Project not found</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link to="/projects" className="text-slate-400 hover:text-white transition-colors">
            <ArrowLeft className="h-4 w-4" />
          </Link>
          <h1 className="text-lg font-semibold text-white">{project.name}</h1>
          <span className="flex items-center gap-1 text-xs text-slate-500">
            <GitBranch className="h-3.5 w-3.5" />
            {project.branch}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {project.github_url && (
            <a
              href={project.github_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-slate-400 hover:text-slate-200 transition-colors"
              title={project.github_url}
            >
              <ExternalLink className="h-4 w-4" />
            </a>
          )}
          <button
            onClick={handleUpdateGithubUrl}
            disabled={savingGithubUrl}
            className="text-xs text-slate-400 hover:text-slate-200 transition-colors disabled:opacity-50"
          >
            {project.github_url ? 'Edit Repo' : 'Link Repo'}
          </button>
          <button
            onClick={() => {
              setEditingProjectMeta((current) => !current);
              setProjectDescriptionDraft(project.description || '');
              setProjectRulesDraft(project.project_rules || '');
            }}
            className="text-xs text-slate-400 hover:text-slate-200 transition-colors"
          >
            {editingProjectMeta ? 'Close Brief' : 'Edit Brief'}
          </button>
          {tasks.length > 0 && (
            <button
              onClick={async () => {
                if (!confirm('Delete all tasks in this project? This cannot be undone.')) return;
                try {
                  await Promise.all(tasks.map(task => tasksAPI.delete(task.id)));
                  alert('All tasks deleted');
                  fetchTasks();
                } catch (error) {
                  console.error('Failed to delete all tasks:', error);
                  alert('Failed to delete all tasks. Please try again.');
                }
              }}
              className="flex items-center gap-1 text-xs text-red-400 hover:text-red-300 transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete All
            </button>
          )}
        </div>
      </div>

      {/* Project Brief */}
      <div className="rounded-lg border border-slate-700 bg-slate-800 p-5">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Project Brief</h2>
            <p className="mt-0.5 text-xs text-slate-500">Persistent project context for planning and execution.</p>
          </div>
        </div>
        {editingProjectMeta ? (
          <div className="space-y-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-400">Description</label>
              <textarea
                value={projectDescriptionDraft}
                onChange={(e) => setProjectDescriptionDraft(e.target.value)}
                className="min-h-[80px] w-full resize-y rounded-md border border-slate-600 bg-slate-950 px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500"
                placeholder="Project brief, scope, expected outcome..."
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-400">Rules</label>
              <textarea
                value={projectRulesDraft}
                onChange={(e) => setProjectRulesDraft(e.target.value)}
                className="min-h-[96px] w-full resize-y rounded-md border border-slate-600 bg-slate-950 px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500"
                placeholder="Constraints, must-follow instructions, architecture rules..."
              />
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setEditingProjectMeta(false);
                  setProjectDescriptionDraft(project.description || '');
                  setProjectRulesDraft(project.project_rules || '');
                }}
                className="rounded-md bg-slate-700 px-3 py-1.5 text-sm text-white transition-colors hover:bg-slate-600"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSaveProjectMeta}
                disabled={savingProjectMeta}
                className="rounded-md bg-sky-600 px-3 py-1.5 text-sm text-white transition-colors hover:bg-sky-500 disabled:opacity-50"
              >
                {savingProjectMeta ? 'Saving...' : 'Save Brief'}
              </button>
            </div>
          </div>
        ) : (
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">Description</h3>
              <p className="whitespace-pre-wrap text-sm text-slate-300">
                {project.description || 'No project description yet.'}
              </p>
            </div>
            <div>
              <h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-slate-500">Rules</h3>
              <p className="whitespace-pre-wrap text-sm text-slate-300">
                {project.project_rules || 'No project rules yet.'}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* Meta row */}
      <div className="flex items-center gap-4 text-xs text-slate-500">
        {project.github_url && (
          <span className="truncate max-w-[280px]">Repo: {project.github_url}</span>
        )}
        <span>{formatDistanceToNow(new Date(project.created_at), { addSuffix: true })}</span>
        <span className="flex items-center gap-1">
          <FileText className="h-3 w-3" />
          {tasks.length} tasks
        </span>
        <span className="flex items-center gap-1">
          <Terminal className="h-3 w-3" />
          {sessions.length} sessions
        </span>
      </div>

      {/* Tabs */}
      <div className="flex gap-0 border-b border-slate-700">
        {[
          { key: 'tasks', label: 'Tasks' },
          { key: 'planner', label: 'Project Architect' },
          { key: 'sessions', label: 'AI Sessions' },
        ].map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveTab(tab.key as 'sessions' | 'tasks' | 'planner')}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.key
                ? 'text-white border-sky-500'
                : 'text-slate-500 border-transparent hover:text-slate-300'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Planner Tab */}
      {activeTab === 'planner' && (
        <ProjectPlannerPanel
          project={project}
          onTasksCommitted={(createdTasks) => {
            setTasks((currentTasks) => [...createdTasks, ...currentTasks]);
            setActiveTab('tasks');
          }}
        />
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-white">AI Sessions</h2>
            <button
              onClick={() => navigate(`/sessions/new?project_id=${id}`)}
              className="flex items-center gap-1.5 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-1.5 rounded-md transition-colors"
            >
              <Plus className="h-4 w-4" />
              New Session
            </button>
          </div>

          {sessions.length === 0 ? (
            <EmptyState
              icon={Terminal}
              title="No AI sessions yet"
              description="Create a session to start orchestrating development tasks"
              action={{
                label: 'New Session',
                onClick: () => navigate(`/sessions/new?project_id=${id}`)
              }}
            />
          ) : (
            <div className="bg-slate-800 rounded-lg border border-slate-700 divide-y divide-slate-700/60">
              {sessions.map((session) => (
                <div key={session.id} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-700/40 transition-colors">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-slate-200">{session.name}</p>
                    {session.description && (
                      <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">{session.description}</p>
                    )}
                    <div className="flex items-center gap-3 mt-1 text-xs text-slate-500">
                      <span className="flex items-center gap-1">
                        <Clock className="h-3 w-3" />
                        {formatDistanceToNow(new Date(session.created_at), { addSuffix: true })}
                      </span>
                      {session.started_at && <span>Started</span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusBadge status={session.status} size="sm" />
                    <button
                      onClick={() => handleDeleteSession(session.id)}
                      className="text-slate-500 hover:text-red-400 transition-colors"
                      title="Delete session"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Tasks Tab */}
      {activeTab === 'tasks' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-white">Tasks</h2>
            <button
              onClick={() => setShowCreateTask(true)}
              className="flex items-center gap-1.5 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-1.5 rounded-md transition-colors"
            >
              <Plus className="h-4 w-4" />
              Add Task
            </button>
          </div>

          {tasks.length === 0 ? (
            <EmptyState
              icon={FileText}
              title="No tasks yet"
              description="Create your first task to get started with AI development"
              action={{
                label: 'Create Task',
                onClick: () => setShowCreateTask(true)
              }}
            />
          ) : (
            <div className="space-y-3">
              {workspaceOverview && (
                <div className="rounded-lg border border-slate-700 bg-slate-800 p-4">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between mb-3">
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Canonical Baseline</p>
                      <p className="mt-1 text-sm text-slate-300">
                        {workspaceOverview.baseline.exists
                          ? `${workspaceOverview.baseline.file_count} files built from ${workspaceOverview.baseline.promoted_task_count} promoted task(s)`
                          : 'No canonical baseline yet'}
                      </p>
                      {workspaceOverview.baseline.path && (
                        <p className="mt-0.5 text-xs text-slate-500">{workspaceOverview.baseline.path}</p>
                      )}
                    </div>
                    <button
                      onClick={handleRebuildBaseline}
                      disabled={rebuildingBaseline || (workspaceOverview.baseline.promoted_task_count || 0) === 0}
                      className="rounded-md bg-slate-700 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-600 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {rebuildingBaseline ? 'Rebuilding...' : 'Rebuild Baseline'}
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Ready</p>
                      <p className="mt-1 text-lg font-semibold text-sky-300">{workspaceOverview.counts.ready || 0}</p>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Promoted</p>
                      <p className="mt-1 text-lg font-semibold text-emerald-300">{workspaceOverview.counts.promoted || 0}</p>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Changes Requested</p>
                      <p className="mt-1 text-lg font-semibold text-amber-300">{workspaceOverview.counts.changes_requested || 0}</p>
                    </div>
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Blocked</p>
                      <p className="mt-1 text-lg font-semibold text-red-300">{workspaceOverview.counts.blocked || 0}</p>
                    </div>
                  </div>
                </div>
              )}

              <div className="bg-slate-800 rounded-lg border border-slate-700 divide-y divide-slate-700/60">
                {tasks.map((task) => (
                  <div key={task.id} className="px-4 py-4 hover:bg-slate-700/30 transition-colors">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3 flex-1 min-w-0">
                        <div className="p-1.5 rounded-md text-blue-400 bg-blue-400/10 mt-0.5 shrink-0">
                          <Activity className="h-4 w-4" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <h3 className="text-sm font-medium text-white">{task.title}</h3>
                          {task.description && (
                            <p className="text-xs text-slate-400 mt-0.5 line-clamp-2">{task.description}</p>
                          )}
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${getWorkspaceBadgeClass(task.workspace_status)}`}>
                              {formatWorkspaceStatus(task.workspace_status)}
                            </span>
                            {task.task_subfolder && (
                              <span className="rounded-full border border-slate-600 px-2.5 py-0.5 text-xs text-slate-400">
                                {task.task_subfolder}
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-3 mt-2 text-xs text-slate-500">
                            <span className="flex items-center gap-1">
                              <Clock className="h-3 w-3" />
                              {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                            </span>
                            {task.current_step > 0 && <span>Step {task.current_step}</span>}
                          </div>
                          {task.promotion_note && (
                            <p className="mt-2 text-xs text-slate-500">
                              Review note: {task.promotion_note}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {task.status !== 'running' && (
                          <button
                            onClick={() => handleRerunTask(task)}
                            className="rounded-md bg-blue-600/80 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-500"
                          >
                            {task.status === 'done' ? 'Run Again' : 'Run'}
                          </button>
                        )}
                        {task.status === 'done' && task.task_subfolder && task.workspace_status !== 'promoted' && (
                          <button
                            onClick={() => handlePromoteTask(task)}
                            className="rounded-md bg-emerald-600/80 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-emerald-500"
                          >
                            Promote
                          </button>
                        )}
                        {task.task_subfolder && task.workspace_status !== 'promoted' && (
                          <button
                            onClick={() => handleRequestChanges(task)}
                            className="rounded-md bg-amber-600/80 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-amber-500"
                          >
                            Changes
                          </button>
                        )}
                        <button
                          onClick={() => startEditTask(task)}
                          className="text-slate-400 hover:text-slate-200 transition-colors"
                          title="Edit task"
                        >
                          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                          </svg>
                        </button>
                        <StatusBadge status={task.status} size="sm" />
                        <button
                          onClick={() => handleDeleteTask(task.id)}
                          className="text-slate-500 hover:text-red-400 transition-colors"
                          title="Delete task"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Create Task Modal */}
      {showCreateTask && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-5 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">Create New Task</h3>
            <form onSubmit={handleCreateTask}>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Task Title <span className="text-red-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500"
                    placeholder="e.g., Build a simple Vite website"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Description
                  </label>
                  <textarea
                    value={taskDescription}
                    onChange={(e) => setTaskDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 resize-none"
                    placeholder="Describe what needs to be done..."
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="block text-xs font-medium text-slate-400">
                      Step-by-Step Plan (JSON)
                    </label>
                    <button
                      type="button"
                      onClick={() => generateStepsFromDescription(taskDescription)}
                      disabled={generatingSteps || !taskDescription.trim()}
                      className="text-xs bg-sky-600/20 hover:bg-sky-600/30 text-sky-400 px-2.5 py-1 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
                    >
                      {generatingSteps ? (
                        <>
                          <div className="h-3 w-3 border-2 border-sky-400/30 border-t-sky-400 rounded-full animate-spin" />
                          Generating...
                        </>
                      ) : (
                        <>
                          <Terminal className="h-3 w-3" />
                          Auto-generate with AI
                        </>
                      )}
                    </button>
                  </div>
                  <textarea
                    value={taskSteps}
                    onChange={(e) => setTaskSteps(e.target.value)}
                    rows={8}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 font-mono resize-none"
                    placeholder='{"task_name": "...", "description": "...", "step_by_step_plan": [{"step": 1, "title": "...", "details": "..."}]}'
                  />
                  <p className="text-xs text-slate-500 mt-1">
                    Leave empty to auto-generate, or edit manually. Required for task execution.
                  </p>
                </div>
                <div className="flex gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setShowCreateTask(false)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!taskTitle.trim() || creatingTask}
                    className="flex-1 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {creatingTask ? (
                      <>
                        <div className="h-3.5 w-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Creating...
                      </>
                    ) : (
                      'Create Task'
                    )}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Task Modal */}
      {editingTaskId && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-5 w-full max-w-md mx-4 shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">Edit Task</h3>
            <form onSubmit={(e) => { e.preventDefault(); handleUpdateTask(editingTaskId); }}>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Task Title *
                  </label>
                  <input
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500"
                    placeholder="e.g., Design homepage"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Description
                  </label>
                  <textarea
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 resize-none"
                    placeholder="Describe what needs to be done..."
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Step-by-Step Plan (JSON)
                  </label>
                  <textarea
                    value={editSteps}
                    onChange={(e) => setEditSteps(e.target.value)}
                    rows={4}
                    className="w-full bg-slate-950 border border-slate-600 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 resize-none font-mono"
                    placeholder='[{"step": 1, "action": "Create component"}, {"step": 2, "action": "Add styling"}]'
                  />
                </div>
                <div className="flex gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setEditingTaskId(null)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!editTitle.trim() || updatingTask}
                    className="flex-1 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {updatingTask ? (
                      <>
                        <div className="h-3.5 w-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Saving...
                      </>
                    ) : (
                      'Save Changes'
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

export default ProjectDetail;
