package com.user.ui.activities

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.user.BuildConfig
import com.user.data.PrefsManager
import com.user.databinding.ActivitySettingsBinding

/**
 * Settings activity for configuring gateway, GitHub, and Orchestrator integration.
 *
 * Network Configuration Guide:
 *
 * OpenClaw Gateway (Required):
 * - Local WiFi (phone on same network as host): http://<host-lan-ip>:18789
 *   Example: http://192.168.1.100:18789
 * - Android Emulator: http://localhost:18789 or http://xxx.x.x.x:18789
 * - Mobile Data / Remote: Tailscale IP of host:18789
 *
 * Orchestrator Dashboard/API (Optional):
 * - Android Emulator: http://xxx.xx.x.x:8080 (Docker bridge network)
 *   Dashboard UI: http://xxx.xx.x.x:3000
 * - Local WiFi (phone on same network as host): http://<host-lan-ip>:8080
 *   (Requires Docker port mapping: -p 8080:8080)
 *
 * Note: Orchestrator integration is optional. The app works fully without it,
 * using only local data from the OpenClaw Gateway.
 */
class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private lateinit var prefs: PrefsManager

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "Settings"

        prefs = PrefsManager(this)

        // Load existing values or use defaults from local.properties (BuildConfig)
        binding.serverUrlInput.setText(prefs.serverUrl)

        // Pre-fill gateway token from BuildConfig if not already saved
        val savedGatewayToken = prefs.gatewayToken
        val defaultApiKey = BuildConfig.MOBILE_GATEWAY_API_KEY
        if (savedGatewayToken.isEmpty() && defaultApiKey.isNotEmpty()) {
            binding.gatewayTokenInput.setText(defaultApiKey)
            binding.gatewayTokenInput.hint = "Auto-filled from local.properties"
        } else {
            binding.gatewayTokenInput.setText(savedGatewayToken)
        }

        binding.githubTokenInput.setText(prefs.githubToken)
        binding.githubApiUrlInput.setText(prefs.githubApiUrl)
        binding.githubDefaultRepoInput.setText(prefs.githubDefaultRepo)

        // Load Orchestrator settings (optional)
        binding.orchestratorServerUrlInput.setText(prefs.orchestratorServerUrl)

        // Pre-fill orchestrator API key from saved value or BuildConfig
        val savedOrchApiKey = prefs.orchestratorApiKey
        if (savedOrchApiKey.isEmpty() && defaultApiKey.isNotEmpty()) {
            binding.orchestratorApiKeyInput.setText(defaultApiKey)
            binding.orchestratorApiKeyInput.hint = "Auto-filled from local.properties"
        } else {
            binding.orchestratorApiKeyInput.setText(savedOrchApiKey)
        }

        // Always show Orchestrator section so users can configure it
        binding.orchestratorSection.visibility = android.view.View.VISIBLE

        binding.saveButton.setOnClickListener {
            val serverUrl   = binding.serverUrlInput.text.toString().trim()
            val gatewayToken = binding.gatewayTokenInput.text.toString().trim()
            val githubToken = binding.githubTokenInput.text.toString().trim()
            val githubApiUrl = binding.githubApiUrlInput.text.toString().trim()
            val githubDefaultRepo = binding.githubDefaultRepoInput.text.toString().trim()

            // Orchestrator settings (optional)
            val orchestratorServerUrl = binding.orchestratorServerUrlInput.text.toString().trim()
            val orchestratorApiKey = binding.orchestratorApiKeyInput.text.toString().trim()

            when {
                serverUrl.isEmpty() -> {
                    Toast.makeText(this, "OpenClaw Gateway URL cannot be empty", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                gatewayToken.isEmpty() -> {
                    Toast.makeText(this, "Gateway Token cannot be empty", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                // Validate Orchestrator URL if provided - only check for invalid paths like /api/v1
                orchestratorServerUrl.isNotEmpty() && orchestratorServerUrl.contains("/api/v1") -> {
                    Toast.makeText(
                        this,
                        "Orchestrator Backend URL should be the base API endpoint (e.g., http://xxx.xx.x.x:8080). Do not include /api/v1 in the URL.",
                        Toast.LENGTH_LONG
                    ).show()
                    return@setOnClickListener
                }
                else -> {
                    prefs.serverUrl = serverUrl
                    prefs.gatewayToken = gatewayToken

                    // GitHub settings (optional)
                    if (githubToken.isNotBlank()) {
                        prefs.githubToken = githubToken
                    }
                    if (githubApiUrl.isNotBlank()) {
                        prefs.githubApiUrl = githubApiUrl
                    }
                    if (githubDefaultRepo.isNotBlank()) {
                        prefs.githubDefaultRepo = githubDefaultRepo
                    }

                    // Orchestrator settings (optional)
                    // Note: The API key is the same as the Gateway Token - there's only one key!
                    if (orchestratorServerUrl.isNotBlank()) {
                        prefs.orchestratorServerUrl = orchestratorServerUrl
                        // Auto-use the gateway token for orchestrator api key since they're the same
                        prefs.orchestratorApiKey = gatewayToken
                        Toast.makeText(
                            this,
                            "Orchestrator configured: $orchestratorServerUrl (using Gateway Token)",
                            Toast.LENGTH_SHORT
                        ).show()
                    } else {
                        // Clear Orchestrator settings if URL is empty - keep API key synced with gateway token
                        prefs.orchestratorServerUrl = ""
                        if (gatewayToken.isNotEmpty()) {
                            prefs.orchestratorApiKey = gatewayToken
                        }
                    }

                    // Keep Orchestrator section visible for continued configuration
                    binding.orchestratorSection.visibility = android.view.View.VISIBLE

                    Toast.makeText(this, "Settings saved! Reconnecting...", Toast.LENGTH_LONG).show()
                    finish()
                }
            }
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}