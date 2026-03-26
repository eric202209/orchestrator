import axios from 'axios';
import type { Project, Task, Session, LogEntry, SessionStatistics, AuthTokens, User } from '../types/api';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1';

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
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

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const refreshToken = localStorage.getItem('refresh_token');
        const response = await axios.post(`${API_BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });

        const { access_token, refresh_token } = response.data;
        localStorage.setItem('access_token', access_token);
        localStorage.setItem('refresh_token', refresh_token);

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
};

// Projects API
export const projectsAPI = {
  getAll: () => apiClient.get<Project[]>('/projects'),

  getById: (id: number) => apiClient.get<Project>(`/projects/${id}`),

  create: (data: { name: string; description?: string; github_url?: string; branch?: string }) =>
    apiClient.post<Project>('/projects', data),

  update: (id: number, data: Partial<Project>) =>
    apiClient.put<Project>(`/projects/${id}`, data),

  delete: (id: number) => apiClient.delete(`/projects/${id}`),
};

// Tasks API
export const tasksAPI = {
  create: (data: { project_id: number; title: string; description?: string; priority?: number }) =>
    apiClient.post<Task>('/tasks', data),

  getByProject: (projectId: number) => apiClient.get<Task[]>(`/projects/${projectId}/tasks`),

  getById: (id: number) => apiClient.get<Task>(`/tasks/${id}`),

  update: (id: number, data: Partial<Task>) =>
    apiClient.patch<Task>(`/tasks/${id}`, data),

  delete: (id: number) => apiClient.delete(`/tasks/${id}`),

  start: (id: number) => apiClient.post(`/tasks/${id}/start`),

  complete: (id: number) => apiClient.post(`/tasks/${id}/complete`),

  execute: (sessionId: number, data: { task: string; timeout_seconds?: number; use_demo_mode?: boolean }) =>
    apiClient.post(`/sessions/${sessionId}/execute`, data),

  // Get sorted logs for a task
  getSortedLogs: (
    id: number,
    order: 'asc' | 'desc' = 'desc',
    deduplicate: boolean = true,
    level?: string,
    limit?: number
  ) => {
    const params = new URLSearchParams();
    params.append('order', order);
    params.append('deduplicate', deduplicate.toString());
    if (level) params.append('level', level);
    if (limit) params.append('limit', limit.toString());
    
    return apiClient.get<{
      task_id: number;
      total_logs: number;
      returned_logs: number;
      sort_order: string;
      deduplicated: boolean;
      logs: Array<unknown>;
    }>(`/tasks/${id}/logs/sorted?${params.toString()}`);
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
    apiClient.post(`/sessions/${id}/stop`, undefined, { params: { force } }),

  pause: (id: number) => apiClient.post(`/sessions/${id}/pause`),

  resume: (id: number) => apiClient.post(`/sessions/${id}/resume`),

  startOpenClaw: (id: number, taskDescription: string) =>
  apiClient.post(`/sessions/${id}/start-openclaw`, { task_description: taskDescription }),

  execute: (id: number, data: { task: string; timeout_seconds?: number }) =>
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
    void localStorage.getItem('access_token');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // WebSocket endpoint is on backend (port 8080), not frontend (port 3000)
    const apiHost = import.meta.env.VITE_API_WS_HOST || window.location.hostname + ':8080';
    const wsUrl = `${protocol}//${apiHost}/api/v1/sessions/${id}/status`;
    return new WebSocket(wsUrl);
  },

  // WebSocket logs stream
  getLogsStream: (id: number) => {
    void localStorage.getItem('access_token');
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // WebSocket endpoint is on backend (port 8080), not frontend (port 3000)
    const apiHost = import.meta.env.VITE_API_WS_HOST || window.location.hostname + ':8080';
    const wsUrl = `${protocol}//${apiHost}/api/v1/sessions/${id}/logs/stream`;
    return new WebSocket(wsUrl);
  },

  // Get sorted logs (with sorting and deduplication options)
  getSortedLogs: (
    id: number,
    order: 'asc' | 'desc' = 'desc',
    deduplicate: boolean = true,
    level?: string,
    limit?: number
  ) => {
    const params = new URLSearchParams();
    params.append('order', order);
    params.append('deduplicate', deduplicate.toString());
    if (level) params.append('level', level);
    if (limit) params.append('limit', limit.toString());
    
    return apiClient.get<{
      session_id: number;
      total_logs: number;
      returned_logs: number;
      sort_order: string;
      deduplicated: boolean;
      logs: Array<unknown>;
    }>(`/sessions/${id}/logs/sorted?${params.toString()}`);
  },

  delete: (id: number) => apiClient.delete(`/sessions/${id}`),
};

export default apiClient;
