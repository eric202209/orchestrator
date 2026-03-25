package com.user.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Task Response Protocol
 * Defines the structure of responses sent from Mobile to Gateway
 *
 * This protocol is used when the mobile device responds to a task request
 * with approval, rejection, or completion status.
 */
@Serializable
data class TaskResponse(
    @SerialName("type")
    val type: String = "task_response",

    @SerialName("request_id")
    val requestId: String,

    @SerialName("task_id")
    val taskId: String,

    @SerialName("status")
    val status: TaskResponseStatus,

    @SerialName("timestamp")
    val timestamp: Long = System.currentTimeMillis(),

    @SerialName("result")
    val result: String? = null,

    @SerialName("error")
    val error: String? = null,

    @SerialName("output")
    val output: String? = null,

    @SerialName("metadata")
    val metadata: Map<String, String> = emptyMap()
)

/**
 * Task Response Status - Status of task execution/approval
 */
@Serializable
enum class TaskResponseStatus {
    @SerialName("approved")
    APPROVED,

    @SerialName("rejected")
    REJECTED,

    @SerialName("in_progress")
    IN_PROGRESS,

    @SerialName("completed")
    COMPLETED,

    @SerialName("failed")
    FAILED,

    @SerialName("timeout")
    TIMEOUT
}

/**
 * Task Approval Response - Specific response for approval requests
 */
@Serializable
data class TaskApprovalResponse(
    @SerialName("type")
    val type: String = "task_approval",

    @SerialName("request_id")
    val requestId: String,

    @SerialName("task_id")
    val taskId: String,

    @SerialName("approved")
    val approved: Boolean,

    @SerialName("timestamp")
    val timestamp: Long = System.currentTimeMillis(),

    @SerialName("reason")
    val reason: String? = null,

    @SerialName("modifications")
    val modifications: List<TaskModification>? = null
)

/**
 * Task Modification - Suggested changes to a task
 */
@Serializable
data class TaskModification(
    @SerialName("field")
    val field: String,

    @SerialName("original_value")
    val originalValue: String,

    @SerialName("suggested_value")
    val suggestedValue: String
)

/**
 * Task Execution Response - Response after task execution
 */
@Serializable
data class TaskExecutionResponse(
    @SerialName("type")
    val type: String = "task_execution",

    @SerialName("request_id")
    val requestId: String,

    @SerialName("task_id")
    val taskId: String,

    @SerialName("status")
    val status: TaskExecutionStatus,

    @SerialName("timestamp")
    val timestamp: Long = System.currentTimeMillis(),

    @SerialName("output")
    val output: String? = null,

    @SerialName("error")
    val error: String? = null,

    @SerialName("execution_time_ms")
    val executionTimeMs: Long? = null,

    @SerialName("resources_used")
    val resourcesUsed: ExecutionResources? = null,

    @SerialName("files_modified")
    val filesModified: List<FileModification>? = null
)

/**
 * Task Execution Status
 */
@Serializable
enum class TaskExecutionStatus {
    @SerialName("success")
    SUCCESS,

    @SerialName("partial_success")
    PARTIAL_SUCCESS,

    @SerialName("failed")
    FAILED,

    @SerialName("cancelled")
    CANCELLED
}

/**
 * Execution Resources - Resource usage during execution
 */
@Serializable
data class ExecutionResources(
    @SerialName("cpu_percent")
    val cpuPercent: Double? = null,

    @SerialName("memory_mb")
    val memoryMb: Double? = null,

    @SerialName("network_bytes")
    val networkBytes: Long? = null,

    @SerialName("disk_io_bytes")
    val diskIoBytes: Long? = null
)

/**
 * File Modification - Files changed during task execution
 */
@Serializable
data class FileModification(
    @SerialName("path")
    val path: String,

    @SerialName("operation")
    val operation: FileOperation,

    @SerialName("size_bytes")
    val sizeBytes: Long? = null
)

/**
 * File Operation type
 */
@Serializable
enum class FileOperation {
    @SerialName("created")
    CREATED,

    @SerialName("modified")
    MODIFIED,

    @SerialName("deleted")
    DELETED,

    @SerialName("copied")
    COPIED,

    @SerialName("moved")
    MOVED
}

/**
 * Companion object for creating TaskResponse from Task entity
 */
object TaskResponseSerializer {
    /**
     * Convert Task entity to TaskResponse
     */
    fun taskToResponse(task: Task, requestId: String): TaskResponse {
        return TaskResponse(
            requestId = requestId,
            taskId = task.taskId,
            status = when (task.status) {
                TaskStatus.APPROVED -> TaskResponseStatus.APPROVED
                TaskStatus.REJECTED -> TaskResponseStatus.REJECTED
                TaskStatus.IN_PROGRESS -> TaskResponseStatus.IN_PROGRESS
                TaskStatus.COMPLETED -> TaskResponseStatus.COMPLETED
                TaskStatus.FAILED -> TaskResponseStatus.FAILED
                TaskStatus.TIMEOUT -> TaskResponseStatus.TIMEOUT
                TaskStatus.PENDING -> TaskResponseStatus.APPROVED // Default to approved
            },
            result = task.result,
            error = task.error,
            output = task.result,
            metadata = task.metadata?.let { parseMetadata(it) } ?: emptyMap()
        )
    }

    /**
     * Convert TaskApprovalResponse to Task status update
     */
    fun approvalToStatus(approval: TaskApprovalResponse): Pair<TaskStatus, String?> {
        return if (approval.approved) {
            Pair(TaskStatus.APPROVED, approval.reason)
        } else {
            Pair(TaskStatus.REJECTED, approval.reason ?: "User rejected the task")
        }
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