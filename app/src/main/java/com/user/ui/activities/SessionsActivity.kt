package com.user.ui.activities

import android.content.Intent
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import androidx.activity.viewModels
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.databinding.ActivitySessionsBinding
import com.user.ui.SessionAdapter
import com.user.viewmodel.SessionViewModel

/**
 * Chat history activity showing all chat sessions
 */
class SessionsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySessionsBinding
    private lateinit var sessionAdapter: SessionAdapter
    private val viewModel: SessionViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySessionsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "Chat History"

        sessionAdapter = SessionAdapter(
            onClick = { session ->
                val intent = Intent(this, MainActivity::class.java).apply {
                    putExtra("session_id",    session.sessionId)
                    putExtra("session_title", session.title)
                }
                startActivity(intent)
            },
            onDelete = { session -> viewModel.deleteSession(session) }
        )

        binding.sessionsRecyclerView.apply {
            layoutManager = LinearLayoutManager(this@SessionsActivity)
            adapter = sessionAdapter
        }

        viewModel.sessions.observe(this) { sessions ->
            sessionAdapter.submitList(sessions)
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, 1, 0, "Newest First").setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
        menu.add(0, 2, 0, "Oldest First").setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
        menu.add(0, 3, 0, "Delete All").setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)

        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            android.R.id.home -> { finish(); true }
            1 -> { viewModel.sortNewest(); true }
            2 -> { viewModel.sortOldest(); true }
            3 -> { viewModel.deleteAll(); true }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onSupportNavigateUp(): Boolean { finish(); return true }
}


