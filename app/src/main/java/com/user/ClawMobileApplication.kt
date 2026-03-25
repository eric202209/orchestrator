package com.user

import android.app.Application
import com.user.data.ChatDatabase
import com.user.data.PrefsManager
import com.user.repository.ChatRepository

/**
 * Application class for ClawMobile
 * Provides access to DAOs and other application-wide components
 */
class ClawMobileApplication : Application() {

    private val database by lazy { ChatDatabase.getDatabase(this) }
    private val prefs by lazy { PrefsManager(this) }

    // DAO instances for use in activities
    val gitConnectionDao by lazy { database.gitConnectionDao() }
    val projectContextDao by lazy { database.projectContextDao() }
    val chatDao by lazy { database.chatDao() }
    val taskDao by lazy { database.taskDao() }

    val repository by lazy {
        ChatRepository(
            chatDao,
            gitConnectionDao,
            projectContextDao,
            taskDao,
            prefs
        )
    }
}