package com.user.service

import android.util.Log
import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.reflect.TypeToken
import com.user.data.DashboardResponse
import com.user.data.DashboardSummary
import com.user.data.OrchestTask
import com.user.data.OrchestTaskResponse
import com.user.data.OrchestratorApiResponse
import com.user.data.PrefsManager
import com.user.data.Project
import com.user.data.ProjectStatusResponse
import com.user.data.ProjectTasksResponse
import com.user.data.TaskStatsResponse
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

/**
 * Orchestrator API client for fetching project and task statistics
 *
 * This client handles connection failures gracefully - all errors are logged silently
 * and returned as failure Results. The UI should handle these failures without showing
 * error messages to the user, since local data is always available as a fallback.
 *
 * Required configuration:
 * - orchestratorServerUrl: Base URL of Orchestrator backend (e.g., http://xxx.xx.x.x:8080)
 * - orchestratorApiKey: API key from Orchestrator dashboard/admin settings
 * - gatewayToken: Same token used for OpenClaw Gateway authentication
 */
class OrchestratorApiClient(
    private val prefs: PrefsManager,
    private val gatewayToken: String
) {
    private companion object {
        private const val TAG = "OrchestratorApiClient"
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .writeTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    private val gson = Gson()

    private fun getBaseUrl(): String {
        return prefs.orchestratorServerUrl.removeSuffix("/")
    }

    private fun getHeaders(): Map<String, String> {
        return mapOf(
            "Content-Type" to "application/json",
            "X-OpenClaw-API-Key" to prefs.orchestratorApiKey,
            "Authorization" to "Bearer $gatewayToken"
        )
    }

    /**
     * Fetch dashboard summary (project count, task stats, session stats)
     */
    suspend fun getDashboardSummary(): Result<DashboardSummary> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/api/v1/mobile/dashboard"
            Log.d(TAG, "Fetching dashboard summary from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Dashboard API failed: ${response.code} ${response.message}")
                    return@withContext Result.failure(
                        Exception("Dashboard API failed: ${response.code} ${response.message}")
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response
                val apiResponse = gson.fromJson(responseBody, DashboardResponse::class.java)

                if (apiResponse.summary != null) {
                    Log.d(TAG, "Successfully fetched dashboard summary: $apiResponse")
                    Result.success(apiResponse.summary)
                } else {
                    Log.w(TAG, "No summary data in response")
                    Result.failure(Exception("No summary data in response"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching dashboard summary: ${e.message}")
            Result.failure(e)
        }
    }

    /**
     * Fetch all projects
     */
    suspend fun getProjects(): Result<List<Project>> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/api/v1/mobile/projects"
            Log.d(TAG, "Fetching projects from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Projects API failed: ${response.code} ${response.message}")
                    return@withContext Result.failure(
                        Exception("Projects API failed: ${response.code} ${response.message}")
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")
                val type = object : TypeToken<OrchestratorApiResponse<List<Project>>>() {}.type
                val apiResponse = gson.fromJson<OrchestratorApiResponse<List<Project>>>(responseBody, type)

                if (apiResponse.success && apiResponse.data != null) {
                    Log.d(TAG, "Successfully fetched ${apiResponse.data.size} projects")
                    Result.success(apiResponse.data)
                } else {
                    Log.w(TAG, "Projects API error: ${apiResponse.error ?: "Unknown error"}")
                    Result.failure(Exception(apiResponse.error ?: "Unknown error"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching projects: ${e.message}")
            Result.failure(e)
        }
    }

    /**
     * Fetch tasks for a specific project
     */
    suspend fun getProjectTasks(projectId: String): Result<List<OrchestTask>> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/api/v1/mobile/projects/${projectId}/tasks"
            Log.d(TAG, "Fetching tasks for project $projectId from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Tasks API failed for project $projectId: ${response.code} ${response.message}")
                    return@withContext Result.failure(
                        Exception("Tasks API failed: ${response.code} ${response.message}")
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response - it's in format { "project_id": N, "tasks": [...], "total": N }
                val projectTasksResponse = gson.fromJson(responseBody, ProjectTasksResponse::class.java)

                if (projectTasksResponse.tasks != null) {
                    // Convert from OrchestTaskResponse to OrchestTask
                    val tasks = projectTasksResponse.tasks.map { it.toOrchestTask() }
                    Log.d(TAG, "Successfully fetched ${tasks.size} tasks for project $projectId")
                    Result.success(tasks)
                } else {
                    Log.w(TAG, "Tasks API error for project $projectId: No tasks in response")
                    Result.failure(Exception("No tasks in response"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching tasks for project $projectId: ${e.message}")
            Result.failure(e)
        }
    }

    /**
     * Extension function to convert OrchestTaskResponse to OrchestTask
     */
    private fun OrchestTaskResponse.toOrchestTask(): OrchestTask {
        return OrchestTask(
            taskId = this.id.toString(),
            title = this.title,
            description = this.description,
            status = this.status.lowercase(),  // Normalize to lowercase
            projectId = this.projectId.toString(),
            sessionId = null,
            createdAt = this.createdAt,
            updatedAt = this.updatedAt ?: this.createdAt,
            priority = this.priority
        )
    }

    /**
     * Get project status including task counts - called by mobile app for project progress display
     */
    suspend fun getProjectStatus(projectId: String): Result<ProjectStatusResponse> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/mobile/projects/${projectId}/status"
            Log.d(TAG, "Fetching status for project $projectId from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Project status API failed for $projectId: ${response.code} ${response.message}")
                    return@withContext Result.failure(
                        Exception("Project status API failed: ${response.code} ${response.message}")
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response - it's not wrapped in success/error, direct object
                val jsonObject = gson.fromJson(responseBody, JsonObject::class.java)

                if (jsonObject.has("project_id")) {
                    val taskStatsJson = jsonObject.getAsJsonObject("tasks")
                    val taskStats = TaskStatsResponse(
                        total = taskStatsJson.get("total")?.asInt ?: 0,
                        pending = taskStatsJson.get("pending")?.asInt ?: 0,
                        running = taskStatsJson.get("running")?.asInt ?: 0,
                        done = taskStatsJson.get("done")?.asInt ?: 0,
                        failed = taskStatsJson.get("failed")?.asInt ?: 0
                    )

                    Log.d(TAG, "Successfully fetched status for project $projectId: $taskStats")
                    Result.success(ProjectStatusResponse(
                        projectId = jsonObject.get("project_id").asInt.toString(),
                        projectName = jsonObject.get("project_name").asString,
                        description = if (jsonObject.has("description")) jsonObject.get("description").asString else null,
                        activeSessions = jsonObject.get("active_sessions")?.asInt ?: 0,
                        tasks = taskStats
                    ))
                } else {
                    Log.w(TAG, "Unexpected response format for project $projectId - missing project_id")
                    Result.failure(Exception("Unexpected response format - missing project_id"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching status for project $projectId: ${e.message}")
            Result.failure(e)
        }
    }

    /**
     * Fetch tasks filtered by status
     */
    suspend fun getTasksByStatus(projectId: String, status: String): Result<List<OrchestTask>> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/api/v1/mobile/projects/${projectId}/tasks?status=$status"
            Log.d(TAG, "Fetching filtered tasks for project $projectId with status $status")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Tasks API failed for project $projectId (status=$status): ${response.code} ${response.message}")
                    return@withContext Result.failure(
                        Exception("Tasks API failed: ${response.code} ${response.message}")
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response - it's in format { "project_id": N, "tasks": [...], "total": N }
                val projectTasksResponse = gson.fromJson(responseBody, ProjectTasksResponse::class.java)

                if (projectTasksResponse.tasks != null) {
                    // Convert from OrchestTaskResponse to OrchestTask and filter by status
                    val tasks = projectTasksResponse.tasks
                        .map { it.toOrchestTask() }
                        .filter { it.status == status.lowercase() }
                    Log.d(TAG, "Successfully fetched ${tasks.size} tasks for project $projectId (status=$status)")
                    Result.success(tasks)
                } else {
                    Log.w(TAG, "Tasks API error for project $projectId: No tasks in response")
                    Result.failure(Exception("No tasks in response"))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching filtered tasks for project $projectId: ${e.message}")
            Result.failure(e)
        }
    }

    private fun buildHeadersArray(): Array<String> {
        val headers = getHeaders()
        return headers.flatMap { (key, value) -> listOf(key, value) }.toTypedArray()
    }

    /**
     * Test connection to Orchestrator API
     */
    suspend fun testConnection(): Result<Boolean> = withContext(Dispatchers.IO) {
        try {
            val url = "${getBaseUrl()}/api/v1/mobile/dashboard"
            Log.d(TAG, "Testing connection to Orchestrator: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                val success = response.isSuccessful
                Log.d(TAG, "Connection test result: ${if (success) "SUCCESS" else "FAILED (${response.code})"}")
                Result.success(success)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Connection test failed: ${e.message}")
            Result.failure(e)
        }
    }
}
