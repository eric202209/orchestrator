package com.user.service

/**
 * Parser for chat commands like @github, @context, etc
 */
class ChatCommandParser(
    private val githubManager: GitHubIntegrationManager,
    private val contextManager: ProjectContextManager,
) {
    /**
     * Parse and execute a command from user message
     * Returns the formatted response to display in chat, or null if no command matched
     */
    suspend fun parseAndExecute(message: String): CommandResult? {
        val trimmed = message.trim()

        // GitHub commands (@github)
        if (trimmed.startsWith("@github")) {
            return parseGithubCommand(trimmed)
        }

        // Context commands (@context)
        if (trimmed.startsWith("@context")) {
            return parseContextCommand(trimmed)
        }

        return null
    }

    /**
     * Parse @github commands
     */
    private suspend fun parseGithubCommand(message: String): CommandResult? {
        if (!githubManager.isConfigured()) {
            return CommandResult(
                response = "❌ GitHub is not configured. Please set your GitHub token in Settings.",
                isSystemMessage = true
            )
        }

        val parts = message.split("\\s+".toRegex()).filter { it.isNotBlank() }
        if (parts.size < 2) {
            return CommandResult(
                response = "📌 **GitHub Commands:**\n" +
                        "• `@github show PRs` - List open PRs\n" +
                        "• `@github issue #123` - Show issue details\n" +
                        "• `@github commit abc123` - Show commit info\n" +
                        "• `@github search query` - Search issues/PRs",
                isSystemMessage = true
            )
        }

        try {
            val subcommand = parts[1].lowercase()

            return when {
                // @github show PRs [repo]
                subcommand == "show" && parts.size > 2 && parts[2] == "PRs" -> {
                    val repo = if (parts.size > 3) {
                        parts[3]
                    } else {
                        "owner/repo" // Placeholder
                    }

                    val repoParts = repo.split("/")
                    if (repoParts.size != 2) {
                        CommandResult(
                            response = "❌ Invalid repo format. Use `owner/repo`",
                            isSystemMessage = true
                        )
                    } else {
                        val prs = githubManager.getPullRequests(repoParts[0], repoParts[1])
                        CommandResult(
                            response = githubManager.formatPrList(prs),
                            isSystemMessage = true
                        )
                    }
                }

                // @github issue #123
                subcommand == "issue" && parts.size > 2 -> {
                    val issueNumStr = parts[2].removePrefix("#")
                    val issueNum = issueNumStr.toIntOrNull()
                    if (issueNum == null) {
                        CommandResult(
                            response = "❌ Invalid issue number: ${parts[2]}",
                            isSystemMessage = true
                        )
                    } else {
                        val repo = if (parts.size > 3) parts[3] else "owner/repo"
                        val repoParts = repo.split("/")
                        if (repoParts.size != 2) {
                            CommandResult(
                                response = "❌ Invalid repo format. Use `owner/repo`",
                                isSystemMessage = true
                            )
                        } else {
                            val issue = githubManager.getIssue(repoParts[0], repoParts[1], issueNum)
                            if (issue != null) {
                                CommandResult(response = githubManager.formatIssue(issue), isSystemMessage = true)
                            } else {
                                CommandResult(response = "❌ Issue #$issueNum not found", isSystemMessage = true)
                            }
                        }
                    }
                }

                // @github commit abc123
                subcommand == "commit" && parts.size > 2 -> {
                    val sha = parts[2]
                    val repo = if (parts.size > 3) parts[3] else "owner/repo"
                    val repoParts = repo.split("/")
                    if (repoParts.size != 2) {
                        CommandResult(
                            response = "❌ Invalid repo format. Use `owner/repo`",
                            isSystemMessage = true
                        )
                    } else {
                        val commit = githubManager.getCommit(repoParts[0], repoParts[1], sha)
                        if (commit != null) {
                            CommandResult(response = githubManager.formatCommit(commit), isSystemMessage = true)
                        } else {
                            CommandResult(response = "❌ Commit `$sha` not found", isSystemMessage = true)
                        }
                    }
                }

                // @github search query
                subcommand == "search" && parts.size > 2 -> {
                    val query = parts.drop(2).joinToString(" ")
                    val result = githubManager.searchGitHub(query)
                    if (result.issues.isNotEmpty()) {
                        val sb = StringBuilder("### Search Results\n\n")
                        for (issue in result.issues.take(5)) {
                            sb.append(githubManager.formatIssue(issue))
                            sb.append("\n")
                        }
                        CommandResult(response = sb.toString(), isSystemMessage = true)
                    } else {
                        CommandResult(response = "❌ No results found for `$query`", isSystemMessage = true)
                    }
                }

                else -> null
            }
        } catch (e: Exception) {
            return CommandResult(
                response = "❌ Error: ${e.message}",
                isSystemMessage = true
            )
        }
    }

    /**
     * Parse @context commands
     */
    private suspend fun parseContextCommand(message: String): CommandResult? {
        val parts = message.split("\\s+".toRegex()).filter { it.isNotBlank() }
        if (parts.size < 2) {
            return CommandResult(
                response = "📌 **Context Commands:**\n" +
                        "• `@context show` - Display current context\n" +
                        "• `@context files` - List project files",
                isSystemMessage = true
            )
        }

        val subcommand = parts[1].lowercase()

        return when (subcommand) {
            "show" -> {
                val context = contextManager.getCurrentContext("current")
                if (context != null) {
                    CommandResult(response = contextManager.formatContext(context), isSystemMessage = true)
                } else {
                    CommandResult(response = "ℹ️ No project context available. Context will be sent by OpenClaw gateway.", isSystemMessage = true)
                }
            }

            "files" -> {
                val context = contextManager.getCurrentContext("current")
                if (context != null) {
                    CommandResult(response = contextManager.getFileList(context), isSystemMessage = true)
                } else {
                    CommandResult(response = "ℹ️ No project context available", isSystemMessage = true)
                }
            }
            else -> null
        }
    }

    data class CommandResult(
        val response: String,
        val isSystemMessage: Boolean = true,
    )

    companion object {
        const val TAG = "ChatCommandParser"
    }
}