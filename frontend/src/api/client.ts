import axios from 'axios';
import type { 
  Project, 
  Task, 
  Session, 
  ExecutionProfile,
  Plan,
  PlannerTaskCandidate,
  PlanningCommitPreview,
  PlanningSession,
  PlanningSessionSummary,
  LogEntry, 
  SessionStatistics, 
  AuthTokens, 
  User,
  SortedLogsResponse,
  TaskSortedLogsResponse,
  ProjectLogsResponse,
  WorkspaceInfo,
  SessionFilters,
  Checkpoint,
  CheckpointInspection,
  OrchestrationEvent,
  SessionStateDiffResponse,
  SessionDivergenceCompareResponse,
  AppSettings,
} from '../types/api';

const API_BASE_URL =
  import.meta.env.VITE_API_URL ||
  (import.meta.env.DEV ? '/api/v1' : 'http://localhost:8080/api/v1');

const getBrowserSafeHost = (host: string): string => {
  if (!host) {
    return host;
  }

  const [hostname, ...portParts] = host.split(':');
  const portSuffix = portParts.length > 0 ? `:${portParts.join(':')}` : '';

  // 0.0.0.0 is valid bind address for servers, but not a stable browser target.
  if (hostname === '0.0.0.0') {
    const fallbackHost = window.location.hostname || '127.0.0.1';
    return `${fallbackHost}${portSuffix}`;
  }

  // Firefox can prefer IPv6 for localhost during WebSocket connection attempts,
  // while local dev servers are commonly bound only on IPv4.
  if (hostname === 'localhost') {
    return `127.0.0.1${portSuffix}`;
  }

  return host;
};

const getWebSocketHost = (): string => {
  const wsHostFromEnv = import.meta.env.VITE_API_WS_HOST;
  if (wsHostFromEnv) {
    return getBrowserSafeHost(wsHostFromEnv);
  }

  if (import.meta.env.DEV) {
    try {
      const apiUrl = new URL(API_BASE_URL, window.location.origin);
      if (apiUrl.origin !== window.location.origin) {
        return getBrowserSafeHost(apiUrl.host);
      }
    } catch {
      // Fall through to the local backend default below.
    }

    // In local dev, a relative API base like "/api/v1" means the HTTP calls
    // are using the Vite proxy. WebSockets should target the backend directly.
    return `${getBrowserSafeHost(window.location.hostname || '127.0.0.1')}:8080`;
  }

  // Keep WS host aligned with the API base URL when possible.
  try {
    const apiUrl = new URL(API_BASE_URL, window.location.origin);
    return getBrowserSafeHost(apiUrl.host);
  } catch {
    return `${getBrowserSafeHost(window.location.hostname || '127.0.0.1')}:8080`;
  }
};

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 60000, // 60 second timeout for initial requests (page load, auth)
});

// Request interceptor for auth tokens
apiClient.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('access_token');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Response interceptor for token refresh
apiClient.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config;

    // Only attempt refresh on 401 errors
    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const refreshToken = localStorage.getItem('refresh_token');
        if (!refreshToken) {
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          window.location.href = '/login';
          return Promise.reject(error);
        }

        const response = await axios.post(`${API_BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });

        const { access_token, refresh_token } = response.data;
        localStorage.setItem('access_token', access_token);
        if (refresh_token) {
          localStorage.setItem('refresh_token', refresh_token);
        }

        originalRequest.headers.Authorization = `Bearer ${access_token}`;
        return apiClient(originalRequest);
      } catch (refreshError) {
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        window.location.href = '/login';
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

// Auth API
export const authAPI = {
  login: (email: string, password: string) =>
    apiClient.post<AuthTokens>('/auth/tokens', { email, password }),

  register: (email: string, password: string) =>
    apiClient.post('/auth/register', { email, password }),

  getMe: () => apiClient.get<User>('/auth/me'),

  createApiKey: (name: string) =>
    apiClient.post('/auth/api-keys', { name }),

  getApiKeys: () => apiClient.get<Array<{ id: number; name: string; created_at: string }>>('/auth/api-keys'),

  revokeApiKey: (id: number) =>
    apiClient.delete(`/auth/api-keys/${id}`),

  refreshToken: (data: { refresh_token: string }) =>
    axios.post<AuthTokens>(`${API_BASE_URL}/auth/refresh`, data),
};

export const settingsAPI = {
  get: () => apiClient.get<AppSettings>('/settings'),
  updateProfile: (data: { name?: string | null }) =>
    apiClient.patch<AppSettings>('/settings/profile', data),
  changePassword: (data: { current_password: string; new_password: string }) =>
    apiClient.post('/settings/password', data),
  updateSystem: (data: {
    workspace_root?: string;
    mobile_api_key?: string;
    rotate_mobile_api_key?: boolean;
    agent_backend?: string;
    agent_model_family?: string;
    agent_adaptation_profile?: string;
    orchestration_policy_profile?: string;
  }) => apiClient.patch<AppSettings>('/settings/system', data),
  revealMobileSecret: () => apiClient.get('/settings/mobile-secret'),
};

// Projects API
export const projectsAPI = {
  getAll: () => apiClient.get<Project[]>('/projects'),

  getById: (id: number) => apiClient.get<Project>(`/projects/${id}`),

  create: (data: { name: string; description?: string; github_url?: string; branch?: string; workspace_path?: string }) =>
    apiClient.post<Project>('/projects', data),

  update: (id: number, data: Partial<Project>) =>
    apiClient.put<Project>(`/projects/${id}`, data),

  delete: (id: number) => apiClient.delete(`/projects/${id}`),

  getSessions: (projectId: number) => apiClient.get<Session[]>(`/projects/${projectId}/sessions`),
  getPlans: (projectId: number) => apiClient.get<Plan[]>(`/projects/${projectId}/plans`),
  getWorkspaceOverview: (projectId: number) =>
    apiClient.get<{
      project_id: number;
      project_name: string;
      counts: Record<string, number>;
      baseline: {
        exists: boolean;
        path?: string | null;
        file_count: number;
        promoted_task_count: number;
      };
      promoted_tasks: Array<{
        id: number;
        title: string;
        plan_position?: number | null;
        workspace_status?: string | null;
        task_subfolder?: string | null;
        promoted_at?: string | null;
      }>;
      ready_task_ids: number[];
    }>(`/projects/${projectId}/workspace-overview`),
  rebuildBaseline: (projectId: number) =>
    apiClient.post<{
      project_id: number;
      project_name: string;
      baseline_path: string;
      promoted_task_count: number;
      files_copied: number;
      applied_tasks: Array<{ task_id: number; title: string; files_copied: number }>;
    }>(`/projects/${projectId}/baseline/rebuild`),

  // Get logs for a project (filters by project_id, not session_id)
  getLogs: (
    projectId: number,
    limit?: number,
    level?: string,
    search?: string,
    order: 'asc' | 'desc' = 'desc'
  ) => {
    const params = new URLSearchParams();
    if (limit) params.append('limit', limit.toString());
    if (level) params.append('level', level);
    if (search) params.append('search', search);
    params.append('order', order);
    
    return apiClient.get<ProjectLogsResponse>(`/projects/${projectId}/logs?${params.toString()}`);
  },

  // WebSocket logs stream for project (filters by project_id)
  getLogsStream: (projectId: number) => {
    const token = localStorage.getItem('access_token');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const apiHost = getWebSocketHost();
    const wsUrl = token 
      ? `${protocol}//${apiHost}/api/v1/projects/${projectId}/logs/stream?token=${token}`
      : `${protocol}//${apiHost}/api/v1/projects/${projectId}/logs/stream`;
    return new WebSocket(wsUrl);
  },
};

export const plannerAPI = {
  generate: (data: { project_id: number; requirement: string; source_brain?: string }) =>
    apiClient.post<{ plan: Plan; tasks_preview: PlannerTaskCandidate[] }>('/planner/generate', data),

  parse: (markdown: string) =>
    apiClient.post<{ tasks: PlannerTaskCandidate[] }>('/planner/parse', { markdown }),

  batchCreateTasks: (
    projectId: number,
    data: {
      markdown?: string;
      plan_id?: number;
      plan_title?: string;
      requirement?: string;
      source_brain?: string;
      tasks: PlannerTaskCandidate[];
    }
  ) => apiClient.post<{ plan: Plan | null; tasks: Task[] }>(`/projects/${projectId}/batch-tasks`, data),

  deletePlan: (projectId: number, planId: number) =>
    apiClient.delete(`/projects/${projectId}/plans/${planId}`),

  updatePlan: (
    projectId: number,
    planId: number,
    data: {
      title?: string;
      requirement?: string;
      markdown?: string;
      source_brain?: string;
      status?: string;
    }
  ) => apiClient.put<Plan>(`/projects/${projectId}/plans/${planId}`, data),
};

export const planningAPI = {
  list: (projectId?: number) =>
    apiClient.get<PlanningSessionSummary[]>('/planning/sessions', {
      params: projectId ? { project_id: projectId } : undefined,
    }),

  start: (data: { project_id: number; prompt: string; source_brain?: string }) =>
    apiClient.post<PlanningSession>('/planning/sessions', data),

  get: (sessionId: number) =>
    apiClient.get<PlanningSession>(`/planning/sessions/${sessionId}`),

  respond: (sessionId: number, response: string) =>
    apiClient.post<PlanningSession>(`/planning/sessions/${sessionId}/respond`, { response }),

  cancel: (sessionId: number) =>
    apiClient.post<PlanningSession>(`/planning/sessions/${sessionId}/cancel`),

  commit: (
    sessionId: number,
    data?: { selected_tasks?: PlannerTaskCandidate[]; planner_markdown?: string }
  ) => apiClient.post<PlanningCommitPreview>(`/planning/sessions/${sessionId}/commit`, data || {}),
};

// Tasks API
export const tasksAPI = {
  getAll: () => apiClient.get<Task[]>('/tasks'),

  create: (data: { project_id: number; title: string; description?: string; priority?: number }) =>
    apiClient.post<Task>('/tasks', data),

  getByProject: (projectId: number) => apiClient.get<Task[]>(`/projects/${projectId}/tasks`),

  getById: (id: number) => apiClient.get<Task>(`/tasks/${id}`),

  update: (id: number, data: Partial<Task>) =>
    apiClient.put<Task>(`/tasks/${id}`, data),

  delete: (id: number) => apiClient.delete(`/tasks/${id}`),
  retry: (id: number) => apiClient.post(`/tasks/${id}/retry`),
  promoteWorkspace: (id: number, note?: string) =>
    apiClient.post<Task>(`/tasks/${id}/promote`, { note }),
  requestWorkspaceChanges: (id: number, note: string) =>
    apiClient.post<Task>(`/tasks/${id}/request-changes`, { note }),

  start: (id: number) => apiClient.post(`/tasks/${id}/start`),

  complete: (id: number) => apiClient.post(`/tasks/${id}/complete`),

  execute: (
    sessionId: number,
    data: {
      task: string;
      timeout_seconds?: number;
      use_demo_mode?: boolean;
      task_id?: number;
      log_timeout_minutes?: number;
      monitor_logs?: boolean;
    }
  ) =>
    apiClient.post(`/sessions/${sessionId}/execute`, data, {
      timeout: 600000, // 10 minutes for task execution (OpenClaw CLI can take a while for complex tasks)
    }),

  // Get sorted logs for a task
  getSortedLogs: (
    id: number,
    order: 'asc' | 'desc' = 'desc',
    deduplicate: boolean = true,
    level?: string,
    limit?: number,
    offset: number = 0
  ) => {
    const params = new URLSearchParams();
    params.append('order', order);
    params.append('deduplicate', deduplicate.toString());
    if (level) params.append('level', level);
    if (limit) params.append('limit', limit.toString());
    params.append('offset', offset.toString());
    
    return apiClient.get<TaskSortedLogsResponse>(`/tasks/${id}/logs/sorted?${params.toString()}`, {
      timeout: 60000, // 1 minute - much faster with database sorting + pagination
    });
  },
};

// Sessions API
export const sessionsAPI = {
  getAll: (params?: SessionFilters) => apiClient.get<Session[]>('/sessions', { params }),

  create: (data: { project_id: number; name: string; description?: string; execution_mode?: 'automatic' | 'manual'; default_execution_profile?: ExecutionProfile }) =>
    apiClient.post<Session>('/sessions', data),

  getByProject: (projectId: number) => apiClient.get<Session[]>(`/projects/${projectId}/sessions`),

  getById: (id: number) => apiClient.get<Session>(`/sessions/${id}`),

  update: (id: number, data: Partial<Session>) =>
    apiClient.patch<Session>(`/sessions/${id}`, data),

  delete: (id: number) => apiClient.delete(`/sessions/${id}`),

  // Lifecycle endpoints
  start: (id: number) => apiClient.post(`/sessions/${id}/start`),

  stop: (id: number, force?: boolean) => 
    apiClient.post(`/sessions/${id}/stop`, undefined, { 
      params: { force },
      timeout: 120000, // 2 minutes for session stop (may take time to terminate OpenClaw CLI)
    }),

  pause: (id: number) => apiClient.post(`/sessions/${id}/pause`),

  resume: (id: number) => apiClient.post(`/sessions/${id}/resume`),

  refreshTasks: (id: number) =>
    apiClient.post<{
      session_id: number;
      execution_mode: 'automatic' | 'manual';
      counts: { total: number; pending: number; running: number; done: number; failed: number };
      queued_task?: { task_id: number; task_name: string; celery_id: string; plan_position?: number | null } | null;
    }>(`/sessions/${id}/refresh-tasks`),

  runTask: (sessionId: number, taskId: number) =>
    apiClient.post<{
      status: string;
      session_id: number;
      execution_mode: 'automatic' | 'manual';
      queued_task: { task_id: number; task_name: string; celery_id: string; plan_position?: number | null };
    }>(`/sessions/${sessionId}/tasks/${taskId}/run`),

  // Overwrite protection endpoints
  checkOverwrites: (sessionId: number, data: { project_id: number; task_subfolder: string; planned_files?: string[] }) =>
    apiClient.post<{ 
      safe_to_proceed: boolean;
      workspace_exists: boolean;
      file_count: number;
      would_overwrite: boolean;
      warning_message?: string;
      conflicting_files: string[];
    }>(`/sessions/${sessionId}/check-overwrites`, data),

  createBackup: (sessionId: number) =>
    apiClient.post<{ 
      success: boolean;
      backup_path?: string;
      files_backed_up?: number;
      error?: string;
    }>(`/sessions/${sessionId}/create-backup`),

  getWorkspaceInfo: (sessionId: number) =>
    apiClient.get<WorkspaceInfo>(`/sessions/${sessionId}/workspace-info`),

  // Checkpoint management endpoints
  saveCheckpoint: (sessionId: number) =>
    apiClient.post<{ 
      success: boolean;
      session_id: number;
      message: string;
    }>(`/sessions/${sessionId}/checkpoint/save`),

  listCheckpoints: (sessionId: number) =>
    apiClient.get<{ 
      session_id: number;
      total_count: number;
      recommended_checkpoint_name?: string | null;
      checkpoints: Checkpoint[];
    }>(`/sessions/${sessionId}/checkpoints`),

  loadCheckpoint: (sessionId: number, checkpointName?: string) =>
    apiClient.post<{ 
      success: boolean;
      session_key: string;
      message: string;
      session_id: number;
    }>(`/sessions/${sessionId}/checkpoint/load`, undefined, {
      params: { checkpoint_name: checkpointName || '' },
    }),

  inspectCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.get<CheckpointInspection>(
      `/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}`
    ),

  getTaskEvents: (sessionId: number, taskId: number, eventType?: string) =>
    apiClient.get<{
      session_id: number;
      task_id: number;
      events: OrchestrationEvent[];
    }>(`/sessions/${sessionId}/tasks/${taskId}/events`, {
      params: eventType ? { event_type: eventType } : undefined,
    }),

  getSessionDiff: (
    sessionId: number,
    params?: { task_id?: number; from_checkpoint?: number; to_checkpoint?: number }
  ) =>
    apiClient.get<SessionStateDiffResponse>(`/sessions/${sessionId}/diff`, {
      params,
    }),

  getSessionDivergenceCompare: (sessionId: number, limit: number = 5) =>
    apiClient.get<SessionDivergenceCompareResponse>(
      `/sessions/${sessionId}/compare-divergence`,
      {
        params: { limit },
      }
    ),

  replayCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.post<{ 
      success: boolean;
      session_key: string;
      message: string;
      session_id: number;
      replay_requested: boolean;
    }>(
      `/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}/replay`
    ),

  deleteCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.delete<{ 
      success: boolean;
      message: string;
    }>(`/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}`),

  cleanupCheckpoints: (sessionId: number, keepLatest?: number, maxAgeHours?: number) =>
    apiClient.post<{ 
      success: boolean;
      deleted_count: number;
      kept_count: number;
    }>(`/sessions/${sessionId}/checkpoint/cleanup`, undefined, { 
      params: {
        keep_latest: keepLatest || 3,
        max_age_hours: maxAgeHours || 24,
      }
    }),

  startSession: (id: number, taskDescription: string) =>
  apiClient.post(`/sessions/${id}/start`, { task_description: taskDescription }),
  startOpenClaw: (id: number, taskDescription: string) =>
  apiClient.post(`/sessions/${id}/start`, { task_description: taskDescription }),

  execute: (id: number, data: { task: string; timeout_seconds?: number; task_id?: number }) =>
    apiClient.post(`/sessions/${id}/execute`, data),

  generateSteps: (data: { task_name: string; description: string }) =>
    apiClient.post<Array<{ title: string; description: string }>>('/generate-steps', data),

  getLogs: (id: number) =>
    apiClient.get<{ logs: LogEntry[]; total: number }>(`/sessions/${id}/logs`),

  getTools: (id: number) => apiClient.get<Array<{ id: number; tool_name: string; parameters: string; result: string; executed_at: string }>>(`/sessions/${id}/tools`),

  getStatistics: (id: number) => apiClient.get<SessionStatistics>(`/sessions/${id}/statistics`),

  trackTool: (id: number, data: { tool_name: string; parameters: string; result: string }) =>
    apiClient.post(`/sessions/${id}/tools/track`, data),

  getPromptTemplate: (id: number, templateName: string) =>
    apiClient.get<{ template: string; variables: string[] }>(`/sessions/${id}/prompts/${templateName}`),

  // WebSocket status stream
  getStatusStream: (id: number) => {
    const token = localStorage.getItem('access_token');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const apiHost = getWebSocketHost();
    const wsUrl = token 
      ? `${protocol}//${apiHost}/api/v1/sessions/${id}/status?token=${token}`
      : `${protocol}//${apiHost}/api/v1/sessions/${id}/status`;
    return new WebSocket(wsUrl);
  },

  // WebSocket logs stream
  getLogsStream: (id: number) => {
    const token = localStorage.getItem('access_token');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const apiHost = getWebSocketHost();
    const wsUrl = token 
      ? `${protocol}//${apiHost}/api/v1/sessions/${id}/logs/stream?token=${token}`
      : `${protocol}//${apiHost}/api/v1/sessions/${id}/logs/stream`;
    return new WebSocket(wsUrl);
  },

  // Get sorted logs (with sorting and deduplication options)
  getSortedLogs: (
    id: number,
    order: 'asc' | 'desc' = 'desc',
    deduplicate: boolean = true,
    level?: string,
    limit?: number,
    offset: number = 0
  ) => {
    const params = new URLSearchParams();
    params.append('order', order);
    params.append('deduplicate', deduplicate.toString());
    if (level) params.append('level', level);
    if (limit) params.append('limit', limit.toString());
    params.append('offset', offset.toString());
    
    return apiClient.get<SortedLogsResponse>(`/sessions/${id}/logs/sorted?${params.toString()}`, {
      timeout: 60000, // 1 minute - much faster with database sorting + pagination
    });
  },

};

export const api = apiClient;
export default apiClient;
