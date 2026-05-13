import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams, useNavigate } from 'react-router-dom';
import { projectsAPI, tasksAPI } from '@/api/client';
import type { Project, Task } from '@/types/api';
import { 
  ArrowLeft, 
  ChevronDown,
  Edit3, 
  Save, 
  X, 
  Calendar,
  AlertCircle,
  FileJson
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, Button, TextArea, Alert } from '@/components/ui';

function TaskDetail() {
  const { projectId, taskId } = useParams<{ projectId: string; taskId: string }>();
  const navigate = useNavigate();
  const [task, setTask] = useState<Task | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [allowCurrentStepEdit, setAllowCurrentStepEdit] = useState(false);
  const [runInNewSession, setRunInNewSession] = useState(false);
  const [editForm, setEditForm] = useState({
    title: '',
    description: '',
    steps: '',
    current_step: 0
  });

  const fetchTask = useCallback(async () => {
    try {
      const response = await tasksAPI.getById(Number(taskId));
      setTask(response.data);
      if (response.data?.project_id) {
        try {
          const projectResponse = await projectsAPI.getById(response.data.project_id);
          setProject(projectResponse.data);
        } catch (error) {
          console.error('Failed to fetch task project:', error);
          setProject(null);
        }
      }
      const steps = response.data?.steps ? JSON.stringify(JSON.parse(response.data.steps), null, 2) : '';
      setEditForm({
        title: response.data?.title || '',
        description: response.data?.description || '',
        steps: steps,
        current_step: response.data?.current_step || 0
      });
      setAllowCurrentStepEdit(false);
    } catch (error) {
      console.error('Failed to fetch task:', error);
      setSaveError('Failed to load task details');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchTask();
  }, [fetchTask]);

  const handleSave = async () => {
    try {
      setSaveError(null);
      
      // Parse steps JSON
      let stepsJson = null;
      if (editForm.steps.trim()) {
        try {
          stepsJson = JSON.stringify(JSON.parse(editForm.steps));
        } catch {
          setSaveError('Invalid JSON format for steps. Please check your syntax.');
          return;
        }
      }
      
      await tasksAPI.update(Number(taskId), {
        title: editForm.title,
        description: editForm.description,
        steps: stepsJson,
        current_step: editForm.current_step
      });
      await fetchTask();
      setEditing(false);
    } catch (error) {
      console.error('Failed to update task:', error);
      setSaveError('Failed to save changes');
    }
  };

  const handleCancel = () => {
    setEditing(false);
    setAllowCurrentStepEdit(false);
    fetchTask();
  };

  const handlePromote = async () => {
    if (!task) return;
    const note = window.prompt('Optional promotion note for this workspace:', task.promotion_note || '');
    if (note === null) return;
    try {
      await tasksAPI.promoteWorkspace(task.id, note || undefined);
      await fetchTask();
    } catch (error) {
      console.error('Failed to promote task workspace:', error);
      setSaveError('Failed to promote task workspace');
    }
  };

  const handleRequestChanges = async () => {
    if (!task) return;
    const note = window.prompt('Describe what still needs to change before promotion:', task.promotion_note || '');
    if (!note) return;
    try {
      await tasksAPI.requestWorkspaceChanges(task.id, note);
      if (window.confirm('Queue a new isolated repair run for this task now?')) {
        await tasksAPI.retry(task.id, { execution_scope: 'new_session', create_new_session: true });
      }
      await fetchTask();
    } catch (error) {
      console.error('Failed to request workspace changes:', error);
      setSaveError('Failed to mark workspace as needing changes');
    }
  };

  const handleRerun = async (isolated = false) => {
    if (!task || task.status === 'running') return;
    try {
      setSaveError(null);
      await tasksAPI.retry(
        task.id,
        isolated ? { execution_scope: 'new_session', create_new_session: true } : undefined
      );
      await fetchTask();
    } catch (error) {
      console.error('Failed to rerun task:', error);
      setSaveError('Failed to queue the task for another run');
    }
  };

  const stepsJsonState = useMemo(() => {
    if (!editForm.steps.trim()) {
      return { valid: true, message: 'No step plan' };
    }
    try {
      const parsed = JSON.parse(editForm.steps);
      return {
        valid: true,
        message: Array.isArray(parsed) ? `${parsed.length} JSON step${parsed.length === 1 ? '' : 's'}` : 'Valid JSON',
      };
    } catch (error) {
      return {
        valid: false,
        message: error instanceof Error ? error.message : 'Invalid JSON',
      };
    }
  }, [editForm.steps]);
  const promotedWorkspace = task?.workspace_status === 'promoted';

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  if (!task) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Alert
          variant="destructive"
          title="Task not found"
          description="The task you're looking for doesn't exist or has been removed."
        />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate('/tasks')}
            className="gap-2"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to Tasks
          </Button>
          {/* Hidden back button for reliable navigation */}
          <button
            onClick={() => navigate('/tasks')}
            className="sr-only"
            aria-label="Back to tasks"
            style={{ position: 'absolute', left: '-9999px', width: '1px', height: '1px', overflow: 'hidden' }}
          >
            Back to tasks
          </button>
          <h1 className="text-2xl font-bold text-slate-100">Task Details</h1>
        </div>
        {!editing ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setEditing(true)}
            className="gap-2"
          >
            <Edit3 className="h-4 w-4" />
            Edit Task
          </Button>
        ) : (
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCancel}
              className="gap-2"
            >
              <X className="h-4 w-4" />
              Cancel
            </Button>
            <Button
              variant="default"
              size="sm"
              onClick={handleSave}
              className="gap-2"
            >
              <Save className="h-4 w-4" />
              Save Changes
            </Button>
          </div>
        )}
      </div>

      {/* Error Alert */}
      {saveError && (
        <Alert
          variant="destructive"
          title="Error"
          description={saveError}
        />
      )}

      {/* Task Content */}
      <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-6">
        <div className="flex items-start justify-between mb-6">
          <div>
            <h2 className="text-xl font-bold text-white">
              {task.title}
            </h2>
            <div className="mt-2">
              <StatusBadge status={task.status} />
            </div>
          </div>
          {task.created_at && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <Calendar className="h-4 w-4" />
              <span>
                Created {new Date(task.created_at).toLocaleDateString()}
              </span>
            </div>
          )}
        </div>

        {/* Edit Mode */}
        {editing ? (
          <div className="space-y-6">
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4">
              <p className="text-sm font-medium text-amber-300">Editing the stored plan can affect future runs.</p>
              <p className="mt-1 text-sm text-amber-200/90">
                Changing steps or moving the current step backward may cause earlier work to run again.
                That can overwrite files, duplicate routes/endpoints, or conflict with existing project state.
                Resume/checkpoint state may also override these values during recovery.
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Title
              </label>
              <input
                value={editForm.title}
                onChange={(e) => setEditForm({ ...editForm, title: e.target.value })}
                placeholder="Task title"
                className="w-full px-3 py-2 bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border-soft)] rounded-lg text-white focus:outline-none focus:ring-1 focus:ring-primary-500/60"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Description
              </label>
              <TextArea
                value={editForm.description}
                onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                placeholder="Task description"
                className="bg-[color:var(--oc-shell)] border-[color:var(--oc-border-soft)] min-h-[150px]"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Step-by-Step Plan (JSON)
              </label>
              <p className="text-xs text-slate-500 mb-2">
                Define task steps as a JSON array. Each step should have: <code className="rounded bg-[color:var(--oc-surface-raised)] px-1">{"{"} "action": "...", "description": "..." {"}"}</code>
              </p>
              <TextArea
                value={editForm.steps}
                onChange={(e) => setEditForm({ ...editForm, steps: e.target.value })}
                placeholder='[{"action": "setup", "description": "Initialize project"}, {"action": "code", "description": "Write main code"}]'
                className={`bg-[color:var(--oc-shell)] min-h-[300px] font-mono text-sm ${
                  stepsJsonState.valid
                    ? 'border-emerald-700/70 focus:ring-emerald-500'
                    : 'border-red-700/80 focus:ring-red-500'
                }`}
              />
              <p
                className={`mt-2 text-xs ${
                  stepsJsonState.valid ? 'text-emerald-300' : 'text-red-300'
                }`}
              >
                {stepsJsonState.valid ? 'Valid JSON' : 'Invalid JSON'} · {stepsJsonState.message}
              </p>
            </div>

            <details className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-4">
              <summary className="cursor-pointer text-sm font-medium text-slate-300">
                Advanced / Dangerous
              </summary>
              <div className="mt-4 space-y-3">
                <label className="flex items-start gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">
                  <input
                    type="checkbox"
                    checked={allowCurrentStepEdit && !promotedWorkspace}
                    disabled={promotedWorkspace}
                    onChange={(event) => setAllowCurrentStepEdit(event.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-amber-500 bg-[color:var(--oc-surface-deep)] disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <span>
                    {promotedWorkspace
                      ? 'Promoted task workspaces cannot lower the current step. Request changes or rerun in a new isolated session.'
                      : 'I understand lowering this step may re-run earlier work and can overwrite or duplicate project files.'}
                  </span>
                </label>
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Current Step
                  </label>
                  <input
                    type="number"
                    min={0}
                    disabled={!allowCurrentStepEdit || promotedWorkspace}
                    value={editForm.current_step}
                    onChange={(e) =>
                      setEditForm({ ...editForm, current_step: parseInt(e.target.value) || 0 })
                    }
                    className="w-full px-3 py-2 bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border-soft)] rounded-lg text-white disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
                  />
                </div>
              </div>
            </details>
          </div>
        ) : (
          /* View Mode */
          <div className="space-y-6">
            {task.description && (
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-2">Description</h3>
                <p className="text-slate-400 whitespace-pre-wrap">{task.description}</p>
              </div>
            )}

            {task.error_message && (
              <div className="flex items-start gap-3 p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
                <AlertCircle className="h-5 w-5 text-red-400 mt-0.5" />
                <div>
                  <h3 className="text-sm font-medium text-red-400 mb-1">Error</h3>
                  <p className="text-sm text-red-300">{task.error_message}</p>
                </div>
              </div>
            )}

            <div className="rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-raised)] p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded-full border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-3 py-1 text-xs capitalize text-slate-200">
                  Workspace: {String(task.workspace_status || 'not_created').replace(/_/g, ' ')}
                </span>
                {task.task_subfolder && (
                  <span className="rounded-full border border-[color:var(--oc-border-soft)] px-3 py-1 text-xs text-slate-400">
                    {task.task_subfolder}
                  </span>
                )}
              </div>
              {task.promotion_note && (
                <p className="mt-3 text-sm text-slate-400">{task.promotion_note}</p>
              )}
              {task.promoted_at && (
                <p className="mt-2 text-xs text-emerald-400">
                  Promoted {new Date(task.promoted_at).toLocaleString()}
                </p>
              )}
              <div className="mt-4 flex flex-wrap gap-2">
                {task.status !== 'running' && (
                  <div className="flex items-center overflow-hidden rounded-md border border-primary-500/40">
                    <Button
                      size="sm"
                      onClick={() => handleRerun(runInNewSession)}
                      className="rounded-none border-0"
                    >
                      {task.status === 'done' ? 'Run Again' : 'Run'}
                    </Button>
                    <label className="flex items-center gap-1 border-l border-primary-500/30 bg-[color:var(--oc-shell)] px-2 text-xs text-slate-300">
                      <ChevronDown className="h-3.5 w-3.5 text-slate-500" />
                      <select
                        value={runInNewSession ? 'new_session' : 'workflow'}
                        onChange={(event) => setRunInNewSession(event.target.value === 'new_session')}
                        className="bg-transparent py-1.5 text-xs text-slate-300 focus:outline-none"
                        aria-label="Run mode"
                      >
                        <option value="workflow">Workflow session</option>
                        <option value="new_session">New isolated session</option>
                      </select>
                    </label>
                  </div>
                )}
                {task.status === 'done' && task.task_subfolder && task.workspace_status !== 'promoted' && (
                  <Button size="sm" onClick={handlePromote}>
                    Promote Workspace
                  </Button>
                )}
                {task.task_subfolder && task.workspace_status !== 'promoted' && (
                  <Button size="sm" variant="outline" onClick={handleRequestChanges}>
                    Request Changes
                  </Button>
                )}
              </div>
            </div>

            {task.steps && (
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-2 flex items-center gap-2">
                  <FileJson className="h-4 w-4" />
                  Step-by-Step Plan
                </h3>
                <pre className="bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border-soft)] p-4 rounded-lg text-sm text-slate-300 overflow-x-auto font-mono">
                  {typeof task.steps === 'string' ? task.steps : JSON.stringify(task.steps, null, 2)}
                </pre>
              </div>
            )}

            <div className="grid grid-cols-2 gap-4 pt-4 border-t border-[color:var(--oc-border)]">
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Project</h3>
                {task.project_id ? (
                  <Link
                    to={`/projects/${task.project_id || projectId}`}
                    className="text-sm text-primary-300 hover:text-primary-200"
                  >
                    {project?.name || `Project ${task.project_id}`}
                  </Link>
                ) : (
                  <p className="text-slate-400 text-sm">N/A</p>
                )}
              </div>
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Latest execution session</h3>
                {task.session_id ? (
                  <Link
                    to={`/sessions/${task.session_id}`}
                    className="text-sm text-primary-300 hover:text-primary-200"
                  >
                    Session {task.session_id}
                  </Link>
                ) : (
                  <p className="text-slate-400 text-sm">N/A</p>
                )}
              </div>
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Current Step</h3>
                <p className="text-slate-400 text-sm">
                  {task.current_step > 0 ? `Step ${task.current_step}` : 'Not started'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default TaskDetail;
