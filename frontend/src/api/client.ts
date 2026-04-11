import axios from 'axios';
import type { 
  Project, 
  Task, 
  Session, 
  LogEntry, 
  SessionStatistics, 
  AuthTokens, 
  User,
  SortedLogsResponse,
  TaskSortedLogsResponse,
  ProjectLogsResponse
} from '../types/api';

const API_BASE_URL =
  import.meta.env.VITE_API_URL ||
  (import.meta.env.DEV ? '/api/v1' : 'http://localhost:8080/api/v1');

const getBrowserSafeHost = (host: string): string => {
  if (!host) {
    return host;
  }
  // 0.0.0.0 is valid bind address for servers, but not a stable browser target.
  if (host.startsWith('0.0.0.0')) {
    const fallbackHost = window.location.hostname || '127.0.0.1';
    const withPort = host.includes(':') ? `:${host.split(':')[1]}` : '';
    return `${fallbackHost}${withPort}`;
  }
  return host;
};

const getWebSocketHost = (): string => {
  // When the dev frontend is using the Vite proxy on the same origin,
  // keep WebSockets on that same origin too so proxying works consistently.
  if (import.meta.env.DEV) {
    try {
      const apiUrl = new URL(API_BASE_URL, window.location.origin);
      if (apiUrl.host === window.location.host) {
        return window.location.host;
      }
    } catch {
      return window.location.host;
    }
  }

  const wsHostFromEnv = import.meta.env.VITE_API_WS_HOST;
  if (wsHostFromEnv) {
    return getBrowserSafeHost(wsHostFromEnv);
  }

  // In dev, prefer same-origin websocket + Vite proxy to avoid host/CORS mismatches.
  if (import.meta.env.DEV) {
    return window.location.host;
  }

  // Keep WS host aligned with the API base URL when possible.
  try {
    const apiUrl = new URL(API_BASE_URL);
    return getBrowserSafeHost(apiUrl.host);
  } catch {
    return `${window.location.hostname || '127.0.0.1'}:8080`;
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
        console.log('Attempting token refresh with refresh_token:', refreshToken ? 'exists' : 'missing');
        
        if (!refreshToken) {
          console.error('No refresh token found, redirecting to login');
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          window.location.href = '/login';
          return Promise.reject(error);
        }

        const response = await axios.post(`${API_BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });

        console.log('Token refresh successful');
        const { access_token, refresh_token } = response.data;
        localStorage.setItem('access_token', access_token);
        if (refresh_token) {
          localStorage.setItem('refresh_token', refresh_token);
        }

        originalRequest.headers.Authorization = `Bearer ${access_token}`;
        return apiClient(originalRequest);
      } catch (refreshError) {
        console.error('Token refresh failed:', refreshError);
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
};

// Projects API
export const projectsAPI = {
  getAll: () => apiClient.get<Project[]>('/projects'),

  getById: (id: number) => apiClient.get<Project>(`/projects/${id}`),

  create: (data: { name: string; description?: string; workspace_path?: string }) =>
    apiClient.post<Project>('/projects', data),

  update: (id: number, data: Partial<Project>) =>
    apiClient.put<Project>(`/projects/${id}`, data),

  delete: (id: number) => apiClient.delete(`/projects/${id}`),

  getSessions: (projectId: number) => apiClient.get<Session[]>(`/projects/${projectId}/sessions`),

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
  create: (data: { project_id: number; name: string; description?: string }) =>
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
    apiClient.get<{ 
      exists: boolean;
      path?: string;
      file_count: number;
      last_modified?: string;
      would_overwrite: boolean;
    }>(`/sessions/${sessionId}/workspace-info`),

  // Checkpoint management endpoints
  saveCheckpoint: (sessionId: number, checkpointName?: string) =>
    apiClient.post<{ 
      success: boolean;
      checkpoint_name: string;
      path: string;
      created_at: string;
    }>(`/sessions/${sessionId}/checkpoints?checkpoint_name=${checkpointName || 'manual'}`),

  listCheckpoints: (sessionId: number) =>
    apiClient.get<{ 
      session_id: number;
      total_count: number;
      checkpoints: Array<{
        name: string;
        created_at: string;
        step_index?: number;
        completed_steps: number;
      }>;
    }>(`/sessions/${sessionId}/checkpoints`),

  loadCheckpoint: (sessionId: number, checkpointName?: string) =>
    apiClient.post<{ 
      success: boolean;
      checkpoint_name: string;
      context: Record<string, unknown>;
      orchestration_state: Record<string, unknown>;
      current_step_index?: number;
      step_results_count: number;
      created_at: string;
    }>(`/sessions/${sessionId}/checkpoints/load?checkpoint_name=${checkpointName || ''}`),

  deleteCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.post<{ 
      success: boolean;
      message: string;
    }>(`/sessions/${sessionId}/checkpoints/delete`, { checkpoint_name: checkpointName }),

  cleanupCheckpoints: (sessionId: number, keepLatest?: number, maxAgeHours?: number) =>
    apiClient.post<{ 
      success: boolean;
      deleted_count: number;
      kept_count: number;
    }>(`/sessions/${sessionId}/checkpoints/cleanup`, { 
      keep_latest: keepLatest || 3,
      max_age_hours: maxAgeHours || 24
    }),

  autoSaveCheckpoint: (sessionId: number, eventType?: string) =>
    apiClient.post<{ 
      success: boolean;
      checkpoint_name: string;
      event_type: string;
      path: string;
      created_at: string;
    }>(`/sessions/${sessionId}/checkpoints/auto-save?event_type=${eventType || 'pause'}`),

  startOpenClaw: (id: number, taskDescription: string) =>
  apiClient.post(`/sessions/${id}/start-openclaw`, { task_description: taskDescription }),

  execute: (id: number, data: { task: string; timeout_seconds?: number; task_id?: number }) =>
    apiClient.post(`/sessions/${id}/execute`, data),

  getLogs: (id: number) => apiClient.get<LogEntry[]>(`/sessions/${id}/logs`),

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

  delete: (id: number) => apiClient.delete(`/sessions/${id}`),
};

export default apiClient;
