export interface Project {
  id: number;
  name: string;
  description: string | null;
  github_url: string | null;
  branch: string;
  created_at: string;
  updated_at: string | null;
}

export interface Task {
  id: number;
  project_id: number;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: number;
  steps: string | null;
  current_step: number;
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  task_subfolder?: string | null;
}

export type TaskStatus = 'pending' | 'running' | 'failed' | 'done' | 'cancelled';

export interface Session {
  id: number;
  project_id: number;
  name: string;
  description: string | null;
  is_active: boolean;
  status: SessionStatus;
  session_key: string | null;
  started_at: string | null;
  stopped_at: string | null;
  paused_at: string | null;
  resumed_at: string | null;
  created_at: string;
  updated_at: string | null;
  // Instance tracking for preventing ID reuse issues
  instance_id?: string | null;
  deleted_at?: string | null;
}

export type SessionStatus = 'pending' | 'running' | 'paused' | 'stopped' | 'completed';

export interface LogEntry {
  id: number;
  session_id: number | null;
  task_id: number | null;
  level: string;
  message: string;
  log_metadata: string | null;
  created_at: string;
  // Instance tracking for preventing ID reuse issues
  session_instance_id?: string | null;
}

export interface SessionStatistics {
  total_tools: number;
  total_time: number;
  tool_stats: Array<{
    tool_name: string;
    usage_count: number;
    total_time: number;
  }>;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
}

export interface User {
  id: number;
  email: string;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

// API Response Types
export interface SortedLogsResponse {
  session_id: number;
  total_logs: number;
  returned_logs: number;
  sort_order: 'asc' | 'desc';
  deduplicated: boolean;
  logs: LogEntry[];
}

export interface TaskSortedLogsResponse {
  task_id: number;
  total_logs: number;
  returned_logs: number;
  sort_order: 'asc' | 'desc';
  deduplicated: boolean;
  logs: LogEntry[];
}

export interface ProjectLogsResponse {
  project_id: number;
  project_name: string;
  total_logs: number;
  returned_logs: number;
  by_level: Record<string, number>;
  logs: LogEntry[];
}

// Overwrite Protection Types
export interface WorkspaceInfo {
  exists: boolean;
  path?: string;
  file_count: number;
  last_modified?: string;
  would_overwrite: boolean;
}

export interface OverwriteCheckResult {
  safe_to_proceed: boolean;
  workspace_exists: boolean;
  file_count: number;
  would_overwrite: boolean;
  warning_message?: string;
  conflicting_files: string[];
}

// Checkpoint Types
export interface Checkpoint {
  name: string;
  created_at: string | null;
  step_index?: number;
  completed_steps?: number;
}
