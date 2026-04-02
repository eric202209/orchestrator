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
 *   Example: If your computer has IP xx.x.x.xxx, use http://xx.x.x.xxx:18789
 * - Android Emulator: http://localhost:18789 or http://xxx.x.x.x:18789
 * - Mobile Data / Remote: Tailscale IP of host:18789
 *
 * Orchestrator Dashboard/API (Optional):
 * - Local WiFi (phone on same network as host): http://<host-lan-ip>:8080
 *   Example: If your computer has IP xx.x.x.xxx, use http://xx.x.x.xxx:8080
 * - Android Emulator ONLY: http://xxx.xx.x.x:8080 (Docker bridge network)
 *   Dashboard UI: http://xxx.xx.x.x:3000
 *
 * IMPORTANT: Find your host machine's LAN IP with: ip addr show | grep "inet "
 * Look for the WiFi/Ethernet interface (e.g., eth0, wlan0), NOT lo or docker0
 * Example output: inet xx.x.x.xxx/xx brd ... scope global wlP9s9
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

            // Orchestrator settings (optional) - auto-correct common URL mistakes
            var orchestratorServerUrl = binding.orchestratorServerUrlInput.text.toString().trim()
            if (orchestratorServerUrl.isNotEmpty()) {
                orchestratorServerUrl = orchestratorServerUrl.trimEnd('/')
                if (orchestratorServerUrl.endsWith("/api/v1")) {
                    orchestratorServerUrl = orchestratorServerUrl.removeSuffix("/api/v1")
                    Toast.makeText(
                        this,
                        "Auto-corrected: removed /api/v1 suffix. URL will be saved as $orchestratorServerUrl",
                        Toast.LENGTH_SHORT
                    ).show()
                }
            }
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

                    // Orchestrator settings (optional) - auto-corrected URL above
                    // Note: The API key is the same as the Gateway Token - there's only one key!
                    if (orchestratorServerUrl.isNotBlank()) {
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

    /**
     * Show a toast with network troubleshooting tips
     */
    private fun showNetworkTroubleshootingTips() {
        val message = """
            Network Troubleshooting:
            1. Check Android device is on same WiFi as host machine
            2. Find host IP: ip addr show | grep "inet "
            3. Use the WiFi/Ethernet IP (e.g., xx.x.x.xxx), NOT localhost or Docker IPs
            4. Make sure firewall allows port 8080: sudo ufw allow 8080/tcp
        """.trimIndent()
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }
}
