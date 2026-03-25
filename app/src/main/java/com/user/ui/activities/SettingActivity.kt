package com.user.ui.activities

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.user.data.PrefsManager
import com.user.databinding.ActivitySettingsBinding

/**
 * Settings activity for configuring gateway and GitHub tokens
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

        // Load existing values
        binding.serverUrlInput.setText(prefs.serverUrl)
        binding.gatewayTokenInput.setText(prefs.gatewayToken)
        binding.githubTokenInput.setText(prefs.githubToken)
        binding.githubApiUrlInput.setText(prefs.githubApiUrl)
        binding.githubDefaultRepoInput.setText(prefs.githubDefaultRepo)

        binding.saveButton.setOnClickListener {
            val serverUrl   = binding.serverUrlInput.text.toString().trim()
            val gatewayToken = binding.gatewayTokenInput.text.toString().trim()
            val githubToken = binding.githubTokenInput.text.toString().trim()
            val githubApiUrl = binding.githubApiUrlInput.text.toString().trim()
            val githubDefaultRepo = binding.githubDefaultRepoInput.text.toString().trim()

            when {
                serverUrl.isEmpty() -> {
                    Toast.makeText(this, "Server URL cannot be empty", Toast.LENGTH_SHORT).show()
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


