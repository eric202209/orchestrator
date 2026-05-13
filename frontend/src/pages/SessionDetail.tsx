import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { sessionsAPI, tasksAPI, projectsAPI } from '@/api/client';
import type {
  Checkpoint,
  CheckpointInspection,
  ExecutionFailureSummary,
  InterventionRequest,
  KnowledgeUsageEntry,
  OrchestrationEvent,
  Project,
  Session,
  SessionDecisionEvent,
  SessionDispatchWatchdogResponse,
  SessionDivergenceCompareResponse,
  SessionReplayResponse,
  SessionStateDiffResponse,
  Task,
} from '@/types/api';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { Alert, LoadingSpinner } from '@/components/ui';
import {
  FailureSummaryPanel,
  HumanInterventionPanel,
  KnowledgeUsagePanel,
  SessionConnectionNotice,
  SessionDiagnosticsPanel,
  SessionHeader,
  SessionLogsPanel,
  SessionSettingsPanel,
  SessionStats,
  SessionTabs,
  SessionTasksPanel,
  SessionTimelinePanel,
  type SessionDetailTab,
  type OffTrackMoment,
  type RepairGenealogyNode,
  type TimelineSpan,
} from '@/components/SessionDetailSections';
import { MessageCircle, Pause, Play, Square, XCircle } from 'lucide-react';
import { isNoisySessionLogMessage } from './sessionLogNoise';

type TimelineEventType =
  | 'planning'
  | 'executing'
  | 'debugging'
  | 'revising_plan'
  | 'summarizing'
  | 'checkpoint'
  | 'validation'
  | 'repair'
  | 'task'
  | 'error'
  | 'status'
  | 'info';

interface TimelineEvent {
  id: string;
  at: string;
  type: TimelineEventType;
  title: string;
  detail: string;
}

interface SessionLogItem {
  message: string;
  level?: string;
  timestamp?: string;
  created_at?: string;
}

interface ApiErrorLike {
  response?: {
    data?: {
      detail?: string;
    };
  };
  message?: string;
}

interface InterventionToastState {
  interventionId: number;
  title: string;
  message: string;
}

type CheckpointActionIntent = 'start' | 'resume';

const MAX_TIMELINE_EVENTS = 150;
const TERMINAL_SESSION_STATUSES = new Set(['stopped', 'failed', 'cancelled', 'canceled']);

const cleanJsonLogMessage = (message: string): string => {
  const trimmed = message.trim();
  if (!trimmed || (!trimmed.startsWith('{') && !trimmed.startsWith('['))) {
    return message;
  }

  try {
    const parsed = JSON.parse(trimmed) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return message;
    }

    const record = parsed as Record<string, unknown>;
    const primary =
      record.message ||
      record.summary ||
      record.event ||
      record.event_type ||
      record.type ||
      record.status ||
      'Log event';
    const details = [
      record.phase ? `phase=${String(record.phase)}` : null,
      record.reason ? `reason=${String(record.reason)}` : null,
      record.task_execution_id ? `TE=${String(record.task_execution_id)}` : null,
      record.task_id ? `task=${String(record.task_id)}` : null,
    ].filter(Boolean);

    return details.length > 0
      ? `${String(primary)} (${details.join(', ')})`
      : String(primary);
  } catch {
    return message;
  }
};

export default function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<Session | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [displayLogs, setDisplayLogs] = useState<TerminalLogEntry[]>([]);
  const [activeTab, setActiveTab] = useState<SessionDetailTab>('logs');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [allLogs, setAllLogs] = useState<TerminalLogEntry[]>([]);
  const [logViewMode, setLogViewMode] = useState<'newest' | 'oldest' | 'success' | 'errors' | 'all'>('newest');
  const [logVerbosity, setLogVerbosity] = useState<'clean' | 'verbose'>('clean');
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [decisionEvents, setDecisionEvents] = useState<SessionDecisionEvent[]>([]);
  const [orchestrationEvents, setOrchestrationEvents] = useState<OrchestrationEvent[]>([]);
  const [checkpointCount, setCheckpointCount] = useState(0);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [recommendedCheckpointName, setRecommendedCheckpointName] = useState<string | null>(null);
  const [checkpointInspection, setCheckpointInspection] = useState<CheckpointInspection | null>(null);
  const [compareMatches, setCompareMatches] = useState<SessionDivergenceCompareResponse | null>(null);
  const [dispatchWatchdog, setDispatchWatchdog] = useState<SessionDispatchWatchdogResponse | null>(null);
  const [replayInvestigation, setReplayInvestigation] = useState<SessionReplayResponse | null>(null);
  const [stateDiff, setStateDiff] = useState<SessionStateDiffResponse | null>(null);
  const [interventions, setInterventions] = useState<InterventionRequest[]>([]);
  const [failureSummary, setFailureSummary] = useState<ExecutionFailureSummary | null>(null);
  const [knowledgeUsage, setKnowledgeUsage] = useState<Record<string, KnowledgeUsageEntry[]>>({});
  const [failureSummaryLoading, setFailureSummaryLoading] = useState(false);
  const [showAgentInterventionModal, setShowAgentInterventionModal] = useState(false);
  const [interventionToast, setInterventionToast] = useState<InterventionToastState | null>(null);
  const [checkpointActionIntent, setCheckpointActionIntent] = useState<CheckpointActionIntent | null>(null);
  const [interventionPrompt, setInterventionPrompt] = useState('');
  const [interventionSubmitting, setInterventionSubmitting] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldReconnectRef = useRef(true);
  const diffAvailableRef = useRef<boolean | null>(null); // null=unknown, true=has data, false=no snapshots
  const tasksRef = useRef<Task[]>([]);
  useEffect(() => { tasksRef.current = tasks; }, [tasks]);
  const seenOrchestrationTimelineKeysRef = useRef<Set<string>>(new Set());
  const lastAutoOpenedAgentInterventionRef = useRef<number | null>(null);
  const toastTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const defaultTabSessionRef = useRef<number | null>(null);

  const pendingInterventions = interventions.filter((i) => i.status === 'pending');
  const pendingAgentInterventions = pendingInterventions.filter((i) => i.initiated_by !== 'human');
  const agentInterventionTimeline = interventions
    .filter((i) => i.initiated_by !== 'human')
    .slice()
    .sort((a, b) => {
      const left = new Date(b.created_at).getTime();
      const right = new Date(a.created_at).getTime();
      return left - right;
    })
    .slice(0, 5);

  useEffect(() => {
    if (!session || defaultTabSessionRef.current === session.id) return;
    defaultTabSessionRef.current = session.id;
    setActiveTab(TERMINAL_SESSION_STATUSES.has(session.status) ? 'timeline' : 'logs');
  }, [session]);

  const dismissInterventionToast = useCallback(() => {
    if (toastTimeoutRef.current) {
      clearTimeout(toastTimeoutRef.current);
      toastTimeoutRef.current = null;
    }
    setInterventionToast(null);
  }, []);

  const playInterventionChime = useCallback(() => {
    if (typeof window === 'undefined') {
      return;
    }

    const AudioContextCtor = window.AudioContext || (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) {
      return;
    }

    try {
      const audioContext = new AudioContextCtor();
      const now = audioContext.currentTime;
      const oscillatorA = audioContext.createOscillator();
      const oscillatorB = audioContext.createOscillator();
      const gainNode = audioContext.createGain();

      oscillatorA.type = 'triangle';
      oscillatorA.frequency.setValueAtTime(740, now);
      oscillatorB.type = 'sine';
      oscillatorB.frequency.setValueAtTime(988, now + 0.08);

      gainNode.gain.setValueAtTime(0.0001, now);
      gainNode.gain.exponentialRampToValueAtTime(0.12, now + 0.03);
      gainNode.gain.exponentialRampToValueAtTime(0.0001, now + 0.42);

      oscillatorA.connect(gainNode);
      oscillatorB.connect(gainNode);
      gainNode.connect(audioContext.destination);

      oscillatorA.start(now);
      oscillatorB.start(now + 0.08);
      oscillatorA.stop(now + 0.22);
      oscillatorB.stop(now + 0.42);

      window.setTimeout(() => {
        void audioContext.close().catch(() => undefined);
      }, 600);
    } catch {
      // Ignore browser audio permission issues.
    }
  }, []);

  const parseApiDate = useCallback((value?: string | null): Date | null => {
    if (!value) return null;
    const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
    const normalized = hasTimezone ? value : `${value}Z`;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }, []);

  const formatDateTime = useCallback((value?: string | null) => {
    const d = parseApiDate(value);
    if (!d) return value ? 'Invalid date' : 'N/A';
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const min = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${yyyy}${mm}${dd} / ${hh}:${min}:${ss}`;
  }, [parseApiDate]);

  const formatLogTimestamp = useCallback((value?: string | null) => {
    const parsed = parseApiDate(value);
    if (!parsed) return '';
    const hh = String(parsed.getHours()).padStart(2, '0');
    const mm = String(parsed.getMinutes()).padStart(2, '0');
    const ss = String(parsed.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  }, [parseApiDate]);

  const toTerminalLogEntry = useCallback((log: SessionLogItem): TerminalLogEntry => ({
    message: logVerbosity === 'clean' ? cleanJsonLogMessage(log.message) : log.message,
    timestamp: formatLogTimestamp(log.timestamp || log.created_at),
  }), [formatLogTimestamp, logVerbosity]);

  const shouldDisplayLog = useCallback(
    (log: SessionLogItem) => logVerbosity === 'verbose' || !isNoisySessionLogMessage(log.message),
    [logVerbosity]
  );

  const visibleLogs = useCallback(
    (logs: SessionLogItem[]) => logs.filter(shouldDisplayLog),
    [shouldDisplayLog]
  );

  const applyLogView = useCallback((sourceLogs: TerminalLogEntry[], mode: string) => {
    let result = [...sourceLogs];
    if (mode === 'newest') {
      result = result.slice().reverse();
    } else if (mode === 'success') {
      result = result.filter((log) => log.message.includes('✓') || log.message.includes('success') || log.message.includes('Success'));
    } else if (mode === 'errors') {
      result = result.filter((log) => log.message.includes('✗') || log.message.includes('error') || log.message.includes('Error') || log.message.includes('failed'));
    }
    setDisplayLogs(result);
  }, []);

  const classifyTimelineEvent = useCallback((message: string, level?: string, at?: string): TimelineEvent => {
    const lower = message.toLowerCase();
    let type: TimelineEventType = 'info';
    let title = 'Log Update';

    if (level === 'ERROR' || lower.includes('[orchestration] failed') || lower.includes('error') || lower.includes('failed')) {
      type = 'error';
      title = 'Error';
    } else if (
      lower.includes('[orchestration] phase 1: planning') ||
      lower.includes('[orchestration] planning phase') ||
      lower.includes('[planning]')
    ) {
      type = 'planning';
      title = 'Planning';
    } else if (
      lower.includes('[orchestration] phase 2: executing') ||
      lower.includes('[orchestration] starting executing phase') ||
      lower.includes('[orchestration] executing step')
    ) {
      type = 'executing';
      title = 'Executing';
    } else if (
      lower.includes('[orchestration] phase 3: debugging') ||
      lower.includes('[orchestration] starting debugging phase') ||
      lower.includes('[debug')
    ) {
      type = 'debugging';
      title = 'Debugging';
    } else if (
      lower.includes('plan_revision') ||
      lower.includes('[orchestration] phase 4: plan_revision') ||
      lower.includes('[orchestration] starting plan_revision phase') ||
      lower.includes('plan revision')
    ) {
      type = 'revising_plan';
      title = 'Plan Revision';
    } else if (
      lower.includes('[orchestration] phase 5: task_summary') ||
      lower.includes('[orchestration] generating summary') ||
      lower.includes('task_summary')
    ) {
      type = 'summarizing';
      title = 'Summary';
    } else if (lower.includes('checkpoint') || lower.includes('pause') || lower.includes('resume')) {
      type = 'checkpoint';
      title = 'Checkpoint';
    } else if (lower.includes('started') || lower.includes('stopped') || lower.includes('running')) {
      type = 'status';
      title = 'Session Status';
    }

    return {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      at: at || new Date().toISOString(),
      type,
      title,
      detail: message,
    };
  }, []);

  const pushTimelineEvent = useCallback((message: string, level?: string, at?: string) => {
    const event = classifyTimelineEvent(message, level, at);
    setTimelineEvents(prev => [...prev.slice(-(MAX_TIMELINE_EVENTS - 1)), event]);
  }, [classifyTimelineEvent]);

  const humanizeToken = useCallback((value?: string | null) => {
    return (value || '')
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (char) => char.toUpperCase());
  }, []);

  const buildOrchestrationTimelineKey = useCallback((event: OrchestrationEvent) => {
    return JSON.stringify([
      event.event_id || '',
      event.timestamp || '',
      event.event_type || '',
      event.task_id ?? null,
      event.parent_event_id ?? null,
      event.details || {},
    ]);
  }, []);

  const normalizeOrchestrationEvents = useCallback((events: OrchestrationEvent[]) => {
    const seen = new Set<string>();
    return events
      .slice()
      .sort((a, b) => {
        const aTime = parseApiDate(a.timestamp)?.getTime() || 0;
        const bTime = parseApiDate(b.timestamp)?.getTime() || 0;
        return aTime - bTime;
      })
      .filter((event) => {
        const key = buildOrchestrationTimelineKey(event);
        if (seen.has(key)) {
          return false;
        }
        seen.add(key);
        return true;
      });
  }, [buildOrchestrationTimelineKey, parseApiDate]);

  const detailToText = useCallback((value: unknown): string | null => {
    if (typeof value === 'string') {
      return value.trim() || null;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    if (Array.isArray(value)) {
      const joined = value
        .map((item) => detailToText(item))
        .filter((item): item is string => Boolean(item))
        .join(', ');
      return joined || null;
    }
    return null;
  }, []);

  const toTimelineEventFromOrchestrationEvent = useCallback((event: OrchestrationEvent): TimelineEvent => {
    const details = event.details || {};
    const failureEnvelope =
      details.failure_envelope && typeof details.failure_envelope === 'object'
        ? (details.failure_envelope as Record<string, unknown>)
        : null;
    const phase = typeof details.phase === 'string' ? details.phase : '';
    const phaseLabel = humanizeToken(phase || event.event_type);
    const stepIndex =
      typeof details.step_index === 'number'
        ? details.step_index
        : typeof details.step_number === 'number'
          ? details.step_number
          : null;
    const stepTotal = typeof details.step_total === 'number' ? details.step_total : null;
    const checkpointName =
      typeof details.checkpoint_name === 'string'
        ? details.checkpoint_name
        : typeof details.resolved_checkpoint_name === 'string'
          ? details.resolved_checkpoint_name
          : null;
    const statusText = detailToText(details.status);
    const reasonsText = detailToText(details.reasons);
    const messageText = detailToText(details.message);
    const failureRootCause = detailToText(failureEnvelope?.root_cause);
    const queueLatencySeconds =
      typeof details.queue_latency_seconds === 'number'
        ? details.queue_latency_seconds
        : typeof details.queue_latency_seconds === 'string'
          ? Number(details.queue_latency_seconds)
          : null;
    const queueAgeSeconds =
      typeof details.queue_age_seconds === 'number'
        ? details.queue_age_seconds
        : typeof details.queue_age_seconds === 'string'
          ? Number(details.queue_age_seconds)
          : null;

    let type: TimelineEventType = 'info';
    let title = humanizeToken(event.event_type);
    let detail = title;

    switch (event.event_type) {
      case 'phase_started':
        type =
          phase === 'planning'
            ? 'planning'
            : phase === 'executing'
              ? 'executing'
              : phase === 'debugging'
                ? 'debugging'
                : phase === 'revising_plan'
                  ? 'revising_plan'
                  : phase === 'summarizing'
                    ? 'summarizing'
                    : 'info';
        title = `${phaseLabel} Started`;
        detail = `${phaseLabel} phase started`;
        break;
      case 'phase_finished':
        type =
          phase === 'planning'
            ? 'planning'
            : phase === 'executing'
              ? 'executing'
              : phase === 'debugging'
                ? 'debugging'
                : phase === 'revising_plan'
                  ? 'revising_plan'
                  : phase === 'summarizing'
                    ? 'summarizing'
                    : 'info';
        title = `${phaseLabel} Finished`;
        detail = statusText
          ? `${phaseLabel} phase finished with status: ${statusText}`
          : `${phaseLabel} phase finished`;
        break;
      case 'step_started':
        type = 'executing';
        title = 'Step Started';
        detail = stepIndex && stepTotal
          ? `Step ${stepIndex} of ${stepTotal} started`
          : stepIndex
            ? `Step ${stepIndex} started`
            : 'Execution step started';
        break;
      case 'step_finished':
        type = statusText === 'failed' ? 'error' : 'executing';
        title = 'Step Finished';
        detail = stepIndex && statusText
          ? `Step ${stepIndex} finished with status: ${statusText}`
          : stepIndex
            ? `Step ${stepIndex} finished`
            : statusText
              ? `Step finished with status: ${statusText}`
              : 'Execution step finished';
        break;
      case 'tool_invoked':
        type = 'executing';
        title = 'Tool Invoked';
        detail = messageText || detailToText(details.tool_name) || 'A tool was invoked';
        break;
      case 'tool_failed':
        type = 'error';
        title = 'Tool Failed';
        detail = messageText || detailToText(details.tool_name) || 'A tool failed';
        break;
      case 'waiting_for_input':
        type = 'status';
        title = 'Waiting For Input';
        detail = messageText || 'Runtime is blocked waiting for input';
        break;
      case 'checkpoint_saved':
        type = 'checkpoint';
        title = 'Checkpoint Saved';
        detail = checkpointName
          ? `Saved checkpoint ${checkpointName}`
          : 'Checkpoint saved';
        break;
      case 'checkpoint_loaded':
        type = 'checkpoint';
        title = 'Checkpoint Loaded';
        detail = checkpointName
          ? `Loaded checkpoint ${checkpointName}`
          : 'Checkpoint loaded';
        break;
      case 'checkpoint_redirected':
        type = 'checkpoint';
        title = 'Checkpoint Redirected';
        detail = checkpointName
          ? `Resume redirected to checkpoint ${checkpointName}`
          : 'Checkpoint selection was redirected';
        break;
      case 'retry_entered':
        type = 'debugging';
        title = 'Retry Entered';
        detail = [
          messageText || 'A retry/debug cycle started',
          failureRootCause ? `root cause: ${humanizeToken(failureRootCause)}` : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'plan_revised':
        type = 'revising_plan';
        title = 'Plan Revised';
        detail = messageText || 'The orchestration plan was revised';
        break;
      case 'reasoning_artifact_generated':
        type = 'validation';
        title = 'Reasoning Artifact Ready';
        detail = [
          detailToText(details.intent) || 'Structured reasoning artifact generated',
          typeof details.planned_action_count === 'number'
            ? `${details.planned_action_count} actions`
            : null,
          statusText ? `status: ${statusText}` : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'task_started':
        type = 'task';
        title = 'Task Started';
        detail = messageText || `Task ${event.task_id} started`;
        break;
      case 'task_completed':
        type = 'task';
        title = 'Task Completed';
        detail = messageText || `Task ${event.task_id} completed`;
        break;
      case 'task_queued':
        type = 'status';
        title = 'Task Queued';
        detail = [
          messageText || 'Task queued and waiting for worker claim',
          queueAgeSeconds !== null && Number.isFinite(queueAgeSeconds)
            ? `age: ${queueAgeSeconds.toFixed(1)}s`
            : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'task_claimed':
        type = 'task';
        title = 'Task Claimed';
        detail = [
          messageText || 'Worker claimed queued task dispatch',
          queueLatencySeconds !== null && Number.isFinite(queueLatencySeconds)
            ? `queue latency: ${queueLatencySeconds.toFixed(1)}s`
            : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'task_queue_stale':
        type = 'error';
        title = 'Queue Stalled';
        detail = [
          messageText || 'Queued task exceeded the dispatch watchdog SLA',
          queueAgeSeconds !== null && Number.isFinite(queueAgeSeconds)
            ? `age: ${queueAgeSeconds.toFixed(1)}s`
            : null,
          failureRootCause ? `root cause: ${humanizeToken(failureRootCause)}` : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'task_dispatch_rejected':
        type = 'status';
        title = 'Dispatch Rejected';
        detail = [
          messageText || detailToText(details.reason) || 'Stale or duplicate worker dispatch rejected',
          failureRootCause ? `root cause: ${humanizeToken(failureRootCause)}` : null,
          queueLatencySeconds !== null && Number.isFinite(queueLatencySeconds)
            ? `queue latency: ${queueLatencySeconds.toFixed(1)}s`
            : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ');
        break;
      case 'task_failed':
        type = 'error';
        title = 'Task Failed';
        detail = messageText || `Task ${event.task_id} failed`;
        break;
      case 'validation_result':
        type = statusText === 'rejected' || statusText === 'failed' ? 'error' : 'validation';
        title = 'Validation Result';
        detail = [
          detailToText(details.stage) ? `${humanizeToken(detailToText(details.stage))} validation` : null,
          statusText ? `status: ${statusText}` : null,
          reasonsText,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ') || 'Validation result recorded';
        break;
      case 'health_score_updated': {
        const score =
          typeof details.score === 'number'
            ? details.score
            : typeof details.score === 'string'
              ? Number(details.score)
              : null;
        const slope =
          typeof details.slope === 'number'
            ? details.slope
            : typeof details.slope === 'string'
              ? Number(details.slope)
              : null;
        type = score !== null && score < 50 ? 'error' : 'status';
        title = 'Health Score';
        detail = score !== null
          ? `Health ${score}/100${typeof slope === 'number' ? ` • slope ${slope > 0 ? '+' : ''}${slope}` : ''}`
          : 'Health score updated';
        break;
      }
      case 'divergence_detected':
        type = 'error';
        title = 'Off-Track Detected';
        detail = [
          detailToText(details.reason),
          detailToText(details.last_known_good_event_id)
            ? `last good: ${detailToText(details.last_known_good_event_id)}`
            : null,
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ') || 'Session divergence detected';
        break;
      case 'intent_outcome_mismatch':
        type = 'repair';
        title = 'Intent Gap';
        detail = [
          detailToText(details.declared_intent),
          detailToText(details.mismatch_score)
            ? `score: ${detailToText(details.mismatch_score)}`
            : null,
          detailToText(details.missing_expected_files),
        ]
          .filter((item): item is string => Boolean(item))
          .join(' | ') || 'Intent and outcome drift detected';
        break;
      case 'repair_generated':
      case 'repair_applied':
      case 'repair_rejected':
        type = event.event_type === 'repair_rejected' ? 'error' : 'repair';
        title = humanizeToken(event.event_type);
        detail = messageText || reasonsText || title;
        break;
      case 'workspace_restore_skipped':
      case 'workspace_preserved':
      case 'resume_workspace_drift':
      case 'workspace_contract_failed':
        type = 'checkpoint';
        title = humanizeToken(event.event_type);
        detail = messageText || reasonsText || title;
        break;
      case 'completion_evidence_failed':
        type = 'error';
        title = 'Completion Evidence Failed';
        detail = messageText || reasonsText || 'Deterministic completion evidence check failed';
        break;
      case 'evaluator_result':
        type = 'validation';
        title = 'Evaluator Result';
        detail = messageText || reasonsText || 'Completion evaluator recorded a result';
        break;
      default:
        title = humanizeToken(event.event_type);
        detail =
          messageText ||
          reasonsText ||
          `${title}${Object.keys(details).length > 0 ? `: ${JSON.stringify(details)}` : ''}`;
        break;
    }

    return {
      id: buildOrchestrationTimelineKey(event),
      at: event.timestamp || new Date().toISOString(),
      type,
      title,
      detail,
    };
  }, [buildOrchestrationTimelineKey, detailToText, humanizeToken]);

  const replaceTimelineWithOrchestrationEvents = useCallback((events: OrchestrationEvent[]) => {
    const normalizedEvents = normalizeOrchestrationEvents(events);
    const nextSeen = new Set<string>(
      normalizedEvents.map((event) => buildOrchestrationTimelineKey(event))
    );
    const nextTimeline = normalizedEvents
      .map(toTimelineEventFromOrchestrationEvent)
      .slice(-MAX_TIMELINE_EVENTS);

    seenOrchestrationTimelineKeysRef.current = nextSeen;
    setOrchestrationEvents(normalizedEvents);
    setTimelineEvents(nextTimeline);
  }, [buildOrchestrationTimelineKey, normalizeOrchestrationEvents, toTimelineEventFromOrchestrationEvent]);

  const appendOrchestrationTimelineEvent = useCallback((event: OrchestrationEvent) => {
    const key = buildOrchestrationTimelineKey(event);
    if (seenOrchestrationTimelineKeysRef.current.has(key)) {
      return;
    }
    seenOrchestrationTimelineKeysRef.current.add(key);
    setOrchestrationEvents((prev) => normalizeOrchestrationEvents([...prev, event]));
    const timelineEvent = toTimelineEventFromOrchestrationEvent(event);
    setTimelineEvents((prev) => [...prev.slice(-(MAX_TIMELINE_EVENTS - 1)), timelineEvent]);
  }, [buildOrchestrationTimelineKey, normalizeOrchestrationEvents, toTimelineEventFromOrchestrationEvent]);

  const loadStateDiff = useCallback(async (currentSessionId: number, sessionTasks: Task[] = []) => {
    if (diffAvailableRef.current === false) return;
    try {
      const relevantTask = (sessionTasks || [])
        .filter((task) => task.session_id === currentSessionId)
        .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))[0];
      const response = await sessionsAPI.getSessionDiff(currentSessionId, {
        task_id: relevantTask?.id,
      });
      if (response.data?.available === false) {
        diffAvailableRef.current = null;
        setStateDiff(null);
      } else {
        diffAvailableRef.current = true;
        setStateDiff(response.data);
      }
    } catch (loadError) {
      diffAvailableRef.current = false;
      console.debug('State diff unavailable:', loadError);
      setStateDiff(null);
    }
  }, []);

  const loadDivergenceCompare = useCallback(async (currentSessionId: number) => {
    try {
      const response = await sessionsAPI.getSessionDivergenceCompare(currentSessionId, 5);
      setCompareMatches(response.data);
    } catch (loadError) {
      console.debug('Divergence compare unavailable:', loadError);
      setCompareMatches(null);
    }
  }, []);

  const loadDispatchWatchdog = useCallback(async (currentSessionId: number) => {
    try {
      const response = await sessionsAPI.getSessionDispatchWatchdog(currentSessionId);
      setDispatchWatchdog(response.data);
    } catch (loadError) {
      console.debug('Dispatch watchdog unavailable:', loadError);
      setDispatchWatchdog(null);
    }
  }, []);

  const loadDecisionTimeline = useCallback(async (currentSessionId: number) => {
    try {
      const response = await sessionsAPI.getDecisionTimeline(currentSessionId);
      setDecisionEvents(response.data.events || []);
    } catch (loadError) {
      console.debug('Decision timeline unavailable:', loadError);
      setDecisionEvents([]);
    }
  }, []);

  const loadReplayInvestigation = useCallback(async (currentSessionId: number) => {
    try {
      const response = await sessionsAPI.getReplay(currentSessionId);
      setReplayInvestigation(response.data);
    } catch (loadError) {
      console.debug('Replay investigation unavailable:', loadError);
      setReplayInvestigation(null);
    }
  }, []);

  const healthEvents = orchestrationEvents
    .filter((event) => event.event_type === 'health_score_updated')
    .map((event) => {
      const rawScore = event.details?.score;
      const rawSlope = event.details?.slope;
      const score =
        typeof rawScore === 'number'
          ? rawScore
          : typeof rawScore === 'string'
            ? Number(rawScore)
            : NaN;
      const slope =
        typeof rawSlope === 'number'
          ? rawSlope
          : typeof rawSlope === 'string'
            ? Number(rawSlope)
            : null;
      return {
        timestamp: event.timestamp,
        score,
        slope: typeof slope === 'number' && Number.isFinite(slope) ? slope : null,
      };
    })
    .filter((event) => Number.isFinite(event.score));

  const offTrackMoment = useMemo<OffTrackMoment | null>(() => {
    if (orchestrationEvents.length === 0) {
      return null;
    }

    const sortedEvents = orchestrationEvents
      .slice()
      .sort((a, b) => {
        const aTime = parseApiDate(a.timestamp)?.getTime() || 0;
        const bTime = parseApiDate(b.timestamp)?.getTime() || 0;
        return aTime - bTime;
      });
    const candidates: OffTrackMoment[] = [];

    for (const event of sortedEvents) {
      const details = event.details || {};
      const score =
        typeof details.score === 'number'
          ? details.score
          : typeof details.score === 'string'
            ? Number(details.score)
            : null;
      const phase =
        detailToText(details.phase) ||
        detailToText(details.stage) ||
        humanizeToken(event.event_type);

      if (event.event_type === 'health_score_updated' && score !== null && score < 40) {
        candidates.push({
          id: `health-${buildOrchestrationTimelineKey(event)}`,
          timestamp: event.timestamp,
          phase,
          reason: `Health score crossed below 40 at ${score}/100.`,
          trigger: 'health_threshold',
          health_score: score,
          event_type: event.event_type,
          event_id: event.event_id,
        });
      }

      if (['divergence_detected', 'intent_outcome_mismatch'].includes(event.event_type)) {
        candidates.push({
          id: `divergence-${buildOrchestrationTimelineKey(event)}`,
          timestamp: event.timestamp,
          phase,
          reason:
            detailToText(details.reason) ||
            detailToText(details.message) ||
            'The runtime recorded divergence between intended and observed progress.',
          trigger: 'divergence',
          event_type: event.event_type,
          event_id: event.event_id,
        });
      }
    }

    for (let index = 0; index < sortedEvents.length; index += 1) {
      const event = sortedEvents[index];
      if (event.event_type !== 'validation_result') {
        continue;
      }
      const details = event.details || {};
      const status = String(details.status || '').toLowerCase();
      if (!['rejected', 'failed', 'invalid'].includes(status)) {
        continue;
      }
      const laterAcceptedRepair = sortedEvents.slice(index + 1).find((candidate) => {
        if (candidate.event_type === 'repair_applied') {
          return true;
        }
        if (candidate.event_type !== 'validation_result') {
          return false;
        }
        const candidateStatus = String(candidate.details?.status || '').toLowerCase();
        return ['accepted', 'passed', 'success'].includes(candidateStatus);
      });
      if (!laterAcceptedRepair) {
        continue;
      }
      const phase =
        detailToText(details.phase) ||
        detailToText(details.stage) ||
        humanizeToken(event.event_type);
      candidates.push({
        id: `accepted-after-rejection-${buildOrchestrationTimelineKey(event)}`,
        timestamp: event.timestamp,
        phase,
        reason:
          detailToText(details.reasons) ||
          detailToText(details.reason) ||
          'Validation rejected an output that later continued through repair.',
        trigger: 'accepted_after_rejection',
        event_type: event.event_type,
        event_id: event.event_id,
      });
    }

    return candidates.sort((a, b) => {
      const aTime = parseApiDate(a.timestamp)?.getTime() || 0;
      const bTime = parseApiDate(b.timestamp)?.getTime() || 0;
      return aTime - bTime;
    })[0] || null;
  }, [buildOrchestrationTimelineKey, detailToText, humanizeToken, orchestrationEvents, parseApiDate]);

  const repairGenealogy = useMemo<RepairGenealogyNode[]>(() => {
    if (!orchestrationEvents.some((event) => event.event_type.startsWith('repair_'))) {
      return [];
    }

    const sortedEvents = orchestrationEvents
      .slice()
      .sort((a, b) => {
        const aTime = parseApiDate(a.timestamp)?.getTime() || 0;
        const bTime = parseApiDate(b.timestamp)?.getTime() || 0;
        return aTime - bTime;
      });
    const firstRepairIndex = sortedEvents.findIndex((event) =>
      event.event_type.startsWith('repair_')
    );
    if (firstRepairIndex < 0) {
      return [];
    }
    let firstRejectedValidationIndex = -1;
    for (let index = firstRepairIndex; index >= 0; index -= 1) {
      const event = sortedEvents[index];
      if (event.event_type !== 'validation_result') {
        continue;
      }
      const status = String(event.details?.status || '').toLowerCase();
      if (['rejected', 'failed', 'invalid'].includes(status)) {
        firstRejectedValidationIndex = index;
        break;
      }
    }
    const startIndex = Math.max(0, firstRejectedValidationIndex);
    const relevantEvents = sortedEvents
      .slice(startIndex)
      .filter((event) => {
        if (event.event_type.startsWith('repair_')) {
          return true;
        }
        if (event.event_type === 'retry_entered') {
          return true;
        }
        if (event.event_type === 'validation_result') {
          return true;
        }
        return ['task_failed', 'completion_evidence_failed'].includes(event.event_type);
      })
      .slice(0, 32);

    let previousId: string | null = null;
    return relevantEvents.map((event, index) => {
      const details = event.details || {};
      const rawStatus = String(details.status || '').toLowerCase();
      const status: RepairGenealogyNode['status'] =
        index === 0 && event.event_type === 'validation_result'
          ? 'original'
          : event.event_type === 'repair_rejected' ||
              ['rejected', 'failed', 'invalid'].includes(rawStatus)
            ? 'rejected'
            : event.event_type === 'repair_applied' ||
                ['accepted', 'passed', 'success'].includes(rawStatus)
              ? 'accepted'
              : ['task_failed', 'completion_evidence_failed'].includes(event.event_type)
                ? 'abandoned'
                : 'repair';
      const nodeId = buildOrchestrationTimelineKey(event);
      const node: RepairGenealogyNode = {
        id: nodeId,
        parent_id: previousId,
        timestamp: event.timestamp,
        event_type: event.event_type,
        title:
          status === 'original'
            ? 'Original Rejected'
            : event.event_type === 'retry_entered'
              ? 'Retry Started'
              : humanizeToken(event.event_type),
        status,
        validator: detailToText(details.validator) || detailToText(details.stage),
        reason:
          detailToText(details.reasons) ||
          detailToText(details.reason) ||
          detailToText(details.message),
        event_id: event.event_id,
        details,
      };
      previousId = nodeId;
      return node;
    });
  }, [buildOrchestrationTimelineKey, detailToText, humanizeToken, orchestrationEvents, parseApiDate]);

  const anomalyEvents = timelineEvents.filter(
    (event) => event.title === 'Off-Track Detected' || event.title === 'Intent Gap'
  );

  const timelineSpans: TimelineSpan[] = (() => {
    if (orchestrationEvents.length === 0) {
      return [];
    }

    const childrenByParent = new Map<string, OrchestrationEvent[]>();
    for (const event of orchestrationEvents) {
      if (!event.parent_event_id) {
        continue;
      }
      const existing = childrenByParent.get(event.parent_event_id) || [];
      existing.push(event);
      childrenByParent.set(event.parent_event_id, existing);
    }

    const classifyLane = (eventType: string): TimelineSpan['lane'] => {
      if (['tool_invoked', 'tool_failed'].includes(eventType)) return 'tool';
      if (
        [
          'task_queued',
          'task_claimed',
          'task_queue_stale',
          'task_dispatch_rejected',
          'checkpoint_saved',
          'checkpoint_loaded',
          'checkpoint_redirected',
          'workspace_restore_skipped',
          'workspace_preserved',
          'resume_workspace_drift',
          'workspace_contract_failed',
        ].includes(eventType)
      ) {
        return 'workspace';
      }
      if (
        [
          'validation_result',
          'reasoning_artifact_generated',
          'completion_evidence_failed',
          'health_score_updated',
          'divergence_detected',
          'intent_outcome_mismatch',
          'evaluator_result',
        ].includes(eventType)
      ) {
        return 'validation';
      }
      if (
        ['phase_started', 'phase_finished', 'step_started', 'step_finished', 'retry_entered', 'plan_revised'].includes(
          eventType
        )
      ) {
        return 'reasoning';
      }
      return 'system';
    };

    const deriveStatus = (events: OrchestrationEvent[]): TimelineSpan['status'] => {
      const types = events.map((event) => event.event_type);
      if (
        types.some((type) =>
          ['tool_failed', 'task_failed', 'divergence_detected'].includes(type)
        )
      ) {
        return 'error';
      }
      if (
        types.some((type) =>
          ['retry_entered', 'intent_outcome_mismatch', 'validation_result'].includes(type)
        )
      ) {
        return 'warning';
      }
      return 'healthy';
    };

    return orchestrationEvents
      .filter((event) => !event.parent_event_id || childrenByParent.has(event.event_id || ''))
      .map((rootEvent) => {
        const relatedEvents = [
          rootEvent,
          ...(childrenByParent.get(rootEvent.event_id || '') || []),
        ];
        const titles = relatedEvents.map((event) => humanizeToken(event.event_type));
        return {
          id: rootEvent.event_id || buildOrchestrationTimelineKey(rootEvent),
          title: humanizeToken(rootEvent.event_type),
          lane: classifyLane(rootEvent.event_type),
          status: deriveStatus(relatedEvents),
          started_at: rootEvent.timestamp,
          event_count: relatedEvents.length,
          summary: titles.slice(0, 4).join(' -> '),
        };
      })
      .slice(-24);
  })();

  const loadTimelineEvents = useCallback(async (currentSessionId: number, sessionTasks: Task[]) => {
    const relevantTaskIds = Array.from(
      new Set(
        (sessionTasks || [])
          .filter((task) => task.session_id === currentSessionId)
          .map((task) => task.id)
      )
    );

    try {
      let taskIds = relevantTaskIds;
      if (taskIds.length === 0) {
        const logsResponse = await sessionsAPI.getLogs(currentSessionId);
        taskIds = Array.from(
          new Set(
            (logsResponse.data.logs || [])
              .map((entry) => entry.task_id)
              .filter((taskId): taskId is number => typeof taskId === 'number' && taskId > 0)
          )
        );
      }

      if (taskIds.length === 0) {
        return;
      }

      const responses = await Promise.all(
        taskIds.map((taskId) => sessionsAPI.getTaskEvents(currentSessionId, taskId))
      );
      const events = responses.flatMap((response) => response.data.events || []);
      if (events.length > 0) {
        replaceTimelineWithOrchestrationEvents(events);
        await loadDivergenceCompare(currentSessionId);
      }
    } catch (loadError) {
      console.error('Failed to load orchestration event timeline:', loadError);
    }
  }, [loadDivergenceCompare, replaceTimelineWithOrchestrationEvents]);

  const loadCheckpointCount = useCallback(async (id: number) => {
    try {
      const checkpointsRes = await sessionsAPI.listCheckpoints(id);
      setCheckpointCount(checkpointsRes.data.total_count || 0);
      setCheckpoints(checkpointsRes.data.checkpoints || []);
      setRecommendedCheckpointName(checkpointsRes.data.recommended_checkpoint_name || null);
    } catch {
      setCheckpointCount(0);
      setCheckpoints([]);
      setRecommendedCheckpointName(null);
    }
  }, []);

  const loadInterventions = useCallback(async (id: number) => {
    try {
      const res = await sessionsAPI.listInterventions(id, false);
      setInterventions(res.data.interventions || []);
    } catch {
      setInterventions([]);
    }
  }, []);

  const loadFailureSummary = useCallback(async (id: number) => {
    setFailureSummaryLoading(true);
    try {
      const res = await sessionsAPI.getFailureSummary(id);
      setFailureSummary(res.data);
    } catch {
      setFailureSummary(null);
    } finally {
      setFailureSummaryLoading(false);
    }
  }, []);

  const handleFeedbackSubmit = useCallback(async (feedbackText: string) => {
    if (!sessionId) return;
    const res = await sessionsAPI.submitOperatorFeedback(Number(sessionId), feedbackText);
    setFailureSummary(res.data);
  }, [sessionId]);

  const handleReplan = useCallback(async () => {
    if (!sessionId || !project) return;
    const res = await sessionsAPI.replanSession(Number(sessionId));
    setFailureSummary((prev) => prev ? { ...prev, replan_planning_session_id: res.data.planning_session_id } : prev);
    navigate(`/projects/${project.id}?tab=planner`);
  }, [sessionId, project, navigate]);

  const handleSubmitReply = useCallback(async (interventionId: number, reply: string) => {
    if (!sessionId) return;
    try {
      await sessionsAPI.replyToIntervention(Number(sessionId), interventionId, { reply });
      await loadInterventions(Number(sessionId));
      await loadDecisionTimeline(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
    } catch (error) {
      console.error('Failed to submit reply:', error);
      alert('Failed to submit reply');
    }
  }, [loadDecisionTimeline, loadInterventions, sessionId]);

  const handleApproveIntervention = useCallback(async (interventionId: number) => {
    if (!sessionId) return;
    try {
      await sessionsAPI.approveIntervention(Number(sessionId), interventionId);
      await loadInterventions(Number(sessionId));
      await loadDecisionTimeline(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
    } catch (error) {
      console.error('Failed to approve intervention:', error);
      alert('Failed to approve intervention');
    }
  }, [loadDecisionTimeline, loadInterventions, sessionId]);

  const handleDenyIntervention = useCallback(async (interventionId: number, reason?: string) => {
    if (!sessionId) return;
    try {
      await sessionsAPI.denyIntervention(Number(sessionId), interventionId, reason ? { reason } : {});
      await loadInterventions(Number(sessionId));
      await loadDecisionTimeline(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
    } catch (error) {
      console.error('Failed to deny intervention:', error);
      alert('Failed to deny intervention');
    }
  }, [loadDecisionTimeline, loadInterventions, sessionId]);

  const inspectCheckpoint = useCallback(async (checkpointName: string) => {
    if (!sessionId) return;
    try {
      const response = await sessionsAPI.inspectCheckpoint(Number(sessionId), checkpointName);
      setCheckpointInspection(response.data);
      pushTimelineEvent(`Inspected checkpoint ${checkpointName}`, 'INFO');
    } catch (error) {
      console.error('Failed to inspect checkpoint:', error);
      alert('Failed to inspect checkpoint');
    }
  }, [pushTimelineEvent, sessionId]);

  const setupWebSocket = useCallback(async (session_id: number) => {
    if (wsRef.current) {
      return;
    }

    let ws!: WebSocket;
    try {
      ws = await sessionsAPI.getLogsStream(session_id);
    } catch (error) {
      console.error('Failed to obtain WebSocket ticket:', error);
      setWsConnected(false);
      return;
    }

    // Guard against concurrent calls resolving after this one
    if (wsRef.current) {
      ws.close();
      return;
    }

    wsRef.current = ws;
    console.log('Attempting WebSocket connection:', ws.url);

    wsRef.current.onopen = () => {
      console.log('✅ WebSocket connected');
      setWsConnected(true);
    };

    wsRef.current.onmessage = (event) => {
        if (!event.data || event.data.length === 0) {
          return;
        }

        if (event.data.trim().startsWith('<')) {
          console.error('WebSocket received HTML instead of JSON:', event.data.substring(0, 100));
          console.error('This usually means the WebSocket is connecting to the wrong port. Backend should be at :8080, not :3000');
          return;
        }

        if (event.data === 'ping' || event.data === 'pong') {
          console.debug('Received plain text message:', event.data);
          if (event.data === 'ping') {
            wsRef.current?.send('pong');
          }
          return;
        }

        try {
          const data = JSON.parse(event.data);

          if (data.type === 'log') {
            const noisyMessage = isNoisySessionLogMessage(data.message);
            if (logVerbosity === 'clean' && noisyMessage) {
              return;
            }
            console.log('✅ Received log message:', data.message);
            setAllLogs(prev => {
              const next = [
                ...prev.slice(-499),
                {
                  message: logVerbosity === 'clean' ? cleanJsonLogMessage(data.message) : data.message,
                  timestamp: formatLogTimestamp(data.timestamp),
                },
              ];
              applyLogView(next, logViewMode);
              return next;
            });
          } else if (data.type === 'orchestration_event') {
            appendOrchestrationTimelineEvent(data as OrchestrationEvent);
            if (sessionId) {
              void loadDecisionTimeline(Number(sessionId));
            }
            if (
              sessionId &&
              ['phase_finished', 'checkpoint_saved', 'retry_entered', 'tool_failed', 'validation_result', 'reasoning_artifact_generated'].includes(
                String(data.event_type || '')
              )
            ) {
              void loadStateDiff(Number(sessionId), tasksRef.current);
            }
            if (
              sessionId &&
              ['task_queued', 'task_claimed', 'task_dispatch_rejected', 'retry_entered'].includes(
                String(data.event_type || '')
              )
            ) {
              void loadDispatchWatchdog(Number(sessionId));
            }
          } else if (data.type === 'ping') {
            console.debug('Received ping, sending pong');
            wsRef.current?.send(JSON.stringify({ type: 'pong' }));
          } else if (data.type === 'pong') {
            console.debug('Received pong');
          } else if (data.type === 'connected') {
            console.log('✅ WebSocket connected message received');
          } else if (data.type === 'session_ended') {
            console.log('Session ended via WebSocket, status:', data.status);
            shouldReconnectRef.current = false;
            setSession(prev => prev ? { ...prev, status: data.status } : prev);
          } else {
            console.debug('WebSocket message received:', data);
          }
        } catch (e) {
          console.warn('❌ Failed to parse WebSocket message:', e);
          console.warn('Raw data:', event.data.substring(0, 200));
          console.warn('Data type:', typeof event.data);
          console.warn('Data length:', event.data?.length);
        }
      };

      wsRef.current.onerror = (error) => {
        console.error('WebSocket error:', error);
        setWsConnected(false);
      };

      wsRef.current.onclose = () => {
        if (!shouldReconnectRef.current) {
          console.log('WebSocket closed');
          setWsConnected(false);
          wsRef.current = null;
          return;
        }

        console.log('WebSocket closed, reconnecting...');
        setWsConnected(false);
        wsRef.current = null;
        reconnectTimeoutRef.current = setTimeout(() => {
          setupWebSocket(session_id);
        }, 3000);
      };
  }, [appendOrchestrationTimelineEvent, applyLogView, formatLogTimestamp, loadDecisionTimeline, loadDispatchWatchdog, loadStateDiff, logVerbosity, logViewMode, sessionId]);

  const scheduleWebSocketConnect = useCallback(
    (session_id: number, delayMs: number = 0) => {
      shouldReconnectRef.current = true;

      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }

      if (wsRef.current) {
        return;
      }

      reconnectTimeoutRef.current = setTimeout(() => {
        reconnectTimeoutRef.current = null;
        setupWebSocket(session_id);
      }, delayMs);
    },
    [setupWebSocket]
  );

  const replayCheckpoint = useCallback(async (checkpointName: string) => {
    if (!sessionId) return;
    try {
      await sessionsAPI.replayCheckpoint(Number(sessionId), checkpointName);
      pushTimelineEvent(`Replay requested from checkpoint ${checkpointName}`, 'INFO');
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      if (updated.data.project_id) {
        const tasksRes = await tasksAPI.getByProject(updated.data.project_id);
        setTasks(tasksRes.data || []);
        await loadTimelineEvents(updated.data.id, tasksRes.data || []);
        await loadStateDiff(updated.data.id, tasksRes.data || []);
      }
      await loadCheckpointCount(Number(sessionId));
      if (!wsRef.current && updated.data.status === 'running') {
        scheduleWebSocketConnect(Number(sessionId), 1200);
      }
    } catch (error) {
      console.error('Failed to replay checkpoint:', error);
      const apiError = error as ApiErrorLike;
      const errorMsg = apiError.response?.data?.detail || apiError.message || 'Unknown error';
      alert(`Failed to replay checkpoint: ${errorMsg}`);
    }
  }, [loadCheckpointCount, loadStateDiff, loadTimelineEvents, pushTimelineEvent, scheduleWebSocketConnect, sessionId]);

  const getUsefulCheckpoints = useCallback(() => {
    return [...checkpoints]
      .filter((checkpoint) => {
        if (checkpoint.resumable === false) {
          return false;
        }
        if (checkpoint.recommended || checkpoint.name === recommendedCheckpointName) {
          return true;
        }
        if ((checkpoint.completed_steps || 0) > 0 || (checkpoint.step_index || 0) > 0) {
          return true;
        }
        if ((checkpoint.progress_score || 0) > 0) {
          return true;
        }
        return checkpoint.restore_fidelity?.status === 'high' || checkpoint.restore_fidelity?.status === 'medium';
      })
      .sort((left, right) => {
        if (left.recommended && !right.recommended) return -1;
        if (!left.recommended && right.recommended) return 1;
        if (left.name === recommendedCheckpointName && right.name !== recommendedCheckpointName) return -1;
        if (left.name !== recommendedCheckpointName && right.name === recommendedCheckpointName) return 1;
        return (right.progress_score || 0) - (left.progress_score || 0);
      });
  }, [checkpoints, recommendedCheckpointName]);

  useEffect(() => {
    if (!sessionId) {
      setError('Session ID not found');
      setLoading(false);
      return;
    }

    const abortController = new AbortController();

    const loadSessionData = async () => {
      try {
        const sessionRes = await sessionsAPI.getById(Number(sessionId));
        const tasksRes = await tasksAPI.getByProject(sessionRes.data.project_id || 0);
        const projectRes = await projectsAPI.getById(sessionRes.data.project_id || 0);

        if (abortController.signal.aborted) return;

        setSession(sessionRes.data);
        setTasks(tasksRes.data || []);
        setProject(projectRes.data);
        await loadCheckpointCount(Number(sessionId));
        await loadTimelineEvents(sessionRes.data.id, tasksRes.data || []);
        await loadDecisionTimeline(sessionRes.data.id);
        await loadReplayInvestigation(sessionRes.data.id);
        await loadDispatchWatchdog(sessionRes.data.id);
        try {
          const kuRes = await sessionsAPI.getKnowledgeUsage(Number(sessionId));
          setKnowledgeUsage(kuRes.data.phases || {});
        } catch {
          setKnowledgeUsage({});
        }
        if (sessionRes.data.status === 'running') {
          await loadStateDiff(sessionRes.data.id, tasksRes.data || []);
        }

        if (abortController.signal.aborted) return;

        if (sessionRes.data.status === 'running' || sessionRes.data.status === 'awaiting_input') {
          scheduleWebSocketConnect(sessionRes.data.id);
        } else {
          console.log(`Session is ${sessionRes.data.status}, not connecting WebSocket yet`);
        }
        if (sessionRes.data.status === 'awaiting_input') {
          await loadInterventions(sessionRes.data.id);
        }
        if (sessionRes.data.status === 'stopped') {
          void loadFailureSummary(sessionRes.data.id);
        }
      } catch (err) {
        if (abortController.signal.aborted) return;
        console.error('Failed to load session:', err);
        setError(err instanceof Error ? err.message : 'Failed to load session');
      } finally {
        setLoading(false);
      }
    };

    // Load logs on initial load (after session data is loaded)
    const loadLogs = async () => {
      if (!sessionId || abortController.signal.aborted) return;
      try {
        const response = await sessionsAPI.getLogs(Number(sessionId));
        if (abortController.signal.aborted) return;
        const loadedLogs = visibleLogs((response.data?.logs || []) as SessionLogItem[]);
        console.log(`Loaded ${loadedLogs.length} visible logs for session ${sessionId}`);
        const terminalLogs = loadedLogs.map(toTerminalLogEntry);
        setAllLogs(terminalLogs);
        applyLogView(terminalLogs, logViewMode);
      } catch (err) {
        if (abortController.signal.aborted) return;
        console.error('Failed to load logs:', err);
      }
    };

    // Load session data and logs
    loadSessionData().then(() => {
      loadLogs();
    });

    // Poll for status updates every 5 seconds
    const statusPollInterval = setInterval(async () => {
      if (!sessionId || abortController.signal.aborted) return;
      try {
        const currentSession = await sessionsAPI.getById(Number(sessionId));
        if (abortController.signal.aborted) return;
        const currentStatus = currentSession.data.status;
        setSession(prev => {
          // Don't let a stale poll response downgrade an active session to "pending"
          if (prev && prev.status === 'running' && currentStatus === 'pending') return prev;
          return currentSession.data;
        });

        if (currentSession.data.project_id) {
          const currentTasks = await tasksAPI.getByProject(currentSession.data.project_id);
          setTasks(currentTasks.data || []);
          if (currentStatus === 'running') {
            await loadStateDiff(Number(sessionId), currentTasks.data || []);
          }
          await loadReplayInvestigation(Number(sessionId));
        }

        if (
          (currentStatus === 'running' || currentStatus === 'awaiting_input') &&
          !wsRef.current
        ) {
          console.log(`Session is now ${currentStatus}, connecting WebSocket...`);
          scheduleWebSocketConnect(Number(sessionId), 1000);
        }

        if (currentStatus === 'awaiting_input') {
          await loadInterventions(Number(sessionId));
        }

        if (currentStatus === 'stopped' || currentStatus === 'paused') {
          await loadCheckpointCount(Number(sessionId));
        }
      } catch (err) {
        if (abortController.signal.aborted) return;
        console.warn('Status poll error:', err);
      }
    }, 5000);

    // Cleanup WebSocket and interval on unmount
    return () => {
      abortController.abort();
      shouldReconnectRef.current = false;
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      clearInterval(statusPollInterval);
    };
  }, [applyLogView, loadCheckpointCount, loadDecisionTimeline, loadDispatchWatchdog, loadFailureSummary, loadInterventions, loadReplayInvestigation, loadStateDiff, loadTimelineEvents, logVerbosity, logViewMode, scheduleWebSocketConnect, sessionId, toTerminalLogEntry, visibleLogs]);

  const handleStartSessionFresh = async () => {
    if (!session || !sessionId) {
      console.error('Cannot start: session or sessionId missing');
      alert('Session not loaded properly');
      return;
    }
    
    console.log(`Starting session ${sessionId}...`);
    pushTimelineEvent(`Start requested for session ${sessionId}`, 'INFO');
    diffAvailableRef.current = null;
    const previousSession = session;
    setSession((current) =>
      current
        ? {
            ...current,
            status: 'running',
            is_active: true,
            started_at: current.started_at || new Date().toISOString(),
          }
        : current
    );
    try {
      const response = await sessionsAPI.start(Number(sessionId));
      console.log('Start API response:', response);
      const updated = await sessionsAPI.getById(Number(sessionId));
      console.log('Updated session:', updated.data);
      setSession(updated.data);
      if (updated.data.project_id) {
        const tasksRes = await tasksAPI.getByProject(updated.data.project_id);
        setTasks(tasksRes.data || []);
        await loadTimelineEvents(updated.data.id, tasksRes.data || []);
        await loadStateDiff(updated.data.id, tasksRes.data || []);
      }
      await loadCheckpointCount(Number(sessionId));
      if (updated.data.status === 'running') {
        if (!wsRef.current) {
          scheduleWebSocketConnect(Number(sessionId), 1000);
        }
      }
      pushTimelineEvent(`Session started with status: ${updated.data.status}`, 'INFO');
      alert(`Session ${session.name} started successfully!`);
    } catch (error: unknown) {
      setSession(previousSession);
      const apiError = error as ApiErrorLike;
      console.error('Failed to start session:', error);
      console.error('Error details:', apiError.response?.data || apiError.message);
      const errorMsg = apiError.response?.data?.detail || apiError.message || 'Unknown error';
      pushTimelineEvent(`Session start failed: ${errorMsg}`, 'ERROR');
      alert(`Failed to start session: ${errorMsg}`);
    }
  };

  const handleStartSession = async () => {
    if (session?.execution_mode === 'manual') {
      await handleStartSessionFresh();
      return;
    }
    const usefulCheckpoints = getUsefulCheckpoints();
    if (usefulCheckpoints.length > 0) {
      setCheckpointActionIntent('start');
      return;
    }
    await handleStartSessionFresh();
  };

  const handleStopSession = async (force: boolean = false) => {
    if (!session || !sessionId) return;
    const previousSession = session;
    const stoppedAt = new Date().toISOString();
    setSession((current) =>
      current
        ? {
            ...current,
            status: 'stopped',
            is_active: false,
            stopped_at: stoppedAt,
          }
        : current
    );
    pushTimelineEvent(`Stop requested for session ${sessionId}`, 'INFO');
    try {
      await sessionsAPI.stop(Number(sessionId), force);
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      await loadCheckpointCount(Number(sessionId));
      await loadStateDiff(Number(sessionId), tasks);
    } catch (error) {
      setSession(previousSession);
      console.error('Failed to stop session:', error);
      alert('Failed to stop session');
    }
  };

  const handlePauseSession = async () => {
    if (!session || !sessionId) return;
    const previousSession = session;
    const pausedAt = new Date().toISOString();
    setSession((current) =>
      current
        ? {
            ...current,
            status: 'paused',
            is_active: false,
            paused_at: pausedAt,
          }
        : current
    );
    pushTimelineEvent(`Pause requested for session ${sessionId}`, 'INFO');
    try {
      await sessionsAPI.pause(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      await loadCheckpointCount(Number(sessionId));
      await loadStateDiff(Number(sessionId), tasks);
      shouldReconnectRef.current = false;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    } catch (error) {
      setSession(previousSession);
      console.error('Failed to pause session:', error);
      alert('Failed to pause session');
    }
  };

  const handleRefreshLogs = async () => {
    if (!sessionId) return;
    try {
      const response = await sessionsAPI.getLogs(Number(sessionId));
      const logs = visibleLogs((response.data?.logs || []) as SessionLogItem[]);
      const terminalLogs = logs.map(toTerminalLogEntry);
      setAllLogs(terminalLogs);
      applyLogView(terminalLogs, logViewMode);
      console.log(`Refreshed ${terminalLogs.length} logs`);
    } catch (error: unknown) {
      const apiError = error as ApiErrorLike;
      console.error('Failed to refresh logs:', error);
      const errorMsg = apiError.response?.data?.detail || apiError.message || 'Unknown error';
      alert(`Failed to refresh logs: ${errorMsg}`);
    }
  };

  const handleResumeSessionDefault = async () => {
    if (!session || !sessionId) return;
    const previousSession = session;
    const resumedAt = new Date().toISOString();
    setSession((current) =>
      current
        ? {
            ...current,
            status: 'running',
            is_active: true,
            resumed_at: resumedAt,
          }
        : current
    );
    pushTimelineEvent(`Resume requested for session ${sessionId}`, 'INFO');
    diffAvailableRef.current = null;
    try {
      await sessionsAPI.resume(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      if (updated.data.project_id) {
        const tasksRes = await tasksAPI.getByProject(updated.data.project_id);
        setTasks(tasksRes.data || []);
        await loadTimelineEvents(updated.data.id, tasksRes.data || []);
      }
      await loadCheckpointCount(Number(sessionId));
      if (!wsRef.current && updated.data.status === 'running') {
        scheduleWebSocketConnect(Number(sessionId), 1200);
      }
    } catch (error) {
      setSession(previousSession);
      console.error('Failed to resume session:', error);
      alert('Failed to resume session');
    }
  };

  const handleResumeSession = async () => {
    const usefulCheckpoints = getUsefulCheckpoints();
    if (usefulCheckpoints.length === 0) {
      const recommendedCheckpoint = checkpoints.find(
        (checkpoint) =>
          checkpoint.name === recommendedCheckpointName && checkpoint.resumable !== false
      );
      if (recommendedCheckpoint) {
        await replayCheckpoint(recommendedCheckpoint.name);
        return;
      }
      const fallbackResumableCheckpoint = checkpoints.find(
        (checkpoint) => checkpoint.resumable !== false
      );
      if (fallbackResumableCheckpoint) {
        await replayCheckpoint(fallbackResumableCheckpoint.name);
        return;
      }
      await handleResumeSessionDefault();
      return;
    }

    if (usefulCheckpoints.length === 1) {
      await replayCheckpoint(usefulCheckpoints[0].name);
      return;
    }

    setCheckpointActionIntent('resume');
  };

  const refreshTasksForSession = useCallback(async () => {
    if (!sessionId || !session) return;
    try {
      const [refreshRes, tasksRes, updatedSession] = await Promise.all([
        sessionsAPI.refreshTasks(Number(sessionId)),
        tasksAPI.getByProject(session.project_id),
        sessionsAPI.getById(Number(sessionId)),
      ]);
      setTasks(tasksRes.data || []);
      setSession(updatedSession.data);
      await loadStateDiff(Number(sessionId), tasksRes.data || []);
      if (refreshRes.data.queued_task) {
        pushTimelineEvent(
          `Queued next task: ${refreshRes.data.queued_task.task_name}`,
          'INFO'
        );
      } else {
        pushTimelineEvent('Session task state refreshed', 'INFO');
      }
    } catch (error) {
      console.error('Failed to refresh session tasks:', error);
      alert('Failed to refresh session tasks');
    }
  }, [loadStateDiff, pushTimelineEvent, session, sessionId]);

  const handleExecuteTask = useCallback(async (task: Task) => {
    if (!sessionId) return;
    try {
      await sessionsAPI.runTask(Number(sessionId), task.id);
      pushTimelineEvent(`Queued task ${task.id}: ${task.title}`, 'INFO');
      const [updatedSession, updatedTasks] = await Promise.all([
        sessionsAPI.getById(Number(sessionId)),
        tasksAPI.getByProject(task.project_id),
      ]);
      setSession(updatedSession.data);
      setTasks(updatedTasks.data || []);
      await loadStateDiff(Number(sessionId), updatedTasks.data || []);
      if (!wsRef.current) {
        scheduleWebSocketConnect(Number(sessionId), 800);
      }
    } catch (error) {
      console.error('Failed to run task manually:', error);
      alert('Failed to queue the selected task');
    }
  }, [loadStateDiff, pushTimelineEvent, scheduleWebSocketConnect, sessionId]);

  const handleExecutionModeChange = useCallback(async (mode: 'automatic' | 'manual') => {
    if (!sessionId || !session) return;
    try {
      const response = await sessionsAPI.update(Number(sessionId), {
        execution_mode: mode,
      });
      setSession(response.data);
      pushTimelineEvent(`Execution mode switched to ${mode}`, 'INFO');
    } catch (error) {
      console.error('Failed to update execution mode:', error);
      alert('Failed to update execution mode');
    }
  }, [pushTimelineEvent, session, sessionId]);

  const handleAddOperatorGuidance = async () => {
    if (!sessionId || !interventionPrompt.trim()) return;
    setInterventionSubmitting(true);
    try {
      await sessionsAPI.addOperatorGuidance(Number(sessionId), {
        guidance: interventionPrompt.trim(),
      });
      pushTimelineEvent('Operator guidance added', 'INFO');
      await loadDecisionTimeline(Number(sessionId));
      setInterventionPrompt('');
    } catch (error) {
      console.error('Failed to add operator guidance:', error);
    } finally {
      setInterventionSubmitting(false);
    }
  };

  const renderOperatorGuidanceComposer = () => {
    if (!session || TERMINAL_SESSION_STATUSES.has(session.status)) {
      return null;
    }

    return (
      <div className="rounded-lg border border-slate-700 bg-slate-900/70 p-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-medium text-slate-100">
            <MessageCircle className="h-4 w-4 text-sky-300" />
            By the way
          </div>
          <span className="text-xs text-slate-500">Next agent turn</span>
        </div>
        <div className="flex flex-col gap-2 md:flex-row">
          <textarea
            rows={2}
            value={interventionPrompt}
            onChange={(e) => setInterventionPrompt(e.target.value)}
            placeholder="Prefer the smaller fix; avoid changing auth files."
            className="min-h-[44px] flex-1 resize-none rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-sky-500 focus:outline-none"
          />
          <div className="flex items-end justify-end gap-2">
            {interventionPrompt && (
              <button
                onClick={() => setInterventionPrompt('')}
                className="rounded px-3 py-2 text-xs text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
              >
                Clear
              </button>
            )}
            <button
              onClick={handleAddOperatorGuidance}
              disabled={interventionSubmitting || !interventionPrompt.trim()}
              className="flex items-center gap-1.5 rounded bg-sky-700 px-3 py-2 text-xs text-white transition-colors hover:bg-sky-600 disabled:opacity-50"
            >
              <MessageCircle className="h-3 w-3" />
              {interventionSubmitting ? 'Sending...' : 'Send'}
            </button>
          </div>
        </div>
      </div>
    );
  };

  useEffect(() => {
    if (pendingAgentInterventions.length === 0) {
      lastAutoOpenedAgentInterventionRef.current = null;
      setShowAgentInterventionModal(false);
      dismissInterventionToast();
      return;
    }

    const newestPendingAgentIntervention = pendingAgentInterventions[0];
    if (lastAutoOpenedAgentInterventionRef.current === newestPendingAgentIntervention.id) {
      return;
    }

    lastAutoOpenedAgentInterventionRef.current = newestPendingAgentIntervention.id;
    dismissInterventionToast();
    setInterventionToast({
      interventionId: newestPendingAgentIntervention.id,
      title:
        newestPendingAgentIntervention.intervention_type === 'approval'
          ? 'OpenClaw wants approval'
          : 'OpenClaw needs guidance',
      message: newestPendingAgentIntervention.prompt,
    });
    toastTimeoutRef.current = setTimeout(() => {
      setInterventionToast((current) =>
        current?.interventionId === newestPendingAgentIntervention.id ? null : current
      );
      toastTimeoutRef.current = null;
    }, 9000);
    playInterventionChime();
    setShowAgentInterventionModal(true);
  }, [dismissInterventionToast, pendingAgentInterventions, playInterventionChime]);

  useEffect(() => () => {
    if (toastTimeoutRef.current) {
      clearTimeout(toastTimeoutRef.current);
    }
  }, []);

  const getActionButtons = () => {
    if (!session) return null;

    switch (session.status) {
      case 'running':
        return (
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <button
                onClick={handlePauseSession}
                className="flex items-center gap-2 px-4 py-2 bg-yellow-600 hover:bg-yellow-700 text-white rounded-lg text-sm transition-colors"
              >
                <Pause className="h-4 w-4" />
                Pause
              </button>
              <button
                onClick={() => handleStopSession(false)}
                className="flex items-center gap-2 px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white rounded-lg text-sm transition-colors"
              >
                <Square className="h-4 w-4" />
                Stop
              </button>
              <button
                onClick={() => handleStopSession(true)}
                className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm transition-colors"
              >
                <XCircle className="h-4 w-4" />
                Force Stop
              </button>
            </div>
          </div>
        );

      case 'awaiting_input':
        return (
          <div className="flex items-center gap-2">
            <span className="flex items-center gap-2 rounded-lg border border-amber-700/50 bg-amber-900/30 px-3 py-2 text-sm text-amber-300">
              <MessageCircle className="h-4 w-4" />
              Waiting for Operator
            </span>
            {pendingAgentInterventions.length > 0 && (
              <button
                onClick={() => setShowAgentInterventionModal(true)}
                className="flex items-center gap-2 px-4 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm transition-colors"
              >
                <MessageCircle className="h-4 w-4" />
                Open Request
              </button>
            )}
            <button
              onClick={() => handleStopSession(true)}
                className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm transition-colors"
              >
              <XCircle className="h-4 w-4" />
              Force Stop
            </button>
          </div>
        );

      case 'paused':
        return (
          <div className="flex items-center gap-2">
            <button
              onClick={handleResumeSession}
              className="flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-sm transition-colors"
            >
              <Play className="h-4 w-4" />
              Resume
            </button>
            <button
              onClick={() => handleStopSession(true)}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm transition-colors"
            >
              <XCircle className="h-4 w-4" />
              Stop
            </button>
          </div>
        );

      case 'stopped':
      default:
        return (
          <div className="flex items-center gap-2">
            {checkpointCount > 0 && (
              <button
                onClick={handleResumeSession}
                className="flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-sm transition-colors"
              >
                <Play className="h-4 w-4" />
                Resume
              </button>
            )}
            <button
              onClick={handleStartSession}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm transition-colors"
            >
              <Play className="h-4 w-4" />
              Start
            </button>
          </div>
        );
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <button
          onClick={() => navigate('/sessions')}
          className="mb-4 text-blue-400 hover:text-blue-300 flex items-center gap-2"
        >
          ← Back to sessions
        </button>
        <div className="bg-red-900/20 border border-red-700 rounded-lg p-4 text-red-400">
          <p className="font-semibold">Error</p>
          <p className="text-sm mt-1">{error}</p>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="p-6">
        <button
          onClick={() => navigate('/sessions')}
          className="mb-4 text-blue-400 hover:text-blue-300 flex items-center gap-2"
        >
          ← Back to sessions
        </button>
        <p>Session not found</p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      {checkpointActionIntent && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur-sm">
          <div className="w-full max-w-2xl rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl">
            <div className="flex items-start justify-between gap-4 border-b border-slate-800 px-6 py-4">
              <div>
                <p className="text-sm font-semibold text-emerald-300">
                  {checkpointActionIntent === 'resume' ? 'Choose A Resume Checkpoint' : 'Choose How To Restart'}
                </p>
                <p className="mt-1 text-sm text-slate-300">
                  Only checkpoints with meaningful progress are shown here so we don&apos;t route back into empty or low-value replay states.
                </p>
              </div>
              <button
                onClick={() => setCheckpointActionIntent(null)}
                className="rounded-md px-2 py-1 text-sm text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
              >
                Close
              </button>
            </div>
            <div className="max-h-[70vh] overflow-y-auto px-6 py-5 space-y-3">
              {getUsefulCheckpoints().map((checkpoint) => (
                <button
                  key={checkpoint.name}
                  onClick={async () => {
                    setCheckpointActionIntent(null);
                    await replayCheckpoint(checkpoint.name);
                  }}
                  className="w-full rounded-xl border border-slate-700 bg-slate-950/70 p-4 text-left transition-colors hover:border-emerald-600 hover:bg-slate-900"
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-sm font-medium text-white">
                      {checkpoint.name}
                      {checkpoint.recommended ? (
                        <span className="ml-2 text-xs text-emerald-400">Recommended</span>
                      ) : null}
                    </p>
                    <span className="text-xs text-slate-400">
                      Score {checkpoint.progress_score || 0}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {formatDateTime(checkpoint.created_at)} • Step {checkpoint.step_index ?? 0} • Completed {checkpoint.completed_steps ?? 0}
                  </p>
                  {checkpoint.restore_fidelity ? (
                    <p className="mt-2 text-xs text-slate-300">
                      Replay fidelity: {checkpoint.restore_fidelity.status} ({checkpoint.restore_fidelity.score}/100)
                    </p>
                  ) : null}
                  {checkpoint.resume_reason ? (
                    <p className="mt-1 text-xs text-slate-400">{checkpoint.resume_reason}</p>
                  ) : null}
                </button>
              ))}
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-slate-800 px-6 py-4">
              <button
                onClick={() => setCheckpointActionIntent(null)}
                className="rounded-lg px-3 py-2 text-sm text-slate-300 transition-colors hover:bg-slate-800"
              >
                Cancel
              </button>
              {checkpointActionIntent === 'start' && (
                <button
                  onClick={async () => {
                    setCheckpointActionIntent(null);
                    await handleStartSessionFresh();
                  }}
                  className="rounded-lg bg-blue-600 px-3 py-2 text-sm text-white transition-colors hover:bg-blue-500"
                >
                  Start Fresh Instead
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {showAgentInterventionModal && pendingAgentInterventions.length > 0 && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur-sm">
          <div className="w-full max-w-3xl rounded-2xl border border-amber-700/50 bg-slate-900 shadow-2xl">
            <div className="flex items-start justify-between gap-4 border-b border-slate-800 px-6 py-4">
              <div>
                <p className="text-sm font-semibold text-amber-300">OpenClaw Needs Your Input</p>
                <p className="mt-1 text-sm text-slate-300">
                  OpenClaw paused execution and is waiting for your confirmation before it continues.
                </p>
              </div>
              <button
                onClick={() => setShowAgentInterventionModal(false)}
                className="rounded-md px-2 py-1 text-sm text-slate-400 transition-colors hover:bg-slate-800 hover:text-slate-200"
              >
                Later
              </button>
            </div>
            <div className="max-h-[70vh] overflow-y-auto px-6 py-5">
              <div className="mb-4 rounded-xl border border-amber-700/40 bg-amber-950/30 p-4">
                <p className="text-sm font-medium text-amber-200">
                  What this looks like now
                </p>
                <p className="mt-1 text-sm text-slate-300">
                  OpenClaw will stop, open this panel, and ask in chat form. You can approve, deny, or reply with guidance, then the run continues from the paused step.
                </p>
              </div>
              {agentInterventionTimeline.length > 0 && (
                <div className="mb-4 rounded-xl border border-slate-800 bg-slate-950/70 p-4">
                  <p className="text-sm font-medium text-slate-100">Recent intervention timeline</p>
                  <div className="mt-3 space-y-3">
                    {agentInterventionTimeline.map((item) => {
                      const actionLabel =
                        item.status === 'pending'
                          ? 'OpenClaw asked'
                          : item.status === 'approved'
                            ? 'You approved'
                            : item.status === 'denied'
                              ? 'You denied'
                              : item.status === 'replied'
                                ? 'You replied'
                                : item.status;

                      const statusTone =
                        item.status === 'approved'
                          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                          : item.status === 'denied'
                            ? 'border-red-500/30 bg-red-500/10 text-red-300'
                            : item.status === 'replied'
                              ? 'border-blue-500/30 bg-blue-500/10 text-blue-300'
                              : 'border-amber-500/30 bg-amber-500/10 text-amber-300';

                      const happenedAt = item.replied_at || item.created_at;

                      return (
                        <div key={item.id} className="flex gap-3">
                          <div className="flex flex-col items-center">
                            <div className="mt-1 h-2.5 w-2.5 rounded-full bg-amber-400" />
                            <div className="mt-1 h-full min-h-6 w-px bg-slate-800 last:hidden" />
                          </div>
                          <div className="flex-1 rounded-lg border border-slate-800 bg-slate-900/80 p-3">
                            <div className="flex items-center justify-between gap-3">
                              <p className="text-sm font-medium text-slate-100">{actionLabel}</p>
                              <span className={`rounded-full border px-2 py-0.5 text-[11px] uppercase tracking-wide ${statusTone}`}>
                                {item.status}
                              </span>
                            </div>
                            <p className="mt-1 text-sm text-slate-300 line-clamp-2">
                              {item.prompt}
                            </p>
                            <p className="mt-2 text-xs text-slate-500">
                              {formatDateTime(happenedAt)}
                              {item.status !== 'pending' && session.status === 'running'
                                ? ' • session resumed'
                                : ''}
                            </p>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
              <HumanInterventionPanel
                interventions={pendingAgentInterventions}
                onApprove={handleApproveIntervention}
                onDeny={handleDenyIntervention}
                onReply={handleSubmitReply}
                variant="chat"
              />
            </div>
          </div>
        </div>
      )}

      <SessionHeader
        project={project}
        session={session}
        wsConnected={wsConnected}
        actionButtons={getActionButtons()}
      />

      <SessionConnectionNotice
        checkpointCount={checkpointCount}
        session={session}
        wsConnected={wsConnected}
      />

      {renderOperatorGuidanceComposer()}

      {interventionToast && (
        <div className="fixed right-4 top-4 z-50 w-full max-w-md animate-[slideIn_220ms_ease-out]">
          <Alert
            className="border-amber-600/40 bg-amber-950/90 text-amber-100 shadow-2xl backdrop-blur"
            title={interventionToast.title}
            description={interventionToast.message}
          >
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  setShowAgentInterventionModal(true);
                  dismissInterventionToast();
                }}
                className="rounded-md bg-amber-500 px-3 py-1.5 text-xs font-medium text-slate-950 transition-colors hover:bg-amber-400"
              >
                Open request
              </button>
              <button
                onClick={dismissInterventionToast}
                className="rounded-md px-3 py-1.5 text-xs text-amber-200 transition-colors hover:bg-white/10"
              >
                Dismiss
              </button>
            </div>
          </Alert>
        </div>
      )}

      {(session.status === 'awaiting_input' || pendingInterventions.length > 0) && (
        <HumanInterventionPanel
          interventions={interventions}
          onApprove={handleApproveIntervention}
          onDeny={handleDenyIntervention}
          onReply={handleSubmitReply}
        />
      )}

      {TERMINAL_SESSION_STATUSES.has(session.status) && (
        <FailureSummaryPanel
          summary={failureSummary}
          loading={failureSummaryLoading}
          onFeedbackSubmit={handleFeedbackSubmit}
          onReplan={handleReplan}
        />
      )}

      <SessionStats
        formatDateTime={formatDateTime}
        session={session}
        tasksCount={tasks.length}
      />

      <SessionTabs
        activeTab={activeTab}
        onChange={setActiveTab}
        tasksCount={tasks.length}
      />

      <div className="min-h-[400px] min-w-0 overflow-x-hidden">
        {activeTab === 'logs' && (
          <SessionLogsPanel
            displayLogs={displayLogs}
            handleRefreshLogs={handleRefreshLogs}
            logVerbosity={logVerbosity}
            logViewMode={logViewMode}
            onLogVerbosityChange={setLogVerbosity}
            onLogViewModeChange={(mode) => {
              setLogViewMode(mode);
              applyLogView(allLogs, mode);
            }}
            wsConnected={wsConnected}
          />
        )}

        {activeTab === 'timeline' && (
          <div className="space-y-4">
            <SessionTimelinePanel
              decisionEvents={decisionEvents}
              formatDateTime={formatDateTime}
              offTrackMoment={offTrackMoment}
              repairGenealogy={repairGenealogy}
              timelineEvents={timelineEvents}
              timelineSpans={timelineSpans}
            />
            <SessionDiagnosticsPanel
              anomalyEvents={anomalyEvents}
              compareMatches={compareMatches}
              dispatchWatchdog={dispatchWatchdog}
              formatDateTime={formatDateTime}
              healthEvents={healthEvents}
              replayInvestigation={replayInvestigation}
              stateDiff={stateDiff}
            />
            <KnowledgeUsagePanel phases={knowledgeUsage} />
          </div>
        )}

        {activeTab === 'tasks' && (
          <SessionTasksPanel
            actionButtons={getActionButtons()}
            formatDateTime={formatDateTime}
            onExecuteTask={handleExecuteTask}
            onRefreshTasks={refreshTasksForSession}
            session={session}
            tasks={tasks}
          />
        )}

        {activeTab === 'settings' && (
          <SessionSettingsPanel
            checkpointInspection={checkpointInspection}
            checkpoints={checkpoints}
            formatDateTime={formatDateTime}
            onInspectCheckpoint={inspectCheckpoint}
            onModeChange={handleExecutionModeChange}
            onReplayCheckpoint={replayCheckpoint}
            onRefreshTasks={refreshTasksForSession}
            session={session}
          />
        )}
      </div>

    </div>
  );
}
