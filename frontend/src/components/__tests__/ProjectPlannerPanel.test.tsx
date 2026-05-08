import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { ProjectPlannerPanel } from '../ProjectPlannerPanel'
import { planningAPI, projectsAPI } from '@/api/client'

import type {
  PlannerTaskCandidate,
  PlanningCommitPreview,
  PlanningSession,
  PlanningSessionSummary,
  Plan,
  Project,
  Task,
} from '@/types/api'

vi.mock('@/api/client', () => ({
  planningAPI: {
    list: vi.fn(),
    start: vi.fn(),
    get: vi.fn(),
    respond: vi.fn(),
    cancel: vi.fn(),
    delete: vi.fn(),
    commit: vi.fn(),
  },
  plannerAPI: {
    parse: vi.fn(),
    generate: vi.fn(),
    batchCreateTasks: vi.fn(),
    deletePlan: vi.fn(),
    updatePlan: vi.fn(),
  },
  projectsAPI: {
    getPlans: vi.fn(),
  },
}))

const project: Project = {
  id: 1,
  name: 'Planner Project',
  description: 'Existing app with API and dashboard',
  github_url: null,
  branch: 'main',
  workspace_path: 'planner-project',
  created_at: '2026-04-22T00:00:00Z',
  updated_at: '2026-04-22T00:00:00Z',
}

const summary: PlanningSessionSummary = {
  id: 42,
  project_id: 1,
  title: 'Improve planner',
  prompt: 'Improve planner',
  status: 'waiting_for_input',
  source_brain: 'local',
  current_prompt_id: 'prompt-1',
  finalized_plan_id: null,
  committed_at: null,
  completed_at: null,
  created_at: '2026-04-22T00:00:00Z',
  updated_at: '2026-04-22T00:00:00Z',
}

function createWaitingSession(): PlanningSession {
  return {
    ...summary,
    last_error: null,
    messages: [
      {
        id: 1,
        role: 'user',
        content: 'Improve planner',
        prompt_id: null,
        metadata_json: { kind: 'prompt' },
        created_at: '2026-04-22T00:00:00Z',
      },
      {
        id: 2,
        role: 'assistant',
        content: 'Which rollout constraints matter most?',
        prompt_id: 'prompt-1',
        metadata_json: { kind: 'clarifying_question' },
        created_at: '2026-04-22T00:00:01Z',
      },
    ],
    artifacts: [],
    tasks_preview: [],
    committed_task_ids: [],
  }
}

function createCompletedSession(): PlanningSession {
  const tasks: PlannerTaskCandidate[] = [
    {
      title: 'Add planning worker',
      description: 'Queue planning in the background',
      execution_profile: 'full_lifecycle',
      priority: 1,
      plan_position: 1,
      estimated_effort: 'medium',
      include: true,
    },
  ]

  return {
    ...summary,
    status: 'completed',
    current_prompt_id: null,
    completed_at: '2026-04-22T00:02:00Z',
    last_error: null,
    messages: [
      {
        id: 1,
        role: 'user',
        content: 'Improve planner',
        prompt_id: null,
        metadata_json: { kind: 'prompt' },
        created_at: '2026-04-22T00:00:00Z',
      },
    ],
    artifacts: [
      {
        id: 1,
        artifact_type: 'requirements',
        filename: 'requirements.md',
        content: '# Requirements',
        created_at: '2026-04-22T00:01:00Z',
      },
      {
        id: 2,
        artifact_type: 'planner_markdown',
        filename: 'planner.md',
        content:
          '# Project: Planner Project\n\n## Task List\n- [ ] TASK_START: Add planning worker | Queue planning in the background | order=1 | P1 | effort=medium | profile=full_lifecycle',
        created_at: '2026-04-22T00:01:01Z',
      },
    ],
    tasks_preview: tasks,
    committed_task_ids: [],
  }
}

function createActiveSession(): PlanningSession {
  return {
    ...summary,
    status: 'active',
    current_prompt_id: null,
    last_error: null,
    messages: [
      {
        id: 1,
        role: 'user',
        content: 'Improve planner',
        prompt_id: null,
        metadata_json: { kind: 'prompt' },
        created_at: '2026-04-22T00:00:00Z',
      },
    ],
    artifacts: [],
    tasks_preview: [],
    committed_task_ids: [],
  }
}

const mockProjectsGetPlans = projectsAPI.getPlans as Mock
const mockPlanningList = planningAPI.list as Mock
const mockPlanningGet = planningAPI.get as Mock
const mockPlanningCommit = planningAPI.commit as Mock
const mockPlanningDelete = planningAPI.delete as Mock

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve()
  })
}

describe('ProjectPlannerPanel', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    vi.stubGlobal('confirm', vi.fn(() => true))
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    mockProjectsGetPlans.mockResolvedValue({ data: [] as Plan[] })
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    container.remove()
    vi.unstubAllGlobals()
  })

  it('renders the pending planning question for a waiting session', async () => {
    mockPlanningList.mockResolvedValue({ data: [summary] })
    mockPlanningGet.mockResolvedValue({ data: createWaitingSession() })

    await act(async () => {
      root.render(<ProjectPlannerPanel project={project} onTasksCommitted={vi.fn()} />)
    })
    await flush()

    expect(container.textContent).toContain('Pending question')
    expect(container.textContent).toContain('Which rollout constraints matter most?')
    expect(container.textContent).toContain('Improve planner')
  })

  it('renders the background processing status for an active session', async () => {
    mockPlanningList.mockResolvedValue({
      data: [{ ...summary, status: 'active', current_prompt_id: null }],
    })
    mockPlanningGet.mockResolvedValue({ data: createActiveSession() })

    await act(async () => {
      root.render(<ProjectPlannerPanel project={project} onTasksCommitted={vi.fn()} />)
    })
    await flush()

    expect(container.textContent).toContain(
      'The planner is generating markdown and task preview directly from your prompt.'
    )
  })

  it('commits selected tasks from a completed session', async () => {
    const completedSession = createCompletedSession()
    const committedTasks: Task[] = [
      {
        id: 9,
        project_id: 1,
        title: 'Add planning worker',
        description: 'Queue planning in the background',
        status: 'pending',
        execution_profile: 'full_lifecycle',
        priority: 1,
        estimated_effort: 'medium',
        plan_position: 1,
        steps: null,
        current_step: 0,
        error_message: null,
        workspace_status: 'isolated',
        promotion_note: null,
        promoted_at: null,
        created_at: '2026-04-22T00:03:00Z',
        updated_at: null,
        started_at: null,
        completed_at: null,
        task_subfolder: null,
      },
    ]
    const commitPayload: PlanningCommitPreview = {
      ...completedSession,
      committed_task_ids: [9],
      plan: null,
      tasks: committedTasks,
    }

    mockPlanningList.mockResolvedValue({
      data: [{ ...summary, status: 'completed', current_prompt_id: null }],
    })
    mockPlanningGet.mockResolvedValue({ data: completedSession })
    mockPlanningCommit.mockResolvedValue({ data: commitPayload })

    const onTasksCommitted = vi.fn()
    await act(async () => {
      root.render(
        <ProjectPlannerPanel project={project} onTasksCommitted={onTasksCommitted} />
      )
    })
    await flush()

    const buttons = Array.from(container.querySelectorAll('button'))
    const commitButton = buttons.find((button) =>
      button.textContent?.includes('Commit Tasks to Project')
    )
    expect(commitButton).toBeTruthy()

    await act(async () => {
      commitButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await flush()

    expect(planningAPI.commit).toHaveBeenCalledWith(
      42,
      expect.objectContaining({
        planner_markdown: expect.stringContaining('TASK_START: Add planning worker'),
        selected_tasks: [
          expect.objectContaining({
            title: 'Add planning worker',
          }),
        ],
      })
    )
    expect(onTasksCommitted).toHaveBeenCalledWith(committedTasks)
  })

  it('allows discarding a completed uncommitted planning session', async () => {
    const completedSession = createCompletedSession()
    mockPlanningList.mockResolvedValue({
      data: [{ ...summary, status: 'completed', current_prompt_id: null }],
    })
    mockPlanningGet.mockResolvedValue({ data: completedSession })
    mockPlanningDelete.mockResolvedValue({ data: null })

    await act(async () => {
      root.render(<ProjectPlannerPanel project={project} onTasksCommitted={vi.fn()} />)
    })
    await flush()

    expect(container.textContent).toContain('ready for review')
    const buttons = Array.from(container.querySelectorAll('button'))
    const discardButton = buttons.find((button) => button.textContent?.includes('Discard'))
    expect(discardButton).toBeTruthy()

    await act(async () => {
      discardButton?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await flush()

    expect(planningAPI.delete).toHaveBeenCalledWith(42)
  })
})
