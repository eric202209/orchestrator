package com.user.data

import com.google.gson.annotations.SerializedName

/**
 * Recent activity item from Orchestrator API
 */
data class RecentActivity(
    @SerializedName("level")
    val level: String = "INFO",
    val message: String = "",
    val timestamp: String = "",
    @SerializedName("session_id")
    val sessionId: Int = 0
)

/**
 * Dashboard summary from Orchestrator API
 */
data class DashboardSummary(
    val projects: Int,
    val sessions: OrchestratorSessionStats,
    val tasks: OrchestratorTaskStats
)

data class DashboardResponse(
    val summary: DashboardSummary? = null,
    @SerializedName("recent_activity")
    val recentActivity: List<RecentActivity> = emptyList()
)

/**
 * Session statistics from Orchestrator API response
 *
 * Orchestrator returns: { "total": N, "active": N, "running": N }
 */
data class OrchestratorSessionStats(
    val total: Int = 0,
    @SerializedName("active")
    val active: Int = 0,
    // Support both 'running' and 'inProgress' field names
    @SerializedName("running")
    val running: Int = 0,
    @SerializedName("inProgress")
    val inProgress: Int = 0,
    // Support both 'completed' (legacy) and 'active' field names
    @SerializedName("completed")
    val completed: Int = 0
)

/**
 * Task statistics from Orchestrator API response
 *
 * Orchestrator returns: { "total": N, "done": N, "running": N, "failed": N, "completion_rate": "X.X%" }
 */
data class OrchestratorTaskStats(
    val total: Int = 0,
    val pending: Int = 0,
    // Support both 'inProgress' and 'running' field names from orchestrator
    @SerializedName("inProgress")
    val inProgress: Int = 0,
    @SerializedName("running")
    val running: Int = 0,
    val approved: Int = 0,
    val done: Int = 0,
    val failed: Int = 0,
    // Use snake_case as returned by Orchestrator API
    @SerializedName("completion_rate")
    val completionRate: String = "N/A"
)

/**
 * Project from Orchestrator API
 * Supports both 'id' (from orchestrator) and 'projectId' formats
 */
data class Project(
    // Support both field names that orchestrator might use
    @SerializedName("id")
    val id: String = "",
    @SerializedName("projectId")
    val _projectId: String = "",
    val name: String,
    val description: String? = null,
    @SerializedName("github_url")
    val githubUrl: String? = null,
    val branch: String = "main",
    val status: String = "unknown",
    @SerializedName("created_at")
    val createdAt: String = "",
    @SerializedName("updated_at")
    val updatedAt: String = ""
) {
    // Return id if set, otherwise projectId for backwards compatibility
    fun getProjectId(): String = if (id.isNotEmpty()) id else _projectId
}

data class MobileProjectsResponse(
    val projects: List<Project> = emptyList(),
    val total: Int = 0
)

/**
 * Task from Orchestrator API (internal model matching exact API response format)
 */
data class OrchestTaskResponse(
    @SerializedName("id")
    val id: Int,
    val title: String,
    val description: String? = null,
    val status: String,
    @SerializedName("project_id")
    val projectId: Int,
    @SerializedName("created_at")
    val createdAt: String,
    @SerializedName("updated_at")
    val updatedAt: String? = null,
    val priority: Int = 0
)

/**
 * Task from Orchestrator API (mobile app model - string IDs for compatibility)
 */
data class OrchestTask(
    val taskId: String,
    val title: String,
    val description: String? = null,
    val status: String,
    val projectId: String,
    val sessionId: String? = null,
    val createdAt: String,
    val updatedAt: String,
    val priority: Int = 0
)

/**
 * Response wrapper for /api/v1/mobile/projects/{projectId}/tasks endpoint
 */
data class ProjectTasksResponse(
    @SerializedName("project_id")
    val projectId: Int,
    val tasks: List<OrchestTaskResponse>,
    @SerializedName("total")
    val total: Int
)

/**
 * API response wrapper for Orchestrator API
 */
data class OrchestratorApiResponse<T>(
    val success: Boolean,
    val data: T? = null,
    val error: String? = null
)

/**
 * Response wrapper for project status endpoint
 */
data class ProjectStatusResponse(
    @SerializedName("project_id")
    val projectId: String = "",
    @SerializedName("project_name")
    val projectName: String = "",
    val description: String? = null,
    @SerializedName("active_sessions")
    val activeSessions: Int = 0,
    val tasks: TaskStatsResponse? = null,
    val sessions: List<ProjectSessionSummary> = emptyList()
)

data class ProjectSessionSummary(
    val id: Int = 0,
    val name: String = "",
    val status: String = "",
    @SerializedName("started_at")
    val startedAt: String? = null
)

data class ProjectTreeResponse(
    @SerializedName("project_id")
    val projectId: String = "",
    @SerializedName("project_name")
    val projectName: String = "",
    val root: String = "",
    val exists: Boolean = false,
    @SerializedName("tree_lines")
    val treeLines: List<String> = emptyList(),
    @SerializedName("total_entries_shown")
    val totalEntriesShown: Int = 0,
    val truncated: Boolean = false
)

/**
 * Task statistics for project status response
 */
data class TaskStatsResponse(
    val total: Int = 0,
    val pending: Int = 0,
    val running: Int = 0,
    val done: Int = 0,
    val failed: Int = 0
)
