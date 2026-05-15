import { useState, useEffect } from 'react';
import { useParams, Link, useNavigate, useSearchParams } from 'react-router-dom';
import { projectsAPI, tasksAPI, sessionsAPI } from '../api/client';
import type { ChangeSetReviewDecision, Project, Task, Session } from '../types/api';
import { ProjectPlannerPanel } from '../components/ProjectPlannerPanel';
import { isLegacyTaskExecutionSession } from '../lib/sessionIdentity';
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
  Plus,
  MoreHorizontal,
  RotateCcw
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { StatusBadge, EmptyState } from '../components/ui';

function ProjectDetail() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
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
    audit?: {
      retained_task_workspace_count: number;
      unpromoted_done_workspace_count: number;
      retained_task_workspaces: Array<{
        task_id: number;
        title: string;
        task_subfolder: string;
        baseline_diff: {
          added_count: number;
          modified_count: number;
          added_files?: string[];
          modified_files?: string[];
        };
      }>;
      duplicated_scaffold_artifacts: Record<string, number>;
      transient_artifact_names: string[];
      issues: string[];
    };
    promoted_tasks: Array<{ id: number; title: string; promoted_at?: string | null }>;
    pending_change_sets: Array<{
      task_id: number;
      title: string;
      workspace_status?: string | null;
      task_execution_id?: number | null;
      change_set: {
        changed_count: number;
        added_count: number;
        modified_count: number;
        deleted_count: number;
        added_files: string[];
        modified_files: string[];
        deleted_files: string[];
        warning_flags: string[];
      };
      review_decision?: ChangeSetReviewDecision;
    }>;
    ready_task_ids: number[];
  } | null>(null);
  type ProjectTab = 'sessions' | 'tasks' | 'planner' | 'review';
  const initialTab = (['sessions', 'tasks', 'planner', 'review'] as const).includes(
    searchParams.get('tab') as ProjectTab
  )
    ? (searchParams.get('tab') as ProjectTab)
    : 'tasks';
  const [activeTab, setActiveTab] = useState<ProjectTab>(initialTab);
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
  const [cleaningWorkspaces, setCleaningWorkspaces] = useState(false);
  const [acceptTask, setAcceptTask] = useState<Task | null>(null);
  const [acceptTaskExecutionId, setAcceptTaskExecutionId] = useState<number | null>(null);
  const [acceptanceNote, setAcceptanceNote] = useState('');
  const [acceptingWorkspace, setAcceptingWorkspace] = useState(false);
  const [requestChangesTask, setRequestChangesTask] = useState<Task | null>(null);
  const [requestChangesNote, setRequestChangesNote] = useState('');
  const [requestChangesRerun, setRequestChangesRerun] = useState(true);
  const [requestChangesSubmitting, setRequestChangesSubmitting] = useState(false);
  const [rejectingChangeSetTaskId, setRejectingChangeSetTaskId] = useState<number | null>(null);
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
            className="border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] px-4 py-2 rounded-md text-sm transition-colors"
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
        return 'border-primary-500/30 bg-primary-400/10 text-primary-300';
      case 'changes_requested':
        return 'border-amber-500/30 bg-amber-500/10 text-amber-300';
      case 'blocked':
        return 'border-red-500/30 bg-red-500/10 text-red-300';
      case 'in_progress':
        return 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300';
      default:
        return 'border-[color:var(--oc-border)] bg-[color:var(--oc-surface-raised)] text-slate-300';
    }
  };

  const formatWorkspaceStatus = (status?: string | null) => {
    if (status === 'promoted') return 'accepted';
    return (status || 'not_created').replace(/_/g, ' ');
  };

  const workspaceCountItems = workspaceOverview
    ? [
        { key: 'ready', label: 'Ready', value: workspaceOverview.counts.ready || 0, className: 'text-primary-300' },
        { key: 'promoted', label: 'Accepted', value: workspaceOverview.counts.promoted || 0, className: 'text-emerald-300' },
        { key: 'changes_requested', label: 'Changes Requested', value: workspaceOverview.counts.changes_requested || 0, className: 'text-amber-300' },
        { key: 'blocked', label: 'Blocked', value: workspaceOverview.counts.blocked || 0, className: 'text-red-300' },
      ].filter((item) => item.value > 0)
    : [];
  const duplicatedScaffoldItems = workspaceOverview?.audit
    ? Object.entries(workspaceOverview.audit.duplicated_scaffold_artifacts || {})
    : [];
  const pendingWorkspaceChangeCount = workspaceOverview?.audit
    ? workspaceOverview.audit.retained_task_workspaces.reduce(
        (total, item) =>
          total + item.baseline_diff.added_count + item.baseline_diff.modified_count,
        0
      )
    : 0;
  const pendingChangeSets = workspaceOverview?.pending_change_sets || [];
  const pendingChangeSetFileCount = pendingChangeSets.reduce(
    (total, item) => total + (item.change_set?.changed_count || 0),
    0
  );
  const activeProjectTask = tasks.find((task) => task.status === 'running') || null;
  const getTaskIconColors = (status: string) => {
    switch (status) {
      case 'done': return 'text-emerald-400 bg-emerald-400/10';
      case 'running': return 'text-blue-400 bg-blue-400/10';
      case 'failed': return 'text-red-400 bg-red-400/10';
      case 'cancelled': return 'text-slate-500 bg-slate-500/10';
      default: return 'text-slate-400 bg-slate-400/10';
    }
  };

  const truncateSubfolder = (subfolder: string) => {
    const segments = subfolder.split('/');
    const last = segments[segments.length - 1];
    return last.length > 32 ? `${last.slice(0, 29)}…` : last;
  };

  const extractArchivePath = (task: Task) => {
    const match = (task.promotion_note || '').match(
      /Archived (?:retained|previous) workspace(?: for repair rerun)? at (.+?)(?:\n|$)/
    );
    return match?.[1]?.trim() || null;
  };

  const isArchivedPromotedWorkspace = (task: Task) =>
    task.workspace_status === 'promoted' &&
    (task.task_subfolder || '').startsWith('.openclaw/promoted-workspace-archive/');

  const getTaskWorkspaceDiff = (task: Task) =>
    workspaceOverview?.audit?.retained_task_workspaces.find((item) => item.task_id === task.id)
      ?.baseline_diff || null;

  const openAcceptTask = (task: Task, taskExecutionId?: number | null) => {
    if (activeProjectTask && activeProjectTask.id !== task.id) {
      alert(`Wait for the active task to finish before accepting workspaces: ${activeProjectTask.title}`);
      return;
    }
    const pendingChangeSet = pendingChangeSets.find((item) => item.task_id === task.id);
    setAcceptTask(task);
    setAcceptTaskExecutionId(taskExecutionId || pendingChangeSet?.task_execution_id || null);
    setAcceptanceNote(task.promotion_note || '');
  };

  const submitAcceptTask = async () => {
    if (!acceptTask || !id) return;
    try {
      setAcceptingWorkspace(true);
      const response = await tasksAPI.acceptWorkspace(
        acceptTask.id,
        {
          note: acceptanceNote.trim() || undefined,
          task_execution_id: acceptTaskExecutionId || undefined,
        }
      );
      setTasks((current) =>
        current.map((item) => (item.id === acceptTask.id ? response.data : item))
      );
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
      setAcceptTask(null);
      setAcceptTaskExecutionId(null);
    } catch (error) {
      console.error('Failed to accept task workspace:', error);
      const detail = (error as any)?.response?.data?.detail;
      alert(detail || 'Failed to accept task workspace. Please try again.');
    } finally {
      setAcceptingWorkspace(false);
    }
  };

  const openRequestChanges = (task: Task) => {
    setRequestChangesTask(task);
    setRequestChangesNote(task.promotion_note || '');
    setRequestChangesRerun(true);
  };

  const submitRequestChanges = async () => {
    if (!requestChangesTask || !id || !requestChangesNote.trim()) return;
    try {
      setRequestChangesSubmitting(true);
      const response = await tasksAPI.requestWorkspaceChanges(
        requestChangesTask.id,
        requestChangesNote.trim()
      );
      if (requestChangesRerun) {
        await tasksAPI.retry(requestChangesTask.id, { execution_scope: 'new_session', create_new_session: true });
      }
      setTasks((current) =>
        current.map((item) => (item.id === requestChangesTask.id ? response.data : item))
      );
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
      setRequestChangesTask(null);
    } catch (error) {
      console.error('Failed to mark task workspace for changes:', error);
      alert('Failed to update workspace review state. Please try again.');
    } finally {
      setRequestChangesSubmitting(false);
    }
  };

  const handleRebuildBaseline = async () => {
    if (!id) return;
    if (!window.confirm('Rebuild the project baseline from all accepted task workspaces?')) {
      return;
    }

    try {
      setRebuildingBaseline(true);
      const result = await projectsAPI.rebuildBaseline(Number(id));
      const workspaceResponse = await projectsAPI.getWorkspaceOverview(Number(id));
      setWorkspaceOverview(workspaceResponse.data || null);
      alert(
        `Baseline rebuilt with ${result.data.files_copied} files from ${result.data.promoted_task_count} accepted task(s).`
      );
    } catch (error) {
      console.error('Failed to rebuild project baseline:', error);
      alert('Failed to rebuild the project baseline. Please try again.');
    } finally {
      setRebuildingBaseline(false);
    }
  };

  const handleCleanupWorkspaces = async () => {
    if (!id) return;
    setCleaningWorkspaces(true);
    try {
      const preview = await projectsAPI.cleanupWorkspaces(Number(id));
      const candidateCount = preview.data.candidate_count || 0;
      if (candidateCount === 0) {
        alert('No blocked retained task workspaces are eligible for cleanup.');
        return;
      }
      if (!window.confirm(`Archive ${candidateCount} blocked retained task workspace folder(s)? Accepted and running workspaces will be preserved.`)) {
        return;
      }
      const result = await projectsAPI.cleanupWorkspaces(Number(id), { dry_run: false });
      const [tasksRes, workspaceRes] = await Promise.all([
        tasksAPI.getByProject(Number(id)),
        projectsAPI.getWorkspaceOverview(Number(id)),
      ]);
      setTasks(tasksRes.data);
      setWorkspaceOverview(workspaceRes.data);
      alert(`Archived ${result.data.deleted_count} retained workspace folder(s).`);
    } catch (error) {
      console.error('Failed to clean up retained workspaces:', error);
      alert('Failed to clean up retained workspaces. Please try again.');
    } finally {
      setCleaningWorkspaces(false);
    }
  };

  const handleRestoreArchivedWorkspace = async (task: Task) => {
    if (!id) return;
    const archivePath = extractArchivePath(task);
    if (!archivePath) {
      alert('No archived workspace path was found for this task.');
      return;
    }
    try {
      await projectsAPI.restoreWorkspaceArchive(Number(id), {
        task_id: task.id,
        archive_path: archivePath,
      });
      const [tasksRes, workspaceRes] = await Promise.all([
        tasksAPI.getByProject(Number(id)),
        projectsAPI.getWorkspaceOverview(Number(id)),
      ]);
      setTasks(tasksRes.data);
      setWorkspaceOverview(workspaceRes.data);
    } catch (error) {
      console.error('Failed to restore archived workspace:', error);
      alert('Failed to restore archived workspace. Please try again.');
    }
  };

  const handleRejectChangeSet = async (taskId: number, taskExecutionId?: number | null) => {
    if (!id) return;
    const note = window.prompt('Reason for rejecting this candidate change set:', 'needs review');
    if (note === null) return;
    try {
      setRejectingChangeSetTaskId(taskId);
      await tasksAPI.rejectChangeSet(taskId, {
        task_execution_id: taskExecutionId || undefined,
        note: note.trim() || 'operator_rejected_change_set',
      });
      const [tasksRes, workspaceRes] = await Promise.all([
        tasksAPI.getByProject(Number(id)),
        projectsAPI.getWorkspaceOverview(Number(id)),
      ]);
      setTasks(tasksRes.data);
      setWorkspaceOverview(workspaceRes.data);
    } catch (error) {
      console.error('Failed to reject change set:', error);
      alert('Failed to reject and restore the change set. Please try again.');
    } finally {
      setRejectingChangeSetTaskId(null);
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

  const handleRerunTask = async (task: Task, isolated = false) => {
    if (task.status === 'running') return;
    try {
      await tasksAPI.retry(
        task.id,
        isolated ? { execution_scope: 'new_session', create_new_session: true } : undefined
      );
      await fetchTasks();
      alert(
        isolated
          ? 'Task queued in a new isolated session'
          : task.status === 'done'
            ? 'Task queued to run again in the workflow session'
            : 'Task queued in the workflow session'
      );
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
        <div className="h-8 w-8 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin" />
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
          <span className="flex items-center gap-1 text-xs text-slate-400">
            <GitBranch className="h-3.5 w-3.5 text-primary-500/60" />
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
            <details className="relative">
              <summary className="flex cursor-pointer list-none items-center rounded-md p-1 text-slate-500 transition-colors hover:bg-[color:var(--oc-surface)] hover:text-slate-300">
                <MoreHorizontal className="h-4 w-4" />
              </summary>
              <div className="absolute right-0 z-20 mt-2 w-44 rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-shell)] p-1 shadow-xl">
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
                  className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-xs text-red-300 transition-colors hover:bg-red-950/40"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Delete all tasks
                </button>
              </div>
            </details>
          )}
        </div>
      </div>

      {/* Project Brief */}
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-5">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-400">Project Brief</h2>
            <p className="mt-0.5 text-xs text-slate-500">Persistent project context for planning and execution.</p>
          </div>
        </div>
        {editingProjectMeta ? (
          <div className="space-y-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-300">Description</label>
              <textarea
                value={projectDescriptionDraft}
                onChange={(e) => setProjectDescriptionDraft(e.target.value)}
                className="min-h-[80px] w-full resize-y rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
                placeholder="Project brief, scope, expected outcome..."
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-300">Rules</label>
              <textarea
                value={projectRulesDraft}
                onChange={(e) => setProjectRulesDraft(e.target.value)}
                className="min-h-[96px] w-full resize-y rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
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
                className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-sm text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSaveProjectMeta}
                disabled={savingProjectMeta}
                className="rounded-md border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] px-3 py-1.5 text-sm text-white transition-colors hover:bg-[color:var(--oc-action-hover)] disabled:opacity-50"
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
      <div className="flex items-center gap-4 text-xs text-slate-400">
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
          {sessions.length} runs
        </span>
      </div>

      {/* Tabs */}
      <div className="flex gap-0 border-b border-[color:var(--oc-border-soft)]">
        {[
          { key: 'tasks', label: 'Tasks' },
          {
            key: 'review',
            label: pendingChangeSets.length > 0 ? `Review (${pendingChangeSets.length})` : 'Review',
          },
          { key: 'planner', label: 'Project Architect' },
          { key: 'sessions', label: 'Runs' },
        ].map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveTab(tab.key as ProjectTab)}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              activeTab === tab.key
                ? 'text-white border-primary-500'
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

      {/* Review Queue Tab */}
      {activeTab === 'review' && (
        <div className="space-y-4">
          <div>
            <h2 className="text-sm font-medium text-white">Review Queue</h2>
            <p className="mt-1 text-xs text-slate-500">
              Task change sets held by backend review policy.
            </p>
          </div>
          {pendingChangeSets.length === 0 ? (
            <EmptyState
              icon={FileText}
              title="No pending change sets"
              description="Nontrivial task outputs will appear here when they need review."
            />
          ) : (
            <div className="grid gap-3">
              {pendingChangeSets.map((item) => {
                const reviewTask = tasks.find((task) => task.id === item.task_id) || null;
                const canShowAccept = Boolean(
                  reviewTask?.status === 'done' &&
                    reviewTask?.task_subfolder &&
                    reviewTask?.workspace_status !== 'promoted'
                );
                const acceptBlockedByActiveTask = Boolean(
                  canShowAccept &&
                    activeProjectTask &&
                    reviewTask &&
                    activeProjectTask.id !== reviewTask.id
                );
                const canRequestChanges = Boolean(
                  reviewTask?.task_subfolder && reviewTask?.workspace_status !== 'promoted'
                );
                return (
                <div
                  key={`${item.task_id}-${item.task_execution_id || 'latest'}`}
                  className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <Link
                        to={`/projects/${project.id}/tasks/${item.task_id}`}
                        className="truncate text-sm font-medium text-slate-100 hover:text-primary-200"
                      >
                        {item.title}
                      </Link>
                      <p className="mt-1 text-xs text-slate-500">
                        Execution {item.task_execution_id || 'latest'} · {formatWorkspaceStatus(item.workspace_status)}
                      </p>
                      {item.review_decision?.reason && (
                        <p className="mt-1 text-xs text-amber-300">
                          {item.review_decision.reason.replace(/_/g, ' ')}
                        </p>
                      )}
                    </div>
                    <div className="flex gap-2 text-xs">
                      <span className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-emerald-200">
                        +{item.change_set.added_count}
                      </span>
                      <span className="rounded-md border border-sky-500/20 bg-sky-500/10 px-2 py-1 text-sky-200">
                        ~{item.change_set.modified_count}
                      </span>
                      <span className="rounded-md border border-red-500/20 bg-red-500/10 px-2 py-1 text-red-200">
                        -{item.change_set.deleted_count}
                      </span>
                    </div>
                  </div>
                  {item.change_set.warning_flags.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {item.change_set.warning_flags.map((flag) => (
                        <span
                          key={`${item.task_id}-${flag}`}
                          className="rounded-full border border-amber-500/25 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-200"
                        >
                          {flag.replace(/_/g, ' ')}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Link
                      to={`/projects/${project.id}/tasks/${item.task_id}`}
                      className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white"
                    >
                      Open task review
                    </Link>
                    {canShowAccept && reviewTask && (
                      <button
                        type="button"
                        onClick={() => openAcceptTask(reviewTask, item.task_execution_id)}
                        disabled={acceptBlockedByActiveTask}
                        title={acceptBlockedByActiveTask ? `Wait for active task to finish: ${activeProjectTask?.title}` : undefined}
                        className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs text-emerald-200 transition-colors hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:border-slate-700 disabled:bg-slate-800/60 disabled:text-slate-500"
                      >
                        {acceptBlockedByActiveTask ? 'Accept after active task' : 'Accept'}
                      </button>
                    )}
                    {canRequestChanges && reviewTask && (
                      <button
                        type="button"
                        onClick={() => openRequestChanges(reviewTask)}
                        className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-200 transition-colors hover:bg-amber-500/15"
                      >
                        Request Changes
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => handleRejectChangeSet(item.task_id, item.task_execution_id)}
                      disabled={rejectingChangeSetTaskId === item.task_id}
                      className="flex items-center gap-1.5 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs text-red-200 transition-colors hover:bg-red-500/15 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      {rejectingChangeSetTaskId === item.task_id ? 'Restoring...' : 'Reject & Restore'}
                    </button>
                  </div>
                </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-white">Runs</h2>
            <button
              onClick={() => navigate(`/sessions/new?project_id=${id}`)}
              className="flex items-center gap-1.5 border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] text-sm px-3 py-1.5 rounded-md transition-colors"
            >
              <Plus className="h-4 w-4" />
              New Run
            </button>
          </div>

          {sessions.length === 0 ? (
            <EmptyState
              icon={Terminal}
              title="No runs yet"
              description="Start a run when this project has work ready for OpenClaw."
              action={{
                label: 'New Run',
                onClick: () => navigate(`/sessions/new?project_id=${id}`)
              }}
            />
          ) : (
            <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] divide-y divide-[color:var(--oc-border-soft)]">
              {sessions.map((session) => {
                const isLegacySession = isLegacyTaskExecutionSession(
                  session,
                  tasks.map((task) => task.title)
                );
                return (
                <div
                  key={session.id}
                  onClick={() => navigate(`/sessions/${session.id}`)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      navigate(`/sessions/${session.id}`);
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  className="flex cursor-pointer items-center gap-4 px-4 py-3 transition-colors hover:bg-[color:var(--oc-surface-raised)] focus:outline-none focus:ring-1 focus:ring-primary-500/60"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-medium text-slate-200">{session.name}</p>
                      {isLegacySession && (
                        <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-300">
                          Legacy task execution session
                        </span>
                      )}
                    </div>
                    {session.description && (
                      <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">{session.description}</p>
                    )}
                    <div className="flex items-center gap-3 mt-1 text-xs text-slate-400">
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
                      onClick={(event) => {
                        event.stopPropagation();
                        handleDeleteSession(session.id);
                      }}
                      className="text-slate-500 hover:text-red-400 transition-colors"
                      title="Delete session"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                );
              })}
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
              className="flex items-center gap-1.5 border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] text-sm px-3 py-1.5 rounded-md transition-colors"
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
                <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between mb-3">
                    <div>
                      <p className="text-xs uppercase tracking-wide text-slate-500">Canonical Baseline</p>
                      <p className="mt-1 text-sm text-slate-300">
                        {workspaceOverview.baseline.exists
                          ? `${workspaceOverview.baseline.file_count} files built from ${workspaceOverview.baseline.promoted_task_count} accepted task(s)`
                          : 'No canonical baseline yet'}
                      </p>
                      {workspaceOverview.baseline.path && (
                        <p className="mt-0.5 text-xs text-slate-500">{workspaceOverview.baseline.path}</p>
                      )}
                    </div>
                    <div className="flex gap-2 shrink-0">
                      <button
                        onClick={handleRebuildBaseline}
                        disabled={rebuildingBaseline || (workspaceOverview.baseline.promoted_task_count || 0) === 0}
                        className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {rebuildingBaseline ? 'Rebuilding…' : 'Rebuild Baseline'}
                      </button>
                      <button
                        onClick={handleCleanupWorkspaces}
                        disabled={cleaningWorkspaces}
                        className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {cleaningWorkspaces ? 'Checking…' : 'Archive Blocked'}
                      </button>
                    </div>
                  </div>
                  {workspaceCountItems.length > 0 && (
                    <div className="flex flex-wrap gap-3">
                      {workspaceCountItems.map((item) => (
                        <div key={item.key} className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2">
                          <p className="text-xs uppercase tracking-wide text-slate-500">{item.label}</p>
                          <p className={`mt-1 text-lg font-semibold ${item.className}`}>{item.value}</p>
                        </div>
                      ))}
                    </div>
                  )}
                  {workspaceOverview.audit && (
                    <div className="mt-3 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2">
                      <div className="grid grid-cols-3 gap-3 text-xs">
                        <div>
                          <p className="text-slate-500 uppercase tracking-wide text-[10px]">Retained</p>
                          <p className="mt-0.5 font-medium text-slate-300">{workspaceOverview.audit.retained_task_workspace_count}</p>
                        </div>
                        <div>
                          <p className="text-slate-500 uppercase tracking-wide text-[10px]">Unaccepted</p>
                          <p className="mt-0.5 font-medium text-slate-300">{workspaceOverview.audit.unpromoted_done_workspace_count}</p>
                        </div>
                        <div>
                          <p className="text-slate-500 uppercase tracking-wide text-[10px]">Pending diffs</p>
                          <p className={`mt-0.5 font-medium ${pendingWorkspaceChangeCount > 0 ? 'text-amber-300' : 'text-slate-300'}`}>{pendingWorkspaceChangeCount}</p>
                        </div>
                      </div>
                      {duplicatedScaffoldItems.length > 0 && (
                        <p className="mt-2 text-xs text-amber-300">
                          Repeated: {duplicatedScaffoldItems.slice(0, 4).map(([name, count]) => `${name} ×${count}`).join(', ')}
                        </p>
                      )}
                      {workspaceOverview.audit.issues.length > 0 && (
                        <p className="mt-2 text-xs text-slate-500">{workspaceOverview.audit.issues[0]}</p>
                      )}
                    </div>
                  )}
                  {pendingChangeSets.length > 0 && (
                    <div className="mt-3 rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <p className="text-xs font-medium uppercase tracking-wide text-amber-200">
                          Pending change sets
                        </p>
                        <span className="text-xs text-amber-200/80">
                          {pendingChangeSetFileCount} changed file{pendingChangeSetFileCount === 1 ? '' : 's'}
                        </span>
                      </div>
                      <div className="mt-2 grid gap-2 lg:grid-cols-2">
                        {pendingChangeSets.slice(0, 4).map((item) => (
                          <Link
                            key={`${item.task_id}-${item.task_execution_id || 'latest'}`}
                            to={`/projects/${project.id}/tasks/${item.task_id}`}
                            className="rounded-md border border-amber-500/20 bg-[color:var(--oc-surface-deep)] px-3 py-2 transition-colors hover:border-amber-500/40"
                          >
                            <div className="flex items-center justify-between gap-3">
                              <span className="truncate text-xs font-medium text-slate-200">
                                {item.title}
                              </span>
                              <span className="shrink-0 text-xs text-amber-200">
                                {item.change_set.changed_count}
                              </span>
                            </div>
                            <p className="mt-1 text-xs text-slate-500">
                              +{item.change_set.added_count} / ~{item.change_set.modified_count} / -{item.change_set.deleted_count}
                              {item.review_decision?.reason ? ` · ${item.review_decision.reason.replace(/_/g, ' ')}` : ''}
                            </p>
                          </Link>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] divide-y divide-[color:var(--oc-border-soft)]">
                {tasks.map((task) => (
                  <div key={task.id} className="px-4 py-4 hover:bg-[color:var(--oc-surface-raised)] transition-colors">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3 flex-1 min-w-0">
                        <div className={`p-1.5 rounded-md mt-0.5 shrink-0 ${getTaskIconColors(task.status)}`}>
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
                            {task.task_subfolder && !isArchivedPromotedWorkspace(task) && (
                              <span
                                className="rounded-full border border-[color:var(--oc-border)] px-2.5 py-0.5 text-xs text-slate-400"
                                title={task.task_subfolder}
                              >
                                {truncateSubfolder(task.task_subfolder)}
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-3 mt-2 text-xs text-slate-400">
                            <span className="flex items-center gap-1">
                              <Clock className="h-3 w-3" />
                              {formatDistanceToNow(new Date(task.created_at), { addSuffix: true })}
                            </span>
                            {task.current_step > 0 && <span>Step {task.current_step}</span>}
                          </div>
                          {task.promotion_note && (
                            <p className="mt-2 text-xs text-slate-400">
                              Review note: {task.promotion_note}
                            </p>
                          )}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <StatusBadge status={task.status} size="sm" />
                        {task.status !== 'running' && (
                          <button
                            onClick={() => handleRerunTask(task)}
                            className="rounded-md border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-[color:var(--oc-action-hover)]"
                          >
                            {task.status === 'done' ? 'Run again' : 'Run'}
                          </button>
                        )}
                        <details className="relative">
                          <summary className="flex cursor-pointer list-none items-center rounded-md p-1.5 text-slate-500 transition-colors hover:bg-[color:var(--oc-surface-raised)] hover:text-slate-300">
                            <MoreHorizontal className="h-4 w-4" />
                          </summary>
                          <div className="absolute right-0 z-20 mt-2 w-56 rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-shell)] p-1 shadow-xl">
                            {task.status !== 'running' && (
                              <button
                                onClick={() => handleRerunTask(task, true)}
                                className="w-full rounded-md px-2.5 py-2 text-left text-xs text-slate-300 transition-colors hover:bg-[color:var(--oc-surface)] hover:text-white"
                              >
                                Run in new isolated session
                              </button>
                            )}
                            {task.status === 'done' && task.task_subfolder && task.workspace_status !== 'promoted' && (
                              <button
                                onClick={() => openAcceptTask(task)}
                                disabled={Boolean(activeProjectTask && activeProjectTask.id !== task.id)}
                                className="w-full rounded-md px-2.5 py-2 text-left text-xs text-emerald-300 transition-colors hover:bg-emerald-950/40"
                              >
                                {activeProjectTask && activeProjectTask.id !== task.id
                                  ? 'Accept after active task finishes'
                                  : 'Accept workspace'}
                              </button>
                            )}
                            {task.task_subfolder && task.workspace_status !== 'promoted' && (
                              <button
                                onClick={() => openRequestChanges(task)}
                                className="w-full rounded-md px-2.5 py-2 text-left text-xs text-amber-300 transition-colors hover:bg-amber-950/40"
                              >
                                Request changes
                              </button>
                            )}
                            {!task.task_subfolder && extractArchivePath(task) && (
                              <button
                                onClick={() => handleRestoreArchivedWorkspace(task)}
                                className="w-full rounded-md px-2.5 py-2 text-left text-xs text-primary-300 transition-colors hover:bg-primary-950/40"
                              >
                                Restore archived workspace
                              </button>
                            )}
                            <button
                              onClick={() => startEditTask(task)}
                              className="w-full rounded-md px-2.5 py-2 text-left text-xs text-slate-300 transition-colors hover:bg-[color:var(--oc-surface)] hover:text-white"
                            >
                              Edit task
                            </button>
                            <button
                              onClick={() => handleDeleteTask(task.id)}
                              className="w-full rounded-md px-2.5 py-2 text-left text-xs text-red-300 transition-colors hover:bg-red-950/40"
                            >
                              Delete task
                            </button>
                          </div>
                        </details>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Accept Workspace Modal */}
      {acceptTask && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
          <div className="w-full max-w-xl rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-5 shadow-2xl">
            <h3 className="text-sm font-semibold text-white">Accept Workspace</h3>
            {(() => {
              const diff = getTaskWorkspaceDiff(acceptTask);
              const acceptBlockedByActiveTask = Boolean(
                activeProjectTask && activeProjectTask.id !== acceptTask.id
              );
              const changedFiles = [
                ...(diff?.added_files || []).map((path) => ({ path, type: 'Added' })),
                ...(diff?.modified_files || []).map((path) => ({ path, type: 'Modified' })),
              ];
              return (
                <div className="mt-4 space-y-4">
                  <div className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] p-3">
                    <p className="text-xs uppercase tracking-wide text-slate-500">Pending diff</p>
                    <p className="mt-1 text-sm text-slate-300">
                      {diff
                        ? `${diff.added_count} added, ${diff.modified_count} modified`
                        : 'No baseline diff data available for this workspace'}
                    </p>
                    {acceptTaskExecutionId && (
                      <p className="mt-1 text-xs text-slate-500">
                        Accepting execution {acceptTaskExecutionId}
                      </p>
                    )}
                    {changedFiles.length > 0 && (
                      <div className="mt-3 max-h-40 overflow-y-auto rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-shell)]">
                        {changedFiles.slice(0, 20).map((item) => (
                          <div
                            key={`${item.type}-${item.path}`}
                            className="flex items-center gap-2 border-b border-[color:var(--oc-border-soft)] px-3 py-1.5 last:border-b-0"
                          >
                            <span className="w-16 text-[11px] uppercase tracking-wide text-slate-500">
                              {item.type}
                            </span>
                            <span className="truncate font-mono text-xs text-slate-300">{item.path}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  {acceptBlockedByActiveTask && (
                    <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-100">
                      Wait for the active task to finish before accepting this workspace: {activeProjectTask?.title}
                    </div>
                  )}
                  <div>
                    <label className="mb-1.5 block text-xs font-medium text-slate-300">
                      Acceptance note
                    </label>
                    <textarea
                      value={acceptanceNote}
                      onChange={(event) => setAcceptanceNote(event.target.value)}
                      rows={3}
                      className="w-full resize-none rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
                      placeholder="Optional note for this accepted workspace."
                    />
                  </div>
                  <div className="flex gap-2 pt-1">
                    <button
                      type="button"
                      onClick={() => {
                        setAcceptTask(null);
                        setAcceptTaskExecutionId(null);
                      }}
                      disabled={acceptingWorkspace}
                      className="flex-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:opacity-50"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={submitAcceptTask}
                      disabled={acceptingWorkspace || acceptBlockedByActiveTask}
                      className="flex-1 rounded-md border border-emerald-500/30 bg-emerald-500/15 px-3 py-2 text-sm text-emerald-200 transition-colors hover:bg-emerald-500/20 disabled:opacity-50"
                    >
                      {acceptingWorkspace ? 'Accepting...' : 'Accept Workspace'}
                    </button>
                  </div>
                </div>
              );
            })()}
          </div>
        </div>
      )}

      {/* Request Changes Modal */}
      {requestChangesTask && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
          <div className="w-full max-w-lg rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-5 shadow-2xl">
            <h3 className="text-sm font-semibold text-white">Request Changes</h3>
            <div className="mt-4 space-y-4">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-300">
                  Change request
                </label>
                <textarea
                  value={requestChangesNote}
                  onChange={(event) => setRequestChangesNote(event.target.value)}
                  rows={5}
                  className="w-full resize-y rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
                  placeholder="Describe what must change before this workspace can be accepted."
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
                <button
                  type="button"
                  onClick={() => setRequestChangesTask(null)}
                  disabled={requestChangesSubmitting}
                  className="flex-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={submitRequestChanges}
                  disabled={!requestChangesNote.trim() || requestChangesSubmitting}
                  className="flex-1 rounded-md border border-amber-500/30 bg-amber-500/15 px-3 py-2 text-sm text-amber-200 transition-colors hover:bg-amber-500/20 disabled:opacity-50"
                >
                  {requestChangesSubmitting ? 'Saving...' : 'Save Request'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Create Task Modal */}
      {showCreateTask && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-5 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">Create New Task</h3>
            <form onSubmit={handleCreateTask}>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Task Title <span className="text-red-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 focus:border-primary-500"
                    placeholder="e.g., Build a simple Vite website"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Description
                  </label>
                  <textarea
                    value={taskDescription}
                    onChange={(e) => setTaskDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 resize-none"
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
                      className="text-xs bg-primary-500/15 hover:bg-primary-500/20 text-primary-300 px-2.5 py-1 rounded-md transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
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
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 font-mono resize-none"
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
                    className="flex-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!taskTitle.trim() || creatingTask}
                    className="flex-1 border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
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
          <div className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-5 w-full max-w-md mx-4 shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">Edit Task</h3>
            <form onSubmit={(e) => { e.preventDefault(); handleUpdateTask(editingTaskId); }}>
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Task Title *
                  </label>
                  <input
                    type="text"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 focus:border-primary-500"
                    placeholder="e.g., Design homepage"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Description
                  </label>
                  <textarea
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                    rows={3}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 resize-none"
                    placeholder="Describe what needs to be done..."
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-300 mb-1.5">
                    Step-by-Step Plan (JSON)
                  </label>
                  <textarea
                    value={editSteps}
                    onChange={(e) => setEditSteps(e.target.value)}
                    rows={4}
                    className="w-full bg-[color:var(--oc-surface-deep)] border border-[color:var(--oc-border)] rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60 resize-none font-mono"
                    placeholder='[{"step": 1, "action": "Create component"}, {"step": 2, "action": "Add styling"}]'
                  />
                </div>
                <div className="flex gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => setEditingTaskId(null)}
                    className="flex-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!editTitle.trim() || updatingTask}
                    className="flex-1 border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)] text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
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
