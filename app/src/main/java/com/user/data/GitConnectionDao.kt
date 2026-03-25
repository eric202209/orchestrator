package com.user.data

import androidx.room.*
import kotlinx.coroutines.flow.Flow

/**
 * DAO for GitConnection operations
 */
@Dao
interface GitConnectionDao {
    @Query("SELECT * FROM git_connections WHERE id = :id")
    suspend fun getConnection(id: String): GitConnection?

    @Query("SELECT * FROM git_connections WHERE platform = 'GITHUB'")
    fun getGithubConnection(): Flow<GitConnection?>

    @Query("SELECT * FROM git_connections")
    fun getAllConnections(): Flow<List<GitConnection>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertConnection(connection: GitConnection): Long

    @Update
    suspend fun updateConnection(connection: GitConnection)

    @Delete
    suspend fun deleteConnection(connection: GitConnection)

    @Query("DELETE FROM git_connections WHERE id = :id")
    suspend fun deleteConnectionById(id: String)
}