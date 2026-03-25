package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * Project context sent by OpenClaw gateway
 */
@Entity(tableName = "project_context")
data class ProjectContext(
    @PrimaryKey
    val id: String = "current",
    val sessionId: String,
    val projectPath: String,
    val files: String, // JSON array of file paths
    val lastUpdated: Long = System.currentTimeMillis(),
    val metadata: String? = null // Additional JSON metadata
)

/**
 * Individual file context
 */
@Entity(tableName = "project_files")
data class ProjectFile(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val contextId: String,
    val path: String,
    val size: Long = 0,
    val language: String = "",
    val lastModified: Long = System.currentTimeMillis()
)