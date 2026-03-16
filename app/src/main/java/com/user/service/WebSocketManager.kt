package com.user.service

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import okhttp3.*

sealed class WsEvent {
    data class Message(val text: String) : WsEvent()
    object Connected : WsEvent()
    object Disconnected : WsEvent()
    data class Error(val error: String) : WsEvent()
}

class WebSocketManager(private val serverUrl: String) {

    private val client = OkHttpClient()
    private var webSocket: WebSocket? = null

    private val _events = MutableSharedFlow<WsEvent>(extraBufferCapacity = 16)
    val events: SharedFlow<WsEvent> = _events

    fun connect(sessionId: String) {
        val wsUrl = serverUrl
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .trimEnd('/') + "/ws/$sessionId"

        val request = Request.Builder().url(wsUrl).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                _events.tryEmit(WsEvent.Connected)
            }

            override fun onMessage(ws: WebSocket, text: String) {
                _events.tryEmit(WsEvent.Message(text))
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                _events.tryEmit(WsEvent.Error(t.message ?: "WebSocket error"))
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                _events.tryEmit(WsEvent.Disconnected)
            }
        })
    }

    fun disconnect() {
        webSocket?.close(1000, "User closed")
        webSocket = null
    }

    fun isConnected() = webSocket != null
}
