import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { HumanGuidanceDashboard } from '../HumanGuidanceDashboard'
import { guidanceAPI } from '@/api/client'

import type {
  HumanGuidanceActivation,
  HumanGuidanceReadiness,
  HumanGuidanceEntry,
  HumanGuidanceConflict,
} from '@/types/api'

vi.mock('@/api/client', () => ({
  guidanceAPI: {
    getReadiness: vi.fn(),
    patchActivation: vi.fn(),
    disableActivation: vi.fn(),
    list: vi.fn(),
    create: vi.fn(),
    patch: vi.fn(),
    archive: vi.fn(),
    getHistory: vi.fn(),
    getRendered: vi.fn(),
    listConflicts: vi.fn(),
    patchConflict: vi.fn(),
  },
}))

const activation: HumanGuidanceActivation = {
  id: 1,
  scope: 'project',
  project_id: 42,
  session_id: null,
  table_enabled: true,
  persistence_enabled: true,
  render_enabled: true,
  injection_enabled: true,
  conflict_detection_enabled: true,
  status: 'active',
  enabled_by: 'test@example.com',
  disabled_at: null,
  disabled_by: null,
  created_at: '2026-06-17T00:00:00Z',
  updated_at: '2026-06-17T00:00:00Z',
}

const readiness: HumanGuidanceReadiness = {
  project_id: 42,
  active_guidance_count: 3,
  is_ready: true,
  checks: { table_enabled: true, injection_enabled: true },
  activation,
  backend_statistics: {
    backend: 'all',
    model_family: 'all',
    matching_guidance: 3,
    filtered_guidance: 0,
    filtered_ids: [],
  },
  purpose_statistics: {
    all: 1,
    planning: 2,
    execution: 1,
    repair: 0,
    validation: 0,
  },
  warnings: [],
}

const guidanceEntry: HumanGuidanceEntry = {
  id: 7,
  project_id: 42,
  session_id: null,
  task_id: null,
  scope: 'project',
  message: 'Never use mutable default arguments.',
  status: 'active',
  priority: 50,
  created_at: '2026-06-17T00:00:00Z',
  updated_at: null,
  expires_at: null,
  created_by: 'test@example.com',
  revision: 1,
  backend_targets: ['all'],
  model_targets: ['all'],
  purpose_targets: ['planning'],
}

const conflictEntry: HumanGuidanceConflict = {
  id: 3,
  guidance_id: 7,
  guidance_message: 'Never use mutable default arguments.',
  task_id: 10,
  task_title: 'Add function with default list arg',
  conflict_excerpt: 'def append(items=[]):',
  conflict_patterns: ['= []'],
  severity: 'warning',
  status: 'open',
  detected_at: '2026-06-17T00:00:00Z',
  resolved: false,
}

function setupMocks({
  conflictItems = [],
  guidanceItems = [guidanceEntry],
}: {
  conflictItems?: HumanGuidanceConflict[];
  guidanceItems?: HumanGuidanceEntry[];
} = {}) {
  ;(guidanceAPI.getReadiness as Mock).mockResolvedValue({ data: readiness })
  ;(guidanceAPI.list as Mock).mockResolvedValue({ data: { project_id: 42, total: guidanceItems.length, items: guidanceItems } })
  ;(guidanceAPI.listConflicts as Mock).mockResolvedValue({ data: { project_id: 42, total: conflictItems.length, items: conflictItems } })
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
})

afterEach(() => {
  act(() => { root.unmount() })
  container.remove()
  vi.clearAllMocks()
})

describe('HumanGuidanceDashboard', () => {
  it('shows loading spinner before data resolves', () => {
    // Never resolve — keep pending
    ;(guidanceAPI.getReadiness as Mock).mockReturnValue(new Promise(() => {}))
    ;(guidanceAPI.list as Mock).mockReturnValue(new Promise(() => {}))
    ;(guidanceAPI.listConflicts as Mock).mockReturnValue(new Promise(() => {}))

    act(() => { root.render(<HumanGuidanceDashboard projectId={42} />) })
    expect(container.querySelector('.animate-spin')).toBeTruthy()
  })

  it('renders readiness panel after data loads', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const text = container.textContent ?? ''
    expect(text).toContain('Human Guidance')
    expect(text).toContain('Active')
    expect(text).toContain('Readiness')
  })

  it('shows active guidance count from readiness', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    // active_guidance_count is 3
    expect(container.textContent).toContain('3')
  })

  it('shows inactive badge when is_ready is false', async () => {
    ;(guidanceAPI.getReadiness as Mock).mockResolvedValue({
      data: { ...readiness, is_ready: false, activation: { ...activation, status: 'disabled' } },
    })
    ;(guidanceAPI.list as Mock).mockResolvedValue({ data: { project_id: 42, total: 0, items: [] } })
    ;(guidanceAPI.listConflicts as Mock).mockResolvedValue({ data: { project_id: 42, total: 0, items: [] } })

    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })
    expect(container.textContent).toContain('Inactive')
  })

  it('renders guidance entry message in list', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('Never use mutable default arguments.')
  })

  it('renders purpose_targets in guidance row', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('planning')
  })

  it('shows Add guidance button', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const addBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Add guidance'),
    )
    expect(addBtn).toBeTruthy()
  })

  it('opens add guidance modal when Add button clicked', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const addBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Add guidance'),
    )
    await act(async () => { addBtn?.click() })

    expect(container.textContent).toContain('Add guidance')
    expect(container.querySelector('textarea')).toBeTruthy()
  })

  it('shows conflict badge when open conflicts exist', async () => {
    setupMocks({ conflictItems: [conflictEntry] })
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('conflict')
  })

  it('shows no conflicts message when conflicts panel expanded and empty', async () => {
    setupMocks({ conflictItems: [] })
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const conflictsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.toLowerCase().includes('conflicts'),
    )
    await act(async () => { conflictsBtn?.click() })

    expect(container.textContent).toContain('No open conflicts')
  })

  it('shows conflict entry when conflicts panel expanded with conflicts', async () => {
    setupMocks({ conflictItems: [conflictEntry] })
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const conflictsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.toLowerCase().includes('conflicts'),
    )
    await act(async () => { conflictsBtn?.click() })

    expect(container.textContent).toContain('Add function with default list arg')
  })

  it('calls patchConflict on resolve', async () => {
    ;(guidanceAPI.patchConflict as Mock).mockResolvedValue({ data: {} })
    setupMocks({ conflictItems: [conflictEntry] })
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const conflictsBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.toLowerCase().includes('conflicts'),
    )
    await act(async () => { conflictsBtn?.click() })

    const resolveBtn = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Resolve',
    )
    await act(async () => { resolveBtn?.click() })

    expect(guidanceAPI.patchConflict).toHaveBeenCalledWith(42, 3, { status: 'resolved' })
  })

  it('shows error state when API fails', async () => {
    ;(guidanceAPI.getReadiness as Mock).mockRejectedValue(new Error('Network error'))
    ;(guidanceAPI.list as Mock).mockRejectedValue(new Error('Network error'))
    ;(guidanceAPI.listConflicts as Mock).mockRejectedValue(new Error('Network error'))

    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('Failed to load guidance data')
  })

  it('shows Advanced mode toggle', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('Advanced mode')
  })

  it('shows purpose statistics in readiness panel', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    expect(container.textContent).toContain('Planning')
    expect(container.textContent).toContain('Execution')
    expect(container.textContent).toContain('Repair')
  })

  it('calls guidanceAPI.list with correct filter when status tab changes', async () => {
    setupMocks()
    await act(async () => { root.render(<HumanGuidanceDashboard projectId={42} />) })

    const allTab = Array.from(container.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'all',
    )
    await act(async () => { allTab?.click() })

    expect(guidanceAPI.list).toHaveBeenCalledWith(42, { status: 'all', limit: 50 })
  })
})
