package com.user.data

import androidx.room.*
import kotlinx.coroutines.flow.Flow

/**
 * DAO for ProjectContext operations
 */
@Dao
interface ProjectContextDao {
    @Query("SELECT * FROM project_context WHERE id = :id")
    suspend fun getContext(id: String): ProjectContext?

    @Query("SELECT * FROM project_context ORDER BY lastUpdated DESC")
    fun getAllContexts(): Flow<List<ProjectContext>>

    @Query("SELECT * FROM project_context WHERE sessionId = :sessionId ORDER BY lastUpdated DESC LIMIT 1")
    suspend fun getCurrentContext(sessionId: String): ProjectContext?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertContext(context: ProjectContext): Long

    @Update
    suspend fun updateContext(context: ProjectContext)

    @Delete
    suspend fun deleteContext(context: ProjectContext)

    @Query("DELETE FROM project_context WHERE id = :id")
    suspend fun deleteContextById(id: String)

    // ProjectFiles
    @Query("SELECT * FROM project_files WHERE contextId = :contextId")
    fun getFilesByContext(contextId: String): Flow<List<ProjectFile>>

    @Query("SELECT * FROM project_files WHERE contextId = :contextId AND path = :path")
    suspend fun getFile(contextId: String, path: String): ProjectFile?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertFile(file: ProjectFile): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertFiles(files: List<ProjectFile>)

    @Delete
    suspend fun deleteFile(file: ProjectFile)

    @Query("DELETE FROM project_files WHERE contextId = :contextId")
    suspend fun deleteFilesByContext(contextId: String)
}