package com.user.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * Updated database with developer tools integration entities
 */
@Database(
    entities = [
        ChatMessage::class,
        ChatSession::class,
        GitConnection::class,
        ProjectContext::class,
        ProjectFile::class,
        Task::class,
        ApiRequest::class,
        ApiResponse::class
    ],
    version = 8,
    exportSchema = false
)
abstract class ChatDatabase : RoomDatabase() {
    abstract fun chatDao(): ChatDao
    abstract fun gitConnectionDao(): GitConnectionDao
    abstract fun projectContextDao(): ProjectContextDao
    abstract fun taskDao(): TaskDao
    abstract fun apiRequestDao(): ApiRequestDao

    companion object {
        @Volatile
        private var INSTANCE: ChatDatabase? = null

        fun getDatabase(context: Context): ChatDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    ChatDatabase::class.java,
                    "openclaw_database"
                )
                    .fallbackToDestructiveMigration()
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}



