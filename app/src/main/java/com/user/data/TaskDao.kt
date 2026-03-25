package com.user.data

import androidx.room.*
import kotlinx.coroutines.flow.Flow

/**
 * Data Access Object for Task entities
 */
@Dao
interface TaskDao {

    /**
     * Get all tasks for a specific session
     */
    @Query("SELECT * FROM tasks WHERE sessionId = :sessionId ORDER BY createdAt DESC")
    fun getTasksBySession(sessionId: String): Flow<List<Task>>

    /**
     * Get all pending tasks
     */
    @Query("SELECT * FROM tasks WHERE status = 'PENDING' ORDER BY priority DESC, createdAt ASC")
    fun getPendingTasks(): Flow<List<Task>>

    /**
     * Get all approved but not yet completed tasks
     */
    @Query("SELECT * FROM tasks WHERE status IN ('APPROVED', 'IN_PROGRESS') ORDER BY priority DESC, createdAt ASC")
    fun getActiveTasks(): Flow<List<Task>>

    /**
     * Get a specific task by ID
     */
    @Query("SELECT * FROM tasks WHERE taskId = :taskId")
    suspend fun getTask(taskId: String): Task?

    /**
     * Get pending tasks that can be approved
     */
    @Query("SELECT * FROM tasks WHERE status = 'PENDING' AND parentTaskId IS NULL ORDER BY priority DESC, createdAt ASC")
    suspend fun getPendingTasksForApproval(): List<Task>

    /**
     * Insert a new task
     */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertTask(task: Task): Long

    /**
     * Insert multiple tasks
     */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertTasks(tasks: List<Task>)

    /**
     * Update an existing task
     */
    @Update
    suspend fun updateTask(task: Task)

    /**
     * Delete a task
     */
    @Delete
    suspend fun deleteTask(task: Task)

    /**
     * Delete all tasks for a session
     */
    @Query("DELETE FROM tasks WHERE sessionId = :sessionId")
    suspend fun deleteTasksBySession(sessionId: String)

    /**
     * Update task status
     */
    @Query("UPDATE tasks SET status = :status, updatedAt = :updatedAt WHERE taskId = :taskId")
    suspend fun updateTaskStatus(taskId: String, status: TaskStatus, updatedAt: Long = System.currentTimeMillis())

    /**
     * Update task result
     */
    @Query("UPDATE tasks SET result = :result, status = 'COMPLETED', updatedAt = :updatedAt WHERE taskId = :taskId")
    suspend fun updateTaskResult(taskId: String, result: String, updatedAt: Long = System.currentTimeMillis())

    /**
     * Update task error
     */
    @Query("UPDATE tasks SET error = :error, status = 'FAILED', updatedAt = :updatedAt WHERE taskId = :taskId")
    suspend fun updateTaskError(taskId: String, error: String, updatedAt: Long = System.currentTimeMillis())

    /**
     * Get task count by status
     */
    @Query("SELECT COUNT(*) FROM tasks WHERE sessionId = :sessionId AND status = :status")
    suspend fun getTaskCountBySessionAndStatus(sessionId: String, status: TaskStatus): Int

    /**
     * Get summary statistics for a session
     */
    @Query("""
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN status = 'PENDING' THEN 1 END) as pending,
            COUNT(CASE WHEN status = 'APPROVED' THEN 1 END) as approved,
            COUNT(CASE WHEN status = 'IN_PROGRESS' THEN 1 END) as inProgress,
            COUNT(CASE WHEN status = 'COMPLETED' THEN 1 END) as completed,
            COUNT(CASE WHEN status = 'FAILED' THEN 1 END) as failed
        FROM tasks WHERE sessionId = :sessionId
    """)
    suspend fun getTaskStats(sessionId: String): TaskStats
}

/**
 * Helper data class for task statistics
 */
data class TaskStats(
    val total: Int = 0,
    val pending: Int = 0,
    val approved: Int = 0,
    val inProgress: Int = 0,
    val completed: Int = 0,
    val failed: Int = 0
)