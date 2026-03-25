package com.user.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Task Request Protocol
 * Defines the structure of task requests sent from Gateway to Mobile
 *
 * This protocol is used when OpenClaw Gateway wants to assign a task
 * to the mobile device for execution or approval.
 */
@Serializable
data class TaskRequest(
    @SerialName("type")
    val type: String = "task_request",

    @SerialName("request_id")
    val requestId: String,

    @SerialName("task")
    val task: TaskInfo,

    @SerialName("timestamp")
    val timestamp: Long = System.currentTimeMillis(),

    @SerialName("priority")
    val priority: Int = 0,

    @SerialName("timeout_seconds")
    val timeoutSeconds: Int? = null,

    @SerialName("requires_approval")
    val requiresApproval: Boolean = true,

    @SerialName("context")
    val context: TaskContext? = null
)

/**
 * Task Information - Core task details
 */
@Serializable
data class TaskInfo(
    @SerialName("id")
    val id: String,

    @SerialName("title")
    val title: String,

    @SerialName("description")
    val description: String,

    @SerialName("type")
    val type: TaskType,

    @SerialName("priority")
    val priority: Int = 0,

    @SerialName("estimated_duration_seconds")
    val estimatedDurationSeconds: Int? = null,

    @SerialName("dependencies")
    val dependencies: List<String> = emptyList(),

    @SerialName("metadata")
    val metadata: Map<String, String> = emptyMap()
)

/**
 * Task Context - Additional context for the task
 */
@Serializable
data class TaskContext(
    @SerialName("project_path")
    val projectPath: String? = null,

    @SerialName("working_directory")
    val workingDirectory: String? = null,

    @SerialName("environment_variables")
    val environmentVariables: Map<String, String> = emptyMap(),

    @SerialName("available_tools")
    val availableTools: List<String> = emptyList(),

    @SerialName("constraints")
    val constraints: List<String> = emptyList()
)

/**
 * Task Type enumeration
 */
@Serializable
enum class TaskType {
    @SerialName("code_execution")
    CODE_EXECUTION,

    @SerialName("file_operation")
    FILE_OPERATION,

    @SerialName("git_operation")
    GIT_OPERATION,

    @SerialName("network_request")
    NETWORK_REQUEST,

    @SerialName("data_query")
    DATA_QUERY,

    @SerialName("system_command")
    SYSTEM_COMMAND,

    @SerialName("documentation")
    DOCUMENTATION,

    @SerialName("approval_required")
    APPROVAL_REQUIRED,

    @SerialName("custom")
    CUSTOM
}

/**
 * Companion object for creating TaskRequest from Task entity
 */
object TaskRequestSerializer {
    /**
     * Convert Task entity to TaskRequest
     */
    fun taskToRequest(task: Task, requestId: String): TaskRequest {
        return TaskRequest(
            requestId = requestId,
            task = TaskInfo(
                id = task.taskId,
                title = task.title,
                description = task.description,
                type = TaskType.valueOf(task.status.name),
                priority = task.priority,
                dependencies = task.dependencies,
                metadata = parseMetadata(task.metadata)
            ),
            priority = task.priority,
            requiresApproval = task.status == TaskStatus.PENDING,
            context = TaskContext(
                projectPath = null, // Could be extracted from metadata
                environmentVariables = emptyMap()
            )
        )
    }

    /**
     * Parse metadata JSON string to Map
     */
    private fun parseMetadata(metadataJson: String?): Map<String, String> {
        return try {
            if (metadataJson.isNullOrEmpty()) return emptyMap()
            // Simple JSON parsing - could use Gson/Kotlinx.serialization
            val json = metadataJson.removePrefix("{").removeSuffix("}")
            return json.split(",").map { it.split(":") }
                .filter { it.size == 2 }
                .map { it[0].trim() to it[1].trim().removePrefix("\"").removeSuffix("\"") }
                .toMap()
        } catch (e: Exception) {
            emptyMap()
        }
    }
}