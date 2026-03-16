package com.user.data

import android.content.Context

class PrefsManager(context: Context) {
    private val prefs = context.getSharedPreferences("openclaw_prefs", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("server_url", "http://192.168.1.100:18789") ?: "http://192.168.1.100:18789"
        set(value) = prefs.edit().putString("server_url", value).apply()
}

