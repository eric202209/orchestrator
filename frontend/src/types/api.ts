export interface Project {
  id: number;
  name: string;
  description: string | null;
  project_rules?: string | null;
  github_url: string | null;
  branch: string;
  workspace_path?: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Task {
  id: number;
  project_id: number;
  plan_id?: number | null;
  session_id?: number | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  execution_profile: ExecutionProfile;
  priority: number;
  estimated_effort?: string | null;
  plan_position?: number | null;
  steps: string | null;
  current_step: number;
  error_message: string | null;
  workspace_status?: string | null;
  promotion_note?: string | null;
  promoted_at?: string | null;
  created_at: string;
  updated_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  task_subfolder?: string | null;
}

export interface TaskExecutionChangeSet {
  schema: string;
  project_id: number;
  task_id: number;
  task_execution_id: number;
  snapshot_key: string;
  snapshot_path: string;
  snapshot_exists: boolean;
  target_path: string;
  status?: string | null;
  captured_at: string;
  added_files: string[];
  modified_files: string[];
  deleted_files: string[];
  added_count: number;
  modified_count: number;
  deleted_count: number;
  changed_count: number;
  warning_flags: string[];
}

export interface ChangeSetReviewDecision {
  workspace_review_policy: string;
  held_for_review: boolean;
  reason: string | null;
  changed_count: number;
  warning_flags: string[];
}

export interface TaskExecutionChangeSetResponse {
  task_id: number;
  task_execution_id: number | null;
  change_set: TaskExecutionChangeSet;
  review_decision?: ChangeSetReviewDecision;
  recorded_at: string | null;
}

export type TaskStatus =
  | "pending"
  | "running"
  | "failed"
  | "done"
  | "cancelled";
export type ExecutionProfile =
  | "full_lifecycle"
  | "execute_only"
  | "test_only"
  | "debug_only"
  | "review_only";

export interface Session {
  id: number;
  project_id: number;
  name: string;
  description: string | null;
  is_active: boolean;
  status: SessionStatus;
  execution_mode: "automatic" | "manual";
  default_execution_profile: ExecutionProfile;
  last_alert_level?: string | null;
  last_alert_message?: string | null;
  last_alert_at?: string | null;
  session_key?: string | null;
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

export interface Plan {
  id: number;
  project_id: number;
  title: string;
  source_brain: string;
  requirement: string;
  markdown: string;
  status: string;
  created_at: string;
  updated_at: string | null;
}

export interface PlannerTaskCandidate {
  title: string;
  description: string | null;
  execution_profile: ExecutionProfile;
  priority: number;
  plan_position?: number | null;
  estimated_effort?: string | null;
  include?: boolean;
}

export type PlanningSessionStatus =
  | "active"
  | "waiting_for_input"
  | "completed"
  | "failed"
  | "cancelled";

export interface PlanningMessage {
  id: number;
  role: "user" | "assistant";
  prompt_id?: string | null;
  content: string;
  metadata_json?: Record<string, unknown> | null;
  created_at: string;
}

export interface PlanningArtifact {
  id: number;
  artifact_type:
    | "requirements"
    | "design"
    | "implementation_plan"
    | "planner_markdown"
    | string;
  filename: string;
  content: string;
  created_at: string;
}

export interface PlanningSessionSummary {
  id: number;
  project_id: number;
  title: string;
  prompt: string;
  status: PlanningSessionStatus;
  source_brain: string;
  current_prompt_id?: string | null;
  finalized_plan_id?: number | null;
  committed_at?: string | null;
  completed_at?: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface PlanningSession extends PlanningSessionSummary {
  last_error?: string | null;
  messages: PlanningMessage[];
  artifacts: PlanningArtifact[];
  tasks_preview: PlannerTaskCandidate[];
  committed_task_ids: number[];
}

export interface PlanningCommitPreview extends PlanningSession {
  plan: Plan | null;
  tasks: Task[];
}

export type SessionStatus =
  | "pending"
  | "running"
  | "paused"
  | "stopped"
  | "completed"
  | "awaiting_input";

export interface InterventionRequest {
  id: number;
  session_id: number;
  task_id: number | null;
  project_id: number;
  intervention_type: "guidance" | "approval" | "information";
  initiated_by: "ai" | "human" | string;
  prompt: string;
  context_snapshot: string | null;
  status: "pending" | "replied" | "approved" | "denied" | "expired";
  operator_reply: string | null;
  operator_id: string | null;
  created_at: string;
  replied_at: string | null;
  expires_at: string | null;
  updated_at: string | null;
}

export interface ExecutionFailureSummary {
  session_id: number;
  summary: string;
  operator_feedback: string | null;
  generated_at: string | null;
  feedback_at: string | null;
  replan_planning_session_id: number | null;
  replan_planning_session_status?: string | null;
  replan_planning_session_title?: string | null;
  diagnostics?: FailureDiagnostics | null;
  message?: string;
}

export interface FailureDiagnostics {
  reason?: string;
  boundary?: string;
  failure_boundary?: string;
  failure_class?: string;
  operation?: string;
  op?: string;
  op_name?: string;
  structured_op?: string;
  target_path?: string;
  path?: string;
  outcome?: string;
  applied?: boolean;
  already_applied?: boolean;
  regex_fallback_applied?: boolean;
  workspace_guard_blocked?: boolean;
  ops?: Array<Record<string, unknown>>;
  operations?: Array<Record<string, unknown>>;
  replacement_ops?: Array<Record<string, unknown>>;
  failed_ops?: Array<Record<string, unknown>>;
  failed_op?: Record<string, unknown>;
  contract_violation_type?: string;
  validation_reasons?: string[];
  contract_violations?: string[];
  semantic_violation_codes?: string[];
  brittle_command_subcodes?: string[];
  brittle_command_step_details?: Record<string, string[]>;
  brittle_command_step_command_lengths?: Record<string, number[]>;
  max_command_length?: number;
  command_total_chars?: number;
  heredoc_command_count?: number;
  weak_verification_steps?: number[];
  missing_verification_steps?: number[];
  log_id?: number;
  level?: string;
  message?: string;
  created_at?: string | null;
  task_id?: number | null;
  task_execution_id?: number | null;
}

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
  name?: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string | null;
}

// API Response Types
export interface SortedLogsResponse {
  session_id: number;
  session_instance_id?: string | null;
  total_logs: number;
  returned_logs: number;
  offset?: number;
  limit?: number;
  sort_order: "asc" | "desc";
  deduplicated: boolean;
  logs: LogEntry[];
  has_more?: boolean;
}

export interface TaskSortedLogsResponse {
  task_id: number;
  total_logs: number;
  returned_logs: number;
  sort_order: "asc" | "desc";
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
  progress_score?: number;
  recommended?: boolean;
  resumable?: boolean;
  resume_reason?: string | null;
  restore_fidelity?: {
    score: number;
    status: "high" | "medium" | "low";
    summary: string;
    present_signals: string[];
    warnings: string[];
  };
}

export interface FailureEnvelopeSummary {
  schema_version: number;
  event_id?: string | null;
  event_type?: string | null;
  timestamp?: string | null;
  phase?: string | null;
  step_index?: number | null;
  model_id?: string | null;
  root_cause?: string | null;
  stderr_preview?: string | null;
  output_preview?: string | null;
  task_id?: number | null;
  task_title?: string | null;
}

export interface ReasoningArtifact {
  schema_version: number;
  intent: string;
  workspace_facts: string[];
  planned_actions: string[];
  verification_plan: string[];
}

export interface CheckpointInspection {
  session_id: number;
  checkpoint_name: string;
  created_at: string | null;
  current_step_index?: number;
  summary: {
    plan_step_count: number;
    completed_step_count: number;
    execution_result_count: number;
    status?: string | null;
    relaxed_mode: boolean;
    completion_repair_attempts: number;
  };
  context: {
    project_name?: string | null;
    task_subfolder?: string | null;
    project_dir_override?: string | null;
  };
  latest_validation?: Record<string, unknown> | null;
  latest_plan_validation?: Record<string, unknown> | null;
  latest_completion_validation?: Record<string, unknown> | null;
  runtime_metadata?: {
    backend: string;
    model_family: string;
    policy_profile: string;
    adaptation_profile: string;
    derived_from_current_settings: boolean;
  };
  reasoning_artifact?: ReasoningArtifact | null;
  validation_verdicts?: {
    latest_status?: string | null;
    plan_status?: string | null;
    completion_status?: string | null;
  };
  replay_source?: {
    requested_checkpoint_name?: string | null;
    resolved_checkpoint_name?: string | null;
    mode: string;
  };
  resume_readiness?: {
    resumable: boolean;
    resume_reason: string;
  };
  restore_fidelity?: {
    score: number;
    status: "high" | "medium" | "low";
    summary: string;
    present_signals: string[];
    warnings: string[];
  };
  dispatch_watchdog?: SessionDispatchWatchdogResponse | null;
  latest_failure?: FailureEnvelopeSummary | null;
  failure_history_preview?: FailureEnvelopeSummary[];
  validation_history: Record<string, unknown>[];
  plan_preview: Array<Record<string, unknown>>;
  step_results_preview: Array<Record<string, unknown>>;
}

export interface OrchestrationEvent {
  event_id?: string;
  timestamp: string;
  event_type: string;
  session_id: number;
  task_id: number;
  parent_event_id?: string | null;
  details: Record<string, unknown>;
}

export type SessionDecisionPhase =
  | "planning"
  | "validation"
  | "execution"
  | "failure"
  | "completion"
  | "system";

export type SessionDecisionSeverity = "info" | "warning" | "error" | string;

export interface SessionDecisionEvent {
  id: string;
  session_id: number;
  task_id: number | null;
  timestamp: string;
  phase: SessionDecisionPhase;
  event_type: string;
  decision_type: string;
  title: string;
  summary: string;
  status: string;
  severity: SessionDecisionSeverity;
  source: string;
  parent_event_id?: string | null;
  related_event_ids: string[];
  knowledge_usage_ids: string[];
  intervention_id?: number | null;
  details: Record<string, unknown>;
}

export interface SessionDecisionTimelineResponse {
  session_id: number;
  events: SessionDecisionEvent[];
  counts: Record<string, number>;
  truncated: boolean;
  limit: number;
}

export interface SessionStateDiffResponse {
  session_id: number;
  task_id: number | null;
  from_checkpoint: number | null;
  to_checkpoint: number | null;
  from_snapshot: Record<string, unknown> | null;
  to_snapshot: Record<string, unknown> | null;
  available?: boolean;
  delta: {
    current_step_index: { from: number; to: number; change: number };
    retry_budget_remaining: { from: number; to: number; change: number };
    completion_repair_attempts: { from: number; to: number; change: number };
    status: { from?: string | null; to?: string | null };
    plan_step_count: { from: number; to: number; change: number };
    validation_verdicts: {
      from_count: number;
      to_count: number;
      new_entries: Array<Record<string, unknown>>;
    };
    files_touched: {
      from_count: number;
      to_count: number;
      added: string[];
      removed: string[];
    };
    prompt_byte_estimate: { from: number; to: number; change: number };
    workspace_hash_changed: boolean;
  } | null;
}

export interface ReplayDeterminism {
  level: "strong" | "bounded" | "degraded" | "failed" | string;
  artifact_gaps: number;
  workspace_reconstructable: boolean;
  notes: string[];
}

export interface SessionReplayResponse {
  reducer_version: string;
  compatibility_version: string;
  session_id: number;
  task_id: number;
  boundary: Record<string, unknown>;
  state: {
    phase?: string | null;
    status?: string | null;
    current_step_index?: number | null;
    retry_count?: number | null;
    repair_count?: number | null;
    latest_checkpoint_name?: string | null;
    latest_failure_event_id?: string | null;
    workspace_evidence_status?: string | null;
    [key: string]: unknown;
  };
  field_classification?: {
    authoritative: string[];
    artifacts: string[];
  };
  integrity: {
    confidence: "high" | "medium" | "low" | "failed" | string;
    event_count_read: number;
    event_count_applied: number;
    malformed_line_count: number;
    unknown_event_types: string[];
    finding_count?: number;
    findings: Array<Record<string, unknown>>;
  };
  determinism: ReplayDeterminism;
  drift_findings: Array<Record<string, unknown>>;
  workspace_evidence: {
    status: string;
    [key: string]: unknown;
  };
  checkpoint_comparison?: Record<string, unknown> | null;
}

export interface SessionDivergenceCompareResponse {
  session_id: number;
  project_id: number;
  current: {
    session_id: number;
    session_name: string;
    status: string;
    created_at?: string | null;
    task_count: number;
    event_count: number;
    retry_count: number;
    tool_failure_count: number;
    intent_gap_count: number;
    divergence_count: number;
    divergence_reasons: string[];
    validation_statuses: string[];
    min_health_score?: number | null;
    anomaly_tags: string[];
  };
  matches: Array<{
    session_id: number;
    session_name: string;
    status: string;
    created_at?: string | null;
    retry_count: number;
    tool_failure_count: number;
    intent_gap_count: number;
    divergence_count: number;
    divergence_reasons: string[];
    anomaly_tags: string[];
    similarity_score: number;
    shared_tags: string[];
  }>;
}

export interface SessionDispatchWatchdogTask {
  task_id: number;
  task_title: string;
  dispatch_state: "queued" | "claimed" | "rejected" | "unknown";
  queued_at?: string | null;
  claimed_at?: string | null;
  rejected_at?: string | null;
  queue_age_seconds?: number | null;
  queue_latency_seconds?: number | null;
  queued_event_id?: string | null;
  claim_event_id?: string | null;
  reject_event_id?: string | null;
  stale: boolean;
  failure_root_cause?: string | null;
  latest_failure?: FailureEnvelopeSummary | null;
}

export interface SessionDispatchWatchdogResponse {
  session_id: number;
  sla_seconds: number;
  stale_task_count: number;
  has_stale_dispatches: boolean;
  latest_failure?: FailureEnvelopeSummary | null;
  failure_history_preview?: FailureEnvelopeSummary[];
  tasks: SessionDispatchWatchdogTask[];
  stale_tasks: SessionDispatchWatchdogTask[];
}

export interface BackendDescriptor {
  name: string;
  display_name: string;
  implementation: string;
  default_model_family: string;
  implemented: boolean;
  available: boolean;
  capabilities: Record<string, boolean | number | string | null>;
  config: {
    auth_mode: string;
    transport_mode: string;
    required_env_vars: string[];
    supported_prompt_format: string;
    streaming_mode: string;
    adaptation_profiles: string[];
  };
  health: {
    available: boolean;
    ready: boolean;
    status: string;
    errors: string[];
    warnings: string[];
  };
}

export interface PolicyProfile {
  name: string;
  display_name: string;
  description: string;
  validation_severity: string;
  completion_repair_budget: number;
  workspace_restore_mode: string;
  effects?: {
    planning_mode: string;
    validation_severity: string;
    completion_repair_budget: number;
    retry_mode: string;
    workspace_restore_mode: string;
    restore_behavior_label: string;
  };
}

export interface AppSettings {
  account: {
    email: string;
    name?: string | null;
  };
  system: {
    workspace_root: string;
    mobile_base_url: string;
    mobile_api_key_configured: boolean;
    mobile_api_key_preview?: string | null;
    mobile_api_key_source?: string | null;
    openclaw_gateway_url: string;
    agent_backend: string;
    agent_model_family: string;
    agent_adaptation_profile: string;
    backend_capabilities: Record<string, boolean | number | string | null>;
    backend_health: {
      available: boolean;
      ready: boolean;
      status: string;
      errors: string[];
      warnings: string[];
    };
    supported_backends: BackendDescriptor[];
    orchestration_policy_profile: string;
    workspace_review_policy:
      | "auto_publish_all"
      | "hold_nontrivial"
      | "hold_all"
      | string;
    available_policy_profiles: PolicyProfile[];
    available_adaptation_profiles: Array<{
      name: string;
      display_name: string;
      backend: string;
      model_family: string;
      prompt_format: string;
      description: string;
    }>;
  };
}

export interface SessionFilters {
  status?: string;
  is_active?: boolean;
  project_id?: number;
  skip?: number;
  limit?: number;
}

export interface TaskFilters {
  status?: TaskStatus;
  project_id?: number;
  search?: string;
}

export interface ProjectFilters {
  search?: string;
}

export type Log = LogEntry;
export interface LogFilters {
  level?: string;
  session_id?: number;
  task_id?: number;
}

export interface KnowledgeUsageEntry {
  knowledge_item_id: string;
  title: string;
  knowledge_type: string;
  confidence_avg: number;
  confidence_max: number;
  retrieval_reason: string;
  used_in_prompt: boolean;
  usage_count: number;
  first_used_at: string | null;
  last_used_at: string | null;
}

export interface KnowledgeUsageResponse {
  session_id: number;
  phases: Record<string, KnowledgeUsageEntry[]>;
}

export interface RecoveryTaskInfo {
  task_id: number;
  title: string;
  status: 'completed' | 'failed' | 'not_started' | 'running';
  files_changed: string[];
  repair_attempts: number;
  committed: boolean;
  validation_evidence?: ValidationEvidence | null;
}

export interface IntegrityFinding {
  code: string;
  message: string;
  path?: string | null;
  line?: number | null;
  severity?: string;
  confidence?: string;
}

export interface ValidationEvidence {
  command_quality?: string | null;
  command_quality_by_step?: Array<Record<string, unknown>>;
  integrity_findings?: IntegrityFinding[];
  semantic_violation_codes?: string[];
  requires_independent_evidence?: boolean;
  verification_insufficient?: boolean;
}

export interface RecoveryAction {
  label: string;
  action: string;
  task_id: number | null;
  variant: 'primary' | 'secondary' | 'danger';
}

export interface SessionRecoveryContext {
  session_id: number;
  session_name: string;
  session_status: string;
  stop_reasons: string[];
  stop_category: string;
  last_checkpoint_id: string | null;
  last_checkpoint_age_minutes: number | null;
  branch: string | null;
  tasks: RecoveryTaskInfo[];
  tasks_total: number;
  tasks_completed: number;
  tasks_failed: number;
  tasks_not_started: number;
  preserved: {
    completed_tasks_checkpointed: boolean;
    conversation_history_resumable: boolean;
    failed_task_rolled_back: boolean;
    remaining_plan_intact: boolean;
  };
  recommended_actions: RecoveryAction[];
  validation_evidence?: ValidationEvidence | null;
  source_note: string;
}

export interface SessionNarrativeTimelineEvent {
  id: string;
  at: string | null;
  phase: string;
  kind: 'milestone' | 'success' | 'warning' | 'failure' | 'checkpoint' | 'repair' | string;
  title: string;
  detail: string | null;
  task_id: number | null;
  token_cost: number | null;
  cause: string | null;
  metadata: Record<string, unknown>;
}

export interface SessionNarrativeTimelinePhase {
  phase: string;
  title: string;
  event_count: number;
  events: SessionNarrativeTimelineEvent[];
}

export interface SessionNarrativeTimeline {
  session_id: number;
  session_status: string;
  generated_at: string;
  phases: SessionNarrativeTimelinePhase[];
  event_count: number;
  source_note: string;
}

export interface SessionDigest {
  session_id: number;
  session_name: string;
  session_status: string;
  generated_at: string;
  summary: string;
  tasks_total: number;
  tasks_completed: number;
  tasks_failed: number;
  changed_files: string[];
  why_stopped: string;
  preserved: {
    completed_tasks_checkpointed: boolean;
    conversation_history_resumable: boolean;
    failed_task_rolled_back: boolean;
    remaining_plan_intact: boolean;
  };
  last_checkpoint_id: string | null;
  last_checkpoint_age_minutes: number | null;
  next_actions: string[];
  validation_evidence?: ValidationEvidence | null;
  command_quality?: string | null;
  integrity_findings?: IntegrityFinding[];
  verification_insufficient?: boolean;
  enriched?: boolean;
  enriched_text?: string | null;
  enrichment_error?: string | null;
  state_hash?: string | null;
}
