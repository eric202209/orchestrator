package com.user.ui.activities

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.user.R
import android.widget.ProgressBar
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class SplashActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_splash)

        val progressBar = findViewById<ProgressBar>(R.id.horizontalProgressBar)

        lifecycleScope.launch {
            // Simulate the process of loading the model
            var progress = 0
            while (progress <= 100) {
                progressBar.progress = progress
                delay(30) // Adjusting the number, control loading speed
                progress += 2
            }

            // Jump to MainActivity
            val intent = Intent(this@SplashActivity, MainActivity::class.java)
            startActivity(intent)

            // Add fade-in and fade-out cutscenes
            overridePendingTransition(android.R.anim.fade_in, android.R.anim.fade_out)
            finish()
        }
    }
}