import { useEffect, useState, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { sessionsAPI, tasksAPI, projectsAPI } from '@/api/client';
import type { Session, Task, Project } from '@/types/api';
import { TerminalViewer } from '@/components/TerminalViewer';
import type { TerminalLogEntry } from '@/components/TerminalViewer';
import { LoadingSpinner, StatusBadge } from '@/components/ui';
import { 
  Play, 
  Pause, 
  Square, 
  RefreshCw, 
  Settings, 
  Terminal as TerminalIcon,
  Activity,
  CheckCircle2,
  XCircle,
  Clock,
  ExternalLink
} from 'lucide-react';
import { cn } from '@/lib/utils';

type TimelineEventType =
  | 'planning'
  | 'executing'
  | 'debugging'
  | 'revising_plan'
  | 'summarizing'
  | 'checkpoint'
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

const NOISY_LOG_PATTERNS = [
  /^"[\w]+":\s?.*$/,
  /^[\[\]{}],?$/,
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
  /^"name":\s*"(healthcheck|memory_get|memory_search|session_status|update_plan|web_search|web_fetch|image|pdf|browser|BOOTSTRAP\.md|MEMORY\.md)".*$/,
  /^"entries":\s*\[$/,
  /^"skills":\s*{$/,
];

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
  const [checkpointCount, setCheckpointCount] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  const parseApiDate = (value?: string | null): Date | null => {
    if (!value) return null;
    const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
    const normalized = hasTimezone ? value : `${value}Z`;
    const parsed = new Date(normalized);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  };

  const formatDateTime = (value?: string | null) => {
    const d = parseApiDate(value);
    if (!d) return value ? 'Invalid date' : 'N/A';
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const hh = String(d.getHours()).padStart(2, '0');
    const min = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${yyyy}${mm}${dd} / ${hh}:${min}:${ss}`;
  };

  const formatLogTimestamp = (value?: string | null) => {
    const parsed = parseApiDate(value);
    if (!parsed) return '';
    const hh = String(parsed.getHours()).padStart(2, '0');
    const mm = String(parsed.getMinutes()).padStart(2, '0');
    const ss = String(parsed.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss}`;
  };

  const toTerminalLogEntry = (log: SessionLogItem): TerminalLogEntry => ({
    message: log.message,
    timestamp: formatLogTimestamp(log.timestamp || log.created_at),
  });

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

  const shouldDisplayLog = (log: SessionLogItem) =>
    logVerbosity === 'verbose' || !isNoisyLogMessage(log.message);

  const visibleLogs = (logs: SessionLogItem[]) => logs.filter(shouldDisplayLog);

  const applyLogView = (sourceLogs: TerminalLogEntry[], mode: string) => {
    let result = [...sourceLogs];
    if (mode === 'newest') {
      result = result.slice().reverse();
    } else if (mode === 'success') {
      result = result.filter((log) => log.message.includes('✓') || log.message.includes('success') || log.message.includes('Success'));
    } else if (mode === 'errors') {
      result = result.filter((log) => log.message.includes('✗') || log.message.includes('error') || log.message.includes('Error') || log.message.includes('failed'));
    }
    setDisplayLogs(result);
  };

  const classifyTimelineEvent = (message: string, level?: string, at?: string): TimelineEvent => {
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
  };

  const pushTimelineEvent = (message: string, level?: string, at?: string) => {
    const event = classifyTimelineEvent(message, level, at);
    setTimelineEvents(prev => [...prev.slice(-99), event]);
  };

  const loadCheckpointCount = async (id: number) => {
    try {
      const checkpointsRes = await sessionsAPI.listCheckpoints(id);
      setCheckpointCount(checkpointsRes.data.total_count || 0);
    } catch {
      setCheckpointCount(0);
    }
  };

  useEffect(() => {
    if (!sessionId) {
      setError('Session ID not found');
      setLoading(false);
      return;
    }

    const loadSessionData = async () => {
      try {
        const sessionRes = await sessionsAPI.getById(Number(sessionId));
        const tasksRes = await tasksAPI.getByProject(sessionRes.data.project_id || 0);
        const projectRes = await projectsAPI.getById(sessionRes.data.project_id || 0);
        
        setSession(sessionRes.data);
        setTasks(tasksRes.data || []);
        setProject(projectRes.data);
        await loadCheckpointCount(Number(sessionId));
        
        // Only connect WebSocket if session is running or paused
        if (sessionRes.data.status === 'running' || sessionRes.data.status === 'paused') {
          setupWebSocket(sessionRes.data.id);
        } else {
          console.log(`Session is ${sessionRes.data.status}, not connecting WebSocket yet`);
        }
      } catch (err) {
        console.error('Failed to load session:', err);
        setError(err instanceof Error ? err.message : 'Failed to load session');
      } finally {
        setLoading(false);
      }
    };

    // Load logs on initial load (after session data is loaded)
    const loadLogs = async () => {
      if (!sessionId) return;
      try {
        const response = await sessionsAPI.getLogs(Number(sessionId));
        const loadedLogs = visibleLogs((response.data?.logs || []) as SessionLogItem[]);
        console.log(`Loaded ${loadedLogs.length} visible logs for session ${sessionId}`);
        const terminalLogs = loadedLogs.map(toTerminalLogEntry);
        setAllLogs(terminalLogs);
        applyLogView(terminalLogs, logViewMode);
        setTimelineEvents(
          loadedLogs
            .slice(-50)
            .map((log) => classifyTimelineEvent(log.message, log.level, log.timestamp || log.created_at))
        );
      } catch (err) {
        console.error('Failed to load logs:', err);
      }
    };

    // Load session data and logs
    loadSessionData().then(() => {
      loadLogs();
    });

    // Poll for status updates every 5 seconds
    const statusPollInterval = setInterval(async () => {
      if (!sessionId) return;
      try {
        const currentSession = await sessionsAPI.getById(Number(sessionId));
        const currentStatus = currentSession.data.status;
        
        // If session changed to running/paused, connect WebSocket
        if ((currentStatus === 'running' || currentStatus === 'paused') && !wsRef.current) {
          console.log(`Session is now ${currentStatus}, connecting WebSocket...`);
          setupWebSocket(Number(sessionId));
        }
        
        // Update session state if changed
        if (session?.status !== currentStatus) {
          setSession(currentSession.data);
          if (currentStatus === 'stopped' || currentStatus === 'paused') {
            await loadCheckpointCount(Number(sessionId));
          }
        }
      } catch (err) {
        console.warn('Status poll error:', err);
      }
    }, 5000);

    // Cleanup WebSocket and interval on unmount
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      clearInterval(statusPollInterval);
    };
  }, [sessionId, logVerbosity]);

  const setupWebSocket = (session_id: number) => {
    // Only connect if we have a token
    const token = localStorage.getItem('access_token');
    if (!token) {
      console.warn('No access token found, cannot connect WebSocket');
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
        // Check if response is empty
        if (!event.data || event.data.length === 0) {
          return;
        }
        
        // Check if it looks like HTML (error page)
        if (event.data.trim().startsWith('<')) {
          console.error('WebSocket received HTML instead of JSON:', event.data.substring(0, 100));
          console.error('This usually means the WebSocket is connecting to the wrong port. Backend should be at :8080, not :3000');
          return;
        }
        
        // Check if it's a plain text message (like "ping")
        if (event.data === 'ping' || event.data === 'pong') {
          console.debug('Received plain text message:', event.data);
          if (event.data === 'ping') {
            wsRef.current?.send('pong');
          }
          return;
        }
        
        try {
          const data = JSON.parse(event.data);
          
          // Handle different message types
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
            pushTimelineEvent(data.message, data.level, data.timestamp);
          } else if (data.type === 'ping') {
            console.debug('Received ping, sending pong');
            // Send pong in response
            wsRef.current?.send(JSON.stringify({ type: 'pong' }));
          } else if (data.type === 'pong') {
            console.debug('Received pong');
          } else if (data.type === 'connected') {
            console.log('✅ WebSocket connected message received');
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
        console.log('WebSocket closed, reconnecting...');
        setWsConnected(false);
        wsRef.current = null;
        // Attempt reconnection after 3 seconds
        reconnectTimeoutRef.current = setTimeout(() => {
          setupWebSocket(session_id);
        }, 3000);
      };
    } catch (error) {
      console.error('Failed to create WebSocket:', error);
      setWsConnected(false);
    }
  };

  const handleStartSession = async () => {
    if (!session || !sessionId) {
      console.error('Cannot start: session or sessionId missing');
      alert('Session not loaded properly');
      return;
    }
    
    console.log(`Starting session ${sessionId}...`);
    pushTimelineEvent(`Start requested for session ${sessionId}`, 'INFO');
    try {
      const response = await sessionsAPI.start(Number(sessionId));
      console.log('Start API response:', response);
      const updated = await sessionsAPI.getById(Number(sessionId));
      console.log('Updated session:', updated.data);
      setSession(updated.data);
      if (updated.data.status === 'running' || updated.data.status === 'paused') {
        if (reconnectTimeoutRef.current) {
          clearTimeout(reconnectTimeoutRef.current);
        }
        if (!wsRef.current) {
          setupWebSocket(Number(sessionId));
        }
      }
      pushTimelineEvent(`Session started with status: ${updated.data.status}`, 'INFO');
      alert(`Session ${session.name} started successfully!`);
    } catch (error: any) {
      console.error('Failed to start session:', error);
      console.error('Error details:', error.response?.data || error.message);
      const errorMsg = error.response?.data?.detail || error.message || 'Unknown error';
      pushTimelineEvent(`Session start failed: ${errorMsg}`, 'ERROR');
      alert(`Failed to start session: ${errorMsg}`);
    }
  };

  const handleStopSession = async (force: boolean = false) => {
    if (!session || !sessionId) return;
    try {
      await sessionsAPI.stop(Number(sessionId), force);
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      await loadCheckpointCount(Number(sessionId));
    } catch (error) {
      console.error('Failed to stop session:', error);
      alert('Failed to stop session');
    }
  };

  const handlePauseSession = async () => {
    if (!session || !sessionId) return;
    try {
      await sessionsAPI.pause(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      await loadCheckpointCount(Number(sessionId));
    } catch (error) {
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
    } catch (error: any) {
      console.error('Failed to refresh logs:', error);
      const errorMsg = error.response?.data?.detail || error.message || 'Unknown error';
      alert(`Failed to refresh logs: ${errorMsg}`);
    }
  };

  const handleResumeSession = async () => {
    if (!session || !sessionId) return;
    try {
      await sessionsAPI.resume(Number(sessionId));
      const updated = await sessionsAPI.getById(Number(sessionId));
      setSession(updated.data);
      await loadCheckpointCount(Number(sessionId));
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (!wsRef.current && (updated.data.status === 'running' || updated.data.status === 'paused')) {
        setupWebSocket(Number(sessionId));
      }
    } catch (error) {
      console.error('Failed to resume session:', error);
      alert('Failed to resume session');
    }
  };

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
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3 mb-2">
            <h1 className="text-2xl font-bold text-slate-100">{session.name}</h1>
            <StatusBadge status={session.status} />
            {wsConnected && (
              <div className="flex items-center gap-1 text-emerald-400 text-sm">
                <Activity className="h-4 w-4 animate-pulse" />
                <span>Live</span>
              </div>
            )}
          </div>
          <p className="text-slate-400 text-sm">
            ID: {session.id} • Project: {project?.name || 'Unknown'}
            {project?.github_url && (
              <a
                href={project.github_url}
                target="_blank"
                rel="noopener noreferrer"
                className="ml-2 text-primary-400 hover:text-primary-300"
              >
                <ExternalLink className="h-4 w-4 inline" />
              </a>
            )}
          </p>
        </div>
        {getActionButtons()}
      </div>

      {/* Connection Status */}
      <div className="flex items-center gap-2 text-sm">
        <div className={cn(
          'w-2 h-2 rounded-full',
          wsConnected ? 'bg-emerald-500 animate-pulse' : 'bg-slate-500'
        )} />
        <span className={cn(
          wsConnected ? 'text-emerald-400' : 'text-slate-500'
        )}>
          {wsConnected ? 'WebSocket Connected - Live Logs' : 'WebSocket Disconnected'}
        </span>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
          <p className="text-slate-400 text-sm mb-1 flex items-center gap-2">
            <Activity className="h-4 w-4" />
            Status
          </p>
          <p className="text-white font-semibold capitalize">{session.status}</p>
        </div>
        <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
          <p className="text-slate-400 text-sm mb-1 flex items-center gap-2">
            <TerminalIcon className="h-4 w-4" />
            Tasks
          </p>
          <p className="text-white font-semibold">{tasks.length}</p>
        </div>
        <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
          <p className="text-slate-400 text-sm mb-1 flex items-center gap-2">
            <Clock className="h-4 w-4" />
            Created
          </p>
          <p className="text-white font-semibold">
            {formatDateTime(session.created_at)}
          </p>
        </div>
        {session.started_at && (
          <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
            <p className="text-slate-400 text-sm mb-1 flex items-center gap-2">
              <CheckCircle2 className="h-4 w-4" />
              Started
            </p>
            <p className="text-white font-semibold">
              {formatDateTime(session.started_at)}
            </p>
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="flex gap-4">
          <button
            onClick={() => setActiveTab('logs')}
            className={cn(
              'pb-2 px-2 text-sm font-medium flex items-center gap-2',
              activeTab === 'logs'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-slate-400 hover:text-slate-200'
            )}
          >
            <TerminalIcon className="h-4 w-4" />
            Logs
          </button>
          <button
            onClick={() => setActiveTab('tasks')}
            className={cn(
              'pb-2 px-2 text-sm font-medium',
              activeTab === 'tasks'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-slate-400 hover:text-slate-200'
            )}
          >
            Tasks ({tasks.length})
          </button>
          <button
            onClick={() => setActiveTab('settings')}
            className={cn(
              'pb-2 px-2 text-sm font-medium flex items-center gap-2',
              activeTab === 'settings'
                ? 'text-blue-400 border-b-2 border-blue-400'
                : 'text-slate-400 hover:text-slate-200'
            )}
          >
            <Settings className="h-4 w-4" />
            Settings
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      <div className="min-h-[400px]">
        {activeTab === 'logs' && (
          <div className="space-y-4">
            <TerminalViewer
              logs={displayLogs}
              autoScroll={true}
              className="bg-slate-900 h-[500px]"
            />
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm">
                <div className={cn(
                  'w-2 h-2 rounded-full',
                  wsConnected ? 'bg-emerald-500 animate-pulse' : 'bg-slate-500'
                )} />
                <span className={cn(wsConnected ? 'text-emerald-400' : 'text-slate-500')}>
                  {displayLogs.length} logs loaded
                </span>
              </div>
              <div className="flex gap-2 items-center">
                <select
                  value={logVerbosity}
                  onChange={(e) => {
                    const mode = e.target.value as 'clean' | 'verbose';
                    setLogVerbosity(mode);
                  }}
                  className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-white text-sm rounded-lg transition-colors"
                >
                  <option value="clean">Clean Logs</option>
                  <option value="verbose">Verbose Logs</option>
                </select>
                <button
                  onClick={handleRefreshLogs}
                  className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-white text-sm rounded-lg transition-colors flex items-center gap-2"
                >
                  <RefreshCw className="h-4 w-4" />
                  Refresh
                </button>
                <select
                  value={logViewMode}
                  onChange={(e) => {
                    const mode = e.target.value as 'newest' | 'oldest' | 'success' | 'errors' | 'all';
                    setLogViewMode(mode);
                    applyLogView(allLogs, mode);
                  }}
                  className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-white text-sm rounded-lg transition-colors"
                >
                  <option value="newest">Sort: Newest First</option>
                  <option value="oldest">Sort: Oldest First</option>
                  <option value="success">Filter: Success Only</option>
                  <option value="errors">Filter: Errors Only</option>
                  <option value="all">Show All</option>
                </select>
              </div>
            </div>

            <div className="bg-slate-900 border border-slate-700 rounded-lg p-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-slate-200">Execution Timeline</h3>
                <span className="text-xs text-slate-400">{timelineEvents.length} events</span>
              </div>
              <div className="max-h-56 overflow-y-auto space-y-2 text-sm">
                {timelineEvents.length === 0 ? (
                  <p className="text-slate-500">No timeline events yet. Start/execute a task to see progress milestones.</p>
                ) : (
                  timelineEvents.slice().reverse().map((event) => (
                    <div key={event.id} className="border border-slate-800 rounded-md p-2">
                      <div className="flex items-center justify-between">
                        <span className={cn(
                          'text-xs font-medium uppercase',
                          event.type === 'error' && 'text-red-400',
                          event.type === 'planning' && 'text-violet-400',
                          event.type === 'executing' && 'text-blue-400',
                          event.type === 'debugging' && 'text-amber-400',
                          event.type === 'revising_plan' && 'text-fuchsia-400',
                          event.type === 'summarizing' && 'text-teal-400',
                          event.type === 'checkpoint' && 'text-emerald-400',
                          event.type === 'status' && 'text-cyan-400',
                          event.type === 'info' && 'text-slate-300'
                        )}>
                          {event.title}
                        </span>
                        <span className="text-xs text-slate-500">
                          {formatDateTime(event.at)}
                        </span>
                      </div>
                      <p className="text-slate-300 mt-1 break-words">{event.detail}</p>
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}

        {activeTab === 'tasks' && (
          <div className="space-y-4">
            {/* Execute Task Button */}
            {getActionButtons() && session.status !== 'running' && (
              <div className="bg-blue-900/20 border border-blue-700/50 rounded-lg p-4 mb-4">
                <p className="text-blue-400 text-sm mb-2">
                  Session is not running. Start the session to execute tasks automatically.
                </p>
              </div>
            )}
            
            {tasks.length === 0 ? (
              <div className="text-center py-12">
                <TerminalIcon className="h-12 w-12 text-slate-500 mx-auto mb-4" />
                <p className="text-slate-400">No tasks yet</p>
                {getActionButtons() && (
                  <p className="text-slate-500 text-sm mt-2">
                    Start the session to automatically execute tasks from your project
                  </p>
                )}
              </div>
            ) : (
              tasks.map((task) => (
                <div key={task.id} className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700 hover:border-slate-600 transition-colors">
                  <div className="flex items-start justify-between mb-2">
                    <h3 className="font-semibold text-white">{task.title}</h3>
                    <StatusBadge status={task.status} size="sm" />
                  </div>
                  {task.description && (
                    <p className="text-slate-400 text-sm mt-1">{task.description}</p>
                  )}
                  {task.error_message && (
                    <div className="mt-2 p-2 bg-red-900/20 border border-red-700/50 rounded-lg">
                      <p className="text-red-400 text-sm flex items-center gap-2">
                        <XCircle className="h-4 w-4" />
                        Error: {task.error_message}
                      </p>
                    </div>
                  )}
                  <div className="mt-3 flex items-center gap-4 text-xs text-slate-500">
                    {task.created_at && (
                      <span>Created: {formatDateTime(task.created_at)}</span>
                    )}
                    {task.started_at && (
                      <span>Started: {formatDateTime(task.started_at)}</span>
                    )}
                    {task.completed_at && (
                      <span className="text-emerald-400">
                        Completed: {formatDateTime(task.completed_at)}
                      </span>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        {activeTab === 'settings' && (
          <div className="space-y-4">
            <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
              <p className="text-slate-400 text-sm mb-1">Session ID</p>
              <p className="text-white font-mono text-sm">{session.id}</p>
            </div>
            <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
              <p className="text-slate-400 text-sm mb-1">Project ID</p>
              <p className="text-white font-mono text-sm">{session.project_id}</p>
            </div>
            <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
              <p className="text-slate-400 text-sm mb-1">Created At</p>
              <p className="text-white">{formatDateTime(session.created_at)}</p>
            </div>
            {session.started_at && (
              <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
                <p className="text-slate-400 text-sm mb-1">Started At</p>
                <p className="text-white">{formatDateTime(session.started_at)}</p>
              </div>
            )}
            {session.stopped_at && (
              <div className="bg-slate-800/50 backdrop-blur rounded-xl p-4 border border-slate-700">
                <p className="text-slate-400 text-sm mb-1">Stopped At</p>
                <p className="text-white">{formatDateTime(session.stopped_at)}</p>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
