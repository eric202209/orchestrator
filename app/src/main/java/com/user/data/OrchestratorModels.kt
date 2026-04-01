package com.user.data

import com.google.gson.annotations.SerializedName

/**
 * Recent activity item from Orchestrator API
 */
data class RecentActivity(
    val id: String,
    val title: String,
    val description: String,
    val type: String,
    val timestamp: String
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
    @SerializedName("recentActivity")
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
    val status: String = "unknown",
    val createdAt: String = "",
    val updatedAt: String = ""
) {
    // Return id if set, otherwise projectId for backwards compatibility
    fun getProjectId(): String = if (id.isNotEmpty()) id else _projectId
}

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
) {
    companion object {
        /**
         * Convert from OrchestTaskResponse (API response model with int IDs) to OrchestTask
         */
        fun fromResponse(response: OrchestTaskResponse): OrchestTask {
            return OrchestTask(
                taskId = response.id.toString(),
                title = response.title,
                description = response.description,
                status = response.status,
                projectId = response.projectId.toString(),
                sessionId = null,
                createdAt = response.createdAt,
                updatedAt = response.updatedAt ?: response.createdAt,
                priority = response.priority
            )
        }
    }
}

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
    val projectId: String,
    val projectName: String,
    val description: String? = null,
    val activeSessions: Int = 0,
    val tasks: TaskStatsResponse? = null
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
