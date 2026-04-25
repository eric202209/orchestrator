import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { sessionsAPI, tasksAPI, projectsAPI } from '@/api/client';
import type {
  Checkpoint,
  CheckpointInspection,
  OrchestrationEvent,
  Project,
  Session,
  SessionDivergenceCompareResponse,
  SessionStateDiffResponse,
  Task,
} from '@/types/api';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { LoadingSpinner } from '@/components/ui';
import {
  SessionConnectionNotice,
  SessionHeader,
  SessionLogsPanel,
  SessionSettingsPanel,
  SessionStats,
  SessionTabs,
  SessionTasksPanel,
  type TimelineSpan,
} from '@/components/SessionDetailSections';
import { Pause, Play, Square, XCircle } from 'lucide-react';

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

const NOISY_LOG_PATTERNS = [
  /^"[\w]+":\s?.*$/,
  /^[[\]{}],?$/,
  /^"propertiesCount":\s*\d+,?$/,
  /^"schemaChars":\s*\d+,?$/,
  /^"summaryChars":\s*\d+,?$/,
  /^"promptChars":\s*\d+,?$/,
  /^"blockChars":\s*\d+,?$/,
  /^"rawChars":\s*\d+,?$/,
  /^"injectedChars":\s*\d+,?$/,
  /^"truncated":\s*(true|false),?$/,
  /^"missing":\s*(true|false),?$/,
  /^"path":\s*".*",?$/,
  /^"name":\s*"[^"]+",?$/,
  /^"name":\s*"(healthcheck|memory_get|memory_search|session_status|update_plan|web_search|web_fetch|image|pdf|browser|BOOTSTRAP\.md|MEMORY\.md)".*$/,
  /^"entries":\s*\[$/,
  /^"skills":\s*{$/,
];

const MAX_TIMELINE_EVENTS = 150;

export default function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<Session | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [displayLogs, setDisplayLogs] = useState<TerminalLogEntry[]>([]);
  const [activeTab, setActiveTab] = useState<'logs' | 'tasks' | 'settings'>('logs');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [allLogs, setAllLogs] = useState<TerminalLogEntry[]>([]);
  const [logViewMode, setLogViewMode] = useState<'newest' | 'oldest' | 'success' | 'errors' | 'all'>('newest');
  const [logVerbosity, setLogVerbosity] = useState<'clean' | 'verbose'>('clean');
  const [timelineEvents, setTimelineEvents] = useState<TimelineEvent[]>([]);
  const [orchestrationEvents, setOrchestrationEvents] = useState<OrchestrationEvent[]>([]);
  const [checkpointCount, setCheckpointCount] = useState(0);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [checkpointInspection, setCheckpointInspection] = useState<CheckpointInspection | null>(null);
  const [compareMatches, setCompareMatches] = useState<SessionDivergenceCompareResponse | null>(null);
  const [stateDiff, setStateDiff] = useState<SessionStateDiffResponse | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shouldReconnectRef = useRef(true);
  const diffAvailableRef = useRef<boolean | null>(null); // null=unknown, true=has data, false=no snapshots
  const tasksRef = useRef<Task[]>([]);
  useEffect(() => { tasksRef.current = tasks; }, [tasks]);
  const seenOrchestrationTimelineKeysRef = useRef<Set<string>>(new Set());

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
    message: log.message,
    timestamp: formatLogTimestamp(log.timestamp || log.created_at),
  }), [formatLogTimestamp]);

  const isNoisyLogMessage = (message?: string | null) => {
    const trimmed = (message || '').trim();
    if (!trimmed) return true;

    if (
      trimmed.includes('"propertiesCount"') ||
      trimmed.includes('"schemaChars"') ||
      trimmed.includes('"summaryChars"') ||
      trimmed.includes('"promptChars"') ||
      trimmed.includes('"blockChars"') ||
      trimmed.includes('"rawChars"') ||
      trimmed.includes('"injectedChars"')
    ) {
      return true;
    }

    return NOISY_LOG_PATTERNS.some((pattern) => pattern.test(trimmed));
  };

  const shouldDisplayLog = useCallback(
    (log: SessionLogItem) => logVerbosity === 'verbose' || !isNoisyLogMessage(log.message),
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
        detail = messageText || 'A retry/debug cycle started';
        break;
      case 'plan_revised':
        type = 'revising_plan';
        title = 'Plan Revised';
        detail = messageText || 'The orchestration plan was revised';
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
        type = 'checkpoint';
        title = humanizeToken(event.event_type);
        detail = messageText || reasonsText || title;
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
      diffAvailableRef.current = true;
      setStateDiff(response.data);
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
          'checkpoint_saved',
          'checkpoint_loaded',
          'checkpoint_redirected',
          'workspace_restore_skipped',
          'workspace_preserved',
          'resume_workspace_drift',
        ].includes(eventType)
      ) {
        return 'workspace';
      }
      if (
        [
          'validation_result',
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
    } catch {
      setCheckpointCount(0);
      setCheckpoints([]);
    }
  }, []);

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

  const setupWebSocket = useCallback((session_id: number) => {
    const token = localStorage.getItem('access_token');

    if (!token) {
      console.warn('No access token found, cannot connect WebSocket');
      return;
    }

    if (wsRef.current) {
      return;
    }

    try {
      wsRef.current = sessionsAPI.getLogsStream(session_id);
      console.log('Attempting WebSocket connection:', wsRef.current.url);

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
            if (logVerbosity === 'clean' && isNoisyLogMessage(data.message)) {
              return;
            }
            console.log('✅ Received log message:', data.message);
            setAllLogs(prev => {
              const next = [
                ...prev.slice(-499),
                {
                  message: data.message,
                  timestamp: formatLogTimestamp(data.timestamp),
                },
              ];
              applyLogView(next, logViewMode);
              return next;
            });
          } else if (data.type === 'orchestration_event') {
            appendOrchestrationTimelineEvent(data as OrchestrationEvent);
            if (
              sessionId &&
              ['phase_finished', 'checkpoint_saved', 'retry_entered', 'tool_failed', 'validation_result'].includes(
                String(data.event_type || '')
              )
            ) {
              void loadStateDiff(Number(sessionId), tasksRef.current);
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
    } catch (error) {
      console.error('Failed to create WebSocket:', error);
      setWsConnected(false);
    }
  }, [appendOrchestrationTimelineEvent, applyLogView, formatLogTimestamp, loadStateDiff, logVerbosity, logViewMode, sessionId]);

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
        if (sessionRes.data.status === 'running') {
          await loadStateDiff(sessionRes.data.id, tasksRes.data || []);
        }

        if (abortController.signal.aborted) return;

        // Only connect WebSocket if session is running
        if (sessionRes.data.status === 'running') {
          scheduleWebSocketConnect(sessionRes.data.id);
        } else {
          console.log(`Session is ${sessionRes.data.status}, not connecting WebSocket yet`);
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
        setSession(currentSession.data);

        if (currentSession.data.project_id) {
          const currentTasks = await tasksAPI.getByProject(currentSession.data.project_id);
          setTasks(currentTasks.data || []);
          if (currentStatus === 'running') {
            await loadStateDiff(Number(sessionId), currentTasks.data || []);
          }
        }

        // If session changed to running, connect WebSocket
        if (currentStatus === 'running' && !wsRef.current) {
          console.log(`Session is now ${currentStatus}, connecting WebSocket...`);
          scheduleWebSocketConnect(Number(sessionId), 1000);
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
  }, [applyLogView, loadCheckpointCount, loadStateDiff, loadTimelineEvents, logVerbosity, logViewMode, scheduleWebSocketConnect, sessionId, toTerminalLogEntry, visibleLogs]);

  const handleStartSession = async () => {
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

  const handleResumeSession = async () => {
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

  const getActionButtons = () => {
    if (!session) return null;

    switch (session.status) {
      case 'running':
        return (
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

      <div className="min-h-[400px]">
        {activeTab === 'logs' && (
          <SessionLogsPanel
            anomalyEvents={anomalyEvents}
            compareMatches={compareMatches}
            displayLogs={displayLogs}
            formatDateTime={formatDateTime}
            healthEvents={healthEvents}
            handleRefreshLogs={handleRefreshLogs}
            logVerbosity={logVerbosity}
            logViewMode={logViewMode}
            onLogVerbosityChange={setLogVerbosity}
            onLogViewModeChange={(mode) => {
              setLogViewMode(mode);
              applyLogView(allLogs, mode);
            }}
            stateDiff={stateDiff}
            timelineEvents={timelineEvents}
            timelineSpans={timelineSpans}
            wsConnected={wsConnected}
          />
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
