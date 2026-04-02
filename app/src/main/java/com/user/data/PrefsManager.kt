package com.user.data

import android.content.Context
import android.util.Log
import com.user.BuildConfig

private const val PREFS_TAG = "PrefsManager"

class PrefsManager(context: Context) {
    private val prefs = context.getSharedPreferences("openclaw_prefs", Context.MODE_PRIVATE)

    // The default comes from local.properties
    var serverUrl: String
        get() = prefs.getString("server_url", BuildConfig.DEFAULT_SERVER_URL)
            ?: BuildConfig.DEFAULT_SERVER_URL
        set(value) = prefs.edit().putString("server_url", value).apply()

    // OpenClaw Gateway token
    var gatewayToken: String
        get() = prefs.getString("gateway_token", "") ?: ""
        set(value) = prefs.edit().putString("gateway_token", value).apply()

    // Device ID for Ed25519 pairing (auto-generated once)
    var deviceId: String
        get() = prefs.getString("device_id", "") ?: ""
        set(value) = prefs.edit().putString("device_id", value).apply()

    // ── GitHub Integration ───────────────────────────────────
    var githubToken: String
        get() = prefs.getString("github_token", "") ?: ""
        set(value) = prefs.edit().putString("github_token", value).apply()

    var githubApiUrl: String
        get() = prefs.getString("github_api_url", "https://api.github.com") ?: "https://api.github.com"
        set(value) = prefs.edit().putString("github_api_url", value).apply()

    var githubDefaultRepo: String
        get() = prefs.getString("github_default_repo", "") ?: ""
        set(value) = prefs.edit().putString("github_default_repo", value).apply()

    // ── API Client Environments ───────────────────────────────
    var currentEnvironment: String
        get() = prefs.getString("api_current_env", "default") ?: "default"
        set(value) = prefs.edit().putString("api_current_env", value).apply()

    // ── Orchestrator Integration ──────────────────────────────
    var orchestratorServerUrl: String
        get() {
            val value = prefs.getString("orchestrator_server_url", "") ?: ""
            Log.d(PREFS_TAG, "orchestratorServerUrl GET: $value")
            return value
        }
        set(value) {
            Log.d(PREFS_TAG, "orchestratorServerUrl SET: $value")
            prefs.edit().putString("orchestrator_server_url", value).apply()
        }

    var orchestratorApiKey: String
        get() {
            val value = prefs.getString("orchestrator_api_key", "") ?: ""
            // Don't log the full API key for security, just show if it's set
            Log.d(PREFS_TAG, "orchestratorApiKey GET: ${if (value.isEmpty()) "(empty)" else "${value.substring(0, 8)}...${value.length} chars"}")
            return value
        }
        set(value) {
            Log.d(PREFS_TAG, "orchestratorApiKey SET: ${if (value.isEmpty()) "(empty)" else "${value.substring(0, 8)}...${value.length} chars"}")
            prefs.edit().putString("orchestrator_api_key", value).apply()
        }

    // Check if Orchestrator is configured
    fun isOrchestratorConfigured(): Boolean {
        return orchestratorServerUrl.isNotEmpty() && orchestratorApiKey.isNotEmpty()
    }
}