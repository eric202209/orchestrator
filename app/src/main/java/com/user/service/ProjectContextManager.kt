package com.user.service

import com.user.data.ProjectContext
import com.user.data.ProjectFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * Manager for project context sent by OpenClaw gateway
 */
class ProjectContextManager(
    private val repository: com.user.repository.ChatRepository
) {
    /**
     * Get current project context
     */
    suspend fun getCurrentContext(sessionId: String): ProjectContext? =
        repository.getCurrentContext(sessionId)

    /**
     * Save project context from gateway
     */
    suspend fun saveContext(sessionId: String, projectPath: String, files: List<String>): ProjectContext =
        withContext(Dispatchers.IO) {
            val jsonFiles = JSONArray(files)
            val context = ProjectContext(
                id = "current",
                sessionId = sessionId,
                projectPath = projectPath,
                files = jsonFiles.toString(),
                lastUpdated = System.currentTimeMillis()
            )
            repository.saveContext(context)
            context
        }

    /**
     * Update project context
     */
    suspend fun updateContext(context: ProjectContext) = repository.updateContext(context)

    /**
     * Get project files
     */
    fun getFiles(contextId: String) = repository.getFilesByContext(contextId)

    /**
     * Parse files from JSON string
     */
    fun parseFiles(filesJson: String): List<String> {
        return try {
            val array = JSONArray(filesJson)
            val files = mutableListOf<String>()
            for (i in 0 until array.length()) {
                files.add(array.getString(i))
            }
            files
        } catch (e: Exception) {
            emptyList()
        }
    }

    /**
     * Save individual file info
     */
    suspend fun saveFile(contextId: String, path: String, language: String = ""): ProjectFile =
        withContext(Dispatchers.IO) {
            val file = ProjectFile(
                contextId = contextId,
                path = path,
                language = language,
                lastModified = System.currentTimeMillis()
            )
            repository.saveFile(file)
            file
        }

    /**
     * Clear all files for a context
     */
    suspend fun clearFiles(contextId: String) = repository.deleteFilesByContext(contextId)

    /**
     * Format context for chat display
     */
    fun formatContext(context: ProjectContext): String {
        val files = parseFiles(context.files)
        val sb = StringBuilder("### Project Context\n\n")
        sb.append("**Session:** ${context.sessionId}\n")
        sb.append("**Path:** ${context.projectPath}\n")
        sb.append("**Files:** ${files.size} files tracked\n\n")

        if (files.isNotEmpty()) {
            sb.append("**Files:**\n```\n")
            for (file in files.take(50)) {
                sb.append("  $file\n")
            }
            if (files.size > 50) {
                sb.append("  ... and ${files.size - 50} more\n")
            }
            sb.append("```\n")
        }

        return sb.toString()
    }

    /**
     * Get file list as string (for @context files command)
     */
    fun getFileList(context: ProjectContext): String {
        val files = parseFiles(context.files)
        return if (files.isEmpty()) "No files in context"
        else files.joinToString("\n") { "  - $it" }
    }

    companion object {
        const val TAG = "ProjectContextManager"
    }
}