package com.user.repository

import com.user.data.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.withContext

/**
 * Single source of truth for all chat and developer tools data.
 */
class ChatRepository(
    private val chatDao: ChatDao,
    private val gitDao: GitConnectionDao,
    private val contextDao: ProjectContextDao,
    private val taskDao: TaskDao,
    private val prefs: PrefsManager
) {
    // ── Chat Messages ─────────────────────────────────────────
    fun getMessages(sessionId: String): Flow<List<ChatMessage>> =
        chatDao.getMessagesBySession(sessionId)

    suspend fun insertMessage(message: ChatMessage): Long =
        withContext(Dispatchers.IO) { chatDao.insertMessage(message) }

    // ── Chat Sessions ────────────────────────────────────────
    fun getSessions(): Flow<List<ChatSession>> =
        chatDao.getAllSessions()

    suspend fun insertSession(session: ChatSession) =
        withContext(Dispatchers.IO) { chatDao.insertSession(session) }

    suspend fun deleteSession(sessionId: String) =
        withContext(Dispatchers.IO) {
            chatDao.deleteMessagesBySession(sessionId)
            chatDao.deleteSession(sessionId)
        }

    // ── Project Context ───────────────────────────────────────
    suspend fun getCurrentContext(sessionId: String): ProjectContext? =
        withContext(Dispatchers.IO) { contextDao.getCurrentContext(sessionId) }

    suspend fun saveContext(context: ProjectContext) =
        withContext(Dispatchers.IO) { contextDao.insertContext(context) }

    suspend fun updateContext(context: ProjectContext) =
        withContext(Dispatchers.IO) { contextDao.updateContext(context) }

    fun getFilesByContext(contextId: String): Flow<List<ProjectFile>> =
        contextDao.getFilesByContext(contextId)

    suspend fun saveFile(file: ProjectFile) =
        withContext(Dispatchers.IO) { contextDao.insertFile(file) }

    suspend fun deleteFilesByContext(contextId: String) =
        withContext(Dispatchers.IO) { contextDao.deleteFilesByContext(contextId) }


    // ── Task Management ───────────────────────────────────────
    fun getTasksBySession(sessionId: String): Flow<List<Task>> =
        taskDao.getTasksBySession(sessionId)

    fun getPendingTasks(): Flow<List<Task>> =
        taskDao.getPendingTasks()

    fun getActiveTasks(): Flow<List<Task>> =
        taskDao.getActiveTasks()

    suspend fun getTask(taskId: String): Task? =
        withContext(Dispatchers.IO) { taskDao.getTask(taskId) }

    suspend fun approveTask(taskId: String): Task? {
        return withContext(Dispatchers.IO) {
            val task = taskDao.getTask(taskId) ?: return@withContext null
            taskDao.updateTaskStatus(taskId, TaskStatus.APPROVED)
            task
        }
    }

    suspend fun rejectTask(taskId: String, reason: String? = null) {
        withContext(Dispatchers.IO) {
            taskDao.updateTaskStatus(taskId, TaskStatus.REJECTED)
            if (!reason.isNullOrEmpty()) {
                // Store rejection reason in metadata or result field
                val task = taskDao.getTask(taskId)
                task?.let {
                    taskDao.updateTask(it.copy(error = reason))
                }
            }
        }
    }

    suspend fun startTask(taskId: String) {
        withContext(Dispatchers.IO) {
            taskDao.updateTaskStatus(taskId, TaskStatus.IN_PROGRESS)
        }
    }

    suspend fun completeTask(taskId: String, result: String) {
        withContext(Dispatchers.IO) {
            taskDao.updateTaskResult(taskId, result)
        }
    }

    suspend fun failTask(taskId: String, error: String) {
        withContext(Dispatchers.IO) {
            taskDao.updateTaskError(taskId, error)
        }
    }

    suspend fun getTaskStats(sessionId: String): TaskStats =
        withContext(Dispatchers.IO) { taskDao.getTaskStats(sessionId) }

}
