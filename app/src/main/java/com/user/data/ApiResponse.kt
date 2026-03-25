package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Entity representing an API response stored in the database
 */
@Entity(tableName = "api_responses")
data class ApiResponse(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val requestId: Long,
    val status: Int,
    val headers: String, // JSON string
    val body: String,
    val durationMs: Long,
    val executedAt: Long = System.currentTimeMillis()
)

