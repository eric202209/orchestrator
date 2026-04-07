import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { projectsAPI, tasksAPI, sessionsAPI } from '../api/client';
import type { Project, Task } from '../types/api';
import { 
  GitBranch, 
  Plus, 
  FileText,
  CheckCircle2,
  XCircle,
  ArrowLeft,
  ExternalLink,
  Trash2,
  Play,
  Terminal,
  Activity,
  Clock
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

function ProjectDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

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

   // const fetchSessions = async () => {
  //   if (!id) return;
  //   try {
  //     const response = await sessionsAPI.getByProject(Number(id));
  //     setSessions(response.data || []);
  //   } catch (error) {
  //     console.error('Failed to fetch sessions:', error);
  //   }
  // };

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

      await tasksAPI.create(payload);
      setTaskTitle('');
      setTaskDescription('');
      setTaskSteps('');
      setShowCreateTask(false);
      fetchTasks();
    } catch (error) {
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
    try {
      // Use DELETE instead of PATCH with cancelled status
      await tasksAPI.delete(taskId);
      console.log('Task deleted successfully');
      fetchTasks();
    } catch (error) {
      console.error('Failed to delete task:', error);
      if (error.response) {
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
    if (!editTitle.trim()) return;

    setUpdatingTask(true);
    try {
      const response = await tasksAPI.update(taskId, {
        title: editTitle.trim(),
        description: editDescription,
        steps: editSteps,
      });

      setTasks((currentTasks) =>
        currentTasks.map((task) =>
          task.id === taskId ? response.data : task
        )
      );

      setEditingTaskId(null);
      await fetchTasks();
    } catch (error) {
      console.error('Failed to update task:', error);
      alert('Failed to update task. Please try again.');
    } finally {
      setUpdatingTask(false);
    }
  };

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const handleDeleteProject: () => void = () => {
    // Disabled for now - implement if needed
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'done': return 'text-green-400 bg-green-400/10';
      case 'running': return 'text-blue-400 bg-blue-400/10';
      case 'failed': return 'text-red-400 bg-red-400/10';
      case 'cancelled': return 'text-slate-400 bg-slate-400/10';
      default: return 'text-yellow-400 bg-yellow-400/10';
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'done': return <CheckCircle2 className="h-4 w-4" />;
      case 'running': return <Activity className="h-4 w-4 animate-pulse" />;
      case 'failed': return <XCircle className="h-4 w-4" />;
      case 'cancelled': return <XCircle className="h-4 w-4" />;
      default: return <Clock className="h-4 w-4" />;
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
            
            <div className="flex items-center gap-4">
              <Link
                to="/"
                className="flex items-center gap-2 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all"
                title="Back to Dashboard"
              >
                <GitBranch className="h-4 w-4" />
                Dashboard
              </Link>
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
              {!project.github_url && (
                <div
                  className="text-slate-500"
                  title="No GitHub repository linked"
                >
                  <ExternalLink className="h-5 w-5" />
                </div>
              )}
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
              <button
                onClick={() => setShowCreateTask(true)}
                className="flex items-center gap-2 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all"
              >
                <Plus className="h-4 w-4" />
                Create Task
              </button>
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
            <Link
              to={`/sessions/new?project_id=${id}`}
              className="flex items-center gap-2 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all"
            >
              <Plus className="h-4 w-4" />
              New Session
            </Link>
          </div>

          {sessions.length === 0 ? (
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-8 text-center">
              <Terminal className="h-12 w-12 mx-auto mb-3 text-slate-600" />
              <p className="text-slate-400">No AI sessions yet. Create a session to start orchestrating development tasks.</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {sessions.map((session) => (
                <Link
                  key={session.id}
                  to={`/sessions/${session.id}`}
                  className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-primary-500/50 transition-all"
                >
                  <div className="flex items-start justify-between mb-3">
                    <div className={`p-2 rounded-lg ${
                      session.status === 'running' ? 'text-green-400 bg-green-400/10' :
                      session.status === 'paused' ? 'text-yellow-400 bg-yellow-400/10' :
                      session.status === 'stopped' ? 'text-slate-400 bg-slate-400/10' :
                      'text-blue-400 bg-blue-400/10'
                    }`}>
                      {session.status === 'running' ? (
                        <Activity className="h-5 w-5 animate-pulse" />
                      ) : session.status === 'paused' ? (
                        <Play className="h-5 w-5" />
                      ) : (
                        <Terminal className="h-5 w-5" />
                      )}
                    </div>
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${
                      session.status === 'running' ? 'text-green-400 bg-green-400/10' :
                      session.status === 'paused' ? 'text-yellow-400 bg-yellow-400/10' :
                      session.status === 'stopped' ? 'text-slate-400 bg-slate-400/10' :
                      'text-blue-400 bg-blue-400/10'
                    }`}>
                      {session.status.toUpperCase()}
                    </span>
                  </div>
                  <h3 className="font-semibold text-white mb-1">{session.name}</h3>
                  {session.description && (
                    <p className="text-sm text-slate-400 mb-3 line-clamp-2">{session.description}</p>
                  )}
                  <div className="flex items-center justify-between text-xs text-slate-500">
                    <span>{formatDistanceToNow(new Date(session.created_at), { addSuffix: true })}</span>
                    {session.started_at && (
                      <span className="flex items-center gap-1">
                        <Clock className="h-3 w-3" />
                        Started
                      </span>
                    )}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Tasks Section */}
        <div>
          <h2 className="text-xl font-semibold text-white mb-4">Tasks</h2>
          
          {tasks.length === 0 ? (
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-12 text-center">
              <FileText className="h-16 w-16 mx-auto mb-4 text-slate-600" />
              <h3 className="text-xl font-semibold text-white mb-2">No tasks yet</h3>
              <p className="text-slate-400 mb-6">Create your first task to get started with AI development</p>
              <button
                onClick={() => setShowCreateTask(true)}
                className="bg-primary-500 hover:bg-primary-600 text-white px-6 py-3 rounded-lg transition-all"
              >
                Create Task
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              {tasks.map((task) => (
                <div key={task.id} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-slate-600 transition-all">
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-4 flex-1">
                      <div className={`p-2 rounded-lg ${getStatusColor(task.status)} mt-1`}>
                        {getStatusIcon(task.status)}
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
                      <span className={`px-3 py-1 rounded-full text-xs font-medium ${getStatusColor(task.status)}`}>
                        {task.status.toUpperCase()}
                      </span>
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

