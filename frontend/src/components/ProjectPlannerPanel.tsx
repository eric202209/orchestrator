import { useCallback, useEffect, useMemo, useState } from 'react'
import { formatDistanceToNow } from 'date-fns'
import {
  ArrowDown,
  ArrowUp,
  Brain,
  CheckSquare,
  FileText,
  Sparkles,
  Trash2,
  Copy,
  Wand2,
  X,
} from 'lucide-react'

import { plannerAPI, projectsAPI } from '@/api/client'
import { Button, Input, TextArea } from '@/components/ui'
import Card from '@/components/ui/Card'
import type { Plan, PlannerTaskCandidate, Project, Task } from '@/types/api'

interface ProjectPlannerPanelProps {
  project: Project
  onTasksCommitted: (tasks: Task[]) => void
}

const defaultRequirement = 'Add a new planning workflow that converts a high-level requirement into executable project tasks.'
const executionProfiles = [
  { value: 'full_lifecycle', label: 'Full Lifecycle' },
  { value: 'execute_only', label: 'Execute Only' },
  { value: 'test_only', label: 'Test Only' },
  { value: 'debug_only', label: 'Debug Only' },
  { value: 'review_only', label: 'Review Only' },
] as const

const sortDraftTasks = (tasks: PlannerTaskCandidate[]) =>
  [...tasks].sort((left, right) => {
    const leftPosition = left.plan_position ?? Number.MAX_SAFE_INTEGER
    const rightPosition = right.plan_position ?? Number.MAX_SAFE_INTEGER
    if (leftPosition !== rightPosition) {
      return leftPosition - rightPosition
    }

    if (left.priority !== right.priority) {
      return right.priority - left.priority
    }

    return (left.title || '').localeCompare(right.title || '')
  })

const normalizeDraftTasks = (tasks: PlannerTaskCandidate[]) =>
  sortDraftTasks(tasks).map((task, index) => ({
    ...task,
    plan_position: index + 1,
    // Preserve the include field if it exists, default to true for backward compatibility
    include: task.include ?? true,
  }))

export function ProjectPlannerPanel({
  project,
  onTasksCommitted,
}: ProjectPlannerPanelProps) {
  const [requirement, setRequirement] = useState(project.description || defaultRequirement)
  const [sourceBrain, setSourceBrain] = useState<'local' | 'cloud'>('local')
  const [markdown, setMarkdown] = useState('')
  const [draftTasks, setDraftTasks] = useState<PlannerTaskCandidate[]>([])
  const [plans, setPlans] = useState<Plan[]>([])
  const [activePlan, setActivePlan] = useState<Plan | null>(null)
  const [loadingPlans, setLoadingPlans] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [parsing, setParsing] = useState(false)
  const [committing, setCommitting] = useState(false)
  const [deletingPlanId, setDeletingPlanId] = useState<number | null>(null)
  const [savingPlan, setSavingPlan] = useState(false)
  const [draggedTaskIndex, setDraggedTaskIndex] = useState<number | null>(null)

  const resetPlannerEditor = () => {
    setActivePlan(null)
    setRequirement(project.description || defaultRequirement)
    setSourceBrain('local')
    setMarkdown('')
    setDraftTasks([])
  }

  const loadPlans = useCallback(async () => {
    try {
      setLoadingPlans(true)
      const response = await projectsAPI.getPlans(project.id)
      setPlans(response.data || [])
    } catch (error) {
      console.error('Failed to load project plans:', error)
    } finally {
      setLoadingPlans(false)
    }
  }, [project.id])

  useEffect(() => {
    loadPlans()
  }, [loadPlans])

  const handleGenerate = async () => {
    if (!requirement.trim()) {
      alert('Add a requirement first so the planner has something to work from.')
      return
    }

    try {
      setGenerating(true)
      const response = await plannerAPI.generate({
        project_id: project.id,
        requirement: requirement.trim(),
        source_brain: sourceBrain,
      })
      setActivePlan(response.data.plan)
      setMarkdown(response.data.plan.markdown)
      setDraftTasks(
        normalizeDraftTasks((response.data.tasks_preview || []).map((task) => ({
          ...task,
          include: task.include ?? true,
        })))
      )
      await loadPlans()
    } catch (error) {
      console.error('Failed to generate plan:', error)
      alert('Failed to generate planner markdown. Please try again.')
    } finally {
      setGenerating(false)
    }
  }

  const handleParse = async () => {
    if (!markdown.trim()) {
      alert('Paste or generate markdown first.')
      return
    }

    try {
      setParsing(true)
      const response = await plannerAPI.parse(markdown)
      setDraftTasks(
        normalizeDraftTasks((response.data.tasks || []).map((task) => ({
          ...task,
          include: task.include ?? true,
        })))
      )
    } catch (error) {
      console.error('Failed to parse markdown:', error)
      alert('Planner markdown could not be parsed. Check the task list format and try again.')
    } finally {
      setParsing(false)
    }
  }

  const updateDraftTask = (
    index: number,
    field: keyof PlannerTaskCandidate,
    value: string | number | boolean
  ) => {
    setDraftTasks((current) => {
      const updatedTasks = current.map((task, taskIndex) =>
        taskIndex === index
          ? {
              ...task,
              [field]: value,
            }
          : task
      )
      return field === 'plan_position' ? normalizeDraftTasks(updatedTasks) : updatedTasks
    })
  }

  const duplicateDraftTask = (index: number) => {
    setDraftTasks((current) => {
      const sourceTask = current[index]
      if (!sourceTask) {
        return current
      }

      const duplicatedTask: PlannerTaskCandidate = {
        ...sourceTask,
        title: sourceTask.title ? `${sourceTask.title} (Copy)` : '',
      }
      const reordered = [...current]
      reordered.splice(index + 1, 0, duplicatedTask)
      return normalizeDraftTasks(reordered)
    })
  }

  const addDraftTask = () => {
    setDraftTasks((current) =>
      normalizeDraftTasks([
        ...current,
        {
          title: '',
          description: '',
          execution_profile: 'full_lifecycle',
          priority: 0,
          plan_position: current.length + 1,
          estimated_effort: 'medium',
          include: true,
        },
      ])
    )
  }

  const removeDraftTask = (index: number) => {
    setDraftTasks((current) =>
      normalizeDraftTasks(current.filter((_, taskIndex) => taskIndex !== index))
    )
  }

  const moveDraftTask = (index: number, direction: 'up' | 'down') => {
    setDraftTasks((current) => {
      const nextIndex = direction === 'up' ? index - 1 : index + 1
      if (nextIndex < 0 || nextIndex >= current.length) {
        return current
      }

      const reordered = [...current]
      const [movedTask] = reordered.splice(index, 1)
      reordered.splice(nextIndex, 0, movedTask)
      return reordered.map((task, taskIndex) => ({
        ...task,
        plan_position: taskIndex + 1,
      }))
    })
  }

  const applySmartSort = () => {
    setDraftTasks((current) => normalizeDraftTasks(current))
  }

  const setAllDraftInclusion = (include: boolean) => {
    setDraftTasks((current) =>
      current.map((task) => ({
        ...task,
        include,
      }))
    )
  }

  const buildMarkdownFromDraftTasks = (tasks: PlannerTaskCandidate[]) => {
    const normalizedTasks = normalizeDraftTasks(tasks)
      const taskLines = normalizedTasks.map((task) => {
        const title = (task.title || 'Untitled task').trim()
        const description = (task.description || '').trim()
        const priority = `P${task.priority}`
        const effort = task.estimated_effort || 'medium'
        const order = task.plan_position || 0
      return `- [ ] TASK_START: ${title} | ${description || `Implement ${title.toLowerCase()}`} | order=${order} | ${priority} | effort=${effort} | profile=${task.execution_profile || 'full_lifecycle'}`
      })

    if (!markdown.trim()) {
      return [
        `# Project: ${project.name}`,
        '',
        '## Overview',
        requirement.trim() || project.description || 'Planner draft',
        '',
        '## Task List',
        ...taskLines,
      ].join('\n')
    }

    if (/^##\s+Task List\s*$/m.test(markdown)) {
      return markdown.replace(
        /^##\s+Task List\s*$[\s\S]*?(?=^##\s+|$)/m,
        `## Task List\n${taskLines.join('\n')}\n`
      ).trim()
    }

    return `${markdown.trim()}\n\n## Task List\n${taskLines.join('\n')}`.trim()
  }

  const syncDraftTasksToMarkdown = () => {
    const normalizedTasks = normalizeDraftTasks(draftTasks)
    setDraftTasks(normalizedTasks)
    setMarkdown(buildMarkdownFromDraftTasks(normalizedTasks))
  }

  const handleSavePlan = async () => {
    if (!activePlan) {
      alert('Generate or load a plan first.')
      return
    }

    try {
      setSavingPlan(true)
      const normalizedTasks = normalizeDraftTasks(draftTasks)
      const nextMarkdown = buildMarkdownFromDraftTasks(normalizedTasks)
      const response = await plannerAPI.updatePlan(project.id, activePlan.id, {
        title: requirement.trim().slice(0, 255) || activePlan.title,
        requirement: requirement.trim() || activePlan.requirement,
        markdown: nextMarkdown,
        source_brain: sourceBrain,
        status: activePlan.status,
      })
      setActivePlan(response.data)
      setMarkdown(response.data.markdown)
      setDraftTasks(normalizedTasks)
      await loadPlans()
      alert('Plan saved.')
    } catch (error) {
      console.error('Failed to save plan:', error)
      alert('Failed to save the current plan.')
    } finally {
      setSavingPlan(false)
    }
  }

  const handleCommit = async () => {
    const includedTasks = draftTasks
      .filter((task) => task.include !== false && task.title.trim())
      .sort((left, right) => (left.plan_position ?? 9999) - (right.plan_position ?? 9999))
      .map((task) => ({
        ...task,
        title: task.title.trim(),
        description: task.description?.trim() || '',
      }))

    if (includedTasks.length === 0) {
      alert('Select or add at least one valid task before committing.')
      return
    }

    try {
      setCommitting(true)
      const response = await plannerAPI.batchCreateTasks(project.id, {
        plan_id: activePlan?.id,
        markdown: markdown || undefined,
        plan_title: activePlan?.title || requirement.trim() || `${project.name} planner draft`,
        requirement: requirement.trim() || activePlan?.requirement,
        source_brain: sourceBrain,
        tasks: includedTasks,
      })
      onTasksCommitted(response.data.tasks || [])
      await loadPlans()
      alert(`Added ${response.data.tasks.length} task${response.data.tasks.length === 1 ? '' : 's'} to the project.`)
    } catch (error) {
      console.error('Failed to commit planner tasks:', error)
      alert('Failed to add planner tasks to the project. Please try again.')
    } finally {
      setCommitting(false)
    }
  }

  const loadPlanIntoEditor = async (plan: Plan) => {
    if (activePlan?.id === plan.id) {
      resetPlannerEditor()
      return
    }

    setActivePlan(plan)
    setRequirement(plan.requirement)
    setSourceBrain((plan.source_brain as 'local' | 'cloud') || 'local')
    setMarkdown(plan.markdown)
    try {
      setParsing(true)
      const response = await plannerAPI.parse(plan.markdown)
      setDraftTasks(
        normalizeDraftTasks((response.data.tasks || []).map((task) => ({
          ...task,
          include: true,
        })))
      )
    } catch (error) {
      console.error('Failed to parse saved plan:', error)
      setDraftTasks([])
    } finally {
      setParsing(false)
    }
  }

  const handleDeletePlan = async (plan: Plan) => {
    const confirmed = window.confirm(`Delete plan "${plan.title}" from Recent Plans?`)
    if (!confirmed) {
      return
    }

    try {
      setDeletingPlanId(plan.id)
      await plannerAPI.deletePlan(project.id, plan.id)
      if (activePlan?.id === plan.id) {
        resetPlannerEditor()
      }
      await loadPlans()
    } catch (error) {
      console.error('Failed to delete plan:', error)
      alert('Failed to delete the selected plan.')
    } finally {
      setDeletingPlanId(null)
    }
  }

  const includedCount = draftTasks.filter((task) => task.include !== false).length
  const validationIssues = useMemo(() => {
    const issues: string[] = []
    const titleCounts = new Map<string, number>()
    const positionCounts = new Map<number, number>()

    for (const task of draftTasks) {
      const normalizedTitle = (task.title || '').trim().toLowerCase()
      if (!normalizedTitle) {
        issues.push('Some draft tasks are missing a title.')
      } else {
        titleCounts.set(normalizedTitle, (titleCounts.get(normalizedTitle) || 0) + 1)
      }

      if (task.plan_position) {
        positionCounts.set(
          task.plan_position,
          (positionCounts.get(task.plan_position) || 0) + 1
        )
      }
    }

    if ([...titleCounts.values()].some((count) => count > 1)) {
      issues.push('Duplicate task titles detected.')
    }
    if ([...positionCounts.values()].some((count) => count > 1)) {
      issues.push('Duplicate order numbers detected. Use Sort + Renumber to normalize.')
    }
    return [...new Set(issues)]
  }, [draftTasks])

  return (
    <div className="grid grid-cols-1 gap-6 2xl:grid-cols-[minmax(0,1.25fr)_360px]">
      <div className="space-y-6">
        <Card className="rounded-2xl border-slate-700/80 bg-slate-800/60 p-6">
          <div className="mb-5 flex items-start justify-between gap-4">
            <div>
              <div className="mb-2 flex items-center gap-2 text-sm font-medium text-amber-300">
                <Sparkles className="h-4 w-4" />
                Project Architect
              </div>
              <h2 className="text-2xl font-semibold text-white">Draft an execution-ready plan</h2>
              <p className="mt-2 max-w-2xl text-sm text-slate-400">
                Describe the outcome you want. The planner will turn it into structured markdown and a task list you can review before it touches the project.
              </p>
            </div>
            <div className="rounded-xl border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-right text-xs text-amber-100">
              <div className="font-medium">Workflow</div>
              <div>Draft → Parse → Review → Commit</div>
            </div>
          </div>

          <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_220px]">
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-300">High-level requirement</label>
              <TextArea
                value={requirement}
                onChange={(event) => setRequirement(event.target.value)}
                className="min-h-[140px]"
                placeholder="Add a new module for user profile images with S3 upload and mobile approval notifications."
              />
            </div>
            <div className="space-y-4">
              <div>
                <label className="mb-2 block text-sm font-medium text-slate-300">Brain selector</label>
                <div className="grid gap-2">
                  <button
                    type="button"
                    onClick={() => setSourceBrain('local')}
                    className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                      sourceBrain === 'local'
                        ? 'border-primary-500 bg-primary-500/10 text-white'
                        : 'border-slate-700 bg-slate-900/70 text-slate-300 hover:border-slate-600'
                    }`}
                  >
                    <div className="flex items-center gap-2 font-medium">
                      <Brain className="h-4 w-4" />
                      Local
                    </div>
                    <div className="mt-1 text-xs text-slate-400">Private planning for repo-aware refinement</div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setSourceBrain('cloud')}
                    className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                      sourceBrain === 'cloud'
                        ? 'border-sky-500 bg-sky-500/10 text-white'
                        : 'border-slate-700 bg-slate-900/70 text-slate-300 hover:border-slate-600'
                    }`}
                  >
                    <div className="flex items-center gap-2 font-medium">
                      <Sparkles className="h-4 w-4" />
                      Cloud
                    </div>
                    <div className="mt-1 text-xs text-slate-400">Architecture-first drafting for greenfield ideas</div>
                  </button>
                </div>
              </div>
              <div className="rounded-xl border border-slate-700 bg-slate-900/60 p-4 text-sm text-slate-400">
                <div className="mb-1 font-medium text-slate-200">Current project</div>
                <div>{project.name}</div>
                <div className="mt-2 text-xs">
                  Plans are stored as markdown so you can reload, edit, and recommit them later.
                </div>
              </div>
            </div>
          </div>

          <div className="mt-5 flex flex-wrap gap-3">
            <Button onClick={handleGenerate} disabled={generating || !requirement.trim()}>
              {generating ? 'Generating...' : 'Generate Blueprint'}
            </Button>
            <Button variant="outline" onClick={handleParse} disabled={parsing || !markdown.trim()}>
              {parsing ? 'Parsing...' : 'Parse Task List'}
            </Button>
            <Button variant="ghost" onClick={addDraftTask}>
              Add Draft Task
            </Button>
            <Button variant="ghost" onClick={applySmartSort} disabled={draftTasks.length === 0}>
              <Wand2 className="mr-2 h-4 w-4" />
              Normalize Order
            </Button>
            <Button variant="ghost" onClick={syncDraftTasksToMarkdown} disabled={draftTasks.length === 0}>
              Sync Review to Markdown
            </Button>
            <Button variant="outline" onClick={handleSavePlan} disabled={!activePlan || savingPlan}>
              {savingPlan ? 'Saving...' : 'Save Plan'}
            </Button>
          </div>
        </Card>

        <div className="grid gap-6 xl:grid-cols-[minmax(0,0.95fr)_minmax(480px,1.05fr)]">
          <Card className="rounded-2xl border-slate-700/80 bg-slate-800/60 p-6">
            <div className="mb-4 flex items-center gap-2 text-white">
              <FileText className="h-5 w-5 text-primary-400" />
              <h3 className="text-lg font-semibold">Planner Markdown</h3>
            </div>
            <TextArea
              value={markdown}
              onChange={(event) => setMarkdown(event.target.value)}
              className="min-h-[420px] font-mono text-sm leading-6"
              placeholder="# Project: ...&#10;&#10;## Overview&#10;...&#10;&#10;## Task List&#10;- [ ] TASK_START: ..."
            />
            <p className="mt-3 text-xs text-slate-500">
              The parser looks for the <span className="font-mono text-slate-400">## Task List</span> section and checkbox items in the form <span className="font-mono text-slate-400">- [ ] TASK_START: Title | Description</span>.
            </p>
          </Card>

          <Card className="rounded-2xl border-slate-700/80 bg-slate-800/60 p-6">
            <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex items-center gap-2 text-white">
                <CheckSquare className="h-5 w-5 text-emerald-400" />
                <h3 className="text-lg font-semibold">Review Tasks</h3>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setAllDraftInclusion(true)}
                  className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-600"
                >
                  Include All
                </button>
                <button
                  type="button"
                  onClick={() => setAllDraftInclusion(false)}
                  className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-600"
                >
                  Exclude All
                </button>
                <div className="text-sm text-slate-400">{includedCount} selected</div>
              </div>
            </div>

            {validationIssues.length > 0 && (
              <div className="mb-4 rounded-xl border border-amber-700/50 bg-amber-900/20 p-4">
                <div className="text-sm font-medium text-amber-300">Review warnings</div>
                <ul className="mt-2 space-y-1 text-sm text-amber-200">
                  {validationIssues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              </div>
            )}

            {draggedTaskIndex !== null && (
              <div className="mb-4 rounded-xl border border-dashed border-primary-500/40 bg-primary-500/10 px-4 py-3 text-sm text-primary-100">
                Drag a task card to a new position, then drop it to renumber the sequence.
              </div>
            )}

            {draftTasks.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-4 py-8 text-center text-sm text-slate-500">
                Generate or parse a plan to start reviewing task cards.
              </div>
            ) : (
              <div className="space-y-3">
                {draftTasks.map((task, index) => (
                  <div
                    key={`${task.title}-${index}`}
                    draggable
                    onDragStart={() => setDraggedTaskIndex(index)}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => {
                      if (draggedTaskIndex === null || draggedTaskIndex === index) {
                        setDraggedTaskIndex(null)
                        return
                      }
                      setDraftTasks((current) => {
                        const reordered = [...current]
                        const [movedTask] = reordered.splice(draggedTaskIndex, 1)
                        reordered.splice(index, 0, movedTask)
                        return reordered.map((item, taskIndex) => ({
                          ...item,
                          plan_position: taskIndex + 1,
                        }))
                      })
                      setDraggedTaskIndex(null)
                    }}
                    onDragEnd={() => setDraggedTaskIndex(null)}
                    className={`rounded-xl border bg-slate-900/55 p-4 transition-all ${
                      draggedTaskIndex === index
                        ? 'border-primary-500/60 shadow-lg shadow-primary-500/10 opacity-80'
                        : 'border-slate-700 hover:border-slate-500'
                    }`}
                  >
                    <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                      <div className="flex flex-wrap items-center gap-3">
                        <label className="flex items-center gap-2 text-sm text-slate-300">
                          <input
                            type="checkbox"
                            checked={task.include !== false}
                            onChange={(event) => updateDraftTask(index, 'include', event.target.checked)}
                            className="rounded border-slate-600 bg-slate-900 text-primary-500 focus:ring-primary-500"
                          />
                          Include
                        </label>
                        <span className="rounded-full border border-primary-500/30 bg-primary-500/10 px-2 py-1 text-xs font-medium text-primary-200">
                          #{task.plan_position ?? index + 1}
                        </span>
                        <span className="rounded-full border border-slate-700 px-2 py-1 text-xs uppercase tracking-wide text-slate-400">
                          {task.estimated_effort || 'medium'}
                        </span>
                      </div>
                      <div className="flex items-center justify-end gap-1 rounded-xl border border-slate-800 bg-slate-950/40 px-2 py-1">
                        <button
                          type="button"
                          onClick={() => moveDraftTask(index, 'up')}
                          disabled={index === 0}
                          className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-800 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                          title="Move up"
                        >
                          <ArrowUp className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => moveDraftTask(index, 'down')}
                          disabled={index === draftTasks.length - 1}
                          className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-800 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                          title="Move down"
                        >
                          <ArrowDown className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => duplicateDraftTask(index)}
                          className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-800 hover:text-primary-300"
                          title="Duplicate draft task"
                        >
                          <Copy className="h-4 w-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => removeDraftTask(index)}
                          className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-800 hover:text-red-400"
                          title="Remove draft task"
                        >
                          <X className="h-4 w-4" />
                        </button>
                      </div>
                    </div>

                    <div className="space-y-3">
                      <Input
                        value={task.title}
                        onChange={(event) => updateDraftTask(index, 'title', event.target.value)}
                        placeholder="Task title"
                        className="text-base font-medium"
                      />
                      <TextArea
                        value={task.description || ''}
                        onChange={(event) => updateDraftTask(index, 'description', event.target.value)}
                        className="min-h-[112px] leading-6"
                        placeholder="Task description"
                      />
                      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                        <Input
                          type="number"
                          min={1}
                          value={task.plan_position ?? index + 1}
                          onChange={(event) => updateDraftTask(index, 'plan_position', Number(event.target.value || index + 1))}
                          placeholder="Order"
                        />
                        <Input
                          type="number"
                          min={0}
                          max={10}
                          value={task.priority}
                          onChange={(event) => updateDraftTask(index, 'priority', Number(event.target.value || 0))}
                          placeholder="Priority"
                        />
                        <Input
                          value={task.estimated_effort || ''}
                          onChange={(event) => updateDraftTask(index, 'estimated_effort', event.target.value)}
                          placeholder="small / medium / large"
                        />
                      </div>
                      <select
                        value={task.execution_profile || 'full_lifecycle'}
                        onChange={(event) => updateDraftTask(index, 'execution_profile', event.target.value)}
                        className="w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
                      >
                        {executionProfiles.map((profile) => (
                          <option key={profile.value} value={profile.value}>
                            {profile.label}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="mt-5 flex gap-3">
              <Button onClick={handleCommit} disabled={committing || draftTasks.length === 0 || validationIssues.length > 0}>
                {committing ? 'Deploying...' : 'Commit to Project'}
              </Button>
              <Button variant="outline" onClick={applySmartSort} disabled={draftTasks.length === 0}>
                Sort + Renumber
              </Button>
              <Button variant="outline" onClick={addDraftTask}>
                Add Another Task
              </Button>
            </div>
          </Card>
        </div>
      </div>

      <Card className="rounded-2xl border-slate-700/80 bg-slate-800/60 p-6">
        <div className="mb-4">
          <h3 className="text-lg font-semibold text-white">Recent Plans</h3>
          <p className="mt-1 text-sm text-slate-400">
            Reload a previous blueprint, adjust the markdown, and recommit a cleaner task list. Click the active plan again to unload it from the editor.
          </p>
        </div>

        {loadingPlans ? (
          <div className="text-sm text-slate-500">Loading plans...</div>
        ) : plans.length === 0 ? (
          <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 px-4 py-8 text-center text-sm text-slate-500">
            No plans saved for this project yet.
          </div>
        ) : (
          <div className="space-y-3">
            {plans.map((plan) => (
              <div
                key={plan.id}
                className={`w-full rounded-xl border p-4 text-left transition-colors ${
                  activePlan?.id === plan.id
                    ? 'border-primary-500 bg-primary-500/10'
                    : 'border-slate-700 bg-slate-900/55 hover:border-slate-600'
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <button
                    type="button"
                    onClick={() => loadPlanIntoEditor(plan)}
                    className="min-w-0 flex-1 text-left"
                    title={activePlan?.id === plan.id ? 'Click again to unload this plan' : 'Load this plan into the editor'}
                  >
                    <div className="font-medium text-white">{plan.title}</div>
                    <div className="mt-1 text-sm text-slate-400 line-clamp-2">{plan.requirement}</div>
                  </button>
                  <div className="flex items-center gap-2">
                    <span className="rounded-full border border-slate-700 px-2 py-1 text-[11px] uppercase tracking-wide text-slate-400">
                      {plan.source_brain}
                    </span>
                    <button
                      type="button"
                      onClick={() => handleDeletePlan(plan)}
                      disabled={deletingPlanId === plan.id}
                      className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-slate-800 hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-50"
                      title="Delete plan"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <div className="mt-3 flex items-center justify-between text-xs text-slate-500">
                  <span>{plan.status}</span>
                  <span>{formatDistanceToNow(new Date(plan.created_at), { addSuffix: true })}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}