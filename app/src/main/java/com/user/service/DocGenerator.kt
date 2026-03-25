package com.user.service

import android.content.Context
import com.user.data.ChatMessage
import com.user.data.ChatSession
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Documentation generator for exporting chat sessions
 */
class DocGenerator(
    private val context: Context
) {
    /**
     * Export session to Markdown and save to file
     */
    suspend fun exportChatSessionToMarkdown(
        sessionTitle: String,
        messages: List<ChatMessage>,
        fileName: String
    ): Boolean = withContext(Dispatchers.IO) {
        try {
            val markdownContent = buildMarkdownContent(sessionTitle, messages)
            val downloadsDir = File(context.getExternalFilesDir(null), "Documents")
                ?: File(context.filesDir.parent, "files/Documents")
            downloadsDir.mkdirs()

            val file = File(downloadsDir, fileName)
            file.writeText(markdownContent)
            true
        } catch (e: Exception) {
            e.printStackTrace()
            false
        }
    }

    /**
     * Build Markdown content for session
     */
    private fun buildMarkdownContent(
        sessionTitle: String,
        messages: List<ChatMessage>
    ): String {
        val sb = StringBuilder()

        sb.append("# $sessionTitle\n\n")
        sb.append("**Exported:** ${formatTimestamp(System.currentTimeMillis())}\n\n")
        sb.append("---\n\n")

        for (message in messages) {
            sb.append(formatMessageForMarkdown(message))
        }

        return sb.toString()
    }

    /**
     * Export session to Markdown (raw content)
     */
    suspend fun exportToMarkdown(session: ChatSession, messages: List<ChatMessage>): String {
        val sb = StringBuilder()

        // Title
        sb.append("# ${session.title}\n\n")
        sb.append("**Session ID:** ${session.sessionId}\n\n")

        // Timestamps
        sb.append("**Created:** ${formatTimestamp(session.createdAt)}\n")
        sb.append("**Last Updated:** ${formatTimestamp(session.lastMessageAt)}\n\n")

        sb.append("---\n\n")

        // Messages
        for (message in messages) {
            sb.append(formatMessageForMarkdown(message))
        }

        return sb.toString()
    }

    /**
     * Export session to HTML
     */
    suspend fun exportToHtml(session: ChatSession, messages: List<ChatMessage>): String {
        val sb = StringBuilder()

        sb.append("""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>${escapeHtml(session.title)}</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #333;
        }
        .container {
            background: white;
            border-radius: 8px;
            padding: 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
        .meta { color: #7f8c8d; font-size: 0.9em; margin-bottom: 20px; }
        .message { margin-bottom: 20px; padding: 15px; border-radius: 8px; }
        .message.user { background: #e8f4f8; border-left: 4px solid #3498db; }
        .message.ai { background: #f5f5f5; border-left: 4px solid #2ecc71; }
        .message.system { background: #fff3cd; border-left: 4px solid #ffc107; }
        .sender { font-weight: bold; margin-bottom: 5px; }
        .user .sender { color: #2980b9; }
        .ai .sender { color: #27ae60; }
        .system .sender { color: #f39c12; }
        .content { white-space: pre-wrap; line-height: 1.6; }
        pre { background: #2c3e50; color: #ecf0f1; padding: 15px; border-radius: 4px; overflow-x: auto; }
        code { font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace; }
        .timestamp { font-size: 0.8em; color: #95a5a6; }
        hr { border: none; border-top: 1px solid #ecf0f1; margin: 30px 0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>${escapeHtml(session.title)}</h1>
        <div class="meta">
            <p><strong>Session ID:</strong> ${session.sessionId}</p>
            <p><strong>Created:</strong> ${formatTimestamp(session.createdAt)}</p>
            <p><strong>Last Updated:</strong> ${formatTimestamp(session.lastMessageAt)}</p>
        </div>
        <hr>
""")

        for (message in messages) {
            val senderType = if (message.isUser) "user" else "ai"
            sb.append("        <div class=\"message $senderType\">\n")
            sb.append("            <div class=\"sender\">${escapeHtml(getSenderName(senderType))}</div>\n")
            sb.append("            <div class=\"content\">${escapeHtml(message.message)}</div>\n")
            sb.append("            <div class=\"timestamp\">${formatTimestamp(message.timestamp)}</div>\n")
            sb.append("        </div>\n")
        }

        sb.append("""    </div>
</body>
</html>
""")

        return sb.toString()
    }

    /**
     * Export selected messages to Markdown
     */
    suspend fun exportMessagesToMarkdown(
        sessionTitle: String,
        messages: List<ChatMessage>,
        timestampStart: Long? = null,
        timestampEnd: Long? = null
    ): String {
        val filteredMessages = if (timestampStart != null && timestampEnd != null) {
            messages.filter { it.timestamp in timestampStart..timestampEnd }
        } else {
            messages
        }

        val sb = StringBuilder()
        sb.append("# $sessionTitle\n\n")
        sb.append("**Exported:** ${formatTimestamp(System.currentTimeMillis())}\n\n")

        for (message in filteredMessages) {
            sb.append(formatMessageForMarkdown(message))
        }

        return sb.toString()
    }

    /**
     * Format single message for Markdown
     */
    private fun formatMessageForMarkdown(message: ChatMessage): String {
        val sender = if (message.isUser) "**You**" else "**AI Assistant**"

        return """
$sender (${formatTimestamp(message.timestamp)})

${message.message}

---

""".trimIndent()
    }

    /**
     * Get sender display name
     */
    private fun getSenderName(sender: String): String {
        return when (sender) {
            "user" -> "You"
            "ai" -> "AI Assistant"
            "system" -> "System"
            else -> sender
        }
    }

    /**
     * Format timestamp for display
     */
    private fun formatTimestamp(timestamp: Long): String {
        val date = java.util.Date(timestamp)
        return java.text.SimpleDateFormat("MMM dd, yyyy HH:mm:ss", java.util.Locale.US).format(date)
    }

    /**
     * Escape HTML special characters
     */
    private fun escapeHtml(text: String): String {
        return text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&#039;")
    }

    /**
     * Generate documentation summary
     */
    suspend fun generateSessionSummary(messages: List<ChatMessage>): String {
        val userMessages = messages.filter { it.isUser }
        val aiMessages = messages.filter { !it.isUser }

        val sb = StringBuilder()
        sb.append("## Session Summary\n\n")
        sb.append("**Total Messages:** ${messages.size}\n")
        sb.append("**User Messages:** ${userMessages.size}\n")
        sb.append("**AI Messages:** ${aiMessages.size}\n\n")

        // Extract topics from messages
        val topics = extractTopics(messages)
        if (topics.isNotEmpty()) {
            sb.append("**Topics Discussed:**\n")
            topics.forEach { topic ->
                sb.append("- $topic\n")
            }
            sb.append("\n")
        }

        return sb.toString()
    }

    /**
     * Extract topics from messages (simplified)
     */
    private fun extractTopics(messages: List<ChatMessage>): List<String> {
        val topicKeywords = setOf(
            "function", "class", "method", "API", "database", "config",
            "error", "bug", "fix", "implementation", "feature", "test"
        )

        val topics = mutableSetOf<String>()
        val text = messages.joinToString(" ") { it.message }.lowercase()

        topicKeywords.forEach { keyword ->
            if (text.contains(keyword)) {
                topics.add(keyword)
            }
        }

        return topics.toList()
    }

    companion object {
        const val TAG = "DocGenerator"
    }
}