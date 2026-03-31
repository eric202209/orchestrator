import { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, Link } from 'react-router-dom';
import { sessionsAPI, tasksAPI, projectsAPI } from '../api/client';
import type { Session, LogEntry, Task, Project, OverwriteCheckResult, Checkpoint } from '../types/api';
import { 
  ArrowLeft, 
  Play, 
  Pause, 
  Square, 
  Activity, 
  Terminal,
  RefreshCw,
  Trash2,
  ArrowUp,
  ArrowDown,
  Settings,
  XCircle,
  Zap,
  ShieldCheck,
  Clock,
  X
} from 'lucide-react';

function SessionDashboard() {
  const { id } = useParams<{ id: string }>();
  const [session, setSession] = useState<Session | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [statusWs, setStatusWs] = useState<WebSocket | null>(null);
  const [logsWs, setLogsWs] = useState<WebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isLogsConnected, setIsLogsConnected] = useState(false);
  const [inputTask, setInputTask] = useState('');
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  const [executing, setExecuting] = useState(false);
  const [isLoadingTasks, setIsLoadingTasks] = useState(false);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const [showSettings, setShowSettings] = useState(false);
  
  // Track reconnect attempts and timeouts
  const reconnectCountRef = useRef(0);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const isConnectingRef = useRef(false);
  
  // Refs for WebSocket connection functions to avoid forward reference issues
  const connectStatusWebSocketRef = useRef<(() => void) | null>(null);
  const connectLogsWebSocketRef = useRef<(() => void) | null>(null);
  
  // Limit logs in memory to prevent performance issues
  const MAX_LOGS_IN_MEMORY = 500;
  
  // Sorting and filtering state
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('desc'); // Newest first by default
  const [deduplicate, setDeduplicate] = useState(true);
  const [filterLevel, setFilterLevel] = useState<string | undefined>(undefined);
  const [isLoadingLogs, setIsLoadingLogs] = useState(false);
  const [isFetching, setIsFetching] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [projectLoaded, setProjectLoaded] = useState(false);
  const [tasksFetched, setTasksFetched] = useState(false);
  
  // New state for overwrite protection
  const [showOverwriteWarning, setShowOverwriteWarning] = useState<OverwriteCheckResult | null>(null);

  // Checkpoint management state
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [showCheckpointModal, setShowCheckpointModal] = useState(false);

  // Helper function to format dates in local time
  const formatLocalTime = (dateString: string | null) => {
    if (!dateString) return 'N/A';
    // Force UTC parsing by replacing 'T' with ' ' and adding 'Z' if missing
    const cleanDate = dateString.replace('T', ' ').replace(/\.?\d+Z?$/, 'Z');
    const date = new Date(cleanDate);
    return date.toLocaleString();
  };

  // Helper function to check if session is running (includes 'active' status)
  const isSessionRunning = useCallback(() => {
    return session?.status === 'running' || session?.status === 'active';
  }, [session]);

  const fetchProjectTasks = useCallback(async () => {
    if (!project?.id || !projectLoaded || tasksFetched) return;
    setIsLoadingTasks(true);
    try {
      const response = await tasksAPI.getByProject(project.id);
      setTasks(response.data || []);
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    } finally {
      setIsLoadingTasks(false);
      setTasksFetched(true);
    }
  }, [project?.id, projectLoaded, tasksFetched]);

  const fetchProject = useCallback(async () => {
    if (!id || projectLoaded) return;
    try {
      const sessionResponse = await sessionsAPI.getById(Number(id));
      setSession(sessionResponse.data);

      const projectResponse = await projectsAPI.getById(sessionResponse.data.project_id);
      setProject(projectResponse.data);
      setProjectLoaded(true);
    } catch (error) {
      console.error('Failed to fetch project:', error);
    }
  }, [id, projectLoaded]);

  const loadCheckpoints = useCallback(async () => {
    if (!id) return;
    
    try {
      const response = await sessionsAPI.listCheckpoints(Number(id));
      // Ensure we always set an array, even if API returns undefined/null
      setCheckpoints(response?.data?.checkpoints || []);
    } catch (error) {
      console.error('Failed to load checkpoints:', error);
      // On error, keep existing checkpoints or reset to empty array
      setCheckpoints([]);
    }
  }, [id]);

  useEffect(() => {
    fetchProject();
    fetchProjectTasks();
  }, [fetchProject, fetchProjectTasks]);

  useEffect(() => {
    if (!id) return;
    if (session?.status === 'paused' || session?.status === 'stopped') {
      loadCheckpoints();
    }
  }, [id, session?.status, loadCheckpoints]);

  // Fetch logs for the current session only (project-scoped)
  const fetchSessionLogs = useCallback(async () => {
    console.log('fetchSessionLogs called, session id:', id);
    
    // Only fetch if we have a session ID
    if (!id) {
      console.log('No session ID, returning early');
      return;
    }
    
    setIsLoadingLogs(true);
    try {
      // Fetch logs only for this specific session
      const response = await sessionsAPI.getSortedLogs(
        Number(id),
        sortOrder, // sort order
        deduplicate, // deduplicate
        filterLevel, // level filter
        500 // limit
      );
      
      console.log('Session logs response:', response);
      console.log('Response data:', response?.data);
      
      // Axios wraps response in .data property
      const apiResponse = response?.data || response;
      const logsArray = Array.isArray(apiResponse?.logs) ? apiResponse.logs : [];
      
      console.log('logsArray:', logsArray);
      console.log('logsArray length:', logsArray.length);
      
      // Transform logs to use created_at
      const transformedLogs = logsArray.map((log: unknown) => ({
        ...log,
        created_at: (log as { timestamp?: string }).timestamp || log.created_at
      }));
      
      setLogs(transformedLogs);
      console.log('Set logs:', transformedLogs.length, 'for session', id);
      
      // Log that we've loaded session-specific logs
      if (transformedLogs.length > 0) {
        console.log('✅ Session logs loaded - showing ONLY logs from Session', id);
      }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: unknown } } };
      console.error('Failed to fetch session logs:', error);
      console.error('Response structure:', err?.response?.data);
      alert(`Failed to load session logs: ${err.response?.data?.detail || error.message || 'Unknown error'}. Please try again.`);
    } finally {
      setIsLoadingLogs(false);
    }
  }, [id, sortOrder, deduplicate, filterLevel]);

  useEffect(() => {
    if (!id) return;
    fetchSession();
    fetchSessionLogs(); // Load session logs on mount
    
    return () => {
      if (statusWs) statusWs.close();
      if (logsWs) logsWs.close();
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Initialize WebSockets after functions are defined
  useEffect(() => {
    if (!id) return;
    
    // Delay WebSocket connection to ensure functions are available
    const initTimeout = setTimeout(() => {
      console.log('Initializing WebSockets for session:', id);
      connectStatusWebSocket();
      connectLogsWebSocket();
    }, 500);
    
    return () => clearTimeout(initTimeout);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Fetch project when session is loaded (to get project_id for logs)
  useEffect(() => {
    if (session?.project_id && !projectLoaded) {
      const fetchProject = async () => {
        try {
          const projectResponse = await projectsAPI.getById(session.project_id);
          console.log('Fetched project for session:', projectResponse.data);
          if (projectResponse.data) {
            setProject(projectResponse.data);
            setProjectLoaded(true);
          }
        } catch (error) {
          console.error('Failed to fetch project:', error);
        }
      };
      fetchProject();
    }
  }, [session?.project_id, projectLoaded]);

  // Poll for task status updates every 5 seconds
  useEffect(() => {
    if (!id) return;
    
    const pollInterval = setInterval(async () => {
      try {
        const response = await sessionsAPI.getById(Number(id));
        if (response.data) {
          setSession(response.data);
          // Also refresh tasks when session updates
          if (response.data.project_id) {
            await fetchProjectTasks();
          }
        }
      } catch (error) {
        const axiosError = error as { code?: string; message?: string };
        // Only log non-abort errors to avoid spam
        if (axiosError.code !== 'ERR_BAD_RESPONSE' && axiosError.code !== 'ECONNABORTED') {
          console.error('Failed to poll session status:', axiosError.message || error);
        }
      }
    }, 5000); // Poll every 5 seconds
    
    return () => {
      clearInterval(pollInterval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Auto-reconnect logs WebSocket if disconnected
  useEffect(() => {
    if (isLogsConnected) return;
    
    const reconnectTimer = setTimeout(() => {
      console.log('Auto-reconnecting logs WebSocket...');
      connectLogsWebSocketRef.current?.();
    }, 3000);
    
    return () => clearTimeout(reconnectTimer);
  }, [isLogsConnected]);

// Re-fetch logs when sorting/filtering changes (debounced)
  useEffect(() => {
    if (!id || isFetching) return;
    
    setIsFetching(true);
    const timer = setTimeout(() => {
      fetchSessionLogs();
      setIsFetching(false);
    }, 100); // Debounce by 100ms
    
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sortOrder, deduplicate, filterLevel, id, fetchSessionLogs]);

  // Debounced scroll to bottom
  useEffect(() => {
    const timer = setTimeout(() => {
      logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, 50);
    
    return () => clearTimeout(timer);
  }, [logs]);

  // Monitor for stuck tasks (no new logs for 5 minutes)
  useEffect(() => {
    if (!id || !isLogsConnected || !isSessionRunning()) return;

    const checkStuckTask = () => {
      if (logs.length === 0) return;

      const lastLogTime = new Date(logs[logs.length - 1].created_at);
      const now = new Date();
      const minutesSinceLastLog = (now.getTime() - lastLogTime.getTime()) / (1000 * 60);

      console.log(`Minutes since last log: ${minutesSinceLastLog.toFixed(1)}`);

      // Only alert if session has been running for at least 2 minutes
      // This prevents false positives during session startup
      const sessionStarted = session?.started_at ? new Date(session.started_at) : now;
      const sessionUptimeMinutes = (now.getTime() - sessionStarted.getTime()) / (1000 * 60);

      if (sessionUptimeMinutes >= 2 && minutesSinceLastLog >= 5) {
        console.warn('⚠️ Task appears stuck - no new logs for 5+ minutes');
        
        // Show warning as a non-blocking notification instead of alert()
        const notification = document.createElement('div');
        notification.style.cssText = `
          position: fixed;
          top: 20px;
          right: 20px;
          background: #f59e0b;
          color: white;
          padding: 16px 24px;
          border-radius: 8px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.3);
          z-index: 9999;
          font-family: system-ui, -apple-system, sans-serif;
          animation: slideIn 0.3s ease-out;
        `;
        notification.innerHTML = `
          <div style="display: flex; align-items: center; gap: 12px;">
            <span style="font-size: 24px;">⚠️</span>
            <div>
              <div style="font-weight: bold; margin-bottom: 4px;">Task may be stuck</div>
              <div style="font-size: 14px; opacity: 0.9;">No new logs for ${Math.round(minutesSinceLastLog)} minutes</div>
            </div>
          </div>
        `;
        document.body.appendChild(notification);
        
        // Auto-dismiss after 10 seconds
        setTimeout(() => {
          notification.style.animation = 'slideOut 0.3s ease-out';
          setTimeout(() => notification.remove(), 300);
        }, 10000);
        
        // Optionally stop the session
        // handleStop();
      }
    };

    // Check every 30 seconds
    const interval = setInterval(checkStuckTask, 30000);
    
    return () => clearInterval(interval);
  }, [logs, isLogsConnected, session?.started_at, id, isSessionRunning]);

  const fetchSession = async () => {
    if (!id) return;
    try {
      const response = await sessionsAPI.getById(Number(id));
      console.log('Session response:', response);
      console.log('Session data:', response.data);
      setSession(response.data);
      setError(null);
    } catch (error) {
      console.error('Failed to fetch session:', error);
      setError(error instanceof Error ? error.message : 'Failed to load session');
    } finally {
      setLoading(false);
    }
  };

  const connectStatusWebSocket = () => {
    if (!id || isConnectingRef.current) return;
    
    isConnectingRef.current = true;
    const webSocket = sessionsAPI.getStatusStream(Number(id));
    
    webSocket.onopen = () => {
      setIsConnected(true);
      isConnectingRef.current = false;
      console.log('Connected to session status stream');
    };

    webSocket.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'status_update') {

        setSession(prev => prev ? { ...prev, ...data.status } : null);
      }
    };

    webSocket.onerror = (error) => {
      console.error('Status WebSocket error:', error);
      setIsConnected(false);
    };

    webSocket.onclose = () => {
      setIsConnected(false);
      // Only reconnect if we're still on the page
      // Use exponential backoff
      const reconnectDelay = Math.min(3000 * Math.pow(1.5, reconnectCountRef.current), 30000);
      console.log(`Status WebSocket closed, reconnecting in ${reconnectDelay}ms...`);
      
      // Track reconnect attempts
      if (!reconnectCountRef.current) {
        reconnectCountRef.current = 0;
      }
      reconnectCountRef.current++;
      
      reconnectTimeoutRef.current = setTimeout(() => {
        if (document.visibilityState === 'visible') {
          connectStatusWebSocketRef.current?.();
        }
      }, reconnectDelay);
    };

    setStatusWs(webSocket);
  };

  // Assign to refs after definition for use in useEffects
  connectStatusWebSocketRef.current = connectStatusWebSocket;

  const connectLogsWebSocket = useCallback(() => {
    console.log('=== connectLogsWebSocket START ===');
    console.log('Session ID:', id);
    console.log('Project:', project?.id);
    console.log('isConnectingRef.current:', isConnectingRef.current);
    console.log('isLogsConnected:', isLogsConnected);
    
    if (!id) {
      console.log('❌ No session ID, cannot connect');
      return;
    }
    
    if (isLogsConnected) {
      console.log('Already connected, skipping');
      return;
    }
    
    if (isConnectingRef.current) {
      console.log('Already connecting, waiting...');
      return;
    }
    
    console.log('✅ Connecting to logs WebSocket...');
    isConnectingRef.current = true;
    // Use session-specific WebSocket endpoint instead of project-level
    const webSocket = sessionsAPI.getLogsStream(Number(id));
    console.log('WebSocket URL:', webSocket.url);
    
    webSocket.onopen = () => {
      console.log('✅ Logs WebSocket opened!');
      setIsLogsConnected(true);
      isConnectingRef.current = false;
      console.log('✅ Connected to logs stream ✅');
    };

    webSocket.onmessage = (event) => {
      // Handle ping/pong heartbeat messages from backend
      if (event.data === 'ping' || event.data === 'pong') {
        return; // Skip heartbeat messages
      }
      
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'log') {
          const logEntry = {
            ...data,
            created_at: data.timestamp || new Date().toISOString()
          };
          setLogs(prev => {
            const updated = [...prev, logEntry];
            // Limit logs in memory to prevent performance issues
            if (updated.length > MAX_LOGS_IN_MEMORY) {
              return updated.slice(-MAX_LOGS_IN_MEMORY);
            }
            return updated;
          });
        } else if (data.type === 'connected') {
          console.log('Logs WebSocket connected, receiving recent logs...');
        }
      } catch (error) {
        // Log parsing error - might be connection issue or invalid data
        console.error('Failed to parse WebSocket message:', error, 'Data:', event.data);
      }
    };

    webSocket.onerror = (error) => {
      console.error('Logs WebSocket error:', error);
      console.error('WebSocket readyState:', webSocket.readyState);
      setIsLogsConnected(false);
      isConnectingRef.current = false;
    };

    webSocket.onclose = (event) => {
      console.log('Logs WebSocket closed, code:', event.code, 'reason:', event.reason);
      console.log('Logs WebSocket readyState before close:', webSocket?.readyState);
      setIsLogsConnected(false);
      isConnectingRef.current = false;
      
      // Don't auto-reconnect if the session is stopped/completed
      // Let the polling handle status updates
      if (session?.status !== 'stopped' && session?.status !== 'completed') {
        console.log('Reconnecting logs WebSocket...');
        setTimeout(() => connectLogsWebSocketRef.current?.(), 3000);
      } else {
        console.log('Session stopped/completed, logs stream ended (polling will continue)');
        // Polling will continue to check for task status updates
      }
    };

    setLogsWs(webSocket);
  }, [id, project?.id, session?.status, isLogsConnected]);

  // Assign to refs after definition for use in useEffects
  connectLogsWebSocketRef.current = connectLogsWebSocket;

  const handleRefreshLogs = () => {
    fetchSessionLogs();
  };

  const handleRefreshWebSocket = async () => {
    if (isRefreshing) return; // Prevent multiple simultaneous refreshes
    
    setIsRefreshing(true);
    try {
      if (statusWs) {
        statusWs.close();
      }
      if (logsWs) {
        logsWs.close();
      }
      
      // Reconnect WebSockets
      connectStatusWebSocketRef.current?.();
      connectLogsWebSocketRef.current?.();
      
      // Also fetch fresh data from API
      await fetchSession();
      await fetchSessionLogs();
      
      // Show success feedback
      console.log('Session data refreshed successfully');
    } catch (error) {
      console.error('Failed to refresh session data:', error);
      alert('Failed to refresh data. Please try again.');
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleDeleteSession = async () => {
    if (!id || !session) return;
    
    if (!window.confirm('Are you sure you want to delete this session? This action cannot be undone.')) {
      return;
    }

    try {
      await sessionsAPI.delete(Number(id));
      if (session?.project_id) {
        window.location.href = `/projects/${session.project_id}`;
      } else {
        window.location.href = '/projects';
      }
    } catch (error) {
      console.error('Failed to delete session:', error);
      alert('Failed to delete session. Please try again.');
    }
  };

  const handleStart = async () => {
    if (!id) return;
    try {
      await sessionsAPI.start(Number(id));
      await fetchSession();
    } catch (error) {
      console.error('Failed to start session:', error);
      alert('Failed to start session. Please try again.');
    }
  };

  const handlePause = async () => {
    if (!id) return;
    
    try {
      // Pause the session (this will automatically save a checkpoint in the backend)
      await sessionsAPI.pause(Number(id));
      
      // Fetch updated session state
      await fetchSession();
      
      // Reload checkpoints to show user it was saved (with error handling)
      try {
        await loadCheckpoints();
      } catch (checkpointError) {
        console.warn('Failed to reload checkpoints after pause, but session is paused:', checkpointError);
        // Don't crash - just keep existing checkpoints or reset safely
        setCheckpoints([]);
      }
      
      alert('✅ Session paused and checkpoint saved successfully!');
    } catch (error) {
      console.error('Failed to pause session:', error);
      alert('Failed to pause session. Please try again.');
    }
  };

  const handleResume = async () => {
    if (!id) return;
    
    // Step 1: Check for overwrites first (same as before)
    try {
      console.log('🛡️ Checking for potential overwrites before resume...');
      
      const workspaceInfo = await sessionsAPI.getWorkspaceInfo(Number(id));
      console.log('Workspace info:', workspaceInfo);
      
      if (workspaceInfo.exists) {
        // Show warning to user
        setShowOverwriteWarning({
          safe_to_proceed: false,
          workspace_exists: true,
          file_count: workspaceInfo.file_count,
          would_overwrite: true,
          conflicting_files: []
        });
        
        const proceed = window.confirm(
          `⚠️  WARNING: Workspace already exists with ${workspaceInfo.file_count} files!\n\n` +
          `Path: ${workspaceInfo.path}\n` +
          `\nThis will resume execution in an existing workspace.\n` +
          `Do you want to:\n` +
          `A) Resume anyway (may overwrite existing files)\n` +
          `B) Cancel and create a new session instead?`
        );
        
        if (!proceed) {
          console.log('❌ User cancelled resume due to overwrite warning');
          return; // Don't proceed with resume
        }
      } else {
        console.log('✅ No existing workspace, safe to resume');
      }
      
    } catch (error) {
      console.warn('⚠️  Overwrite check failed, proceeding anyway:', error);
      // Continue with resume even if check fails
    }
    
    // Step 2: Proceed with actual resume
    try {
      await sessionsAPI.resume(Number(id));
      
      // Clear warning after successful resume
      setShowOverwriteWarning(null);
      
      await fetchSession();
      
      // Reload checkpoints (with error handling to prevent crashes)
      try {
        await loadCheckpoints();
      } catch (checkpointError) {
        console.warn('Failed to reload checkpoints after resume, but session is resumed:', checkpointError);
        setCheckpoints([]);
      }
      
      // Show success message (if we had a warning, it's now cleared)
      if (!showOverwriteWarning || showOverwriteWarning.safe_to_proceed) {
        alert('✅ Session resumed successfully!');
      }
    } catch (error) {
      console.error('Failed to resume session:', error);
      
      // Clear any existing warning on failure
      setShowOverwriteWarning(null);
      
      alert('Failed to resume session. Please try again.');
    }
  };

  const handleDeleteCheckpoint = async (checkpointName: string) => {
    if (!id) return;
    
    if (!window.confirm(`Are you sure you want to delete checkpoint "${checkpointName}"?`)) {
      return;
    }
    
    try {
      await sessionsAPI.deleteCheckpoint(Number(id), checkpointName);
      await loadCheckpoints();
      alert('✅ Checkpoint deleted successfully');
    } catch (error) {
      console.error('Failed to delete checkpoint:', error);
      alert('Failed to delete checkpoint. Please try again.');
    }
  };

  const handleCleanupCheckpoints = async () => {
    if (!id) return;
    
    if (!window.confirm('This will delete all checkpoints except the 3 most recent ones. Continue?')) {
      return;
    }
    
    try {
      await sessionsAPI.cleanupCheckpoints(Number(id), 3, 24); // Keep latest 3, older than 24 hours
      await loadCheckpoints();
      alert('✅ Checkpoint cleanup completed');
    } catch (error) {
      console.error('Failed to cleanup checkpoints:', error);
      alert('Failed to cleanup checkpoints. Please try again.');
    }
  };

  const handleStop = async () => {
    if (!id) return;
    try {
      await sessionsAPI.stop(Number(id));
      await fetchSession();
      alert('✅ Session stopped successfully!');
    } catch (error) {
      const axiosError = error as { code?: string; message?: string };
      console.error('Failed to stop session:', error);
      // Suppress timeout errors - session stop might take a moment
      if (axiosError.code !== 'ECONNABORTED' && axiosError.code !== 'ERR_BAD_RESPONSE') {
        alert('Failed to stop session. Please try again.');
      } else {
        // Request timed out but session might be stopping in background
        alert('Stopping session... (may take a moment)');
      }
    }
  };

  const handleExecute = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!id || !inputTask.trim() || executing) return;

    // Show success notification IMMEDIATELY when button is clicked
    alert('✅ Task submitted successfully! Execution has started.');
    
    setExecuting(true);
    try {
      // Note: tasksAPI.execute already has 5 minute timeout in client.ts
      await tasksAPI.execute(Number(id), {
        task: inputTask,
        timeout_seconds: 300,
        task_id: selectedTaskId || undefined,
        log_timeout_minutes: 5, // Fail if no new logs for 5 minutes
        monitor_logs: true // Enable log monitoring
      });

      setInputTask('');
      setSelectedTaskId(null);
      await fetchSession();
      
      // Remove redundant notification - already shown at start
      // alert('Task executed successfully!');
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: unknown } }; message?: string };
      console.error('Failed to execute task:', error);
      // Show more helpful error message for timeouts
      if (err.message?.includes('timeout')) {
        alert('⚠️ Task execution timed out after 10 minutes. The task may still be running in the background. Check the logs for status or wait for it to complete.');
      } else {
        alert(err.response?.data?.detail || 'Failed to execute task. Please try again.');
      }
    } finally {
      setExecuting(false);
    }
  };

  const getFilteredLogs = () => {
    if (!filterLevel) return logs;
    return logs.filter(log => log.level === filterLevel);
  };

  const getSortedLogs = () => {
    const filtered = getFilteredLogs();
    const sorted = [...filtered].sort((a, b) => {
      const dateA = new Date(a.created_at || 0);
      const dateB = new Date(b.created_at || 0);
      return sortOrder === 'asc' ? dateA.getTime() - dateB.getTime() : dateB.getTime() - dateA.getTime();
    });
    
    if (deduplicate) {
      const seen = new Set();
      return sorted.filter(log => {
        const key = `${log.created_at}-${log.message}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }
    
    return sorted;
  };

  const displayLogs = getSortedLogs();

  const getLogCount = () => {
    if (!filterLevel) return logs.length;
    return logs.filter(log => log.level === filterLevel).length;
  };

  // Error boundary
  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="text-center">
          <XCircle className="h-16 w-16 text-red-500 mx-auto mb-4" />
          <h2 className="text-xl font-semibold text-white mb-2">Error Loading Session</h2>
          <p className="text-slate-400 mb-4">{error}</p>
          <button
            onClick={() => window.history.back()}
            className="bg-primary-500 hover:bg-primary-600 text-white px-6 py-2 rounded-lg transition-colors"
          >
            Go Back
          </button>
        </div>
      </div>
    );
  }

  // Loading state
  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="h-8 w-8 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950">
      {/* Header */}
      <div className="bg-slate-900/80 backdrop-blur border-b border-slate-800 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <Link to={`/projects/${session?.project_id}`}>
                <ArrowLeft className="h-5 w-5 text-slate-400 hover:text-white transition-colors" />
              </Link>
              <div>
                <h1 className="text-2xl font-bold text-white">
                  {session?.name || 'Session'}
                </h1>
                <p className="text-sm text-slate-400">
                  Project ID: {session?.project_id}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setShowSettings(!showSettings)}
                className="p-2 text-slate-400 hover:text-white transition-colors"
                title="Log settings"
              >
                <Settings className="h-5 w-5" />
              </button>
              <Link
                to={`/projects/${session?.project_id}`}
                className="px-4 py-2 text-slate-300 hover:text-white transition-colors"
              >
                Back to Project
              </Link>
            </div>
          </div>
        </div>
      </div>

      {/* Settings Panel */}
      {showSettings && (
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
            <h3 className="text-lg font-semibold text-white mb-4">Log Settings</h3>
            
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              {/* Sort Order */}
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-2">
                  Sort Order
                </label>
                <div className="flex gap-2">
                  <button
                    onClick={() => setSortOrder('asc')}
                    className={`flex-1 flex items-center justify-center gap-2 px-4 py-2 rounded-lg transition-all ${
                      sortOrder === 'asc'
                        ? 'bg-primary-500 text-white'
                        : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                    }`}
                  >
                    <ArrowUp className="h-4 w-4" />
                    Oldest First
                  </button>
                  <button
                    onClick={() => setSortOrder('desc')}
                    className={`flex-1 flex items-center justify-center gap-2 px-4 py-2 rounded-lg transition-all ${
                      sortOrder === 'desc'
                        ? 'bg-primary-500 text-white'
                        : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                    }`}
                  >
                    <ArrowDown className="h-4 w-4" />
                    Newest First
                  </button>
                </div>
              </div>

              {/* Deduplicate */}
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-2">
                  Deduplicate Logs
                </label>
                <button
                  onClick={() => setDeduplicate(!deduplicate)}
                  className={`w-full px-4 py-2 rounded-lg transition-all ${
                    deduplicate
                      ? 'bg-primary-500 text-white'
                      : 'bg-slate-700 text-slate-300 hover:bg-slate-600'
                  }`}
                >
                  {deduplicate ? 'Enabled' : 'Disabled'}
                </button>
              </div>

              {/* Filter by Level */}
              <div>
                <label className="block text-sm font-medium text-slate-300 mb-2">
                  Filter by Level
                </label>
                <select
                  value={filterLevel || ''}
                  onChange={(e) => setFilterLevel(e.target.value || undefined)}
                  className="w-full px-4 py-2 bg-slate-700 text-slate-300 rounded-lg border border-slate-600 focus:outline-none focus:ring-2 focus:ring-primary-500"
                >
                  <option value="">All Levels</option>
                  <option value="INFO">INFO</option>
                  <option value="WARNING">WARNING</option>
                  <option value="ERROR">ERROR</option>
                </select>
              </div>
            </div>

            {/* Info */}
            <div className="mt-4 p-3 bg-slate-700/50 rounded-lg">
              <p className="text-sm text-slate-300">
                Showing {displayLogs.length} of {getLogCount()} logs 
                {filterLevel && <span> (filtered: {filterLevel})</span>}
                {deduplicate && <span> (deduplicated)</span>}
                <span> (sorted: {sortOrder === 'asc' ? 'oldest first' : 'newest first'})</span>
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left Column - Session Info & Controls */}
          <div className="lg:col-span-1 space-y-6">
            {/* Session Status */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <Activity className="h-5 w-5" />
                Session Status
              </h2>
              
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <span className="text-slate-400">Status:</span>
                  <span className={`font-medium ${
                    isSessionRunning() ? 'text-green-400' :
                    session?.status === 'paused' ? 'text-yellow-400' :
                    session?.status === 'stopped' ? 'text-red-400' :
                    'text-slate-300'
                  }`}>
                    {session?.status || 'unknown'}
                  </span>
                </div>
                
                <div className="flex items-center justify-between">
                  <span className="text-slate-400">Active:</span>
                  <span className={`font-medium ${
                    session?.is_active ? 'text-green-400' : 'text-slate-300'
                  }`}>
                    {session?.is_active ? 'Yes' : 'No'}
                  </span>
                </div>
                
                {session?.started_at && (
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400">Started:</span>
                    <span className="text-slate-300 text-sm">{formatLocalTime(session.started_at)}</span>
                  </div>
                )}
                
                {session?.stopped_at && (
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400">Stopped:</span>
                    <span className="text-slate-300 text-sm">{formatLocalTime(session.stopped_at)}</span>
                  </div>
                )}
              </div>
            </div>

            {/* Session Controls */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4">Controls</h2>
              
              <div className="grid grid-cols-2 gap-3">
                {session?.status === 'stopped' && (
                  <button
                    onClick={handleStart}
                    disabled={executing}
                    className="flex items-center justify-center gap-2 px-4 py-3 bg-green-500 hover:bg-green-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                  >
                    <Play className="h-5 w-5" />
                    Start
                  </button>
                )}
                
                {session?.status === 'pending' && (
                  <button
                    onClick={handleStart}
                    disabled={executing}
                    className="flex items-center justify-center gap-2 px-4 py-3 bg-green-500 hover:bg-green-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                  >
                    <Play className="h-5 w-5" />
                    Start
                  </button>
                )}
                
                {isSessionRunning() && (
                  <>
                    <button
                      onClick={handlePause}
                      disabled={executing}
                      className="flex items-center justify-center gap-2 px-4 py-3 bg-yellow-500 hover:bg-yellow-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                    >
                      <Pause className="h-5 w-5" />
                      Pause
                    </button>
                    <button
                      onClick={handleStop}
                      disabled={executing}
                      className="flex items-center justify-center gap-2 px-4 py-3 bg-red-500 hover:bg-red-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                    >
                      <Square className="h-5 w-5" />
                      Stop
                    </button>
                  </>
                )}
                
                {(session?.status === 'paused' || (session?.status === 'stopped' && checkpoints.length > 0)) && (
                  <div className="col-span-2 space-y-2">
                    {checkpoints.length > 0 && (
                      <div className="rounded-lg border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-xs text-blue-200">
                        <span>Resume from checkpoint: </span>
                        <span className="font-medium text-white">{checkpoints[checkpoints.length - 1]?.name}</span>
                        <span className="mx-2 text-blue-300">•</span>
                        <span>Completed steps: </span>
                        <span className="font-medium text-white">{checkpoints[checkpoints.length - 1]?.completed_steps ?? 0}</span>
                      </div>
                    )}
                    <button
                      onClick={handleResume}
                      disabled={executing}
                      className="flex w-full items-center justify-center gap-2 px-4 py-3 bg-blue-500 hover:bg-blue-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                    >
                      <Play className="h-5 w-5" />
                      Resume Session
                    </button>
                  </div>
                )}
                
                <button
                  onClick={handleRefreshWebSocket}
                  disabled={executing || isRefreshing}
                  className="flex items-center justify-center gap-2 px-4 py-3 bg-slate-700 hover:bg-slate-600 text-white rounded-lg transition-all font-medium disabled:opacity-50"
                >
                  {isRefreshing ? (
                    <>
                      <RefreshCw className="h-5 w-5 animate-spin" />
                      Refreshing...
                    </>
                  ) : (
                    <>
                      <RefreshCw className="h-5 w-5" />
                      Refresh
                    </>
                  )}
                </button>
                
                <button
                  onClick={handleDeleteSession}
                  disabled={executing}
                  className="flex items-center justify-center gap-2 px-4 py-3 bg-red-600/20 hover:bg-red-600/30 text-red-400 hover:text-red-300 rounded-lg transition-all font-medium disabled:opacity-50 col-span-2"
                >
                  <Trash2 className="h-5 w-5" />
                  Delete Session
                </button>

                {/* Checkpoint Management Button */}
                {Array.isArray(checkpoints) && checkpoints.length > 0 && (
                  <button
                    onClick={() => setShowCheckpointModal(true)}
                    disabled={executing}
                    className="flex items-center justify-center gap-2 px-4 py-3 bg-purple-600/20 hover:bg-purple-600/30 text-purple-400 hover:text-purple-300 rounded-lg transition-all font-medium disabled:opacity-50 col-span-2"
                  >
                    <Clock className="h-5 w-5" />
                    View Checkpoints ({checkpoints.length})
                  </button>
                )}
              </div>
            </div>

            {/* Overwrite Warning Alert */}
            {showOverwriteWarning && (
              <div className="bg-amber-500/10 backdrop-blur rounded-xl border border-amber-500/30 p-6">
                <h2 className="text-lg font-semibold text-amber-400 mb-4 flex items-center gap-2">
                  <ShieldCheck className="h-5 w-5" />
                  Overwrite Protection Warning
                </h2>
                
                {showOverwriteWarning.workspace_exists && (
                  <div className="space-y-3 text-sm text-slate-300">
                    <p>
                      ⚠️  Existing workspace detected with 
                      <span className="font-semibold text-white"> {showOverwriteWarning.file_count} files</span>.
                    </p>
                    
                    {showOverwriteWarning.conflicting_files.length > 0 && (
                      <div>
                        <p className="font-medium text-amber-300 mb-1">Potential conflicts:</p>
                        <ul className="list-disc list-inside space-y-1 text-slate-400">
                          {showOverwriteWarning.conflicting_files.map((file, idx) => (
                            <li key={idx}>{file}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    
                    <p className="text-xs text-slate-400 mt-3">
                      💡 Tip: Consider creating a backup or starting a new session to preserve existing work.
                    </p>
                  </div>
                )}
                
                {!showOverwriteWarning.workspace_exists && (
                  <p className="text-sm text-green-400">
                    ✅ No existing workspace - safe to proceed
                  </p>
                )}
              </div>
            )}

            {/* Connection Status */}
            <div className={`bg-slate-800/50 backdrop-blur rounded-xl border p-6 ${
              isConnected 
                ? 'border-green-400/20 bg-green-400/5' 
                : 'border-red-400/20 bg-red-400/5'
            }`}>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className={`h-3 w-3 rounded-full ${
                    isConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'
                  }`} />
                  <span className="font-medium text-white">WebSocket</span>
                </div>
                <span className={`text-sm ${
                  isConnected ? 'text-green-400' : 'text-red-400'
                }`}>
                  {isConnected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
              {!isConnected && (
                <div className="text-xs text-slate-400 mt-2 space-y-1">
                  {session?.status === 'stopped' || session?.status === 'completed' ? (
                    <p>
                      ⚠️ Session completed - task execution finished
                    </p>
                  ) : (
                    <p>
                      🔄 Reconnecting in 3 seconds...
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Right Column - Logs & Execution */}
          <div className="lg:col-span-2 space-y-6">
            {/* Logs Connection Status */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <div className={`h-3 w-3 rounded-full ${
                    isLogsConnected ? 'bg-green-400 animate-pulse' : 'bg-red-400'
                  }`} />
                  <span className="font-medium text-white">Logs Stream</span>
                </div>
                <span className={`text-sm ${
                  isLogsConnected ? 'text-green-400' : 'text-red-400'
                }`}>
                  {isLogsConnected ? 'Connected' : 'Disconnected'}
                </span>
              </div>
            </div>

            {/* Available Tasks */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold text-white flex items-center gap-2">
                  <Zap className="h-5 w-5" />
                  Available Tasks
                </h2>
                <button
                  onClick={fetchProjectTasks}
                  disabled={isLoadingTasks}
                  className="p-2 text-slate-400 hover:text-white transition-colors disabled:opacity-50"
                  title="Refresh tasks"
                >
                  <RefreshCw className={`h-5 w-5 ${isLoadingTasks ? 'animate-spin' : ''}`} />
                  {isLoadingTasks && <span className="ml-2 text-xs text-slate-400">(loading...)</span>}
                </button>
              </div>
              
              {isLoadingTasks ? (
                <div className="text-center py-8 text-slate-400">
                  <div className="inline-block h-6 w-6 border-2 border-slate-400 border-t-transparent rounded-full animate-spin" />
                  <p className="mt-2 text-sm">Loading tasks...</p>
                </div>
              ) : tasks.length === 0 ? (
                <div className="text-center py-8 text-slate-400">
                  <p className="text-sm">No tasks in this project yet.</p>
                  <Link to={`/projects/${session?.project_id}`} className="mt-2 text-primary-400 hover:text-primary-300 text-sm">
                    Go to project to add tasks
                  </Link>
                </div>
              ) : (
                <div className="space-y-3">
                  {tasks.map((task) => (
                    <button
                      key={task.id}
                      onClick={() => {
                        setInputTask(`${task.title}\n\n${task.description || ''}`);
                        setSelectedTaskId(task.id);
                        // Scroll to execution box
                        setTimeout(() => {
                          document.querySelector('textarea')?.focus();
                        }, 100);
                      }}
                      disabled={executing || !isSessionRunning()}
                      className="w-full text-left bg-slate-700/50 hover:bg-slate-700 border border-slate-600 hover:border-primary-500 rounded-lg p-4 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <div className="flex items-start justify-between">
                        <div className="flex-1">
                          <h3 className="font-medium text-white mb-1">{task.title}</h3>
                          {task.description && (
                            <p className="text-sm text-slate-400 line-clamp-2">
                              {task.description}
                            </p>
                          )}
                          {task.steps && (
                            <p className="text-xs text-slate-500 mt-2">
                              {task.steps}
                            </p>
                          )}
                        </div>
                        <div className={`ml-4 px-2 py-1 rounded text-xs font-medium ${
                          task.status === 'done' ? 'bg-green-500/20 text-green-400' :
                          task.status === 'running' ? 'bg-blue-500/20 text-blue-400' :
                          task.status === 'failed' ? 'bg-red-500/20 text-red-400' :
                          'bg-slate-600/20 text-slate-400'
                        }`}>
                          {task.status}
                        </div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>

            {/* Task Execution */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <Terminal className="h-5 w-5" />
                Execute Task
              </h2>
              
              <form onSubmit={handleExecute}>
                <div className="space-y-3">
                  {selectedTaskId && (
                    <div className="flex items-center justify-between rounded-lg border border-primary-500/40 bg-primary-500/10 px-4 py-3 text-sm text-primary-100">
                      <span>
                        Selected task workspace:{' '}
                        {tasks.find((task) => task.id === selectedTaskId)?.title || `Task #${selectedTaskId}`}
                      </span>
                      <button
                        type="button"
                        onClick={() => setSelectedTaskId(null)}
                        className="text-primary-200 transition-colors hover:text-white"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  )}
                  <textarea
                    value={inputTask}
                    onChange={(e) => setInputTask(e.target.value)}
                    placeholder="Enter a task for the AI session to execute..."
                    rows={4}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
                    disabled={!isSessionRunning()}
                  />
                  <button
                    type="submit"
                    disabled={!inputTask.trim() || executing || !isSessionRunning()}
                    className="w-full bg-primary-500 hover:bg-primary-600 text-white px-4 py-3 rounded-lg transition-all font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {executing ? (
                      <>
                        <div className="h-5 w-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Executing...
                      </>
                    ) : (
                      <>
                        <Zap className="h-5 w-5" />
                        Execute Task
                      </>
                    )}
                  </button>
                </div>
              </form>
            </div>

            {/* Live Logs */}
            <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h2 className="text-lg font-semibold text-white flex items-center gap-2">
                    <Terminal className="h-5 w-5" />
                    Live Logs
                  </h2>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xs text-slate-400">
                      Session #{id}
                    </span>
                    {project && (
                      <span className="text-xs text-primary-400">
                        • Project: {project.name}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-green-400 mt-1 flex items-center gap-1">
                    <ShieldCheck className="h-3 w-3" />
                    Showing logs from this session ONLY (isolated)
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-slate-400">
                    {displayLogs.length} logs
                  </span>
                  <button
                    onClick={handleRefreshLogs}
                    disabled={isLoadingLogs}
                    className="p-2 text-slate-400 hover:text-white transition-colors disabled:opacity-50"
                    title="Refresh logs"
                  >
                    <RefreshCw className={`h-4 w-4 ${isLoadingLogs ? 'animate-spin' : ''}`} />
                  </button>
                </div>
              </div>
              
              <div className="bg-slate-950 rounded-lg border border-slate-800 p-4 h-96 overflow-y-auto font-mono text-sm">
                {isLoadingLogs ? (
                  <div className="h-full flex items-center justify-center text-slate-500">
                    <RefreshCw className="h-8 w-8 animate-spin mb-2" />
                    <p>Loading logs...</p>
                  </div>
                ) : displayLogs.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-slate-500">
                    <Terminal className="h-12 w-12 mb-2 opacity-50" />
                    <p className="text-center">
                      <span className="block mb-1">No logs yet. Start the session to see activity.</span>
                      <span className="text-xs">Logs will only show from this session (isolated from other projects)</span>
                    </p>
                  </div>
                ) : (
                  <div className="space-y-1">
                    {displayLogs.map((log, index) => (
                      <div key={`${log.created_at || index}-${index}`} className="flex gap-3">
                        <span className="text-slate-500 whitespace-nowrap">
                          {formatLocalTime(log.created_at)}
                        </span>
                        <span className={`font-medium whitespace-nowrap ${
                          log.level === 'ERROR' ? 'text-red-400' :
                          log.level === 'WARNING' ? 'text-yellow-400' :
                          log.level === 'INFO' ? 'text-blue-400' :
                          'text-slate-300'
                        }`}>
                          [{log.level}]
                        </span>
                        <span className="text-slate-300 break-all">
                          {log.message}
                        </span>
                      </div>
                    ))}
                    <div ref={logsEndRef} />
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Checkpoint Modal */}
        {showCheckpointModal && (
          <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div className="bg-slate-800 rounded-xl border border-slate-700 max-w-2xl w-full max-h-[80vh] overflow-hidden flex flex-col">
              {/* Modal Header */}
              <div className="flex items-center justify-between p-6 border-b border-slate-700">
                <h3 className="text-lg font-semibold text-white flex items-center gap-2">
                  <Clock className="h-5 w-5" />
                  Session Checkpoints
                </h3>
                <button
                  onClick={() => setShowCheckpointModal(false)}
                  className="p-2 hover:bg-slate-700 rounded-lg transition-colors text-slate-400 hover:text-white"
                >
                  <X className="h-5 w-5" />
                </button>
              </div>

              {/* Modal Content */}
              <div className="flex-1 overflow-y-auto p-6">
                {checkpoints.length === 0 ? (
                  <div className="text-center py-8 text-slate-400">
                    <Clock className="h-12 w-12 mb-3 mx-auto opacity-50" />
                    <p>No checkpoints found for this session</p>
                    <p className="text-sm mt-2">Checkpoints are automatically saved when you pause a session</p>
                  </div>
                ) : (
                  <>
                    {/* Checkpoint List */}
                    <div className="space-y-3 mb-6">
                      {checkpoints.map((checkpoint, index) => (
                        <div key={index} className="bg-slate-900/50 rounded-lg border border-slate-700 p-4 hover:border-primary-500/50 transition-colors">
                          <div className="flex items-start justify-between">
                            <div className="flex-1">
                              <div className="font-medium text-white mb-1">{checkpoint.name}</div>
                              <div className="text-sm text-slate-400 space-y-1">
                                <p>Created: {formatLocalTime(checkpoint.created_at)}</p>
                                {checkpoint.completed_steps && (
                                  <p>Completed steps: {checkpoint.completed_steps}</p>
                                )}
                              </div>
                            </div>
                            <div className="flex items-center gap-2 ml-4">
                              <button
                                onClick={() => handleDeleteCheckpoint(checkpoint.name)}
                                className="p-2 hover:bg-red-500/10 text-slate-400 hover:text-red-400 rounded-lg transition-colors"
                                title="Delete checkpoint"
                              >
                                <Trash2 className="h-4 w-4" />
                              </button>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>

                    {/* Cleanup Button */}
                    <div className="flex items-center justify-between p-3 bg-slate-700/50 rounded-lg">
                      <span className="text-sm text-slate-300">
                        Keep latest 3 checkpoints, delete older than 24 hours
                      </span>
                      <button
                        onClick={handleCleanupCheckpoints}
                        className="px-4 py-2 bg-slate-600 hover:bg-slate-500 text-white rounded-lg transition-colors text-sm"
                      >
                        Cleanup Old Checkpoints
                      </button>
                    </div>
                  </>
                )}
              </div>

              {/* Modal Footer */}
              <div className="p-6 border-t border-slate-700">
                <button
                  onClick={() => setShowCheckpointModal(false)}
                  className="w-full px-4 py-2 bg-primary-500 hover:bg-primary-600 text-white rounded-lg transition-colors font-medium"
                >
                  Close
                </button>
              </div>
            </div>
          </div>
        )}

      </div>
    </div>
  );
}

export default SessionDashboard;
