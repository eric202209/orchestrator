package com.user.service

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import org.json.JSONObject
import java.io.IOException

/**
 * GitHub integration for linking PRs, issues, and commits to chats
 */
class GitHubIntegrationManager(
    private val githubToken: String,
    private val githubApiUrl: String = "https://api.github.com"
) {
    private val client = OkHttpClient()

    /**
     * GitHub repository info
     */
    data class RepoInfo(
        val full_name: String,
        val description: String?,
        val html_url: String,
        val language: String?,
        val stars: Int,
        val forks: Int
    )

    /**
     * GitHub Pull Request
     */
    data class PullRequest(
        val number: Int,
        val title: String,
        val state: String,
        val html_url: String,
        val user: String,
        val created_at: String,
        val additions: Int,
        val deletions: Int,
        val draft: Boolean
    )

    /**
     * GitHub Issue
     */
    data class Issue(
        val number: Int,
        val title: String,
        val state: String,
        val html_url: String,
        val user: String,
        val created_at: String,
        val body: String?
    )

    /**
     * GitHub Commit
     */
    data class Commit(
        val sha: String,
        val message: String,
        val html_url: String,
        val author: String?,
        val committed_at: String,
        val additions: Int,
        val deletions: Int
    )

    /**
     * Check if GitHub is configured
     */
    fun isConfigured(): Boolean = githubToken.isNotBlank()

    /**
     * Get repositories from GitHub
     */
    suspend fun getRepositories(owner: String? = null): List<RepoInfo> = withContext(Dispatchers.IO) {
        try {
            val targetOwner = owner ?: getAuthenticatedUser()
            val url = if (owner != null) {
                "$githubApiUrl/users/$owner/repos"
            } else {
                "$githubApiUrl/user/repos"
            }

            val request = Request.Builder()
                .url(url)
                .header("Authorization", "token $githubToken")
                .header("Accept", "application/vnd.github.v3+json")
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext emptyList()

                val body = response.body?.string() ?: return@withContext emptyList()
                val jsonArray = org.json.JSONArray(body)
                val repos = mutableListOf<RepoInfo>()

                for (i in 0 until jsonArray.length()) {
                    val repo = jsonArray.getJSONObject(i)
                    repos.add(RepoInfo(
                        full_name = repo.optString("full_name", ""),
                        description = repo.optString("description"),
                        html_url = repo.optString("html_url", ""),
                        language = repo.optString("language"),
                        stars = repo.optInt("stargazers_count", 0),
                        forks = repo.optInt("forks_count", 0)
                    ))
                }
                repos
            }
        } catch (e: Exception) {
            emptyList()
        }
    }

    /**
     * Get open pull requests for a repository
     */
    suspend fun getPullRequests(owner: String, repo: String, state: String = "open"): List<PullRequest> =
        withContext(Dispatchers.IO) {
            try {
                val url = "$githubApiUrl/repos/$owner/$repo/pulls?state=$state&per_page=100"
                val request = Request.Builder()
                    .url(url)
                    .header("Authorization", "token $githubToken")
                    .header("Accept", "application/vnd.github.v3+json")
                    .build()

                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@withContext emptyList()

                    val body = response.body?.string() ?: return@withContext emptyList()
                    val jsonArray = org.json.JSONArray(body)
                    val prs = mutableListOf<PullRequest>()

                    for (i in 0 until jsonArray.length()) {
                        val pr = jsonArray.getJSONObject(i)
                        val userObj = pr.optJSONObject("user")
                        prs.add(PullRequest(
                            number = pr.optInt("number"),
                            title = pr.optString("title", ""),
                            state = pr.optString("state", ""),
                            html_url = pr.optString("html_url", ""),
                            user = userObj?.optString("login", "unknown") ?: "unknown",
                            created_at = pr.optString("created_at", ""),
                            additions = pr.optJSONObject("additions")?.optInt("total") ?: 0,
                            deletions = pr.optJSONObject("deletions")?.optInt("total") ?: 0,
                            draft = pr.optBoolean("draft", false)
                        ))
                    }
                    prs
                }
            } catch (e: Exception) {
                emptyList()
            }
        }

    /**
     * Get issue details
     */
    suspend fun getIssue(owner: String, repo: String, issueNumber: Int): Issue? =
        withContext(Dispatchers.IO) {
            try {
                val url = "$githubApiUrl/repos/$owner/$repo/issues/$issueNumber"
                val request = Request.Builder()
                    .url(url)
                    .header("Authorization", "token $githubToken")
                    .header("Accept", "application/vnd.github.v3+json")
                    .build()

                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@withContext null

                    val body = response.body?.string() ?: return@withContext null
                    val json = JSONObject(body)
                    val userObj = json.optJSONObject("user")

                    Issue(
                        number = json.optInt("number"),
                        title = json.optString("title", ""),
                        state = json.optString("state", ""),
                        html_url = json.optString("html_url", ""),
                        user = userObj?.optString("login", "unknown") ?: "unknown",
                        created_at = json.optString("created_at", ""),
                        body = json.optString("body")
                    )
                }
            } catch (e: Exception) {
                null
            }
        }

    /**
     * Get commit details
     */
    suspend fun getCommit(owner: String, repo: String, sha: String): Commit? =
        withContext(Dispatchers.IO) {
            try {
                val url = "$githubApiUrl/repos/$owner/$repo/commits/$sha"
                val request = Request.Builder()
                    .url(url)
                    .header("Authorization", "token $githubToken")
                    .header("Accept", "application/vnd.github.v3+json")
                    .build()

                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@withContext null

                    val body = response.body?.string() ?: return@withContext null
                    val json = JSONObject(body)
                    val authorObj = json.optJSONObject("author")
                    val commitObj = json.optJSONObject("commit")
                    val stats = commitObj?.optJSONObject("stats")

                    Commit(
                        sha = sha,
                        message = commitObj?.optString("message", "") ?: "",
                        html_url = json.optString("html_url", ""),
                        author = authorObj?.optString("login"),
                        committed_at = json.optString("committed_date", ""),
                        additions = stats?.optInt("additions") ?: 0,
                        deletions = stats?.optInt("deletions") ?: 0
                    )
                }
            } catch (e: Exception) {
                null
            }
        }

    /**
     * Search issues and PRs
     */
    suspend fun searchGitHub(query: String): SearchResult = withContext(Dispatchers.IO) {
        try {
            val url = "$githubApiUrl/search/issues?q=$query&per_page=10"
            val request = Request.Builder()
                .url(url)
                .header("Authorization", "token $githubToken")
                .header("Accept", "application/vnd.github.v3+json")
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext SearchResult(emptyList())

                val body = response.body?.string() ?: return@withContext SearchResult(emptyList())
                val json = JSONObject(body)
                val items = json.optJSONArray("items") ?: return@withContext SearchResult(emptyList())

                val results = mutableListOf<Issue>()
                for (i in 0 until items.length()) {
                    val item = items.getJSONObject(i)
                    val userObj = item.optJSONObject("user")
                    results.add(Issue(
                        number = item.optInt("number"),
                        title = item.optString("title", ""),
                        state = item.optString("state", ""),
                        html_url = item.optString("html_url", ""),
                        user = userObj?.optString("login", "unknown") ?: "unknown",
                        created_at = item.optString("created_at", ""),
                        body = item.optString("body")
                    ))
                }
                SearchResult(results)
            }
        } catch (e: Exception) {
            SearchResult(emptyList())
        }
    }

    /**
     * Get authenticated user
     */
    suspend fun getAuthenticatedUser(): String = withContext(Dispatchers.IO) {
        try {
            val url = "$githubApiUrl/user"
            val request = Request.Builder()
                .url(url)
                .header("Authorization", "token $githubToken")
                .header("Accept", "application/vnd.github.v3+json")
                .build()

            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext ""
                val body = response.body?.string() ?: return@withContext ""
                JSONObject(body).optString("login", "")
            }
        } catch (e: Exception) {
            ""
        }
    }

    /**
     * Format GitHub data for chat display
     */
    fun formatPrList(prs: List<PullRequest>): String {
        if (prs.isEmpty()) return "No pull requests found."

        val sb = StringBuilder("### Pull Requests\n\n")
        for (pr in prs) {
            val draftBadge = if (pr.draft) "[DRAFT] " else ""
            sb.append("• **#${pr.number}** $draftBadge${pr.title}\n")
            sb.append("  - Author: @${pr.user} | State: ${pr.state}\n")
            sb.append("  - Changes: +${pr.additions}/-${pr.deletions}\n")
            sb.append("  - ${pr.html_url}\n\n")
        }
        return sb.toString()
    }

    fun formatIssue(issue: Issue): String {
        return """
            ### Issue #${issue.number}: ${issue.title}
            - **State:** ${issue.state}
            - **Author:** @${issue.user}
            - **Created:** ${issue.created_at}
            - **URL:** ${issue.html_url}
            ${if (!issue.body.isNullOrBlank()) "- **Body:**\n${issue.body.trim()}\n" else ""}
        """.trimIndent()
    }

    fun formatCommit(commit: Commit): String {
        return """
            ### Commit `$commit.sha`
            - **Message:** ${commit.message.split('\n').firstOrNull() ?: ""}
            - **Author:** ${commit.author ?: "Unknown"}
            - **Changes:** +${commit.additions}/-${commit.deletions}
            - **URL:** ${commit.html_url}
        """.trimIndent()
    }

    data class SearchResult(val issues: List<Issue>)

    companion object {
        const val TAG = "GitHubIntegration"
    }
}