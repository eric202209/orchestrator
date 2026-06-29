import axios from "axios";
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
  User,
  SortedLogsResponse,
  TaskSortedLogsResponse,
  TaskExecutionChangeSetResponse,
  ProjectLogsResponse,
  WorkspaceInfo,
  SessionFilters,
  TaskFilters,
  Page,
  DashboardAttention,
  Checkpoint,
  CheckpointInspection,
  OrchestrationEvent,
  SessionDispatchWatchdogResponse,
  SessionDecisionTimelineResponse,
  SessionReplayResponse,
  SessionStateDiffResponse,
  SessionDivergenceCompareResponse,
  AppSettings,
  InterventionRequest,
  ExecutionFailureSummary,
  KnowledgeUsageResponse,
  ChangeSetReviewDecision,
  SessionRecoveryContext,
  SessionDigest,
  SessionNarrativeTimeline,
  HumanGuidanceEntry,
  HumanGuidanceActivation,
  HumanGuidanceReadiness,
  HumanGuidanceConflict,
  HumanGuidanceRendered,
  OperationalAnalytics,
  FailureAnalytics,
  KnowledgeAnalytics,
  ExecutionAnalytics,
  OperatorAnalytics,
  DecisionAnalytics,
  DecisionDrilldown,
  AnalyticsWindow,
} from "../types/api";

export type { Page, DashboardAttention };

const normalizeApiBaseUrl = (value: string): string => {
  const raw = value.trim().replace(/\/+$/, "");
  if (!raw) {
    return "/api/v1";
  }
  return raw.endsWith("/api/v1") ? raw : `${raw}/api/v1`;
};

const API_BASE_URL = normalizeApiBaseUrl(
  import.meta.env.VITE_API_URL ||
    (import.meta.env.DEV ? "" : "http://localhost:8080"),
);

const getBrowserSafeHost = (host: string): string => {
  if (!host) {
    return host;
  }

  const [hostname, ...portParts] = host.split(":");
  const portSuffix = portParts.length > 0 ? `:${portParts.join(":")}` : "";

  // 0.0.0.0 is valid bind address for servers, but not a stable browser target.
  if (hostname === "0.0.0.0") {
    const fallbackHost = window.location.hostname || "127.0.0.1";
    return `${fallbackHost}${portSuffix}`;
  }

  // Firefox can prefer IPv6 for localhost during WebSocket connection attempts,
  // while local dev servers are commonly bound only on IPv4.
  if (hostname === "localhost") {
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
    return `${getBrowserSafeHost(window.location.hostname || "127.0.0.1")}:8080`;
  }

  // Keep WS host aligned with the API base URL when possible.
  try {
    const apiUrl = new URL(API_BASE_URL, window.location.origin);
    return getBrowserSafeHost(apiUrl.host);
  } catch {
    return `${getBrowserSafeHost(window.location.hostname || "127.0.0.1")}:8080`;
  }
};

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    "Content-Type": "application/json",
  },
  timeout: 60000, // 60 second timeout for initial requests (page load, auth)
  withCredentials: true, // send httpOnly session cookie on every request
});

apiClient.interceptors.request.use((config) => {
  const token =
    window.localStorage.getItem("access_token") ||
    window.sessionStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  } else {
    delete config.headers.Authorization;
  }
  return config;
});

// Response interceptor: redirect to login on 401
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      window.location.href = "/login";
    }
    return Promise.reject(error);
  },
);

// Auth API
export const authAPI = {
  login: (email: string, password: string) =>
    apiClient.post<User>("/auth/session/login", { email, password }),

  logout: () => apiClient.post("/auth/session/logout"),

  register: (email: string, password: string) =>
    apiClient.post("/auth/register", { email, password }),

  getMe: () => apiClient.get<User>("/auth/me"),

  createApiKey: (name: string) => apiClient.post("/auth/api-keys", { name }),

  getApiKeys: () =>
    apiClient.get<Array<{ id: number; name: string; created_at: string }>>(
      "/auth/api-keys",
    ),

  revokeApiKey: (id: number) => apiClient.delete(`/auth/api-keys/${id}`),

  getWsTicket: () =>
    apiClient.post<{ ticket: string; expires_at: string }>("/auth/ws-ticket"),
};

export const settingsAPI = {
  get: () => apiClient.get<AppSettings>("/settings"),
  updateProfile: (data: { name?: string | null }) =>
    apiClient.patch<AppSettings>("/settings/profile", data),
  changePassword: (data: { current_password: string; new_password: string }) =>
    apiClient.post("/settings/password", data),
  updateSystem: (data: {
    workspace_root?: string;
    mobile_api_key?: string;
    rotate_mobile_api_key?: boolean;
    agent_backend?: string;
    agent_model_family?: string;
    agent_adaptation_profile?: string;
    orchestration_policy_profile?: string;
    workspace_review_policy?: string;
  }) => apiClient.patch<AppSettings>("/settings/system", data),
  revealMobileSecret: () => apiClient.get("/settings/mobile-secret"),
};

// Projects API
export const projectsAPI = {
  getAll: (params?: { limit?: number; skip?: number; page?: number; per_page?: number; search?: string; order_by?: string; order_dir?: string }) =>
    apiClient.get<Project[] | Page<Project>>("/projects", { params }),

  getById: (id: number) => apiClient.get<Project>(`/projects/${id}`),

  create: (data: {
    name: string;
    description?: string;
    project_rules?: string;
    github_url?: string;
    branch?: string;
    workspace_path?: string;
  }) => apiClient.post<Project>("/projects", data),

  update: (id: number, data: Partial<Project>) =>
    apiClient.put<Project>(`/projects/${id}`, data),

  delete: (id: number) => apiClient.delete(`/projects/${id}`),

  getSessions: (projectId: number, params?: SessionFilters) =>
    apiClient.get<Session[] | Page<Session>>(`/projects/${projectId}/sessions`, { params }),
  getPlans: (projectId: number) =>
    apiClient.get<Plan[]>(`/projects/${projectId}/plans`),
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
      audit: {
        project_root: string;
        retained_task_workspace_count: number;
        unpromoted_done_workspace_count: number;
        retained_task_workspaces: Array<{
          task_id: number;
          title: string;
          task_subfolder: string;
          baseline_diff: {
            added_count: number;
            modified_count: number;
            unchanged_count: number;
            added_files: string[];
            modified_files: string[];
          };
        }>;
        duplicated_scaffold_artifacts: Record<string, number>;
        transient_artifact_names: string[];
        issues: string[];
      };
      promoted_tasks: Array<{
        id: number;
        title: string;
        plan_position?: number | null;
        workspace_status?: string | null;
        task_subfolder?: string | null;
        promoted_at?: string | null;
      }>;
      pending_change_sets: Array<{
        task_id: number;
        title: string;
        workspace_status?: string | null;
        task_execution_id?: number | null;
        recorded_at?: string | null;
        change_set: {
          changed_count: number;
          added_count: number;
          modified_count: number;
          deleted_count: number;
          added_files: string[];
          modified_files: string[];
          deleted_files: string[];
          warning_flags: string[];
        };
        review_decision?: ChangeSetReviewDecision;
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
      applied_tasks: Array<{
        task_id: number;
        title: string;
        files_copied: number;
      }>;
    }>(`/projects/${projectId}/baseline/rebuild`),
  cleanupWorkspaces: (
    projectId: number,
    options?: {
      dry_run?: boolean;
      include_ready?: boolean;
      include_changes_requested?: boolean;
      include_blocked?: boolean;
    },
  ) =>
    apiClient.post<{
      project_id: number;
      project_name: string;
      dry_run: boolean;
      archive_root: string;
      candidate_count: number;
      deleted_count: number;
      candidates: Array<{
        task_id: number;
        title: string;
        task_subfolder: string;
        archive_path: string;
      }>;
      deleted: Array<{
        task_id: number;
        title: string;
        task_subfolder: string;
        archive_path: string;
      }>;
    }>(`/projects/${projectId}/workspace-cleanup`, options || {}),
  restoreWorkspaceArchive: (
    projectId: number,
    data: {
      task_id: number;
      archive_path: string;
    },
  ) =>
    apiClient.post<{
      project_id: number;
      project_name: string;
      restored: boolean;
      task_id: number;
      archive_path: string;
      workspace_path: string;
      task_subfolder: string;
      workspace_status: string;
    }>(`/projects/${projectId}/workspace-archive/restore`, data),

  // Get logs for a project (filters by project_id, not session_id)
  getLogs: (
    projectId: number,
    limit?: number,
    level?: string,
    search?: string,
    order: "asc" | "desc" = "desc",
  ) => {
    const params = new URLSearchParams();
    if (limit) params.append("limit", limit.toString());
    if (level) params.append("level", level);
    if (search) params.append("search", search);
    params.append("order", order);

    return apiClient.get<ProjectLogsResponse>(
      `/projects/${projectId}/logs?${params.toString()}`,
    );
  },

  // WebSocket logs stream for project — fetches a short-lived ticket first
  getLogsStream: async (projectId: number) => {
    const { data } = await authAPI.getWsTicket();
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiHost = getWebSocketHost();
    return new WebSocket(
      `${protocol}//${apiHost}/api/v1/projects/${projectId}/logs/stream?ticket=${data.ticket}`,
    );
  },
};

export const plannerAPI = {
  generate: (data: {
    project_id: number;
    requirement: string;
    source_brain?: string;
  }) =>
    apiClient.post<{ plan: Plan; tasks_preview: PlannerTaskCandidate[] }>(
      "/planner/generate",
      data,
    ),

  parse: (markdown: string) =>
    apiClient.post<{ tasks: PlannerTaskCandidate[] }>("/planner/parse", {
      markdown,
    }),

  batchCreateTasks: (
    projectId: number,
    data: {
      markdown?: string;
      plan_id?: number;
      plan_title?: string;
      requirement?: string;
      source_brain?: string;
      tasks: PlannerTaskCandidate[];
    },
  ) =>
    apiClient.post<{ plan: Plan | null; tasks: Task[] }>(
      `/projects/${projectId}/batch-tasks`,
      data,
    ),

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
    },
  ) => apiClient.put<Plan>(`/projects/${projectId}/plans/${planId}`, data),
};

export const planningAPI = {
  list: (projectId?: number) =>
    apiClient.get<PlanningSessionSummary[]>("/planning/sessions", {
      params: projectId ? { project_id: projectId } : undefined,
    }),

  start: (data: {
    project_id: number;
    prompt: string;
    source_brain?: string;
    skip_clarification?: boolean;
  }) => apiClient.post<PlanningSession>("/planning/sessions", data),

  get: (sessionId: number) =>
    apiClient.get<PlanningSession>(`/planning/sessions/${sessionId}`),

  respond: (sessionId: number, response: string) =>
    apiClient.post<PlanningSession>(`/planning/sessions/${sessionId}/respond`, {
      response,
    }),

  cancel: (sessionId: number) =>
    apiClient.post<PlanningSession>(`/planning/sessions/${sessionId}/cancel`),

  delete: (sessionId: number) =>
    apiClient.delete(`/planning/sessions/${sessionId}`),

  retry: (sessionId: number) =>
    apiClient.post<PlanningSession>(`/planning/sessions/${sessionId}/retry`),

  commit: (
    sessionId: number,
    data?: {
      selected_tasks?: PlannerTaskCandidate[];
      planner_markdown?: string;
    },
  ) =>
    apiClient.post<PlanningCommitPreview>(
      `/planning/sessions/${sessionId}/commit`,
      data || {},
    ),
};

// Tasks API
export const tasksAPI = {
  getAll: (params?: TaskFilters & { limit?: number; skip?: number }) =>
    apiClient.get<Task[] | Page<Task>>("/tasks", { params }),

  create: (data: {
    project_id: number;
    title: string;
    description?: string;
    priority?: number;
  }) => apiClient.post<Task>("/tasks", data),

  getByProject: (projectId: number, params?: TaskFilters) =>
    apiClient.get<Task[] | Page<Task>>(`/projects/${projectId}/tasks`, { params }),

  getById: (id: number) => apiClient.get<Task>(`/tasks/${id}`),

  update: (id: number, data: Partial<Task>) =>
    apiClient.put<Task>(`/tasks/${id}`, data),

  delete: (id: number) => apiClient.delete(`/tasks/${id}`),
  retry: (
    id: number,
    data?: {
      session_id?: number;
      execution_scope?: "workflow_session" | "new_session";
      create_new_session?: boolean;
    },
  ) => apiClient.post(`/tasks/${id}/retry`, data || {}),
  acceptWorkspace: (
    id: number,
    data?: { note?: string; task_execution_id?: number },
  ) => apiClient.post<Task>(`/tasks/${id}/accept`, data || {}),
  requestWorkspaceChanges: (id: number, note: string) =>
    apiClient.post<Task>(`/tasks/${id}/request-changes`, { note }),
  getChangeSet: (id: number) =>
    apiClient.get<TaskExecutionChangeSetResponse>(`/tasks/${id}/change-set`),
  rejectChangeSet: (
    id: number,
    data?: { task_execution_id?: number; note?: string },
  ) => apiClient.post(`/tasks/${id}/change-set/reject`, data || {}),

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
    },
  ) =>
    apiClient.post(`/sessions/${sessionId}/execute`, data, {
      timeout: 600000, // 10 minutes for task execution (OpenClaw CLI can take a while for complex tasks)
    }),

  // Get sorted logs for a task
  getSortedLogs: (
    id: number,
    order: "asc" | "desc" = "desc",
    deduplicate: boolean = true,
    level?: string,
    limit?: number,
    offset: number = 0,
  ) => {
    const params = new URLSearchParams();
    params.append("order", order);
    params.append("deduplicate", deduplicate.toString());
    if (level) params.append("level", level);
    if (limit) params.append("limit", limit.toString());
    params.append("offset", offset.toString());

    return apiClient.get<TaskSortedLogsResponse>(
      `/tasks/${id}/logs/sorted?${params.toString()}`,
      {
        timeout: 60000, // 1 minute - much faster with database sorting + pagination
      },
    );
  },
};

// Sessions API
export const sessionsAPI = {
  getAll: (params?: SessionFilters) =>
    apiClient.get<Session[] | Page<Session>>("/sessions", { params }),

  create: (data: {
    project_id: number;
    name: string;
    description?: string;
    execution_mode?: "automatic" | "manual";
    default_execution_profile?: ExecutionProfile;
  }) => apiClient.post<Session>("/sessions", data),

  getByProject: (projectId: number, params?: SessionFilters) =>
    apiClient.get<Session[] | Page<Session>>(`/projects/${projectId}/sessions`, { params }),

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
      execution_mode: "automatic" | "manual";
      counts: {
        total: number;
        pending: number;
        running: number;
        done: number;
        failed: number;
      };
      queued_task?: {
        task_id: number;
        task_name: string;
        celery_id: string;
        plan_position?: number | null;
      } | null;
    }>(`/sessions/${id}/refresh-tasks`),

  runTask: (sessionId: number, taskId: number) =>
    apiClient.post<{
      status: string;
      session_id: number;
      execution_mode: "automatic" | "manual";
      queued_task: {
        task_id: number;
        task_name: string;
        celery_id: string;
        plan_position?: number | null;
      };
    }>(`/sessions/${sessionId}/tasks/${taskId}/run`),

  // Overwrite protection endpoints
  checkOverwrites: (
    sessionId: number,
    data: {
      project_id: number;
      task_subfolder: string;
      planned_files?: string[];
    },
  ) =>
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
      params: { checkpoint_name: checkpointName || "" },
    }),

  inspectCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.get<CheckpointInspection>(
      `/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}`,
    ),

  getTaskEvents: (sessionId: number, taskId: number, eventType?: string) =>
    apiClient.get<{
      session_id: number;
      task_id: number;
      events: OrchestrationEvent[];
    }>(`/sessions/${sessionId}/tasks/${taskId}/events`, {
      params: eventType ? { event_type: eventType } : undefined,
    }),

  getDecisionTimeline: (sessionId: number) =>
    apiClient.get<SessionDecisionTimelineResponse>(
      `/sessions/${sessionId}/decision-timeline`,
    ),

  getReplay: (
    sessionId: number,
    params?: {
      task_id?: number;
      boundary_mode?: string;
      event_id?: string;
      timestamp?: string;
      snapshot_index?: number;
      checkpoint_name?: string;
    },
  ) =>
    apiClient.get<SessionReplayResponse>(`/sessions/${sessionId}/replay`, {
      params,
    }),

  getSessionDiff: (
    sessionId: number,
    params?: {
      task_id?: number;
      from_checkpoint?: number;
      to_checkpoint?: number;
    },
  ) =>
    apiClient.get<SessionStateDiffResponse>(`/sessions/${sessionId}/diff`, {
      params,
    }),

  getSessionDivergenceCompare: (sessionId: number, limit: number = 5) =>
    apiClient.get<SessionDivergenceCompareResponse>(
      `/sessions/${sessionId}/compare-divergence`,
      {
        params: { limit },
      },
    ),

  getSessionDispatchWatchdog: (sessionId: number, syncAlert: boolean = true) =>
    apiClient.get<SessionDispatchWatchdogResponse>(
      `/sessions/${sessionId}/dispatch-watchdog`,
      {
        params: { sync_alert: syncAlert },
      },
    ),

  getRecoveryContext: (sessionId: number) =>
    apiClient.get<SessionRecoveryContext>(`/sessions/${sessionId}/recovery-context`),

  retryPlanningLane: (sessionId: number) =>
    apiClient.post<{
      status: string;
      session_id: number;
      task_id: number;
      task_name: string;
      task_execution_id: number;
      celery_id: string;
      escalation_backend_id: string;
      evidence_payload_summary: Record<string, unknown>;
    }>(`/sessions/${sessionId}/retry-planning-lane`),

  getNarrativeTimeline: (sessionId: number) =>
    apiClient.get<SessionNarrativeTimeline>(`/sessions/${sessionId}/timeline`),

  getSessionDigest: (sessionId: number, enrich: boolean = false) =>
    apiClient.get<SessionDigest>(`/sessions/${sessionId}/digest`, {
      params: enrich ? { enrich: true } : undefined,
    }),

  replayCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.post<{
      success: boolean;
      session_key: string;
      message: string;
      session_id: number;
      replay_requested: boolean;
    }>(
      `/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}/replay`,
    ),

  deleteCheckpoint: (sessionId: number, checkpointName: string) =>
    apiClient.delete<{
      success: boolean;
      message: string;
    }>(
      `/sessions/${sessionId}/checkpoints/${encodeURIComponent(checkpointName)}`,
    ),

  cleanupCheckpoints: (
    sessionId: number,
    keepLatest?: number,
    maxAgeHours?: number,
  ) =>
    apiClient.post<{
      success: boolean;
      deleted_count: number;
      kept_count: number;
    }>(`/sessions/${sessionId}/checkpoint/cleanup`, undefined, {
      params: {
        keep_latest: keepLatest || 3,
        max_age_hours: maxAgeHours || 24,
      },
    }),

  startSession: (id: number, taskDescription: string) =>
    apiClient.post(`/sessions/${id}/start`, {
      task_description: taskDescription,
    }),
  startOpenClaw: (id: number, taskDescription: string) =>
    apiClient.post(`/sessions/${id}/start`, {
      task_description: taskDescription,
    }),

  execute: (
    id: number,
    data: { task: string; timeout_seconds?: number; task_id?: number },
  ) => apiClient.post(`/sessions/${id}/execute`, data),

  generateSteps: (data: { task_name: string; description: string }) =>
    apiClient.post<Array<{ title: string; description: string }>>(
      "/generate-steps",
      data,
    ),

  getLogs: (id: number) =>
    apiClient.get<{ logs: LogEntry[]; total: number }>(`/sessions/${id}/logs`),

  getTools: (id: number) =>
    apiClient.get<
      Array<{
        id: number;
        tool_name: string;
        parameters: string;
        result: string;
        executed_at: string;
      }>
    >(`/sessions/${id}/tools`),

  getStatistics: (id: number) =>
    apiClient.get<SessionStatistics>(`/sessions/${id}/statistics`),

  trackTool: (
    id: number,
    data: { tool_name: string; parameters: string; result: string },
  ) => apiClient.post(`/sessions/${id}/tools/track`, data),

  getPromptTemplate: (id: number, templateName: string) =>
    apiClient.get<{ template: string; variables: string[] }>(
      `/sessions/${id}/prompts/${templateName}`,
    ),

  // WebSocket status stream — fetches a short-lived ticket first
  getStatusStream: async (id: number) => {
    const { data } = await authAPI.getWsTicket();
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiHost = getWebSocketHost();
    return new WebSocket(
      `${protocol}//${apiHost}/api/v1/sessions/${id}/status?ticket=${data.ticket}`,
    );
  },

  // WebSocket logs stream — fetches a short-lived ticket first
  getLogsStream: async (id: number) => {
    const { data } = await authAPI.getWsTicket();
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiHost = getWebSocketHost();
    return new WebSocket(
      `${protocol}//${apiHost}/api/v1/sessions/${id}/logs/stream?ticket=${data.ticket}`,
    );
  },

  // Human-in-the-loop intervention endpoints
  requestIntervention: (
    sessionId: number,
    data: {
      intervention_type: string;
      prompt: string;
      task_id?: number;
      context_snapshot?: Record<string, unknown>;
      expires_in_minutes?: number;
    },
  ) =>
    apiClient.post<InterventionRequest>(
      `/sessions/${sessionId}/request-intervention`,
      data,
    ),

  addOperatorGuidance: (
    sessionId: number,
    data: { guidance: string; task_id?: number },
  ) =>
    apiClient.post<{
      session_id: number;
      task_id?: number | null;
      checkpoint_name?: string | null;
      non_blocking: boolean;
      message: string;
    }>(`/sessions/${sessionId}/operator-guidance`, data),

  listInterventions: (sessionId: number, pendingOnly?: boolean) =>
    apiClient.get<{
      session_id: number;
      interventions: InterventionRequest[];
      total: number;
    }>(`/sessions/${sessionId}/interventions`, {
      params:
        pendingOnly !== undefined ? { pending_only: pendingOnly } : undefined,
    }),

  replyToIntervention: (
    sessionId: number,
    interventionId: number,
    data: { reply: string },
  ) =>
    apiClient.post<InterventionRequest>(
      `/sessions/${sessionId}/interventions/${interventionId}/reply`,
      data,
    ),

  approveIntervention: (sessionId: number, interventionId: number) =>
    apiClient.post<InterventionRequest>(
      `/sessions/${sessionId}/interventions/${interventionId}/approve`,
    ),

  denyIntervention: (
    sessionId: number,
    interventionId: number,
    data?: { reason?: string },
  ) =>
    apiClient.post<InterventionRequest>(
      `/sessions/${sessionId}/interventions/${interventionId}/deny`,
      data || {},
    ),

  // Replan flow endpoints
  getFailureSummary: (sessionId: number) =>
    apiClient.get<ExecutionFailureSummary>(
      `/sessions/${sessionId}/failure-summary`,
      {
        timeout: 120000, // LLM summary generation can take up to 2 min
      },
    ),

  submitOperatorFeedback: (sessionId: number, feedback: string) =>
    apiClient.post<ExecutionFailureSummary>(
      `/sessions/${sessionId}/operator-feedback`,
      { feedback },
    ),

  replanSession: (sessionId: number) =>
    apiClient.post<{
      planning_session_id: number;
      session_id: number;
      message: string;
    }>(`/sessions/${sessionId}/replan`),

  getKnowledgeUsage: (sessionId: number) =>
    apiClient.get<KnowledgeUsageResponse>(
      `/sessions/${sessionId}/knowledge-usage`,
    ),

  // Get sorted logs (with sorting and deduplication options)
  getSortedLogs: (
    id: number,
    order: "asc" | "desc" = "desc",
    deduplicate: boolean = true,
    level?: string,
    limit?: number,
    offset: number = 0,
  ) => {
    const params = new URLSearchParams();
    params.append("order", order);
    params.append("deduplicate", deduplicate.toString());
    if (level) params.append("level", level);
    if (limit) params.append("limit", limit.toString());
    params.append("offset", offset.toString());

    return apiClient.get<SortedLogsResponse>(
      `/sessions/${id}/logs/sorted?${params.toString()}`,
      {
        timeout: 60000, // 1 minute - much faster with database sorting + pagination
      },
    );
  },
};

export const dashboardAPI = {
  getAttention: () => apiClient.get<DashboardAttention>("/dashboard/attention"),
};

export const adminAPI = {
  getOutcomeRates: (limit = 50) =>
    apiClient.get<{
      computed_at: string;
      sessions_analyzed: number;
      classified_sessions?: number;
      outcome_rates: Record<string, number>;
      outcome_counts: Record<string, number>;
      task_outcomes?: {
        total: number;
        counts: Record<string, number>;
        rates: Record<string, number>;
        execution_attempts: number;
        execution_attempts_done: number;
        first_pass_task_ids: number[];
        recovered_task_ids: number[];
      };
      operator_review_count: number;
      gate_pass: boolean;
      stuck_sessions: Array<{ session_id: number; status: string; terminal_class: string }>;
    }>(`/admin/outcome-rates?limit=${limit}`),
};

export const guidanceAPI = {
  getReadiness: (
    projectId: number,
    params?: { backend?: string; model_family?: string },
  ) =>
    apiClient.get<HumanGuidanceReadiness>(
      `/projects/${projectId}/guidance/readiness`,
      { params },
    ),

  patchActivation: (
    projectId: number,
    data: {
      table_enabled: boolean;
      persistence_enabled: boolean;
      render_enabled: boolean;
      injection_enabled: boolean;
      conflict_detection_enabled: boolean;
    },
  ) =>
    apiClient.patch<HumanGuidanceActivation>(
      `/projects/${projectId}/guidance/activation`,
      data,
    ),

  disableActivation: (projectId: number) =>
    apiClient.post<HumanGuidanceActivation>(
      `/projects/${projectId}/guidance/activation/disable`,
    ),

  list: (
    projectId: number,
    params?: { status?: string; limit?: number; offset?: number },
  ) =>
    apiClient.get<{ project_id: number; total: number; items: HumanGuidanceEntry[] }>(
      `/projects/${projectId}/guidance`,
      { params },
    ),

  create: (
    projectId: number,
    data: {
      message: string;
      scope?: string;
      priority?: number;
      expires_at?: string | null;
      backend_targets?: string[] | null;
      model_targets?: string[] | null;
      purpose_targets?: string[] | null;
    },
  ) =>
    apiClient.post<HumanGuidanceEntry>(
      `/projects/${projectId}/guidance`,
      data,
    ),

  patch: (
    guidanceId: number,
    data: {
      message?: string;
      status?: string;
      priority?: number;
      expires_at?: string | null;
      change_reason?: string;
    },
  ) =>
    apiClient.patch<HumanGuidanceEntry>(`/guidance/${guidanceId}`, data),

  archive: (guidanceId: number) =>
    apiClient.delete<{
      id: number;
      status: string;
      archived_at: string | null;
      message: string;
    }>(`/guidance/${guidanceId}`),

  getHistory: (guidanceId: number) =>
    apiClient.get<{
      id: number;
      revisions: Array<{
        revision: number;
        message: string;
        changed_by: string | null;
        changed_at: string | null;
        change_reason: string | null;
      }>;
    }>(`/guidance/${guidanceId}/history`),

  getRendered: (
    projectId: number,
    params?: {
      backend?: string;
      model_family?: string;
      purpose?: string;
      session_id?: number;
      task_id?: number;
    },
  ) =>
    apiClient.get<HumanGuidanceRendered>(
      `/projects/${projectId}/guidance/rendered`,
      { params },
    ),

  listConflicts: (
    projectId: number,
    params?: { status?: string; limit?: number },
  ) =>
    apiClient.get<{
      project_id: number;
      total: number;
      items: HumanGuidanceConflict[];
    }>(`/projects/${projectId}/guidance/conflicts`, { params }),

  patchConflict: (
    projectId: number,
    conflictId: number,
    data: { status: string; resolution_note?: string },
  ) =>
    apiClient.patch(
      `/projects/${projectId}/guidance/conflicts/${conflictId}`,
      data,
    ),
};

export interface PilotSummary {
  computed_at: string;
  project_id: number;
  task_executions: {
    total: number;
    done: number;
    failed: number;
    pending: number;
    running: number;
    cancelled: number;
  };
  rates: {
    success_rate: number | null;
    rejection_rate: number | null;
    timeout_rate: number | null;
  };
  symbol_verification: {
    applicable_tasks: number;
    passed: number | null;
    failed: number;
  };
}

export interface PilotGuidanceStats {
  computed_at: string;
  project_id: number;
  usage: {
    total_injections: number;
    total_rendered: number;
    tasks_with_guidance: number;
    top_entries: Array<{
      guidance_id: number;
      message_preview: string;
      injection_count: number;
    }>;
  };
  conflicts: {
    total: number;
    open: number;
    resolved: number;
    conflict_rate: number | null;
  };
}

export interface PilotTokenStats {
  computed_at: string;
  project_id: number;
  tasks_with_tokens: number;
  token_availability_rate: number | null;
  avg_tokens_in: number | null;
  avg_tokens_out: number | null;
  total_tokens_in: number | null;
  total_tokens_out: number | null;
  top_consumers: Array<{
    task_id: number | null;
    task_title: string;
    tokens_in: number | null;
    tokens_out: number | null;
  }>;
}

export interface PilotPermissionStats {
  computed_at: string;
  project_id: number;
  approvals: number;
  denials: number;
  pending: number;
  avg_response_seconds: number | null;
  max_response_seconds: number | null;
}

export interface QueueLatencyStats {
  computed_at: string;
  window_days: number;
  executions_with_latency: number;
  avg_queue_latency_seconds: number | null;
  max_queue_latency_seconds: number | null;
  p95_queue_latency_seconds: number | null;
}

export interface AuditEventsResponse {
  total: number;
  limit: number;
  offset: number;
  items: Array<{
    id: number;
    event_type: string;
    message: string;
    level: string;
    session_id: number | null;
    task_id: number | null;
    created_at: string | null;
    metadata: unknown;
  }>;
}

export const pilotAPI = {
  getSummary: (projectId: number) =>
    apiClient.get<PilotSummary>(`/ops/pilot-summary?project_id=${projectId}`),

  getGuidanceStats: (projectId: number) =>
    apiClient.get<PilotGuidanceStats>(
      `/ops/pilot-guidance-stats?project_id=${projectId}`,
    ),

  getTokenStats: (projectId: number) =>
    apiClient.get<PilotTokenStats>(
      `/ops/pilot-token-stats?project_id=${projectId}`,
    ),

  getPermissionStats: (projectId: number) =>
    apiClient.get<PilotPermissionStats>(
      `/ops/pilot-permission-stats?project_id=${projectId}`,
    ),

  getQueueLatency: (days = 7) =>
    apiClient.get<QueueLatencyStats>(`/ops/queue-latency?days=${days}`),

  getAuditEvents: (params: {
    project_id?: number;
    event_type?: string;
    limit?: number;
    offset?: number;
    order?: 'asc' | 'desc';
  }) =>
    apiClient.get<AuditEventsResponse>('/ops/audit-events', { params }),
};

export const analyticsAPI = {
  getOperational: () => apiClient.get<OperationalAnalytics>('/analytics/operational'),
  getFailures: () => apiClient.get<FailureAnalytics>('/analytics/failures'),
  getKnowledge: () => apiClient.get<KnowledgeAnalytics>('/analytics/knowledge'),
  getExecution: () => apiClient.get<ExecutionAnalytics>('/analytics/execution'),
  getOperators: () => apiClient.get<OperatorAnalytics>('/analytics/operators'),
  getDecision: () => apiClient.get<DecisionAnalytics>('/analytics/decision'),
  getDecisionDrilldown: (params: {
    kind: string;
    target: string;
    window?: AnalyticsWindow;
  }) => apiClient.get<DecisionDrilldown>('/analytics/decision/drilldown', { params }),
};

export const api = apiClient;
export default apiClient;
