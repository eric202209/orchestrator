package com.user.service

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

// ── Events emitted to MainActivity ──────────────────────────
sealed class GatewayEvent {
    object Connecting           : GatewayEvent()
    object HandshakeStarted     : GatewayEvent()
    object Ready                : GatewayEvent()  // fully connected, can chat
    object Disconnected         : GatewayEvent()
    data class StreamDelta(val text: String)          : GatewayEvent()
    data class StreamFinal(val fullText: String)      : GatewayEvent()
    data class ToolCall(val name: String, val done: Boolean) : GatewayEvent()
    data class Error(val message: String)             : GatewayEvent()
    data class AuthError(val message: String)         : GatewayEvent()
    data class PairingRequired(val deviceId: String)  : GatewayEvent()
}

class GatewayClient(
    private val serverUrl: String,
    private val gatewayToken: String,
    private val ed25519: Ed25519Manager
) {
    companion object {
        private val SCOPES = listOf(
            "operator.admin",
            "operator.approvals",
            "operator.pairing",
            "operator.read",
            "operator.write"
        )
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.SECONDS)   // no timeout for streaming
        .build()

    private var webSocket: WebSocket? = null
    private var state = State.DISCONNECTED
    private var sessionKey: String = ""

    // Accumulate streaming delta tokens
    private val streamBuffer = StringBuilder()

    private val _events = MutableSharedFlow<GatewayEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<GatewayEvent> = _events

    private var connectFallbackTimer: android.os.Handler? = null
    private var connectFallbackRunnable: Runnable? = null

    enum class State { DISCONNECTED, CONNECTING, HANDSHAKING, READY }

    // ── Public API ───────────────────────────────────────────

    fun connect() {
        if (state != State.DISCONNECTED) return
        state = State.CONNECTING
        _events.tryEmit(GatewayEvent.Connecting)

        val wsUrl = serverUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .trimEnd('/')  // no path — connect directly to Gateway root

        val request = Request.Builder().url(wsUrl).build()
        webSocket = client.newWebSocket(request, Listener())
    }

    fun sendMessage(message: String) {
        if (state != State.READY || sessionKey.isEmpty()) {
            _events.tryEmit(GatewayEvent.Error("Not connected"))
            return
        }
        streamBuffer.clear()

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
        state = State.DISCONNECTED
        webSocket?.close(1000, "User closed")
        webSocket = null
    }

    fun isReady() = state == State.READY

    // ── WebSocket Listener ───────────────────────────────────

    private inner class Listener : WebSocketListener() {

        override fun onOpen(ws: WebSocket, response: Response) {
            state = State.HANDSHAKING
            _events.tryEmit(GatewayEvent.HandshakeStarted)

            val handler = android.os.Handler(android.os.Looper.getMainLooper())
            val runnable = Runnable {
                if (state == State.HANDSHAKING) {
                    sendConnectFrame(ws, java.util.UUID.randomUUID().toString())
                }
            }
            connectFallbackTimer = handler
            connectFallbackRunnable = runnable
            handler.postDelayed(runnable, 2000)
        }

        override fun onMessage(ws: WebSocket, text: String) {
            if (text == "pong" || text.isBlank()) return
            try {
                val msg = JSONObject(text)
                handleMessage(ws, msg)
            } catch (e: Exception) {
                // ignore non-JSON
            }
        }

        override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
            state = State.DISCONNECTED
            val code = response?.code
            val msg = when {
                code == 4001           -> "Auth failed — check your token"
                t.message?.contains("ECONNREFUSED") == true ->
                    "Cannot reach OpenClaw — is it running?"
                else -> t.message ?: "Connection failed"
            }
            _events.tryEmit(GatewayEvent.Error(msg))
        }

        override fun onClosing(ws: WebSocket, code: Int, reason: String) {
            if (code == 4001) {
                _events.tryEmit(GatewayEvent.AuthError("Token invalid (4001)"))
            }
        }

        override fun onClosed(ws: WebSocket, code: Int, reason: String) {
            state = State.DISCONNECTED
            _events.tryEmit(GatewayEvent.Disconnected)
        }
    }

    // ── Message Handler ──────────────────────────────────────

    private fun handleMessage(ws: WebSocket, msg: JSONObject) {
        android.util.Log.d("GatewayClient", "RAW: $msg")
        val type  = msg.optString("type")
        val event = msg.optString("event")
        val msgId = msg.optString("id")

        when {

            // ── connect.challenge → send connect frame ─────────
            type == "event" && event == "connect.challenge" -> {
                // cancel fallback timer
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

                    when {
                        errCode.contains("UNPAIRED") || errCode.contains("NOT_PAIRED") ||
                                errMsg.lowercase().contains("pair") -> {
                            _events.tryEmit(GatewayEvent.PairingRequired(ed25519.deviceId))
                        }
                        errCode.contains("AUTH") || errCode.contains("TOKEN") -> {
                            _events.tryEmit(GatewayEvent.AuthError(errMsg))
                        }
                        else -> {
                            _events.tryEmit(GatewayEvent.Error("Handshake failed: $errMsg"))
                        }
                    }
                    state = State.DISCONNECTED
                    return
                }

                // ── Handshake success ──────────────────────────
                val payload  = msg.optJSONObject("payload")
                val snapshot = payload?.optJSONObject("snapshot")
                val defaults = snapshot?.optJSONObject("sessionDefaults")
                val mainKey  = defaults?.optString("mainSessionKey")

                sessionKey = if (!mainKey.isNullOrEmpty()) {
                    mainKey
                } else {
                    val agentId = defaults?.optString("defaultAgentId") ?: "main"
                    "agent:$agentId:main"
                }

                state = State.READY
                _events.tryEmit(GatewayEvent.Ready)
            }

            // ── agent event (tool calls) ───────────────────────
            type == "event" && event == "agent" -> {
                val payload = msg.optJSONObject("payload") ?: return
                val stream  = payload.optString("stream")
                val data    = payload.optJSONObject("data")

                when (stream) {
                    "assistant" -> {
                        // data.text 是累積全文，data.delta 是這次新增的字
                        // 用 delta 做打字機效果
                        val delta = data?.optString("delta") ?: ""
                        if (delta.isNotEmpty()) {
                            streamBuffer.append(delta)
                            _events.tryEmit(GatewayEvent.StreamDelta(delta))
                        }
                    }
                    "lifecycle" -> {
                        when (data?.optString("phase")) {
                            "end" -> {
                                // 串流結束，發出完整文字
                                val full = streamBuffer.toString().also { streamBuffer.clear() }
                                if (full.isNotEmpty()) {
                                    _events.tryEmit(GatewayEvent.StreamFinal(full))
                                }
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

    // ── Build & Send Connect Frame ───────────────────────────
    // Mirrors clawapp createConnectFrame() exactly

    private fun sendConnectFrame(ws: WebSocket, nonce: String) {
        val signedAt   = System.currentTimeMillis()
        val credential = gatewayToken
        val signature  = ed25519.buildSignature(signedAt, credential, nonce)
        val scopesStr  = SCOPES.joinToString(",")

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
                put("scopes", JSONArray(SCOPES))
                put("caps",   JSONArray())
                put("auth",   JSONObject().apply {
                    put("token", gatewayToken)
                })
                put("device", JSONObject().apply {
                    put("id",        ed25519.deviceId)
                    put("publicKey", ed25519.publicKeyBase64url)
                    put("signedAt",  signedAt)
                    put("nonce", nonce.ifEmpty { java.util.UUID.randomUUID().toString() })
                    put("signature", signature)
                })
                put("locale",    "en-US")
                put("userAgent", "ClawMobile-Android/1.3.0")
            })
        }
        ws.send(frame.toString())
    }
}
