package com.user.service

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import okhttp3.*
import org.json.JSONObject
import java.util.UUID

sealed class WsEvent {
    object Connected       : WsEvent()
    object Ready           : WsEvent()
    object Disconnected    : WsEvent()
    data class Message(val text: String)  : WsEvent()
    data class Error(val error: String)   : WsEvent()
    data class AuthFailed(val reason: String) : WsEvent()
}

class WebSocketManager(
    private val serverUrl: String,
    private val gatewayToken: String
) {
    private val client = OkHttpClient()
    private var webSocket: WebSocket? = null
    private var _ready = false
    private var sessionKey: String = "agent:main:main" // fallback

    private val _events = MutableSharedFlow<WsEvent>(extraBufferCapacity = 64)
    val events: SharedFlow<WsEvent> = _events

    fun connect() {
        _ready = false
        // Correct URL format: ws://host/ws?token=xxx
        val wsUrl = serverUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .trimEnd('/') + "/ws?token=${gatewayToken}"

        val request = Request.Builder().url(wsUrl).build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(ws: WebSocket, response: Response) {
                _events.tryEmit(WsEvent.Connected)
            }

            override fun onMessage(ws: WebSocket, text: String) {
                handleMessage(text)
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                if (code == 4001) {
                    _events.tryEmit(WsEvent.AuthFailed("Token error，Please check the Settings"))
                }
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                _ready = false
                _events.tryEmit(WsEvent.Error(t.message ?: "Connection failed"))
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                _ready = false
                _events.tryEmit(WsEvent.Disconnected)
            }
        })
    }

    private fun handleMessage(raw: String) {
        if (raw == "pong") return
        try {
            val json = JSONObject(raw)
            val type = json.optString("type")

            when (type) {
                // ── Request/Response ──────────────────────────────────
                "res" -> {
                    val ok = json.optBoolean("ok", false)
                    if (!ok) {
                        val err = json.optJSONObject("error")
                            ?.optString("message") ?: "Request failed"
                        _events.tryEmit(WsEvent.Error(err))
                    }
                    // successful chat.send response — actual reply comes via event
                }

                // ── Events ────────────────────────────────────────────
                "event" -> {
                    when (json.optString("event")) {

                        "proxy.ready" -> {
                            // Extract sessionKey from snapshot
                            val data = json.optJSONObject("data")
                                ?: json.optJSONObject("payload")
                            val hello = data?.optJSONObject("hello")
                            val snapshot = hello?.optJSONObject("snapshot")
                            val defaults = snapshot?.optJSONObject("sessionDefaults")
                            val mainKey = defaults?.optString("mainSessionKey")

                            sessionKey = if (!mainKey.isNullOrEmpty()) {
                                mainKey
                            } else {
                                val agentId = defaults?.optString("defaultAgentId") ?: "main"
                                "agent:$agentId:main"
                            }
                            _ready = true
                            _events.tryEmit(WsEvent.Ready)
                        }

                        "proxy.disconnect" -> {
                            _ready = false
                            _events.tryEmit(WsEvent.Disconnected)
                        }

                        "proxy.error" -> {
                            val msg = json.optJSONObject("data")
                                ?.optString("message") ?: "Proxy error"
                            _events.tryEmit(WsEvent.Error(msg))
                        }

                        // ── Streaming chat events ──────────────────────
                        "chat.reply.delta" -> {
                            val delta = json.optJSONObject("data")
                                ?.optString("delta") ?: return
                            if (delta.isNotEmpty()) {
                                _events.tryEmit(WsEvent.Message(delta))
                            }
                        }

                        "chat.reply.done" -> {
                            // streaming complete — no action needed,
                            // full text already delivered via deltas
                        }

                        "chat.message" -> {
                            // non-streaming full message
                            val text = json.optJSONObject("data")
                                ?.optString("text") ?: return
                            if (text.isNotEmpty()) {
                                _events.tryEmit(WsEvent.Message(text))
                            }
                        }
                    }
                }
            }
        } catch (e: Exception) {
            // ignore parse errors
        }
    }

    fun sendMessage(message: String) {
        if (!_ready) return
        val req = JSONObject().apply {
            put("type", "req")
            put("id", UUID.randomUUID().toString())
            put("method", "chat.send")
            put("params", JSONObject().apply {
                put("sessionKey", sessionKey)
                put("message", message)
                put("deliver", false)
                put("idempotencyKey", UUID.randomUUID().toString())
            })
        }
        webSocket?.send(req.toString())
    }

    fun disconnect() {
        webSocket?.close(1000, "User closed")
        webSocket = null
        _ready = false
    }

    fun isReady() = _ready
}
