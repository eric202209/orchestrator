package com.user

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.data.ChatDatabase
import com.user.databinding.ActivitySessionsBinding
import com.user.ui.SessionAdapter
import kotlinx.coroutines.launch

class SessionsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySessionsBinding
    private lateinit var sessionAdapter: SessionAdapter
    private lateinit var database: ChatDatabase

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySessionsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "Chat History"

        database = ChatDatabase.getDatabase(application)

        sessionAdapter = SessionAdapter(
            onClick = { session ->
                val intent = Intent(this, MainActivity::class.java).apply {
                    putExtra("session_id", session.sessionId)
                    putExtra("session_title", session.title)
                    flags = Intent.FLAG_ACTIVITY_CLEAR_TOP
                }
                startActivity(intent)
            },
            onDelete = { session ->
                lifecycleScope.launch {
                    database.chatDao().deleteMessagesBySession(session.sessionId)
                    database.chatDao().deleteSession(session.sessionId)
                }
            }
        )

        binding.sessionsRecyclerView.apply {
            layoutManager = LinearLayoutManager(this@SessionsActivity)
            adapter = sessionAdapter
        }

        lifecycleScope.launch {
            database.chatDao().getAllSessions().collect { sessions ->
                sessionAdapter.submitList(sessions)
            }
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}


