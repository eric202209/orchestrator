package com.user.service

import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import com.user.data.DashboardResponse
import com.user.data.DashboardSummary
import com.user.data.RecentActivity
import com.user.data.OrchestTask
import com.user.data.OrchestTaskResponse
import com.user.data.MobileProjectsResponse
import com.user.data.OrchestratorApiResponse
import com.user.data.PrefsManager
import com.user.data.Project
import com.user.data.ProjectTreeResponse
import com.user.data.ProjectStatusResponse
import com.user.data.ProjectTasksResponse
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.net.SocketTimeoutException

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
    private val gatewayToken: String,
    private val overrideServerUrl: String? = null,
    private val overrideApiKey: String? = null
) {
    private companion object {
        private const val TAG = "OrchestratorApiClient"
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .writeTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        // Add network interceptor to log all requests/responses for debugging
        .addNetworkInterceptor { chain ->
            val request = chain.request()
            Log.d(TAG, "=== NETWORK REQUEST ===")
            Log.d(TAG, "URL: ${request.url}")
            Log.d(TAG, "Method: ${request.method}")
            Log.d(TAG, "Headers:")
            request.headers.forEach { (name, value) ->
                Log.d(TAG, "  $name: $value")
            }

            val response = chain.proceed(request)
            Log.d(TAG, "Response Status: ${response.code} ${response.message}")
            Log.d(TAG, "Response Headers:")
            response.headers.forEach { (name, value) ->
                Log.d(TAG, "  $name: $value")
            }
            Log.d(TAG, "========================")

            response
        }
        .build()

    private val gson = Gson()

    private fun getBaseUrl(): String {
        val rawBaseUrl = (overrideServerUrl ?: prefs.orchestratorServerUrl).trim().trimEnd('/')
        return when {
            rawBaseUrl.endsWith("/api/v1") -> rawBaseUrl.removeSuffix("/api/v1")
            rawBaseUrl.endsWith("/mobile") -> rawBaseUrl.removeSuffix("/mobile")
            else -> rawBaseUrl
        }
    }

    private fun buildMobileUrl(path: String): String {
        val normalizedPath = path.trimStart('/')
        return "${getBaseUrl()}/api/v1/mobile/$normalizedPath"
    }

    private fun <T> buildFailure(message: String, exception: Exception? = null): Result<T> {
        val rootCause = exception?.message?.takeIf { it.isNotBlank() }
        val detail = when (exception) {
            is SocketTimeoutException -> "Request timed out. Check if Orchestrator is reachable."
            is IOException -> "Unable to reach Orchestrator. Check the server URL, network, and backend status."
            else -> rootCause
        }

        val fullMessage = if (detail != null && detail != message) {
            "$message $detail"
        } else {
            message
        }

        return Result.failure(Exception(fullMessage, exception))
    }

    private fun getHeaders(): Map<String, String> {
        return mapOf(
            "Content-Type" to "application/json",
            "X-OpenClaw-API-Key" to (overrideApiKey ?: prefs.orchestratorApiKey),
            "Authorization" to "Bearer $gatewayToken"
        )
    }

    /**
     * Fetch full dashboard payload including recent activity.
     */
    suspend fun getDashboard(): Result<DashboardResponse> = withContext(Dispatchers.IO) {
        try {
            val url = buildMobileUrl("dashboard")
            Log.d(TAG, "Fetching full dashboard payload from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Dashboard API failed: ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Dashboard API failed (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")
                val apiResponse = gson.fromJson(responseBody, DashboardResponse::class.java)

                if (apiResponse.summary != null) {
                    Result.success(apiResponse)
                } else {
                    buildFailure("Dashboard response did not include summary data.")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching full dashboard payload: ${e.message}")
            buildFailure("Failed to load Orchestrator dashboard.", e)
        }
    }

    /**
     * Fetch dashboard summary (project count, task stats, session stats)
     */
    suspend fun getDashboardSummary(): Result<DashboardSummary> = withContext(Dispatchers.IO) {
        getDashboard().fold(
            onSuccess = { dashboard ->
                Result.success(dashboard.summary!!)
            },
            onFailure = { error ->
                Result.failure(error)
            }
        )
    }

    /**
     * Fetch all projects
     */
    suspend fun getProjects(): Result<List<Project>> = withContext(Dispatchers.IO) {
        try {
            val url = buildMobileUrl("projects")
            Log.d(TAG, "Fetching projects from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Projects API failed: ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Projects API failed (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Debug: log the raw response body to see what we got
                Log.d(TAG, "Projects API raw response: $responseBody")

                val mobileProjects = runCatching {
                    gson.fromJson(responseBody, MobileProjectsResponse::class.java)
                }.getOrNull()

                if (mobileProjects?.projects != null) {
                    Log.d(TAG, "Successfully fetched ${mobileProjects.projects.size} projects from mobile endpoint")
                    return@withContext Result.success(mobileProjects.projects)
                }

                val type = object : TypeToken<OrchestratorApiResponse<List<Project>>>() {}.type
                val apiResponse = gson.fromJson<OrchestratorApiResponse<List<Project>>>(responseBody, type)

                if (apiResponse.success && apiResponse.data != null) {
                    Log.d(TAG, "Successfully fetched ${apiResponse.data.size} projects")
                    Result.success(apiResponse.data)
                } else {
                    Log.w(TAG, "Projects API error: success=${apiResponse.success}, data=${apiResponse.data}, error=${apiResponse.error}")
                    buildFailure(apiResponse.error ?: "Projects response was missing usable data.")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching projects: ${e.message}")
            buildFailure("Failed to load Orchestrator projects.", e)
        }
    }

    /**
     * Fetch tasks for a specific project
     */
    suspend fun getProjectTasks(projectId: String): Result<List<OrchestTask>> = withContext(Dispatchers.IO) {
        try {
            val url = buildMobileUrl("projects/${projectId}/tasks")
            Log.d(TAG, "Fetching tasks for project $projectId from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Tasks API failed for project $projectId: ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Tasks API failed for project $projectId (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response - it's in format { "project_id": N, "tasks": [...], "total": N }
                val projectTasksResponse = gson.fromJson(responseBody, ProjectTasksResponse::class.java)

                // Convert from OrchestTaskResponse to OrchestTask
                val tasks = projectTasksResponse.tasks.map { it.toOrchestTask() }
                Log.d(TAG, "Successfully fetched ${tasks.size} tasks for project $projectId")
                Result.success(tasks)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching tasks for project $projectId: ${e.message}")
            buildFailure("Failed to load tasks for project $projectId.", e)
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
            val url = buildMobileUrl("projects/${projectId}/status")
            Log.d(TAG, "Fetching status for project $projectId from: $url [project-status-v1]")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Project status API failed for $projectId: ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Project status API failed for $projectId (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")
                Log.d(TAG, "Project status raw response for $projectId: $responseBody")
                val parsed = gson.fromJson(responseBody, ProjectStatusResponse::class.java)

                if (parsed.projectId.isBlank()) {
                    Log.w(TAG, "Unexpected response format for project $projectId - missing project_id")
                    return@withContext buildFailure("Project status response for $projectId was missing project_id.")
                }

                val normalized = parsed.copy(
                    projectId = parsed.projectId.ifBlank { projectId },
                    projectName = parsed.projectName.ifBlank { "Project" },
                    sessions = parsed.sessions.filter { it.id != 0 || it.name.isNotBlank() || it.status.isNotBlank() }
                )

                Log.d(TAG, "Successfully fetched status for project $projectId: ${normalized.tasks}")
                Result.success(normalized)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching status for project $projectId [project-status-v1]: ${e.message}")
            buildFailure("Failed to load project status for $projectId [project-status-v1].", e)
        }
    }

    /**
     * Fetch a compact file tree for a project.
     */
    suspend fun getProjectTree(projectId: String): Result<ProjectTreeResponse> = withContext(Dispatchers.IO) {
        try {
            val url = buildMobileUrl("projects/${projectId}/tree")
            Log.d(TAG, "Fetching file tree for project $projectId from: $url")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Project tree API failed for $projectId: ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Project tree API failed for $projectId (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")
                val projectTree = gson.fromJson(responseBody, ProjectTreeResponse::class.java)
                Result.success(projectTree)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching file tree for project $projectId: ${e.message}")
            buildFailure("Failed to load file tree for project $projectId.", e)
        }
    }

    /**
     * Fetch tasks filtered by status
     */
    @Suppress("unused")
    suspend fun getTasksByStatus(projectId: String, status: String): Result<List<OrchestTask>> = withContext(Dispatchers.IO) {
        try {
            val url = buildMobileUrl("projects/${projectId}/tasks?status=$status")
            Log.d(TAG, "Fetching filtered tasks for project $projectId with status $status")

            val request = Request.Builder()
                .url(url)
                .headers(okhttp3.Headers.headersOf(*buildHeadersArray()))
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    Log.w(TAG, "Tasks API failed for project $projectId (status=$status): ${response.code} ${response.message}")
                    return@withContext buildFailure(
                        "Filtered tasks API failed for project $projectId (${response.code} ${response.message})."
                    )
                }

                val responseBody = response.body?.string() ?: throw Exception("Empty response")

                // Parse the response - it's in format { "project_id": N, "tasks": [...], "total": N }
                val projectTasksResponse = gson.fromJson(responseBody, ProjectTasksResponse::class.java)

                // Convert from OrchestTaskResponse to OrchestTask and filter by status
                val tasks = projectTasksResponse.tasks
                    .map { it.toOrchestTask() }
                    .filter { it.status == status.lowercase() }
                Log.d(TAG, "Successfully fetched ${tasks.size} tasks for project $projectId (status=$status)")
                Result.success(tasks)
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error fetching filtered tasks for project $projectId: ${e.message}")
            buildFailure("Failed to load filtered tasks for project $projectId.", e)
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
            val url = buildMobileUrl("dashboard")
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
            buildFailure("Orchestrator connection test failed.", e)
        }
    }
}