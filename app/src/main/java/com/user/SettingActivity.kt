package com.user

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.user.data.PrefsManager
import com.user.databinding.ActivitySettingsBinding

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
        binding.serverUrlInput.setText(prefs.serverUrl)
        binding.gatewayTokenInput.setText(prefs.gatewayToken)

        binding.saveButton.setOnClickListener {
            val url   = binding.serverUrlInput.text.toString().trim()
            val token = binding.gatewayTokenInput.text.toString().trim()
            when {
                url.isEmpty()   -> Toast.makeText(this,
                    "Server URL cannot be empty", Toast.LENGTH_SHORT).show()
                token.isEmpty() -> Toast.makeText(this,
                    "Gateway Token cannot be empty", Toast.LENGTH_SHORT).show()
                else -> {
                    prefs.serverUrl    = url
                    prefs.gatewayToken = token
                    Toast.makeText(this,
                        "Saved! Restart app to reconnect.", Toast.LENGTH_LONG).show()
                    finish()
                }
            }
        }
    }

    override fun onSupportNavigateUp(): Boolean { finish(); return true }
}


