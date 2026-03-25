package com.user.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/**
 * GitHub integration data model - stores GitHub Personal Access Token
 */
@Entity(tableName = "git_connections")
data class GitConnection(
    @PrimaryKey
    val id: String = "github_default",
    val platform: String, // "GITHUB"
    val apiUrl: String = "https://api.github.com",
    val token: String, // GitHub PAT
    val defaultRepo: String? = null, // "owner/repo" format
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis()
)