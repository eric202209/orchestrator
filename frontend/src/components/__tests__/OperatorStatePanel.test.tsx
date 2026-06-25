import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { OperatorStatePanel } from '../SessionDetailSections'
import type { OrchestrationState } from '@/types/api'

const base = (overrides: Partial<OrchestrationState> = {}): OrchestrationState => ({
  current_phase: 'step_executing',
  terminal_reason: null,
  coordinator: null,
  is_terminal: false,
  allowed_actions: ['view_logs', 'view_timeline'],
  ...overrides,
})

describe('OperatorStatePanel', () => {
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

  const render = (state: OrchestrationState | null | undefined) => {
    act(() => {
      root.render(<OperatorStatePanel state={state} />)
    })
  }

  it('renders nothing when orchestration_state is undefined', () => {
    render(undefined)
    expect(container.textContent).toBe('')
  })

  it('renders nothing when orchestration_state is null', () => {
    render(null)
    expect(container.textContent).toBe('')
  })

  it('renders the phase chip with human-readable label', () => {
    render(base({ current_phase: 'step_executing' }))
    const chip = container.querySelector('[data-testid="operator-state-phase"]')
    expect(chip?.textContent).toBe('Executing')
  })

  it('renders awaiting_input phase chip', () => {
    render(base({ current_phase: 'awaiting_input' }))
    const chip = container.querySelector('[data-testid="operator-state-phase"]')
    expect(chip?.textContent).toBe('Awaiting Input')
  })

  it('renders unknown phase with humanized fallback', () => {
    render(base({ current_phase: 'custom_phase_x' }))
    const chip = container.querySelector('[data-testid="operator-state-phase"]')
    expect(chip?.textContent).toBe('Custom Phase X')
  })

  it('renders null phase as em-dash', () => {
    render(base({ current_phase: null }))
    const chip = container.querySelector('[data-testid="operator-state-phase"]')
    expect(chip?.textContent).toBe('—')
  })

  it('shows Active badge for non-terminal state', () => {
    render(base({ is_terminal: false }))
    const badge = container.querySelector('[data-testid="operator-state-terminal"]')
    expect(badge?.textContent).toBe('Active')
  })

  it('shows Terminal badge for terminal state', () => {
    render(base({ is_terminal: true, current_phase: 'failed' }))
    const badge = container.querySelector('[data-testid="operator-state-terminal"]')
    expect(badge?.textContent).toBe('Terminal')
  })

  it('renders coordinator when present', () => {
    render(base({ coordinator: 'ExecutionCoordinator' }))
    const el = container.querySelector('[data-testid="operator-state-coordinator"]')
    expect(el?.textContent).toBe('ExecutionCoordinator')
  })

  it('omits coordinator row when coordinator is null', () => {
    render(base({ coordinator: null }))
    expect(container.querySelector('[data-testid="operator-state-coordinator"]')).toBeNull()
  })

  it('renders terminal_reason with known label', () => {
    render(base({ is_terminal: true, terminal_reason: 'repair_churn_limit' }))
    const el = container.querySelector('[data-testid="operator-state-terminal-reason"]')
    expect(el?.textContent).toBe('Completion repair churn limit hit')
  })

  it('renders terminal_reason as humanized fallback for unknown reason', () => {
    render(base({ is_terminal: true, terminal_reason: 'some_unknown_reason' }))
    const el = container.querySelector('[data-testid="operator-state-terminal-reason"]')
    expect(el?.textContent).toBe('Some Unknown Reason')
  })

  it('omits terminal reason row when terminal_reason is null', () => {
    render(base({ terminal_reason: null }))
    expect(container.querySelector('[data-testid="operator-state-terminal-reason"]')).toBeNull()
  })

  it('renders allowed actions from server list', () => {
    render(
      base({
        allowed_actions: ['view_logs', 'pause_session', 'stop_session'],
      })
    )
    const actions = container.querySelector('[data-testid="operator-state-actions"]')
    expect(actions?.textContent).toContain('View Logs')
    expect(actions?.textContent).toContain('Pause')
    expect(actions?.textContent).toContain('Stop')
  })

  it('omits actions section when allowed_actions is empty', () => {
    render(base({ allowed_actions: [] }))
    expect(container.querySelector('[data-testid="operator-state-actions"]')).toBeNull()
  })

  it('paused session shows resume_session action (operator pause)', () => {
    render(
      base({
        current_phase: 'awaiting_input',
        allowed_actions: ['view_logs', 'view_timeline', 'resume_session', 'stop_session'],
      })
    )
    expect(container.querySelector('[data-testid="operator-action-resume_session"]')?.textContent).toBe('Resume')
    expect(container.querySelector('[data-testid="operator-action-submit_guidance"]')).toBeNull()
  })

  it('HITL session shows submit_guidance action, not resume_session', () => {
    render(
      base({
        current_phase: 'awaiting_input',
        allowed_actions: ['view_logs', 'view_timeline', 'submit_guidance', 'stop_session'],
      })
    )
    expect(container.querySelector('[data-testid="operator-action-submit_guidance"]')?.textContent).toBe('Submit Guidance')
    expect(container.querySelector('[data-testid="operator-action-resume_session"]')).toBeNull()
  })

  it('paused vs HITL distinction is driven by allowed_actions, not current_phase', () => {
    const pausedState = base({
      current_phase: 'awaiting_input',
      allowed_actions: ['resume_session'],
    })
    const hitlState = base({
      current_phase: 'awaiting_input',
      allowed_actions: ['submit_guidance'],
    })

    render(pausedState)
    expect(container.querySelector('[data-testid="operator-action-resume_session"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="operator-action-submit_guidance"]')).toBeNull()

    act(() => {
      root.render(<OperatorStatePanel state={hitlState} />)
    })

    expect(container.querySelector('[data-testid="operator-action-submit_guidance"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="operator-action-resume_session"]')).toBeNull()
  })
})
