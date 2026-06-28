import { describe, expect, it } from 'vitest';

import {
  deriveRunState,
  deriveRunStateFromSession,
  deriveRunStateFromTask,
  getRunStateDisplay,
} from './runState';

describe('deriveRunState', () => {
  it('maps accepted workspace state to accepted product language', () => {
    expect(
      deriveRunState({ taskStatus: 'done', workspaceStatus: 'promoted' })
    ).toBe('accepted');
  });

  it('maps held change sets to needs_review', () => {
    expect(
      deriveRunState({
        taskStatus: 'done',
        workspaceStatus: 'ready',
        reviewDecision: { held_for_review: true },
        changeSet: { changed_count: 2 },
      })
    ).toBe('needs_review');
  });

  it('maps requested changes to rejected', () => {
    expect(
      deriveRunState({ taskStatus: 'done', workspaceStatus: 'changes_requested' })
    ).toBe('rejected');
  });

  it('maps failed runs with restore state to rollback_available', () => {
    expect(
      deriveRunState({ taskStatus: 'failed', rollbackAvailable: true })
    ).toBe('rollback_available');
  });

  it('maps active sessions to running', () => {
    expect(deriveRunStateFromSession({ status: 'awaiting_input' })).toBe('running');
  });

  it('maps pending sessions to the active state', () => {
    expect(deriveRunStateFromSession({ status: 'pending' })).toBe('running');
    expect(getRunStateDisplay('running').label).toBe('Active');
  });

  it('does not map stopped sessions to running', () => {
    expect(deriveRunStateFromSession({ status: 'stopped' })).toBe('failed');
    expect(deriveRunState({ sessionStatus: 'stop' })).toBe('failed');
  });

  it('does not map completed sessions to running', () => {
    expect(deriveRunStateFromSession({ status: 'completed' })).toBe('completed');
    expect(deriveRunState({ sessionStatus: 'done' })).toBe('completed');
    expect(getRunStateDisplay('completed').label).toBe('Completed');
  });

  it('maps task data without exposing change-set terms', () => {
    expect(
      deriveRunStateFromTask(
        { status: 'done', workspace_status: 'ready', task_subfolder: 'task-1' },
        null,
        { changed_count: 1 }
      )
    ).toBe('needs_review');
  });
});

describe('getRunStateDisplay', () => {
  it('returns product-facing labels for review states', () => {
    expect(getRunStateDisplay('needs_review').label).toBe('Needs Review');
    expect(getRunStateDisplay('rejected').label).toBe('Changes Requested');
  });
});
