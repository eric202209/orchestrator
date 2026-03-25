package com.user.service

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import okhttp3.*
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

data class AgentInfo(
    val agentId: String,
    val name: String
)

sealed class GatewayEvent {
    object Connecting                                        : GatewayEvent()
    object HandshakeStarted                                  : GatewayEvent()
    data class Ready(val agents: List<AgentInfo>)            : GatewayEvent()
    object Disconnected                                      : GatewayEvent()
    data class StreamDelta(val text: String)                 : GatewayEvent()
    data class StreamFinal(val fullText: String)             : GatewayEvent()
    data class ToolCall(val name: String, val done: Boolean) : GatewayEvent()
    data class Error(val message: String)                    : GatewayEvent()
    data class AuthError(val message: String)                : GatewayEvent()
    data class PairingRequired(val deviceId: String)         : GatewayEvent()

    // Developer Tools Events
    data class ProjectContextUpdate(val sessionId: String, val projectPath: String, val files: List<String>) : GatewayEvent()
    data class TerminalResult(val sessionId: String, val command: String, val status: String, val output: String?, val error: String?) : GatewayEvent()
}

class GatewayClient(
    private val serverUrl: String,
    private val gatewayToken: String,
    private val ed25519: Ed25519Manager,
    private var currentRunId: String = ""
) {
    companion object {
        private val SCOPES = listOf(
            "operator.admin", "operator.approvals", "operator.pairing",
            "operator.read", "operator.write"
        )
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)
        .build()

    private var webSocket: WebSocket? = null
    private var state = State.DISCONNECTED
    var shouldReconnect: Boolean = true
    private var reconnectAttempts = 0

    // Default sessionKey — updated when user picks an agent
    var sessionKey: String = ""
        private set

    // Available agents from snapshot
    var availableAgents: List<AgentInfo> = emptyList()
        private set

    private val streamBuffer = StringBuilder()
    private var connectFallbackTimer: android.os.Handler? = null
    private var connectFallbackRunnable: Runnable? = null

    private val _events = MutableSharedFlow<GatewayEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<GatewayEvent> = _events

    // Callback for successful pairing
    var onPairingComplete: ((String) -> Unit)? = null

    enum class State { DISCONNECTED, CONNECTING, HANDSHAKING, READY }

    // ── Public API ───────────────────────────────────────────

    fun connect() {
        if (state != State.DISCONNECTED) return
        state = State.CONNECTING
        _events.tryEmit(GatewayEvent.Connecting)

        val wsUrl = serverUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .trimEnd('/')

        val request = Request.Builder().url(wsUrl).build()
        webSocket = client.newWebSocket(request, Listener())
    }

    /**
     * Switch to a different agent.
     * sessionKey format: agent:<agentId>:main
     */
    fun switchAgent(agentId: String) {
        sessionKey = "agent:$agentId:main"
        streamBuffer.clear()
    }

    fun sendMessage(text: String, imageBase64: String? = null, imageMimeType: String = "image/jpeg") {
        if (state != State.READY || sessionKey.isEmpty()) {
            _events.tryEmit(GatewayEvent.Error("Not connected"))
            return
        }
        streamBuffer.clear()

        // Build message string - backend expects a string, not JSON array
        // If imageBase64 is provided, we wrap it in the [image_base64:...] format for the proxy
        val message = if (imageBase64 != null) {
            "${text.ifBlank { "Please analyze this image." }} [image_base64:$imageMimeType:$imageBase64]"
        } else {
            text
        }

        val req = JSONObject().apply {
            put("type",   "req")
            put("id",     UUID.randomUUID().toString())
            put("method", "chat.send")
            put("params", JSONObject().apply {
                put("sessionKey",     sessionKey)
                put("message",        message)
                put("deliver",        false)
                put("idempotencyKey", UUID.randomUUID().toString())
            })
        }
        webSocket?.send(req.toString())
    }

    fun disconnect() {
        shouldReconnect = false
        reconnectAttempts = 0
        state = State.DISCONNECTED
        connectFallbackRunnable?.let { connectFallbackTimer?.removeCallbacks(it) }
        webSocket?.close(1000, "User closed")
        webSocket = null
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect || reconnectAttempts >= 5) return
        val delay = (reconnectAttempts + 1) * 3000L
        reconnectAttempts++
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            if (shouldReconnect) {
                state = State.DISCONNECTED
                webSocket = null
                connect()
            }
        }, delay)
    }

    private fun resetReconnect() {
        reconnectAttempts = 0
    }

    fun isReady() = state == State.READY


    // ── WebSocket Listener ───────────────────────────────────

    private inner class Listener : WebSocketListener() {

        override fun onOpen(ws: WebSocket, response: Response) {
            state = State.HANDSHAKING
            _events.tryEmit(GatewayEvent.HandshakeStarted)

            // 2s fallback if no challenge arrives
            val handler = android.os.Handler(android.os.Looper.getMainLooper())
            val runnable = Runnable {
                if (state == State.HANDSHAKING) {
                    sendConnectFrame(ws, UUID.randomUUID().toString())
                }
            }
            connectFallbackTimer = handler
            connectFallbackRunnable = runnable
            handler.postDelayed(runnable, 2000)
        }

        override fun onMessage(ws: WebSocket, text: String) {
            if (text == "pong" || text.isBlank()) return
            try {
                handleMessage(ws, JSONObject(text))
            } catch (e: Exception) { }
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            state = State.DISCONNECTED
            _events.tryEmit(GatewayEvent.Error(t.message ?: "Connection failed"))
            scheduleReconnect()
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            if (code == 4001) {
                _events.tryEmit(GatewayEvent.AuthError("Token invalid (4001)"))
            }
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            state = State.DISCONNECTED
            _events.tryEmit(GatewayEvent.Disconnected)
            if (code != 1000) scheduleReconnect()
        }
    }

    // ── Message Handler ──────────────────────────────────────

    private fun handleMessage(ws: WebSocket, msg: JSONObject) {
        val type  = msg.optString("type")
        val event = msg.optString("event")
        val msgId = msg.optString("id")

        when {

            // ── connect.challenge ──────────────────────────────
            type == "event" && event == "connect.challenge" -> {
                connectFallbackRunnable?.let { connectFallbackTimer?.removeCallbacks(it) }
                connectFallbackTimer = null
                connectFallbackRunnable = null
                val nonce = msg.optJSONObject("payload")?.optString("nonce") ?: ""
                sendConnectFrame(ws, nonce)
            }

            // ── connect response ───────────────────────────────
            type == "res" && msgId.startsWith("connect-") -> {
                if (!msg.optBoolean("ok", false)) {
                    val errCode = msg.optJSONObject("error")?.optString("code") ?: ""
                    val errMsg  = msg.optJSONObject("error")?.optString("message") ?: "Handshake failed"

                    // Fixed: More specific pairing check to avoid false positives with "repair"
                    val isPairingError = errCode == "UNPAIRED" ||
                            errCode == "NOT_PAIRED" ||
                            errCode == "PAIRING_REQUIRED" ||
                            errMsg.lowercase().contains("pairing required") ||
                            errMsg.lowercase().contains("not paired")

                    if (isPairingError) {
                        _events.tryEmit(GatewayEvent.PairingRequired(ed25519.deviceId))
                    } else {
                        _events.tryEmit(GatewayEvent.Error("Handshake failed: $errMsg"))
                    }
                    state = State.DISCONNECTED
                    return
                }

                // ── Parse agents from snapshot ─────────────────
                val payload  = msg.optJSONObject("payload")
                val snapshot = payload?.optJSONObject("snapshot")
                val defaults = snapshot?.optJSONObject("sessionDefaults")

                val agentsJson = snapshot?.optJSONObject("health")?.optJSONArray("agents")
                val agents = mutableListOf<AgentInfo>()
                if (agentsJson != null) {
                    for (i in 0 until agentsJson.length()) {
                        val a = agentsJson.getJSONObject(i)
                        agents.add(AgentInfo(
                            agentId = a.optString("agentId", "main"),
                            name    = a.optString("name", "Main")
                        ))
                    }
                }

                if (agents.isEmpty()) {
                    agents.add(AgentInfo("main", "Main"))
                }
                availableAgents = agents

                val mainKey = defaults?.optString("mainSessionKey")
                sessionKey = if (!mainKey.isNullOrEmpty()) {
                    mainKey
                } else {
                    val defaultAgentId = defaults?.optString("defaultAgentId") ?: "main"
                    "agent:$defaultAgentId:main"
                }

                state = State.READY
                resetReconnect()
                onPairingComplete?.invoke(ed25519.deviceId)
                _events.tryEmit(GatewayEvent.Ready(agents))
            }

            // ── agent streaming ────────────────────────────────
            type == "event" && event == "agent" -> {
                val payload = msg.optJSONObject("payload") ?: return
                val stream  = payload.optString("stream")
                val data    = payload.optJSONObject("data")
                val runId   = payload.optString("runId")

                when (stream) {
                    "assistant" -> {
                        val delta = data?.optString("delta") ?: ""
                        if (delta.isNotEmpty()) {
                            currentRunId = payload.optString("runId")
                            streamBuffer.append(delta)
                            _events.tryEmit(GatewayEvent.StreamDelta(delta))
                        }
                    }
                    "lifecycle" -> {
                        if (data?.optString("phase") == "end") {
                            if (runId != currentRunId) return
                            val full = streamBuffer.toString().also { streamBuffer.clear() }
                            currentRunId = ""
                            if (full.isNotEmpty()) {
                                _events.tryEmit(GatewayEvent.StreamFinal(full))
                            }
                        }
                    }
                    "tool" -> {
                        val phase = data?.optString("phase")
                        val tool  = data?.optString("tool") ?: "tool"
                        when (phase) {
                            "start" -> _events.tryEmit(GatewayEvent.ToolCall(tool, false))
                            "end"   -> _events.tryEmit(GatewayEvent.ToolCall(tool, true))
                        }
                    }
                }
            }
        }
    }

    // ── Connect Frame ────────────────────────────────────────

    private fun sendConnectFrame(ws: WebSocket, nonce: String) {
        val signedAt  = System.currentTimeMillis()
        val signature = ed25519.buildSignature(signedAt, gatewayToken, nonce)

        val frame = JSONObject().apply {
            put("type",   "req")
            put("id",     "connect-${UUID.randomUUID()}")
            put("method", "connect")
            put("params", JSONObject().apply {
                put("minProtocol", 3)
                put("maxProtocol", 3)
                put("client", JSONObject().apply {
                    put("id",       "gateway-client")
                    put("version",  "1.0.0")
                    put("platform", "android")
                    put("mode",     "backend")
                })
                put("role",   "operator")
                put("scopes", org.json.JSONArray(SCOPES))
                put("caps",   org.json.JSONArray())
                put("auth",   JSONObject().apply { put("token", gatewayToken) })
                put("device", JSONObject().apply {
                    put("id",        ed25519.deviceId)
                    put("publicKey", ed25519.publicKeyBase64url)
                    put("signedAt",  signedAt)
                    put("nonce",     nonce)
                    put("signature", signature)
                })
                put("locale",    "en-US")
                put("userAgent", "ClawMobile-Android/1.5.0")
            })
        }
        ws.send(frame.toString())
    }
}
