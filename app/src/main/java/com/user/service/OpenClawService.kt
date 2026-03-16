package com.user.service

import com.user.data.ChatDao
import com.user.data.ChatMessage
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import retrofit2.http.Body
import retrofit2.http.POST
import java.util.concurrent.TimeUnit

interface OpenClawApi {
    @POST("api/messages/send")
    suspend fun sendMessage(@Body request: SendMessageRequest): SendMessageResponse
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

class OpenClawService(private val chatDao: ChatDao, baseUrl: String) {

    private val client = OkHttpClient.Builder()
        .addInterceptor(HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BODY
        })
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val retrofit = Retrofit.Builder()
        .baseUrl(if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/")
        .client(client)
        .addConverterFactory(GsonConverterFactory.create())
        .build()

    private val api = retrofit.create(OpenClawApi::class.java)

    suspend fun sendMessage(sessionId: String, message: String): SendMessageResponse {
        return withContext(Dispatchers.IO) {
            try {
                api.sendMessage(SendMessageRequest(sessionId, message, System.currentTimeMillis()))
            } catch (e: Exception) {
                SendMessageResponse(false, error = e.message ?: "Unknown error")
            }
        }
    }

    suspend fun saveMessageToLocal(message: ChatMessage) {
        withContext(Dispatchers.IO) {
            chatDao.insertMessage(message)
        }
    }
}


