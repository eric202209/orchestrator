package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey
import androidx.room.TypeConverters

/**
 * Task entity representing a task assigned by OpenClaw Gateway
 * Tasks can be in various states: PENDING, IN_PROGRESS, COMPLETED, FAILED, REJECTED
 */
@Entity(tableName = "tasks")
@TypeConverters(TaskConverter::class)
data class Task(
    @PrimaryKey
    val taskId: String,              // Unique task ID from Gateway
    val sessionId: String,            // Associated chat session
    val title: String,                // Brief task title
    val description: String,          // Detailed task description
    val status: TaskStatus,           // Current task status
    val priority: Int = 0,            // Priority level (higher = more important)
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    val dueDate: Long? = null,        // Optional deadline
    val parentTaskId: String? = null, // For subtasks
    val assignedTo: String? = null,   // Agent/worker identifier
    val dependencies: List<String> = emptyList(), // Parent task IDs
    val result: String? = null,       // Task result/output
    val error: String? = null,        // Error message if failed
    val metadata: String? = null      // JSON metadata
)

/**
 * Task status enumeration
 */
enum class TaskStatus {
    PENDING,        // Waiting for approval or assignment
    APPROVED,       // Approved by user
    REJECTED,       // Rejected by user
    IN_PROGRESS,    // Currently being executed
    COMPLETED,      // Successfully completed
    FAILED,         // Execution failed
    TIMEOUT         // Task exceeded time limit
}