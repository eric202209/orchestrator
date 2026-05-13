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
  FileJson,
  FileWarning,
  RotateCcw
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, Button, TextArea, Alert } from '@/components/ui';
import type { TaskExecutionChangeSetResponse } from '@/types/api';

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
  const [requestChangesOpen, setRequestChangesOpen] = useState(false);
  const [requestChangesNote, setRequestChangesNote] = useState('');
  const [requestChangesRerun, setRequestChangesRerun] = useState(true);
  const [requestChangesSubmitting, setRequestChangesSubmitting] = useState(false);
  const [changeSet, setChangeSet] = useState<TaskExecutionChangeSetResponse | null>(null);
  const [changeSetRejecting, setChangeSetRejecting] = useState(false);
  const [editForm, setEditForm] = useState({
    title: '',
    description: '',
    steps: '',
    current_step: 0
  });

  const fetchTask = useCallback(async () => {
    try {
      setChangeSet(null);
      const response = await tasksAPI.getById(Number(taskId));
      setTask(response.data);
      try {
        const changeSetResponse = await tasksAPI.getChangeSet(Number(taskId));
        setChangeSet(changeSetResponse.data);
      } catch {
        setChangeSet(null);
      }
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

  const openRequestChanges = () => {
    if (!task) return;
    setRequestChangesNote(task.promotion_note || '');
    setRequestChangesRerun(true);
    setRequestChangesOpen(true);
  };

  const submitRequestChanges = async () => {
    if (!task || !requestChangesNote.trim()) return;
    try {
      setRequestChangesSubmitting(true);
      await tasksAPI.requestWorkspaceChanges(task.id, requestChangesNote.trim());
      if (requestChangesRerun) {
        await tasksAPI.retry(task.id, { execution_scope: 'new_session', create_new_session: true });
      }
      await fetchTask();
      setRequestChangesOpen(false);
    } catch (error) {
      console.error('Failed to request workspace changes:', error);
      setSaveError('Failed to mark workspace as needing changes');
    } finally {
      setRequestChangesSubmitting(false);
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

  const extractArchivePath = (targetTask: Task) => {
    const match = (targetTask.promotion_note || '').match(
      /Archived (?:retained|previous) workspace(?: for repair rerun)? at (.+?)(?:\n|$)/
    );
    return match?.[1]?.trim() || null;
  };

  const handleRestoreArchivedWorkspace = async () => {
    if (!task) return;
    const archivePath = extractArchivePath(task);
    if (!archivePath) {
      setSaveError('No archived workspace path was found for this task.');
      return;
    }
    try {
      setSaveError(null);
      await projectsAPI.restoreWorkspaceArchive(task.project_id, {
        task_id: task.id,
        archive_path: archivePath,
      });
      await fetchTask();
    } catch (error) {
      console.error('Failed to restore archived workspace:', error);
      setSaveError('Failed to restore archived workspace');
    }
  };

  const handleRejectChangeSet = async () => {
    if (!task || !changeSet?.task_execution_id) return;
    const note = window.prompt('Reason for rejecting this candidate change set:', 'needs review');
    if (note === null) return;
    try {
      setSaveError(null);
      setChangeSetRejecting(true);
      await tasksAPI.rejectChangeSet(task.id, {
        task_execution_id: changeSet.task_execution_id,
        note: note.trim() || 'operator_rejected_change_set',
      });
      await fetchTask();
    } catch (error) {
      console.error('Failed to reject change set:', error);
      setSaveError('Failed to reject and restore the change set');
    } finally {
      setChangeSetRejecting(false);
    }
  };

  const isArchivedPromotedWorkspace = (targetTask: Task) =>
    targetTask.workspace_status === 'promoted' &&
    (targetTask.task_subfolder || '').startsWith('.openclaw/promoted-workspace-archive/');

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
  const latestChangeSet = changeSet?.change_set || null;
  const renderChangeSetFiles = (label: string, files: string[]) => {
    if (!files.length) return null;
    return (
      <div>
        <h4 className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</h4>
        <ul className="mt-1 max-h-28 overflow-y-auto rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-2 text-xs text-slate-300">
          {files.slice(0, 20).map((file) => (
            <li key={`${label}-${file}`} className="truncate font-mono">{file}</li>
          ))}
          {files.length > 20 && (
            <li className="pt-1 text-slate-500">+{files.length - 20} more</li>
          )}
        </ul>
      </div>
    );
  };

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
                {task.task_subfolder && !isArchivedPromotedWorkspace(task) && (
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
                  <Button size="sm" variant="outline" onClick={openRequestChanges}>
                    Request Changes
                  </Button>
                )}
                {!task.task_subfolder && extractArchivePath(task) && (
                  <Button size="sm" variant="outline" onClick={handleRestoreArchivedWorkspace}>
                    Restore Archived Workspace
                  </Button>
                )}
              </div>
            </div>

            {latestChangeSet && (
              <div className="rounded-lg border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-raised)] p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="flex items-center gap-2 text-sm font-medium text-slate-200">
                      <FileWarning className="h-4 w-4 text-amber-300" />
                      Workspace Change Set
                    </h3>
                    <p className="mt-1 text-xs text-slate-500">
                      Execution {latestChangeSet.task_execution_id} · {latestChangeSet.changed_count} changed file{latestChangeSet.changed_count === 1 ? '' : 's'}
                    </p>
                  </div>
                  {latestChangeSet.changed_count > 0 && task.workspace_status !== 'promoted' && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={handleRejectChangeSet}
                      disabled={changeSetRejecting}
                      className="gap-2 border-red-500/40 text-red-200 hover:bg-red-500/10"
                    >
                      <RotateCcw className="h-4 w-4" />
                      {changeSetRejecting ? 'Restoring...' : 'Reject & Restore'}
                    </Button>
                  )}
                </div>
                {latestChangeSet.warning_flags.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {latestChangeSet.warning_flags.map((flag) => (
                      <span
                        key={flag}
                        className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200"
                      >
                        {flag.replace(/_/g, ' ')}
                      </span>
                    ))}
                  </div>
                )}
                <div className="mt-4 grid gap-3 md:grid-cols-3">
                  {renderChangeSetFiles('Added', latestChangeSet.added_files)}
                  {renderChangeSetFiles('Modified', latestChangeSet.modified_files)}
                  {renderChangeSetFiles('Deleted', latestChangeSet.deleted_files)}
                </div>
                {latestChangeSet.changed_count === 0 && (
                  <p className="mt-3 text-sm text-slate-400">No file changes were recorded for this execution.</p>
                )}
              </div>
            )}

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
      {requestChangesOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-5 shadow-2xl">
            <h3 className="text-sm font-semibold text-white">Request Changes</h3>
            <div className="mt-4 space-y-4">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-300">
                  Change request
                </label>
                <TextArea
                  value={requestChangesNote}
                  onChange={(event) => setRequestChangesNote(event.target.value)}
                  className="min-h-[120px] bg-[color:var(--oc-surface-deep)] border-[color:var(--oc-border-soft)]"
                  placeholder="Describe what must change before this workspace can be promoted."
                />
              </div>
              <label className="flex items-start gap-3 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-3 text-sm text-slate-300">
                <input
                  type="checkbox"
                  checked={requestChangesRerun}
                  onChange={(event) => setRequestChangesRerun(event.target.checked)}
                  className="mt-0.5 h-4 w-4 rounded border-[color:var(--oc-border)] bg-[color:var(--oc-shell)]"
                />
                <span>Queue a new isolated repair run after saving this request.</span>
              </label>
              <div className="flex gap-2 pt-1">
                <Button
                  type="button"
                  variant="outline"
                  className="flex-1"
                  onClick={() => setRequestChangesOpen(false)}
                  disabled={requestChangesSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  type="button"
                  className="flex-1"
                  onClick={submitRequestChanges}
                  disabled={!requestChangesNote.trim() || requestChangesSubmitting}
                >
                  {requestChangesSubmitting ? 'Saving...' : 'Save Request'}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default TaskDetail;
