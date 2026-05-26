import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FailureSummaryPanel, SessionAdvancedPanel } from '../SessionDetailSections'

import type { ExecutionFailureSummary, SessionDecisionEvent } from '@/types/api'

const baseSummary = (
  overrides: Partial<ExecutionFailureSummary>
): ExecutionFailureSummary => ({
  session_id: 9,
  summary: 'Task error: stopped before completion.',
  operator_feedback: null,
  generated_at: '2026-05-13T00:00:00Z',
  feedback_at: null,
  replan_planning_session_id: null,
  ...overrides,
})

describe('FailureSummaryPanel', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => {
      root.unmount()
    })
    container.remove()
  })

  const renderPanel = (summary: ExecutionFailureSummary) => {
    act(() => {
      root.render(
        <FailureSummaryPanel
          summary={summary}
          loading={false}
          onFeedbackSubmit={vi.fn(async () => undefined)}
          onReplan={vi.fn(async () => undefined)}
          onOpenProjectArchitect={vi.fn()}
        />
      )
    })
  }

  it('shows planning validation failure evidence with replan action', () => {
    renderPanel(
      baseSummary({
        summary: 'Task error: Plan validation failed after repair.',
        diagnostics: {
          reason: 'planning validation failure',
          validation_reasons: ['Plan contains placeholder implementation'],
          task_execution_id: 101,
        },
      })
    )

    expect(container.textContent).toContain('Operator evidence')
    expect(container.textContent).toContain('Planning Validation')
    expect(container.textContent).toContain('Failed')
    expect(container.textContent).toContain('Send to Project Architect')
    expect(container.textContent).toContain('planning validation failure')
  })

  it('shows structured operation name, path, and already-applied state', () => {
    renderPanel(
      baseSummary({
        summary: 'Task error: replace_in_file old text not found in package.json.',
        diagnostics: {
          reason: 'replace_in_file old text not found in package.json',
          op_name: 'replace_in_file',
          target_path: 'package.json',
          already_applied: true,
          task_execution_id: 102,
        },
      })
    )

    expect(container.textContent).toContain('Structured Operation')
    expect(container.textContent).toContain('replace_in_file')
    expect(container.textContent).toContain('package.json')
    expect(container.textContent).toContain('Already applied')
  })

  it('shows completion repair failure evidence', () => {
    renderPanel(
      baseSummary({
        summary: 'Task error: Completion repair failed: repair_attempt_limit_reached.',
        diagnostics: {
          reason: 'completion repair failed',
          failure_class: 'completion_repair',
          outcome: 'failed',
          task_execution_id: 103,
        },
      })
    )

    expect(container.textContent).toContain('Completion Repair')
    expect(container.textContent).toContain('Failed')
    expect(container.textContent).toContain('completion repair failed')
  })

  it('shows existing replan recovery handoff for stopped sessions', () => {
    renderPanel(
      baseSummary({
        summary: 'Stopped session recovery needed after terminal failure.',
        replan_planning_session_id: 55,
        replan_planning_session_status: 'active',
        diagnostics: {
          reason: 'stopped session recovery needed',
          outcome: 'needs operator decision',
          task_execution_id: 104,
        },
      })
    )

    expect(container.textContent).toContain('Stopped Session Recovery')
    expect(container.textContent).toContain('Needs Operator Decision')
    expect(container.textContent).toContain('Open Project Architect')
    expect(container.textContent).toContain('planning session #55')
  })

  it('shows workspace guard blocked state as inspect-workspace evidence', () => {
    renderPanel(
      baseSummary({
        summary: 'Workspace guard blocked a write outside the project.',
        diagnostics: {
          reason: 'workspace guard blocked write_file outside declared scope',
          boundary: 'workspace_guard',
          op_name: 'write_file',
          target_path: '../outside.txt',
          workspace_guard_blocked: true,
          task_execution_id: 105,
        },
      })
    )

    expect(container.textContent).toContain('Workspace Guard')
    expect(container.textContent).toContain('Blocked by workspace guard')
    expect(container.textContent).toContain('write_file')
    expect(container.textContent).toContain('../outside.txt')
    expect(container.textContent).toContain('Inspect workspace')
  })

  it('shows regex fallback state for structured operations', () => {
    renderPanel(
      baseSummary({
        summary: 'replace_in_file app_config.py used a regex replacement.',
        diagnostics: {
          reason: 'replace_in_file regex fallback applied',
          op_name: 'replace_in_file',
          target_path: 'app_config.py',
          regex_fallback_applied: true,
          task_execution_id: 106,
        },
      })
    )

    expect(container.textContent).toContain('Structured Operation')
    expect(container.textContent).toContain('Regex fallback applied')
    expect(container.textContent).toContain('replace_in_file')
    expect(container.textContent).toContain('app_config.py')
  })

  it('shows multiple structured operations when diagnostics include an ops list', () => {
    renderPanel(
      baseSummary({
        summary: 'Structured operation batch failed.',
        diagnostics: {
          reason: 'append_file target missing',
          failure_boundary: 'structured_operation',
          outcome: 'failed',
          ops: [
            { op: 'write_file', path: 'package.json', status: 'applied' },
            { op: 'append_file', path: 'README.md', status: 'failed' },
          ],
        },
      })
    )

    expect(container.textContent).toContain('Structured Operation')
    expect(container.textContent).toContain('write_file package.json (applied)')
    expect(container.textContent).toContain('append_file README.md (failed)')
  })
})

describe('SessionAdvancedPanel operator evidence', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(() => {
    act(() => {
      root.unmount()
    })
    container.remove()
  })

  const renderTimeline = (event: SessionDecisionEvent) => {
    act(() => {
      root.render(
        <SessionAdvancedPanel
          decisionEvents={[event]}
          formatDateTime={(value) => value || ''}
          open
          timelineEvents={[]}
        />
      )
    })
  }

  it('surfaces decision timeline structured-op evidence without opening raw details', () => {
    renderTimeline({
      id: 'event-1',
      session_id: 9,
      task_id: 3,
      timestamp: '2026-05-13T00:00:00Z',
      phase: 'execution',
      event_type: 'operation_failed',
      decision_type: 'halt',
      title: 'Structured operation failed',
      summary: 'replace_in_file old text not found in package.json',
      status: 'failed',
      severity: 'error',
      source: 'orchestration',
      related_event_ids: [],
      knowledge_usage_ids: [],
      details: {
        operations: [
          { op: 'replace_in_file', path: 'package.json', status: 'failed' },
        ],
        reason: 'old text not found',
      },
    })

    expect(container.textContent).toContain('Boundary:')
    expect(container.textContent).toContain('Structured Operation')
    expect(container.textContent).toContain('Outcome:')
    expect(container.textContent).toContain('Failed')
    expect(container.textContent).toContain('replace_in_file')
    expect(container.textContent).toContain('package.json')
    expect(container.textContent).toContain('replace_in_file package.json (failed)')
    expect(container.textContent).toContain('Review recovery path')
  })
})
