package com.user.data

import androidx.room.TypeConverter

/**
 * Type converters for Task entity fields
 */
class TaskConverter {

    /**
     * Convert List<String> to JSON String for storage
     */
    @TypeConverter
    fun fromStringList(value: List<String>): String {
        return value.joinToString("|")
    }

    /**
     * Convert JSON String to List<String>
     */
    @TypeConverter
    fun toStringList(value: String): List<String> {
        return if (value.isEmpty()) emptyList() else value.split("|")
    }
}