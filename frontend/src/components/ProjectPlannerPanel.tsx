import type { Dispatch, SetStateAction } from 'react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { formatDistanceToNow } from 'date-fns'
import {
  Bot,
  CheckSquare,
  FileText,
  Lightbulb,
  MessageSquare,
  PencilLine,
  Play,
  Send,
  Sparkles,
  Trash2,
} from 'lucide-react'

import { plannerAPI, planningAPI, projectsAPI } from '@/api/client'
import { Button, TextArea } from '@/components/ui'
import Card from '@/components/ui/Card'
import type {
  Plan,
  PlannerTaskCandidate,
  PlanningArtifact,
  PlanningSession,
  PlanningSessionSummary,
  Project,
  Task,
} from '@/types/api'

interface ProjectPlannerPanelProps {
  project: Project
  onTasksCommitted: (tasks: Task[]) => void
}

const defaultPrompt =
  'Design a safe, execution-ready implementation plan for this project enhancement.'

const artifactLabels: Record<string, string> = {
  requirements: 'Requirements',
  design: 'Design',
  implementation_plan: 'Implementation Plan',
  planner_markdown: 'Planner Markdown',
}

const normalizeDraftTasks = (tasks: PlannerTaskCandidate[]) =>
  tasks.map((task, index) => ({
    ...task,
    plan_position: task.plan_position ?? index + 1,
    include: task.include ?? true,
  }))

const getStatusClass = (status: string) => {
  switch (status) {
    case 'waiting_for_input':
      return 'border-amber-500/30 bg-amber-500/10 text-amber-200'
    case 'completed':
      return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200'
    case 'failed':
      return 'border-red-500/30 bg-red-500/10 text-red-200'
    case 'cancelled':
      return 'border-slate-600 bg-slate-700/40 text-slate-300'
    default:
      return 'border-sky-500/30 bg-sky-500/10 text-sky-200'
  }
}

const findArtifact = (artifacts: PlanningArtifact[], type: string) =>
  artifacts.find((artifact) => artifact.artifact_type === type)

export function ProjectPlannerPanel({
  project,
  onTasksCommitted,
}: ProjectPlannerPanelProps) {
  const [sessions, setSessions] = useState<PlanningSessionSummary[]>([])
  const [activeSessionId, setActiveSessionId] = useState<number | null>(null)
  const [activeSession, setActiveSession] = useState<PlanningSession | null>(null)
  const [loadingSessions, setLoadingSessions] = useState(true)
  const [startingSession, setStartingSession] = useState(false)
  const [replying, setReplying] = useState(false)
  const [committingSession, setCommittingSession] = useState(false)
  const [newPrompt, setNewPrompt] = useState(project.description || defaultPrompt)
  const [sourceBrain, setSourceBrain] = useState<'local' | 'cloud'>('local')
  const [reply, setReply] = useState('')
  const [selectedArtifact, setSelectedArtifact] = useState<string>('requirements')
  const [plannerMarkdownDraft, setPlannerMarkdownDraft] = useState('')
  const [sessionDraftTasks, setSessionDraftTasks] = useState<PlannerTaskCandidate[]>([])

  const [plans, setPlans] = useState<Plan[]>([])
  const [loadingPlans, setLoadingPlans] = useState(true)
  const [activeLegacyPlan, setActiveLegacyPlan] = useState<Plan | null>(null)
  const [manualRequirement, setManualRequirement] = useState(
    project.description || defaultPrompt
  )
  const [manualMarkdown, setManualMarkdown] = useState('')
  const [manualDraftTasks, setManualDraftTasks] = useState<PlannerTaskCandidate[]>([])
  const [manualGenerating, setManualGenerating] = useState(false)
  const [manualParsing, setManualParsing] = useState(false)
  const [manualSaving, setManualSaving] = useState(false)
  const [manualCommitting, setManualCommitting] = useState(false)
  const [deletingPlanId, setDeletingPlanId] = useState<number | null>(null)

  const loadSessions = useCallback(async () => {
    try {
      setLoadingSessions(true)
      const response = await planningAPI.list(project.id)
      const nextSessions = response.data || []
      setSessions(nextSessions)
      setActiveSessionId((current) => {
        if (nextSessions.length === 0) {
          return null
        }
        if (current && nextSessions.some((session) => session.id === current)) {
          return current
        }
        return nextSessions[0].id
      })
    } catch (error) {
      console.error('Failed to load planning sessions:', error)
    } finally {
      setLoadingSessions(false)
    }
  }, [project.id])

  const loadPlans = useCallback(async () => {
    try {
      setLoadingPlans(true)
      const response = await projectsAPI.getPlans(project.id)
      setPlans(response.data || [])
    } catch (error) {
      console.error('Failed to load plans:', error)
    } finally {
      setLoadingPlans(false)
    }
  }, [project.id])

  const loadSession = useCallback(async (sessionId: number) => {
    try {
      const response = await planningAPI.get(sessionId)
      setActiveSession(response.data)
    } catch (error) {
      console.error('Failed to load planning session:', error)
    }
  }, [])

  useEffect(() => {
    setActiveSessionId(null)
    setActiveSession(null)
    setSessions([])
    setReply('')
    setPlannerMarkdownDraft('')
    setSessionDraftTasks([])
    setSelectedArtifact('requirements')
    setNewPrompt(project.description || defaultPrompt)
    setActiveLegacyPlan(null)
    setManualMarkdown('')
    setManualDraftTasks([])
    setManualRequirement(project.description || defaultPrompt)
    void loadSessions()
    void loadPlans()
  }, [loadPlans, loadSessions, project.description, project.id])

  useEffect(() => {
    if (activeSessionId) {
      void loadSession(activeSessionId)
    } else {
      setActiveSession(null)
    }
  }, [activeSessionId, loadSession])

  useEffect(() => {
    if (!activeSession) {
      setPlannerMarkdownDraft('')
      setSessionDraftTasks([])
      return
    }

    const plannerArtifact = findArtifact(activeSession.artifacts, 'planner_markdown')
    setPlannerMarkdownDraft(plannerArtifact?.content || '')
    setSessionDraftTasks(normalizeDraftTasks(activeSession.tasks_preview || []))
    if (activeSession.artifacts.length > 0) {
      setSelectedArtifact((current) =>
        activeSession.artifacts.some((artifact) => artifact.artifact_type === current)
          ? current
          : activeSession.artifacts[0].artifact_type
      )
    }
  }, [activeSession])

  useEffect(() => {
    if (!activeSessionId || !activeSession) {
      return
    }
    if (!['active', 'waiting_for_input'].includes(activeSession.status)) {
      return
    }

    const timer = window.setInterval(() => {
      void loadSession(activeSessionId)
      void loadSessions()
    }, 2500)

    return () => window.clearInterval(timer)
  }, [activeSession, activeSessionId, loadSession, loadSessions])

  const handleStartSession = async () => {
    if (!newPrompt.trim()) {
      return
    }

    try {
      setStartingSession(true)
      const response = await planningAPI.start({
        project_id: project.id,
        prompt: newPrompt.trim(),
        source_brain: sourceBrain,
      })
      setActiveSessionId(response.data.id)
      setActiveSession(response.data)
      await loadSessions()
    } catch (error) {
      console.error('Failed to start planning session:', error)
      alert('Failed to start a planning session. Finish or cancel the current one first.')
    } finally {
      setStartingSession(false)
    }
  }

  const handleRespond = async () => {
    if (!activeSession || !reply.trim()) {
      return
    }

    try {
      setReplying(true)
      const response = await planningAPI.respond(activeSession.id, reply.trim())
      setActiveSession(response.data)
      setReply('')
      await loadSessions()
      await loadPlans()
    } catch (error) {
      console.error('Failed to respond to planning session:', error)
      alert('Failed to submit your response.')
    } finally {
      setReplying(false)
    }
  }

  const handleCancelSession = async (sessionId: number) => {
    try {
      const response = await planningAPI.cancel(sessionId)
      if (activeSessionId === sessionId) {
        setActiveSession(response.data)
      }
      await loadSessions()
    } catch (error) {
      console.error('Failed to cancel planning session:', error)
      alert('Failed to cancel the planning session.')
    }
  }

  const handleReparseSessionMarkdown = async () => {
    if (!plannerMarkdownDraft.trim()) {
      return
    }

    try {
      const response = await plannerAPI.parse(plannerMarkdownDraft)
      setSessionDraftTasks(normalizeDraftTasks(response.data.tasks || []))
      setSelectedArtifact('planner_markdown')
    } catch (error) {
      console.error('Failed to parse planner markdown:', error)
      alert('Planner markdown could not be parsed.')
    }
  }

  const handleCommitSession = async () => {
    if (!activeSession) {
      return
    }

    const selectedTasks = sessionDraftTasks.filter(
      (task) => task.include !== false && task.title.trim()
    )
    if (selectedTasks.length === 0) {
      alert('Select at least one task before committing.')
      return
    }

    try {
      setCommittingSession(true)
      const response = await planningAPI.commit(activeSession.id, {
        selected_tasks: selectedTasks,
        planner_markdown: plannerMarkdownDraft,
      })
      setActiveSession(response.data)
      onTasksCommitted(response.data.tasks || [])
      await loadSessions()
      await loadPlans()
      alert(
        `Committed ${response.data.tasks.length} task${
          response.data.tasks.length === 1 ? '' : 's'
        } from the planning session.`
      )
    } catch (error) {
      console.error('Failed to commit planning session:', error)
      alert('Failed to commit the planning session.')
    } finally {
      setCommittingSession(false)
    }
  }

  const handleGenerateLegacyPlan = async () => {
    if (!manualRequirement.trim()) {
      return
    }

    try {
      setManualGenerating(true)
      const response = await plannerAPI.generate({
        project_id: project.id,
        requirement: manualRequirement.trim(),
        source_brain: sourceBrain,
      })
      setActiveLegacyPlan(response.data.plan)
      setManualMarkdown(response.data.plan.markdown)
      setManualDraftTasks(normalizeDraftTasks(response.data.tasks_preview || []))
      await loadPlans()
    } catch (error) {
      console.error('Failed to generate legacy plan:', error)
      alert('Failed to generate a manual planner draft.')
    } finally {
      setManualGenerating(false)
    }
  }

  const handleParseLegacyPlan = async () => {
    if (!manualMarkdown.trim()) {
      return
    }

    try {
      setManualParsing(true)
      const response = await plannerAPI.parse(manualMarkdown)
      setManualDraftTasks(normalizeDraftTasks(response.data.tasks || []))
    } catch (error) {
      console.error('Failed to parse manual planner markdown:', error)
      alert('Manual planner markdown could not be parsed.')
    } finally {
      setManualParsing(false)
    }
  }

  const handleSaveLegacyPlan = async () => {
    if (!activeLegacyPlan) {
      return
    }

    try {
      setManualSaving(true)
      const response = await plannerAPI.updatePlan(project.id, activeLegacyPlan.id, {
        title: manualRequirement.trim().slice(0, 255) || activeLegacyPlan.title,
        requirement: manualRequirement.trim() || activeLegacyPlan.requirement,
        markdown: manualMarkdown,
        source_brain: sourceBrain,
      })
      setActiveLegacyPlan(response.data)
      await loadPlans()
    } catch (error) {
      console.error('Failed to save manual plan:', error)
      alert('Failed to save the manual planner draft.')
    } finally {
      setManualSaving(false)
    }
  }

  const handleCommitLegacyPlan = async () => {
    const selectedTasks = manualDraftTasks.filter(
      (task) => task.include !== false && task.title.trim()
    )
    if (selectedTasks.length === 0) {
      alert('Select at least one task before committing.')
      return
    }

    try {
      setManualCommitting(true)
      const response = await plannerAPI.batchCreateTasks(project.id, {
        plan_id: activeLegacyPlan?.id,
        markdown: manualMarkdown || undefined,
        plan_title: activeLegacyPlan?.title || manualRequirement.trim(),
        requirement: manualRequirement.trim() || activeLegacyPlan?.requirement,
        source_brain: sourceBrain,
        tasks: selectedTasks,
      })
      onTasksCommitted(response.data.tasks || [])
      await loadPlans()
      alert(
        `Added ${response.data.tasks.length} task${
          response.data.tasks.length === 1 ? '' : 's'
        } from the legacy planner.`
      )
    } catch (error) {
      console.error('Failed to commit manual planner draft:', error)
      alert('Failed to commit the manual planner draft.')
    } finally {
      setManualCommitting(false)
    }
  }

  const loadLegacyPlan = async (plan: Plan) => {
    setActiveLegacyPlan(plan)
    setManualRequirement(plan.requirement)
    setManualMarkdown(plan.markdown)
    try {
      const response = await plannerAPI.parse(plan.markdown)
      setManualDraftTasks(normalizeDraftTasks(response.data.tasks || []))
    } catch (error) {
      console.error('Failed to parse saved plan:', error)
      setManualDraftTasks([])
    }
  }

  const handleDeleteLegacyPlan = async (plan: Plan) => {
    const confirmed = window.confirm(`Delete plan "${plan.title}"?`)
    if (!confirmed) {
      return
    }

    try {
      setDeletingPlanId(plan.id)
      await plannerAPI.deletePlan(project.id, plan.id)
      if (activeLegacyPlan?.id === plan.id) {
        setActiveLegacyPlan(null)
        setManualMarkdown('')
        setManualDraftTasks([])
      }
      await loadPlans()
    } catch (error) {
      console.error('Failed to delete plan:', error)
      alert('Failed to delete the selected plan.')
    } finally {
      setDeletingPlanId(null)
    }
  }

  const selectedArtifactContent = useMemo(() => {
    if (selectedArtifact === 'planner_markdown') {
      return plannerMarkdownDraft
    }
    return findArtifact(activeSession?.artifacts || [], selectedArtifact)?.content || ''
  }, [activeSession?.artifacts, plannerMarkdownDraft, selectedArtifact])

  const pendingQuestion = useMemo(
    () =>
      activeSession?.messages
        ?.slice()
        .reverse()
        .find(
          (message) =>
            message.role === 'assistant' &&
            message.prompt_id &&
            message.prompt_id === activeSession.current_prompt_id
        ) || null,
    [activeSession]
  )

  const renderTaskPreview = (
    tasks: PlannerTaskCandidate[],
    setTasks: Dispatch<SetStateAction<PlannerTaskCandidate[]>>
  ) => {
    if (tasks.length === 0) {
      return (
        <div className="rounded-xl border border-dashed border-slate-700/60 bg-slate-900/30 px-4 py-8 text-center text-sm text-slate-500">
          No parsed tasks yet.
        </div>
      )
    }

    return (
      <div className="space-y-3">
        {tasks.map((task, index) => (
          <div
            key={index}
            className="rounded-xl border border-slate-700/60 bg-slate-800/50 p-4"
          >
            <div className="flex items-start gap-3">
              <input
                type="checkbox"
                checked={task.include !== false}
                onChange={(event) =>
                  setTasks((current) =>
                    current.map((item, itemIndex) =>
                      itemIndex === index ? { ...item, include: event.target.checked } : item
                    )
                  )
                }
                className="mt-1 h-4 w-4 rounded border-slate-600 bg-slate-800"
              />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-white">{task.title || 'Untitled task'}</div>
                <div className="mt-1 text-sm text-slate-400 leading-relaxed">
                  {task.description || 'No description provided.'}
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-500">
                  <span>Order {task.plan_position ?? index + 1}</span>
                  <span>Priority {task.priority}</span>
                  <span>{task.execution_profile}</span>
                  {task.estimated_effort && <span>{task.estimated_effort}</span>}
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card className="rounded-2xl border-slate-700/50 bg-slate-900/80 p-5">
          <div className="mb-5 pb-4 border-b border-slate-700/50 flex items-center gap-2 text-sm font-semibold text-amber-300">
            <Lightbulb className="h-4 w-4" />
            Interactive Planning
          </div>
          <div className="space-y-4">
            <div>
              <label className="mb-2 block text-xs font-semibold uppercase tracking-wider text-slate-400">
                Planning prompt
              </label>
              <TextArea
                value={newPrompt}
                onChange={(event) => setNewPrompt(event.target.value)}
                className="min-h-[140px] leading-relaxed px-4 py-3"
                placeholder="Describe the feature, constraints, and what a good plan should optimize for."
              />
            </div>
            <div className="grid gap-3">
              <button
                type="button"
                onClick={() => setSourceBrain('local')}
                className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                  sourceBrain === 'local'
                    ? 'border-primary-500 bg-primary-500/10 text-white'
                    : 'border-slate-700 bg-slate-900/70 text-slate-300'
                }`}
              >
                <div className="font-medium">Local brain</div>
                <div className="mt-1 text-xs text-slate-400">Repo-aware planning with OpenClaw</div>
              </button>
              <button
                type="button"
                onClick={() => setSourceBrain('cloud')}
                className={`rounded-xl border px-4 py-3 text-left transition-colors ${
                  sourceBrain === 'cloud'
                    ? 'border-sky-500 bg-sky-500/10 text-white'
                    : 'border-slate-700 bg-slate-900/70 text-slate-300'
                }`}
              >
                <div className="font-medium">Cloud brain</div>
                <div className="mt-1 text-xs text-slate-400">Keep this for architecture-heavy prompts</div>
              </button>
            </div>
            <Button
              onClick={handleStartSession}
              disabled={startingSession || !newPrompt.trim()}
              className="w-full"
            >
              <Play className="mr-2 h-4 w-4" />
              {startingSession ? 'Starting...' : 'Start Planning Session'}
            </Button>
          </div>

          <div className="mt-8 pt-6 border-t border-slate-700/50">
            <div className="mb-3 flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-400">Planning Sessions</div>
              <div className="text-xs text-slate-500">{sessions.length}</div>
            </div>
            {loadingSessions ? (
              <div className="text-sm text-slate-500">Loading sessions...</div>
            ) : sessions.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-700/60 bg-slate-900/30 px-4 py-8 text-sm text-slate-500">
                No planning sessions yet.
              </div>
            ) : (
              <div className="space-y-3">
                {sessions.map((session) => (
                  <button
                    key={session.id}
                    type="button"
                    onClick={() => setActiveSessionId(session.id)}
                    className={`w-full rounded-xl border p-4 text-left transition-colors ${
                      activeSessionId === session.id
                        ? 'border-primary-500/70 bg-primary-500/10'
                        : 'border-slate-700/60 bg-slate-900/30 hover:border-slate-600 hover:bg-slate-900/60'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-white">{session.title}</div>
                        <div className="mt-1 line-clamp-2 text-xs text-slate-400">
                          {session.prompt}
                        </div>
                        <div className="mt-2 text-xs text-slate-500">
                          Updated{' '}
                          {formatDistanceToNow(new Date(session.updated_at || session.created_at), {
                            addSuffix: true,
                          })}
                        </div>
                      </div>
                      <div
                        className={`rounded-full border px-2 py-1 text-[11px] ${getStatusClass(
                          session.status
                        )}`}
                      >
                        {session.status.replace(/_/g, ' ')}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </Card>

        <div className="space-y-6">
          <Card className="rounded-2xl border-slate-700/50 bg-slate-800/40 p-6">
            {!activeSession ? (
              <div className="rounded-xl border border-dashed border-slate-700/60 bg-slate-900/30 px-6 py-16 text-center text-sm text-slate-500">
                Select a planning session or start a new one to begin the interactive flow.
              </div>
            ) : (
              <div className="space-y-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <div className="mb-2 flex items-center gap-2 text-sm font-medium text-amber-300">
                      <Sparkles className="h-4 w-4" />
                      Session-first planning
                    </div>
                    <h2 className="text-2xl font-semibold text-white">{activeSession.title}</h2>
                    <p className="mt-2 max-w-3xl text-sm text-slate-400 leading-relaxed">{activeSession.prompt}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <div
                      className={`rounded-full border px-3 py-1.5 text-xs ${getStatusClass(
                        activeSession.status
                      )}`}
                    >
                      {activeSession.status.replace(/_/g, ' ')}
                    </div>
                    {['active', 'waiting_for_input'].includes(activeSession.status) && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleCancelSession(activeSession.id)}
                      >
                        Cancel
                      </Button>
                    )}
                  </div>
                </div>

                <div className="grid gap-6 xl:grid-cols-[minmax(0,0.95fr)_minmax(380px,1.05fr)]">
                  <div className="space-y-4">
                    <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 p-4">
                      <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
                        <MessageSquare className="h-4 w-4 text-primary-400" />
                        Conversation
                      </div>
                      <div className="space-y-3">
                        {activeSession.messages.map((message) => (
                          <div
                            key={message.id}
                            className={`rounded-xl px-4 py-3 ${
                              message.role === 'assistant'
                                ? 'bg-slate-800 text-slate-100'
                                : 'bg-primary-500/10 text-primary-50'
                            }`}
                          >
                            <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-wide text-slate-400">
                              {message.role === 'assistant' ? (
                                <Bot className="h-3.5 w-3.5" />
                              ) : (
                                <PencilLine className="h-3.5 w-3.5" />
                              )}
                              {message.role}
                            </div>
                            <div className="whitespace-pre-wrap text-sm leading-relaxed">{message.content}</div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {activeSession.status === 'waiting_for_input' && (
                      <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
                        <div className="mb-2 text-sm font-medium text-amber-200">
                          Pending question
                        </div>
                        <div className="mb-3 text-sm text-amber-50">
                          {pendingQuestion?.content || 'The planner is waiting for more context.'}
                        </div>
                        <TextArea
                          value={reply}
                          onChange={(event) => setReply(event.target.value)}
                          className="min-h-[120px] leading-relaxed px-4 py-3"
                          placeholder="Answer with the constraints, desired outcomes, and acceptance criteria you want the plan to reflect."
                        />
                        <div className="mt-3 flex justify-end">
                          <Button onClick={handleRespond} disabled={replying || !reply.trim()}>
                            <Send className="mr-2 h-4 w-4" />
                            {replying ? 'Sending...' : 'Submit Response'}
                          </Button>
                        </div>
                      </div>
                    )}

                    {activeSession.status === 'active' && (
                      <div className="rounded-xl border border-sky-500/30 bg-sky-500/10 p-4 text-sm text-sky-100">
                        The planner is still running in the background. This view refreshes automatically while it decides whether to ask one more question or generate final artifacts.
                      </div>
                    )}

                    {activeSession.status === 'failed' && activeSession.last_error && (
                      <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-100">
                        {activeSession.last_error}
                      </div>
                    )}
                  </div>

                  <div className="space-y-4">
                    <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 p-4">
                      <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
                        <FileText className="h-4 w-4 text-sky-400" />
                        Planning Artifacts
                      </div>
                      {activeSession.artifacts.length === 0 ? (
                        <div className="text-sm text-slate-500">
                          Artifacts will appear here once the planning session completes.
                        </div>
                      ) : (
                        <>
                          <div className="mb-3 flex flex-wrap gap-2">
                            {activeSession.artifacts.map((artifact) => (
                              <button
                                key={artifact.id}
                                type="button"
                                onClick={() => setSelectedArtifact(artifact.artifact_type)}
                                className={`rounded-lg border px-3 py-1.5 text-xs transition-colors ${
                                  selectedArtifact === artifact.artifact_type
                                    ? 'border-primary-500 bg-primary-500/10 text-white'
                                    : 'border-slate-700 bg-slate-800 text-slate-300'
                                }`}
                              >
                                {artifactLabels[artifact.artifact_type] || artifact.filename}
                              </button>
                            ))}
                          </div>
                          <TextArea
                            value={selectedArtifactContent}
                            onChange={(event) => {
                              if (selectedArtifact === 'planner_markdown') {
                                setPlannerMarkdownDraft(event.target.value)
                              }
                            }}
                            readOnly={selectedArtifact !== 'planner_markdown'}
                            className="min-h-[280px] font-mono text-sm leading-relaxed px-4 py-3"
                          />
                          {selectedArtifact === 'planner_markdown' && (
                            <div className="mt-3 flex justify-end">
                              <Button variant="outline" onClick={handleReparseSessionMarkdown}>
                                Parse Markdown Preview
                              </Button>
                            </div>
                          )}
                        </>
                      )}
                    </div>

                    <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 p-4">
                      <div className="mb-4 flex items-center justify-between">
                        <div className="flex items-center gap-2 text-sm font-semibold text-white">
                          <CheckSquare className="h-4 w-4 text-emerald-400" />
                          Task Preview
                        </div>
                        <div className="text-xs text-slate-500">
                          {sessionDraftTasks.filter((task) => task.include !== false).length} selected
                        </div>
                      </div>
                      {renderTaskPreview(sessionDraftTasks, setSessionDraftTasks)}
                      <div className="mt-4 flex justify-end">
                        <Button
                          onClick={handleCommitSession}
                          disabled={
                            committingSession ||
                            activeSession.status !== 'completed' ||
                            activeSession.committed_task_ids.length > 0
                          }
                        >
                          {activeSession.committed_task_ids.length > 0
                            ? 'Already Committed'
                            : committingSession
                            ? 'Committing...'
                            : 'Commit Tasks to Project'}
                        </Button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </Card>

          <Card className="rounded-2xl border-slate-700/50 bg-slate-800/40 p-6">
            <div className="mb-6 pb-4 border-b border-slate-700/50 flex items-center justify-between gap-4">
              <div>
                <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-200">
                  <PencilLine className="h-4 w-4 text-slate-400" />
                  Legacy Markdown Planner
                </div>
                <p className="text-sm text-slate-400 leading-relaxed">
                  Keep using the old markdown-driven planner when you want direct editing without the interactive Q&A flow.
                </p>
              </div>
              <div className="text-xs text-slate-500">
                {loadingPlans ? 'Loading plans...' : `${plans.length} saved`}
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
              <div className="space-y-4">
                <TextArea
                  value={manualRequirement}
                  onChange={(event) => setManualRequirement(event.target.value)}
                  className="min-h-[120px] leading-relaxed px-4 py-3"
                  placeholder="Describe the outcome you want the legacy planner to turn into markdown."
                />
                <div className="flex flex-wrap items-center gap-3">
                  <Button onClick={handleGenerateLegacyPlan} disabled={manualGenerating}>
                    {manualGenerating ? 'Generating...' : 'Generate Manual Plan'}
                  </Button>
                  <Button variant="outline" onClick={handleParseLegacyPlan} disabled={manualParsing}>
                    {manualParsing ? 'Parsing...' : 'Parse Task List'}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={handleSaveLegacyPlan}
                    disabled={!activeLegacyPlan || manualSaving}
                  >
                    {manualSaving ? 'Saving...' : 'Save'}
                  </Button>
                  <Button onClick={handleCommitLegacyPlan} disabled={manualCommitting} className="ml-auto">
                    {manualCommitting ? 'Committing...' : 'Commit Manual Tasks'}
                  </Button>
                </div>
                <TextArea
                  value={manualMarkdown}
                  onChange={(event) => setManualMarkdown(event.target.value)}
                  className="min-h-[280px] font-mono text-sm leading-relaxed px-4 py-3"
                  placeholder="# Project: ...\n\n## Task List\n- [ ] TASK_START: ..."
                />
                {renderTaskPreview(manualDraftTasks, setManualDraftTasks)}
              </div>

              <div className="space-y-3">
                <div className="text-xs font-semibold uppercase tracking-wider text-slate-500 pb-1">
                  Saved Plans
                </div>
                {plans.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-slate-700/60 bg-slate-900/30 px-4 py-10 text-center text-sm text-slate-500">
                    Saved manual plans will appear here.
                  </div>
                ) : (
                  plans.map((plan) => (
                    <div
                      key={plan.id}
                      className={`rounded-xl border p-4 transition-colors ${
                        activeLegacyPlan?.id === plan.id
                          ? 'border-primary-500/70 bg-primary-500/10'
                          : 'border-slate-700/60 bg-slate-900/30 hover:border-slate-600 hover:bg-slate-900/60'
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => void loadLegacyPlan(plan)}
                        className="w-full text-left"
                      >
                        <div className="text-sm font-medium text-white">{plan.title}</div>
                        <div className="mt-1 text-xs text-slate-400">
                          {formatDistanceToNow(new Date(plan.created_at), { addSuffix: true })}
                        </div>
                      </button>
                      <div className="mt-3 flex justify-end">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void handleDeleteLegacyPlan(plan)}
                          disabled={deletingPlanId === plan.id}
                        >
                          <Trash2 className="mr-2 h-4 w-4" />
                          Delete
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}
