package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Chat message entity with task support
 * Tasks can be embedded in messages as JSON
 */
@Entity(tableName = "chat_messages")
data class ChatMessage(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val sessionId: String,
    val message: String,
    val isUser: Boolean,
    val timestamp: Long = System.currentTimeMillis(),
    val status: MessageStatus = MessageStatus.SENT,
    val imageBase64: String? = null,
    val taskJson: String? = null,  // JSON representation of embedded Task
    val metadata: String? = null    // Additional JSON metadata
)

enum class MessageStatus {
    SENT, DELIVERED, READ, FAILED, STREAMING, FINAL
}