package com.user.viewmodel

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.viewModelScope
import com.user.ClawMobileApplication
import com.user.data.ChatMessage
import com.user.data.ChatSession
import com.user.data.MessageStatus
import com.user.data.PrefsManager
import com.user.repository.ChatRepository
import com.user.service.AgentInfo
import com.user.service.Ed25519Manager
import com.user.service.GatewayClient
import com.user.service.GatewayConnectionService
import com.user.service.GatewayEvent
import com.tom_roush.pdfbox.pdmodel.PDDocument
import com.tom_roush.pdfbox.text.PDFTextStripper
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import java.nio.charset.StandardCharsets
import java.util.UUID

class ChatViewModel(application: Application) : AndroidViewModel(application) {
    companion object {
        private const val MAX_ATTACHMENT_TEXT_CHARS = 20_000
    }

    private val app = application as ClawMobileApplication
    private val prefs = PrefsManager(application)

    private val repository = ChatRepository(
        app.chatDao,
        app.gitConnectionDao,
        app.projectContextDao,
        app.taskDao,
        prefs
    )

    private val ed25519 = Ed25519Manager(application)
    private val gateway = GatewayClient(prefs.serverUrl, prefs.gatewayToken, ed25519)

    // ── LiveData ──────────────────────────────────────────────
    private val _messages = MutableLiveData<List<ChatMessage>>()
    val messages: LiveData<List<ChatMessage>> get() = _messages

    private val _status = MutableLiveData("○ Connecting…")
    val status: LiveData<String> get() = _status

    private val _agents = MutableLiveData<List<AgentInfo>>()
    val agents: LiveData<List<AgentInfo>> get() = _agents

    private val _isSending = MutableLiveData(false)
    val isSending: LiveData<Boolean> get() = _isSending

    private val _showTyping = MutableLiveData(false)
    val showTyping: LiveData<Boolean> get() = _showTyping

    private val _pairingRequired = MutableLiveData<String?>(null)
    val pairingRequired: LiveData<String?> get() = _pairingRequired

    private val _toast = MutableLiveData<String?>(null)
    val toast: LiveData<String?> get() = _toast

    private val _currentSessionId = MutableLiveData<String>()

    init {
        observeGatewayEvents()
    }

    fun loadSession(sessionId: String) {
        _currentSessionId.value = sessionId
        viewModelScope.launch {
            repository.getMessages(sessionId).collectLatest {
                _messages.postValue(it)
            }
        }
    }

    fun startNewSession() {
        val sessionId = UUID.randomUUID().toString()
        _currentSessionId.value = sessionId
        viewModelScope.launch {
            repository.insertSession(ChatSession(sessionId = sessionId, title = "New Chat"))
            repository.getMessages(sessionId).collectLatest {
                _messages.postValue(it)
            }
        }
    }

    fun startService(context: Context) {
        val intent = Intent(context, GatewayConnectionService::class.java)
        context.startForegroundService(intent)
        connect()
    }

    private fun observeGatewayEvents() {
        viewModelScope.launch {
            gateway.events.collect { event ->
                when (event) {
                    is GatewayEvent.Connecting -> _status.postValue("○ Connecting…")
                    is GatewayEvent.HandshakeStarted -> _status.postValue("○ Handshaking…")
                    is GatewayEvent.Ready -> {
                        _status.postValue("● Connected")
                        _agents.postValue(event.agents)
                    }
                    is GatewayEvent.Disconnected -> _status.postValue("✕ Disconnected")
                    is GatewayEvent.StreamDelta -> {
                        _showTyping.postValue(true)
                    }
                    is GatewayEvent.StreamFinal -> {
                        _showTyping.postValue(false)
                        handleIncomingMessage(event.fullText)
                    }
                    is GatewayEvent.PairingRequired -> {
                        _pairingRequired.postValue(event.deviceId)
                        _status.postValue("✕ Pairing Required")
                    }
                    is GatewayEvent.Error -> {
                        _status.postValue("✕ Error: ${event.message}")
                    }
                    else -> {}
                }
            }
        }
    }

    private fun handleIncomingMessage(text: String) {
        val sessionId = _currentSessionId.value ?: return
        viewModelScope.launch {
            val message = ChatMessage(
                sessionId = sessionId,
                message = text,
                isUser = false,
                status = MessageStatus.SENT,
                timestamp = System.currentTimeMillis()
            )
            repository.insertMessage(message)
        }
    }

    fun sendMessage(text: String) {
        val sessionId = _currentSessionId.value ?: return
        if (text.isBlank()) return

        viewModelScope.launch {
            val message = ChatMessage(
                sessionId = sessionId,
                message = text,
                isUser = true,
                status = MessageStatus.SENT,
                timestamp = System.currentTimeMillis()
            )
            repository.insertMessage(message)

            _isSending.postValue(true)
            try {
                gateway.sendMessage(text)
            } catch (e: Exception) {
                _toast.postValue("Failed to send: ${e.message}")
            } finally {
                _isSending.postValue(false)
            }
        }
    }

    /**
     * Sends a file to the gateway.
     * Images are sent using the native Base64 format.
     * Text files and PDFs have their content embedded in the message.
     * Other binary files are rejected until a real upload path exists.
     */
    fun sendFile(context: Context, uri: Uri, text: String) {
        val sessionId = _currentSessionId.value ?: return
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val contentResolver = context.contentResolver
                val mimeType = contentResolver.getType(uri) ?: "application/octet-stream"
                val fileName = getFileName(context, uri) ?: "file"
                val lowerFileName = fileName.lowercase()

                val bytes = contentResolver.openInputStream(uri)?.use {
                    it.readBytes()
                } ?: throw Exception("Could not read file")

                _isSending.postValue(true)

                if (mimeType.startsWith("image/")) {
                    val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                    val message = ChatMessage(
                        sessionId = sessionId,
                        message = text,
                        isUser = true,
                        status = MessageStatus.SENT,
                        timestamp = System.currentTimeMillis(),
                        imageBase64 = base64
                    )
                    repository.insertMessage(message)
                    gateway.sendMessage(text, base64, mimeType)
                } else {
                    val finalMessage = when {
                        isPdfFile(mimeType, lowerFileName) -> {
                            val fileContent = extractPdfText(bytes)
                            buildAttachmentMessage(
                                prompt = text,
                                fileName = fileName,
                                fileContent = fileContent,
                                label = "Attached PDF"
                            )
                        }
                        isTextFile(mimeType) || isTextLikeFileName(lowerFileName) -> {
                            val fileContent = String(bytes, StandardCharsets.UTF_8)
                            buildAttachmentMessage(
                                prompt = text,
                                fileName = fileName,
                                fileContent = fileContent,
                                label = "Attached File"
                            )
                        }
                        else -> {
                            throw IllegalArgumentException(
                                "This file type is not supported yet. Use PDF, TXT, MD, JSON, CSV, or XML."
                            )
                        }
                    }

                    val message = ChatMessage(
                        sessionId = sessionId,
                        message = finalMessage,
                        isUser = true,
                        status = MessageStatus.SENT,
                        timestamp = System.currentTimeMillis()
                    )
                    repository.insertMessage(message)
                    gateway.sendMessage(finalMessage)
                }
            } catch (e: Exception) {
                _toast.postValue("Failed to attach file: ${e.message}")
            } finally {
                _isSending.postValue(false)
            }
        }
    }

    private fun isTextFile(mimeType: String): Boolean {
        val textMimeTypes = listOf(
            "text/plain", "text/markdown", "application/json",
            "text/csv", "text/tab-separated-values", "application/xml", "text/xml"
        )
        return mimeType in textMimeTypes || mimeType.startsWith("text/")
    }

    private fun isTextLikeFileName(fileName: String): Boolean {
        return fileName.endsWith(".txt") ||
                fileName.endsWith(".md") ||
                fileName.endsWith(".json") ||
                fileName.endsWith(".csv") ||
                fileName.endsWith(".xml")
    }

    private fun isPdfFile(mimeType: String, fileName: String): Boolean {
        return mimeType == "application/pdf" || fileName.endsWith(".pdf")
    }

    private fun extractPdfText(bytes: ByteArray): String {
        PDDocument.load(bytes).use { document ->
            if (document.isEncrypted) {
                throw IllegalArgumentException("Password-protected PDFs are not supported yet.")
            }

            val extracted = PDFTextStripper().getText(document).trim()
            if (extracted.isBlank()) {
                throw IllegalArgumentException("Could not extract readable text from this PDF.")
            }
            return extracted
        }
    }

    private fun buildAttachmentMessage(
        prompt: String,
        fileName: String,
        fileContent: String,
        label: String
    ): String {
        val trimmedContent = fileContent.trim()
        if (trimmedContent.isBlank()) {
            throw IllegalArgumentException("The selected file did not contain readable text.")
        }

        val wasTruncated = trimmedContent.length > MAX_ATTACHMENT_TEXT_CHARS
        val safeContent = trimmedContent.take(MAX_ATTACHMENT_TEXT_CHARS)
        val header = buildString {
            append("\n\n--- ")
            append(label)
            append(": ")
            append(fileName)
            append(" ---")
            if (wasTruncated) {
                append("\n[Content truncated to ")
                append(MAX_ATTACHMENT_TEXT_CHARS)
                append(" characters]")
            }
        }

        val attachmentBlock = "$header\n```\n$safeContent\n```"
        return if (prompt.isBlank()) {
            "Please analyze this attachment.$attachmentBlock"
        } else {
            "$prompt$attachmentBlock"
        }
    }

    private fun getFileName(context: Context, uri: Uri): String? {
        var result: String? = null
        if (uri.scheme == "content") {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (index != -1) {
                        result = cursor.getString(index)
                    }
                }
            }
        }
        if (result == null) {
            result = uri.path
            val cut = result?.lastIndexOf('/') ?: -1
            if (cut != -1) {
                result = result?.substring(cut + 1)
            }
        }
        return result
    }

    fun switchAgent(agentId: String) {
        gateway.switchAgent(agentId)
    }

    fun clearPairingDialog() {
        _pairingRequired.value = null
    }

    fun clearToast() {
        _toast.value = null
    }

    fun connect() {
        viewModelScope.launch(Dispatchers.IO) {
            gateway.connect()
        }
    }
}

