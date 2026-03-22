package com.user.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import com.user.data.ChatDatabase
import com.user.data.ChatMessage
import com.user.data.ChatSession
import com.user.data.PrefsManager
import com.user.repository.ChatRepository
import com.user.service.AgentInfo
import com.user.service.Ed25519Manager
import com.user.service.GatewayClient
import com.user.service.GatewayEvent
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import android.content.Context
import android.content.Intent
import android.os.Build
import com.user.service.GatewayConnectionService

class ChatViewModel(application: Application) : AndroidViewModel(application) {

    // ── Dependencies ──────────────────────────────────────────
    private val prefs      = PrefsManager(application)
    private val db         = ChatDatabase.getDatabase(application)
    private val repository = ChatRepository(db.chatDao(), prefs)
    private val ed25519    = Ed25519Manager(application)
    private val gateway    = GatewayClient(prefs.serverUrl, prefs.gatewayToken, ed25519)

    // ── Session ───────────────────────────────────────────────
    private var _sessionId = UUID.randomUUID().toString()
    val sessionId get() = _sessionId

    // ── LiveData exposed to UI ────────────────────────────────
    private val _status = MutableLiveData("○ Disconnected")
    val status: LiveData<String> = _status

    private val _toast = MutableLiveData<String?>()
    val toast: LiveData<String?> = _toast

    private val _agents = MutableLiveData<List<AgentInfo>>(emptyList())
    val agents: LiveData<List<AgentInfo>> = _agents

    private val _messages = MutableLiveData<List<ChatMessage>>(emptyList())
    val messages: LiveData<List<ChatMessage>> = _messages

    private val _isSending = MutableLiveData(false)
    val isSending: LiveData<Boolean> = _isSending

    private val _showTyping = MutableLiveData(false)
    val showTyping: LiveData<Boolean> = _showTyping

    private val _pairingRequired = MutableLiveData<String?>(null)
    val pairingRequired: LiveData<String?> = _pairingRequired

    // ── Streaming state ───────────────────────────────────────
    private var streamingMsgId: Long = -1L

    // ── Init ──────────────────────────────────────────────────
    init {
        // Ensure device ID exists
        if (prefs.deviceId.isEmpty()) prefs.deviceId = UUID.randomUUID().toString()
        observeGatewayEvents()
    }

    // ── Session management ────────────────────────────────────

    fun loadSession(sessionId: String?, sessionTitle: String?) {
        if (sessionId != null) {
            _sessionId = sessionId
        }
        observeMessages()
    }

    fun startNewSession() {
        _sessionId = UUID.randomUUID().toString()
        viewModelScope.launch {
            repository.insertSession(
                ChatSession(
                    sessionId = _sessionId,
                    title = "Chat ${SimpleDateFormat("MMM dd HH:mm",
                        Locale.getDefault()).format(Date())}"
                )
            )
        }
        observeMessages()
    }

    private fun observeMessages() {
        viewModelScope.launch {
            repository.getMessages(_sessionId).collectLatest { msgs ->
                _messages.postValue(msgs)
            }
        }
    }

    fun startService(context: Context) {
        val intent = Intent(context, GatewayConnectionService::class.java)
        context.startForegroundService(intent)
        // The ViewModel's own GatewayClient continues to handle UI updates.
        connect()
    }

    // ── Gateway ───────────────────────────────────────────────

    fun connect() {
        gateway.connect()
    }

    fun switchAgent(agentId: String) {
        gateway.switchAgent(agentId)
        val agentName = _agents.value?.find { it.agentId == agentId }?.name ?: agentId
        _status.postValue("● $agentName")
    }

    private fun observeGatewayEvents() {
        viewModelScope.launch {
            gateway.events.collect { event ->
                when (event) {
                    is GatewayEvent.Connecting ->
                        _toast.postValue("○ Connecting…")

                    is GatewayEvent.HandshakeStarted ->
                        _toast.postValue("● Handshaking…")

                    is GatewayEvent.Ready -> {
                        _toast.postValue("● Connected")  // Briefly display
                        _agents.postValue(event.agents)
                        _isSending.postValue(false)
                        observeMessages()
                    }

                    is GatewayEvent.PairingRequired ->
                        _pairingRequired.postValue(event.deviceId)

                    is GatewayEvent.AuthError -> {
                        _status.postValue("✕ Auth failed")
                    }

                    is GatewayEvent.StreamDelta -> {
                        _showTyping.postValue(false)
                        if (streamingMsgId == -1L) {
                            val placeholder = ChatMessage(
                                sessionId = _sessionId,
                                message   = event.text,
                                isUser    = false
                            )
                            streamingMsgId = repository.insertMessage(placeholder)
                        } else {
                            val current = _messages.value
                                ?.firstOrNull { it.id == streamingMsgId }
                            if (current != null) {
                                repository.updateMessageContent(
                                    streamingMsgId,
                                    current.message + event.text
                                )
                            }
                        }
                    }

                    is GatewayEvent.StreamFinal -> {
                        if (streamingMsgId != -1L) {
                            repository.updateMessageContent(
                                streamingMsgId, event.fullText
                            )
                            streamingMsgId = -1L
                        } else {
                            repository.insertMessage(
                                ChatMessage(
                                    sessionId = _sessionId,
                                    message   = event.fullText,
                                    isUser    = false
                                )
                            )
                        }
                        repository.updateSessionTime(_sessionId, System.currentTimeMillis())
                        _isSending.postValue(false)
                        _showTyping.postValue(false)
                    }

                    is GatewayEvent.ToolCall -> {
                        val icon = if (event.done) "✅" else "⚙️"
                        _toast.postValue("$icon ${event.name}")
                    }

                    is GatewayEvent.Disconnected ->
                        _status.postValue("○ Disconnected")

                    is GatewayEvent.Error -> {
                        _status.postValue("✕ ${event.message}")
                        _showTyping.postValue(false)
                        _isSending.postValue(false)
                    }
                }
            }
        }
    }

    fun clearToast() { _toast.postValue(null) }

    // ── Send message ──────────────────────────────────────────

    fun sendMessage(text: String) {
        if (text.isBlank()) return
        _isSending.postValue(true)
        _showTyping.postValue(true)

        viewModelScope.launch {
            // Save user message locally
            repository.insertMessage(
                ChatMessage(sessionId = _sessionId, message = text, isUser = true)
            )
            repository.updateSessionTime(_sessionId, System.currentTimeMillis())

            // Send to Gateway
            gateway.sendMessage(text)
        }
    }

    // ── Cleanup ───────────────────────────────────────────────

    override fun onCleared() {
        super.onCleared()
        gateway.disconnect()
    }
}

