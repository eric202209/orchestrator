import type { ChangeSetReviewDecision, Session, Task } from '@/types/api';

export type ProductRunState =
  | 'running'
  | 'failed'
  | 'needs_review'
  | 'accepted'
  | 'rejected'
  | 'rollback_available';

export interface RunStateInput {
  sessionStatus?: string | null;
  taskStatus?: string | null;
  workspaceStatus?: string | null;
  reviewDecision?: Pick<ChangeSetReviewDecision, 'held_for_review'> | null;
  changeSet?: { changed_count?: number | null } | null;
  changeDisposition?: string | null;
  rollbackAvailable?: boolean;
}

export interface ProductRunStateDisplay {
  label: string;
  description: string;
  badgeClass: string;
}

const runningStatuses = new Set([
  'pending',
  'queued',
  'running',
  'active',
  'awaiting_input',
  'paused',
]);
const failedStatuses = new Set([
  'failed',
  'error',
  'cancelled',
  'canceled',
  'stop',
  'stopped',
]);
const completedStatuses = new Set(['done', 'completed', 'complete', 'success']);
const acceptedDispositions = new Set(['accepted', 'promoted']);
const rejectedDispositions = new Set(['rejected', 'changes_requested']);

const normalize = (value?: string | null) => (value || '').trim().toLowerCase();

export function deriveRunState(input: RunStateInput): ProductRunState {
  const workspaceStatus = normalize(input.workspaceStatus);
  const changeDisposition = normalize(input.changeDisposition);
  const taskStatus = normalize(input.taskStatus);
  const sessionStatus = normalize(input.sessionStatus);
  const changedCount = Number(input.changeSet?.changed_count || 0);

  if (workspaceStatus === 'promoted' || acceptedDispositions.has(changeDisposition)) {
    return 'accepted';
  }

  if (workspaceStatus === 'changes_requested' || rejectedDispositions.has(changeDisposition)) {
    return 'rejected';
  }

  if (
    input.reviewDecision?.held_for_review ||
    (workspaceStatus === 'ready' && changedCount > 0)
  ) {
    return 'needs_review';
  }

  if (runningStatuses.has(taskStatus) || runningStatuses.has(sessionStatus)) {
    return 'running';
  }

  if (input.rollbackAvailable) {
    return 'rollback_available';
  }

  if (failedStatuses.has(taskStatus) || failedStatuses.has(sessionStatus)) {
    return 'failed';
  }

  if (completedStatuses.has(taskStatus) || completedStatuses.has(sessionStatus)) {
    return 'accepted';
  }

  return 'failed';
}

export function deriveRunStateFromTask(
  task: Pick<Task, 'status' | 'workspace_status' | 'task_subfolder'>,
  reviewDecision?: Pick<ChangeSetReviewDecision, 'held_for_review'> | null,
  changeSet?: { changed_count?: number | null } | null
): ProductRunState {
  return deriveRunState({
    taskStatus: task.status,
    workspaceStatus: task.workspace_status,
    reviewDecision,
    changeSet,
    rollbackAvailable: Boolean(task.task_subfolder && task.status === 'failed'),
  });
}

export function deriveRunStateFromSession(
  session: Pick<Session, 'status'>
): ProductRunState {
  return deriveRunState({ sessionStatus: session.status });
}

export function getRunStateDisplay(state: ProductRunState): ProductRunStateDisplay {
  switch (state) {
    case 'accepted':
      return {
        label: 'Accepted',
        description: 'Changes are accepted into the project workspace.',
        badgeClass: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
      };
    case 'rejected':
      return {
        label: 'Changes Requested',
        description: 'Changes were sent back for revision.',
        badgeClass: 'border-amber-500/30 bg-amber-500/10 text-amber-300',
      };
    case 'needs_review':
      return {
        label: 'Needs Review',
        description: 'Review the generated changes before accepting them.',
        badgeClass: 'border-primary-500/30 bg-primary-400/10 text-primary-300',
      };
    case 'rollback_available':
      return {
        label: 'Rollback Available',
        description: 'The run failed and a restore point is available.',
        badgeClass: 'border-purple-500/30 bg-purple-500/10 text-purple-200',
      };
    case 'failed':
      return {
        label: 'Failed',
        description: 'The run stopped without accepted changes.',
        badgeClass: 'border-red-500/30 bg-red-500/10 text-red-300',
      };
    case 'running':
    default:
      return {
        label: 'Active',
        description: 'Work is not in a terminal state.',
        badgeClass: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
      };
  }
}
