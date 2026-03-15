package com.yourname.openclawassistant.service

import com.yourname.openclawassistant.data.ChatMessage
import com.yourname.openclawassistant.data.ChatDao
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.POST

interface OpenClawApi {
    @POST("api/messages/send")
    suspend fun sendMessage(@Body request: SendMessageRequest): SendMessageResponse
    
    @POST("api/messages/receive")
    suspend fun receiveMessages(@Body request: ReceiveMessagesRequest): ReceiveMessagesResponse
}

data class SendMessageRequest(
    val sessionId: String,
    val message: String,
    val timestamp: Long
)

data class SendMessageResponse(
    val success: Boolean,
    val response: String? = null,
    val error: String? = null
)

data class ReceiveMessagesRequest(
    val sessionId: String,
    val since: Long
)

data class ReceiveMessagesResponse(
    val messages: List<ChatMessage>
)

class OpenClawService(private val chatDao: ChatDao) {
    private val retrofit = Retrofit.Builder()
        .baseUrl("http://YOUR_OPENCLAW_IP:18789/") // Replace with your server IP
        .addConverterFactory(GsonConverterFactory.create())
        .build()
    
    private val api = retrofit.create(OpenClawApi::class.java)
    
    suspend fun sendMessage(sessionId: String, message: String): SendMessageResponse {
        return withContext(Dispatchers.IO) {
            try {
                val request = SendMessageRequest(sessionId, message, System.currentTimeMillis())
                api.sendMessage(request)
            } catch (e: Exception) {
                SendMessageResponse(false, error = e.message ?: "Unknown error")
            }
        }
    }
    
    suspend fun receiveMessages(sessionId: String, since: Long): List<ChatMessage> {
        return withContext(Dispatchers.IO) {
            try {
                val request = ReceiveMessagesRequest(sessionId, since)
                api.receiveMessages(request).messages
            } catch (e: Exception) {
                emptyList()
            }
        }
    }
    
    suspend fun saveMessageToLocal(message: ChatMessage) {
        withContext(Dispatchers.IO) {
            chatDao.insertMessage(message)
        }
    }
}
