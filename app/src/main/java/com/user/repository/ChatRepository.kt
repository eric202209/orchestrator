package com.user.repository

import com.user.data.ChatDao
import com.user.data.ChatMessage
import com.user.data.ChatSession
import com.user.data.PrefsManager
import com.user.data.MessageStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.withContext

/**
 * Single source of truth for all chat data.
 * ViewModel talks to Repository, Repository talks to Room + Prefs.
 */
class ChatRepository(
    private val chatDao: ChatDao,
    private val prefs: PrefsManager
) {
    // ── Messages ─────────────────────────────────────────────
    fun getMessages(sessionId: String): Flow<List<ChatMessage>> =
        chatDao.getMessagesBySession(sessionId)

    suspend fun insertMessage(message: ChatMessage): Long =
        withContext(Dispatchers.IO) { chatDao.insertMessage(message) }

    suspend fun updateMessageContent(id: Long, content: String) =
        withContext(Dispatchers.IO) { chatDao.updateMessageContent(id, content) }

    suspend fun updateMessageStatus(id: Long, status: MessageStatus) =
        withContext(Dispatchers.IO) { chatDao.updateMessageStatus(id, status) }

    // ── Sessions ─────────────────────────────────────────────
    fun getSessions(): Flow<List<ChatSession>> =
        chatDao.getAllSessions()

    suspend fun insertSession(session: ChatSession) =
        withContext(Dispatchers.IO) { chatDao.insertSession(session) }

    suspend fun updateSessionTime(sessionId: String, time: Long) =
        withContext(Dispatchers.IO) { chatDao.updateSessionTime(sessionId, time) }

    suspend fun deleteSession(sessionId: String) =
        withContext(Dispatchers.IO) {
            chatDao.deleteMessagesBySession(sessionId)
            chatDao.deleteSession(sessionId)
        }

    // ── Prefs ─────────────────────────────────────────────────
    var serverUrl: String
        get() = prefs.serverUrl
        set(value) { prefs.serverUrl = value }

    var gatewayToken: String
        get() = prefs.gatewayToken
        set(value) { prefs.gatewayToken = value }

    var deviceId: String
        get() = prefs.deviceId
        set(value) { prefs.deviceId = value }
}

