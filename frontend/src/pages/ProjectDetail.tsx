import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { projectsAPI, tasksAPI, sessionsAPI } from '../api/client';
import type { Project, Task, Session } from '../types/api';
import { 
  GitBranch, 
  FileText,
  XCircle,
  ArrowLeft,
  ExternalLink,
  Trash2,
  Terminal,
  Activity,
  Clock
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { StatusBadge, EmptyState } from '../components/ui';

function ProjectDetail() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const id = projectId; // Keep consistent naming
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
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
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console.log('🔍 ProjectDetail useEffect triggered, id:', id);
    setError(null);
    if (!id) {
      setError('Invalid project ID');
      setLoading(false);
      return;
    }
    
    const loadProjectData = async () => {
      try {
        console.log('📡 Fetching project data for ID:', id);
        // Fetch project, tasks, and sessions in parallel
        const [projectRes, tasksRes, sessionsRes] = await Promise.all([
          projectsAPI.getById(Number(id)),
          tasksAPI.getByProject(Number(id)),
          sessionsAPI.getByProject(Number(id))
        ]);
        
        console.log('✅ Data loaded successfully');
        setProject(projectRes.data);
        setTasks(tasksRes.data || []);
        setSessions(sessionsRes.data || []);
      } catch (err) {
        console.error('❌ Failed to load project data:', err);
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
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="text-center">
          <XCircle className="h-16 w-16 text-red-500 mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-white mb-2">Error Loading Project</h2>
          <p className="text-slate-400 mb-4">{error}</p>
          <button
            onClick={() => navigate('/projects')}
            className="bg-primary-500 hover:bg-primary-600 text-white px-6 py-2 rounded-lg transition-colors"
          >
            Back to Projects
          </button>
        </div>
      </div>
    );
  }

  // Refresh tasks and sessions
  const fetchTasks = async () => {
    if (!id) return;
    try {
      const response = await tasksAPI.getByProject(Number(id));
      setTasks(response.data || []);
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    }
  };
  const generateStepsFromDescription = async (description: string) => {
    setGeneratingSteps(true);
    try {
      // Use OpenClaw AI to generate steps (uses LOCALHOST from .env)
      const response = await fetch(`${import.meta.env.VITE_API_URL}/generate-steps`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_name: taskTitle || 'Task',
          description: description,
        }),
      });
      
      if (!response.ok) throw new Error('Failed to generate steps');
      
      const data = await response.json();
      setTaskSteps(JSON.stringify(data, null, 2));
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
      priority: 0,
      steps: taskSteps.trim() ? taskSteps : null,
      current_step: 0,
      error_message: null,
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
      
      // Include steps if provided (either auto-generated or manually entered)
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

    console.log('Deleting task:', taskId);
    const previousTasks = tasks;
    setTasks((current) => current.filter((task) => task.id !== taskId));
    try {
      // Use DELETE instead of PATCH with cancelled status
      await tasksAPI.delete(taskId);
      console.log('Task deleted successfully');
    } catch (error) {
      setTasks(previousTasks);
      console.error('Failed to delete task:', error);
      if (
        typeof error === 'object' &&
        error !== null &&
        'response' in error &&
        typeof error.response === 'object' &&
        error.response !== null &&
        'data' in error.response
      ) {
        console.error('Error response:', error.response.data);
      }
      alert('Failed to delete task. Please try again.');
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

    if (nextValue === null) {
      return;
    }

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

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="h-8 w-8 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin" />
      </div>
    );
  }

  if (!project) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <p className="text-white">Project not found</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-900">
      {/* Navbar */}
      <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-4">
              <Link
                to="/projects"
                className="text-slate-400 hover:text-white transition-colors"
                title="Back to Projects"
              >
                <ArrowLeft className="h-5 w-5" />
              </Link>
              <div className="flex items-center gap-2">
                <GitBranch className="h-6 w-6 text-primary-500" />
                <span className="text-xl font-bold text-white">{project.name}</span>
              </div>
            </div>
            
            <div className="flex items-center gap-3">
              {project.github_url && (
                <a
                  href={project.github_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-slate-400 hover:text-white transition-colors"
                  onClick={(e) => {
                    e.preventDefault();
                    // Open in new tab without waiting for response
                    window.open(project.github_url, '_blank');
                  }}
                  title={project.github_url}
                >
                  <ExternalLink className="h-5 w-5" />
                </a>
              )}
              <button
                onClick={handleUpdateGithubUrl}
                disabled={savingGithubUrl}
                className="text-sm text-slate-300 hover:text-white transition-colors disabled:opacity-50"
                title={project.github_url ? 'Update GitHub repository link' : 'Link a GitHub repository'}
              >
                {project.github_url ? 'Edit Repo' : 'Link Repo'}
              </button>
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Project Header */}
        <div className="mb-8">
          <div className="flex items-start justify-between mb-4">
            <div>
              <h1 className="text-3xl font-bold text-white mb-2">{project.name}</h1>
              {project.description && (
                <p className="text-slate-400">{project.description}</p>
              )}
            </div>
            <div className="flex items-center gap-3">
              {tasks.length > 0 && (
                <button
                  onClick={async () => {
                    if (!confirm('Delete all tasks in this project? This cannot be undone.')) {
                      return;
                    }
                    try {
                      await Promise.all(tasks.map(task => tasksAPI.delete(task.id)));
                      alert('All tasks deleted');
                      fetchTasks();
                    } catch (error) {
                      console.error('Failed to delete all tasks:', error);
                      alert('Failed to delete all tasks. Please try again.');
                    }
                  }}
                  className="flex items-center gap-2 text-red-400 hover:text-red-300 px-4 py-2 rounded-lg transition-all"
                >
                  <Trash2 className="h-4 w-4" />
                  Delete All
                </button>
              )}
            </div>
          </div>
          <div className="flex items-center gap-4 text-sm text-slate-400">
            <span className="flex items-center gap-1">
              <GitBranch className="h-4 w-4" />
              {project.branch}
            </span>
            {project.github_url && (
              <span className="truncate max-w-[320px]">
                Repo: {project.github_url}
              </span>
            )}
            <span>{formatDistanceToNow(new Date(project.created_at), { addSuffix: true })}</span>
            <span className="flex items-center gap-1">
              <FileText className="h-4 w-4" />
              {tasks.length} tasks
            </span>
            <span className="flex items-center gap-1">
              <Terminal className="h-4 w-4" />
              {sessions.length} sessions
            </span>
          </div>
        </div>

        {/* Sessions Section */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold text-white flex items-center gap-2">
              <Terminal className="h-5 w-5" />
              AI Sessions
            </h2>
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
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {sessions.map((session) => (
                <div key={session.id} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-slate-600 transition-all">
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-4 flex-1">
                      <div className="p-2 rounded-lg text-blue-400 bg-blue-400/10 mt-1">
                        <Terminal className="h-5 w-5" />
                      </div>
                      <div className="flex-1">
                        <h3 className="text-lg font-semibold text-white mb-2">{session.name}</h3>
                        {session.description && (
                          <p className="text-sm text-slate-400 mb-3">{session.description}</p>
                        )}
                        <div className="flex items-center gap-4 text-sm text-slate-500">
                          <span className="flex items-center gap-1">
                            <Clock className="h-3 w-3" />
                            {formatDistanceToNow(new Date(session.created_at), { addSuffix: true })}
                          </span>
                          {session.started_at && <span>Started</span>}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <StatusBadge status={session.status} size="sm" />
                      <button
                        onClick={() => handleDeleteSession(session.id)}
                        className="text-slate-400 hover:text-red-400 transition-colors"
                        title="Delete session"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Tasks Section */}
        <div>
          <h2 className="text-xl font-semibold text-white mb-4">Tasks</h2>
          
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
            <div className="space-y-4">
              {tasks.map((task) => (
                <div key={task.id} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-slate-600 transition-all">
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-4 flex-1">
                      <div className="p-2 rounded-lg text-blue-400 bg-blue-400/10 mt-1">
                        <Activity className="h-5 w-5" />
                      </div>
                      <div className="flex-1">
                        <h3 className="text-lg font-semibold text-white mb-2">{task.title}</h3>
                        {task.description && (
                          <p className="text-sm text-slate-400 mb-3">{task.description}</p>
                        )}
                        <div className="flex items-center gap-4 text-sm text-slate-500">
                          <span className="flex items-center gap-1">
                            <Clock className="h-3 w-3" />
                            {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                          </span>
                          {task.current_step > 0 && (
                            <span>Step {task.current_step}</span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => startEditTask(task)}
                        className="text-slate-400 hover:text-blue-400 transition-colors"
                        title="Edit task"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                        </svg>
                      </button>
                      <StatusBadge status={task.status} size="sm" />
                      <button
                        onClick={() => handleDeleteTask(task.id)}
                        className="text-slate-400 hover:text-red-400 transition-colors"
                        title="Delete task"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Create Task Modal */}
      {showCreateTask && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold text-white mb-4">Create New Task</h3>
            <form onSubmit={handleCreateTask}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Task Title <span className="text-red-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
                    placeholder="e.g., Build a simple Vite website"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Description
                  </label>
                  <textarea
                    value={taskDescription}
                    onChange={(e) => setTaskDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
                    placeholder="Describe what needs to be done..."
                  />
                </div>
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <label className="block text-sm font-medium text-slate-300">
                      Step-by-Step Plan (JSON)
                    </label>
                    <button
                      type="button"
                      onClick={() => generateStepsFromDescription(taskDescription)}
                      disabled={generatingSteps || !taskDescription.trim()}
                      className="text-xs bg-primary-500/20 hover:bg-primary-500/30 text-primary-400 px-3 py-1 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
                    >
                      {generatingSteps ? (
                        <>
                          <div className="h-3 w-3 border-2 border-primary-400/30 border-t-primary-400 rounded-full animate-spin" />
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
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 font-mono text-sm resize-none"
                    placeholder='{"task_name": "...", "description": "...", "step_by_step_plan": [{"step": 1, "title": "...", "details": "..."}]}'
                  />
                  <p className="text-xs text-slate-500 mt-1">
                    Leave empty to auto-generate, or edit manually. Required for task execution.
                  </p>
                </div>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => setShowCreateTask(false)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!taskTitle.trim() || creatingTask}
                    className="flex-1 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {creatingTask ? (
                      <>
                        <div className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
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
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 w-full max-w-md mx-4">
            <h3 className="text-lg font-semibold text-white mb-4">Edit Task</h3>
            <form onSubmit={(e) => { e.preventDefault(); handleUpdateTask(editingTaskId); }}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Task Title *
                  </label>
                  <input
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
                    placeholder="e.g., Design homepage"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Description
                  </label>
                  <textarea
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
                    placeholder="Describe what needs to be done..."
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Step-by-Step Plan (JSON)
                  </label>
                  <textarea
                    value={editSteps}
                    onChange={(e) => setEditSteps(e.target.value)}
                    rows={4}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none font-mono text-sm"
                    placeholder='[{"step": 1, "action": "Create component"}, {"step": 2, "action": "Add styling"}]'
                  />
                  <p className="text-xs text-slate-500 mt-1">Format: JSON array of steps with "step" and "action" fields</p>
                </div>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => setEditingTaskId(null)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!editTitle.trim() || updatingTask}
                    className="flex-1 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {updatingTask ? (
                      <>
                        <div className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
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

