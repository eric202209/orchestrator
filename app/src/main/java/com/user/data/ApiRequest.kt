package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Entity representing an API request for the API Client tool
 */
@Entity(tableName = "api_requests")
data class ApiRequest(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val name: String,
    val url: String,
    val method: String,
    val headers: String, // JSON string
    val body: String?,
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)
