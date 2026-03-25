package com.user.data

import androidx.room.*
import kotlinx.coroutines.flow.Flow

/**
 * Data Access Object for API requests and responses
 */
@Dao
interface ApiRequestDao {
    // ── Requests ─────────────────────────────────────────────
    @Query("SELECT * FROM api_requests ORDER BY updatedAt DESC")
    fun getAllRequests(): Flow<List<ApiRequest>>

    @Query("SELECT * FROM api_requests WHERE id = :id")
    suspend fun getRequest(id: Long): ApiRequest?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertRequest(request: ApiRequest): Long

    @Update
    suspend fun updateRequest(request: ApiRequest)

    @Delete
    suspend fun deleteRequest(request: ApiRequest)

    // ── Responses ────────────────────────────────────────────
    @Query("SELECT * FROM api_responses WHERE requestId = :requestId ORDER BY executedAt DESC")
    fun getResponsesForRequest(requestId: Long): Flow<List<ApiResponse>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertResponse(response: ApiResponse): Long

    @Query("DELETE FROM api_responses WHERE requestId = :requestId")
    suspend fun deleteResponsesForRequest(requestId: Long)
}