package com.user

import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.KeyEvent
import android.view.Menu
import android.view.MenuItem
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.data.ChatDatabase
import com.user.data.ChatMessage
import com.user.data.ChatSession
import com.user.data.PrefsManager
import com.user.databinding.ActivityMainBinding
import com.user.service.OpenClawService
import com.user.service.WebSocketManager
import com.user.service.WsEvent
import com.user.ui.ChatAdapter
import kotlinx.coroutines.launch
import java.util.UUID

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private lateinit var openClawService: OpenClawService
    private lateinit var webSocketManager: WebSocketManager
    private lateinit var prefs: PrefsManager
    private lateinit var database: ChatDatabase

    private var currentSessionId = UUID.randomUUID().toString()

    companion object {
        private const val VOICE_REQUEST_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        prefs = PrefsManager(this)
        database = ChatDatabase.getDatabase(application)

        setupRecyclerView()
        setupServices()
        setupSession()
        loadMessages()
        connectWebSocket()

        binding.sendButton.setOnClickListener { sendMessage() }
        binding.voiceButton.setOnClickListener { startVoiceInput() }
        binding.historyButton.setOnClickListener {
            startActivity(Intent(this, SessionsActivity::class.java))
        }

        binding.messageEditText.setOnKeyListener { _, keyCode, event ->
            if (keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN) {
                sendMessage(); true
            } else false
        }
    }

    private fun setupRecyclerView() {
        chatAdapter = ChatAdapter()
        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity).also {
                it.stackFromEnd = true
            }
            adapter = chatAdapter
        }
    }

    private fun setupServices() {
        openClawService = OpenClawService(database.chatDao(), prefs.serverUrl)
        webSocketManager = WebSocketManager(prefs.serverUrl)
    }

    private fun setupSession() {
        val sessionFromIntent = intent.getStringExtra("session_id")
        if (sessionFromIntent != null) {
            currentSessionId = sessionFromIntent
            title = intent.getStringExtra("session_title") ?: "Chat"
        } else {
            lifecycleScope.launch {
                database.chatDao().insertSession(
                    ChatSession(
                        sessionId = currentSessionId,
                        title = "Chat ${java.text.SimpleDateFormat("MMM dd HH:mm",
                            java.util.Locale.getDefault()).format(java.util.Date())}"
                    )
                )
            }
        }
    }

    private fun loadMessages() {
        lifecycleScope.launch {
            database.chatDao().getMessagesBySession(currentSessionId).collect { messages ->
                chatAdapter.submitList(messages)
                if (messages.isNotEmpty()) {
                    binding.recyclerView.scrollToPosition(messages.size - 1)
                }
            }
        }
    }

    private fun connectWebSocket() {
        webSocketManager.connect(currentSessionId)
        lifecycleScope.launch {
            webSocketManager.events.collect { event ->
                when (event) {
                    is WsEvent.Connected ->
                        binding.statusText.text = "● Connected"
                    is WsEvent.Disconnected ->
                        binding.statusText.text = "○ Disconnected"
                    is WsEvent.Error ->
                        binding.statusText.text = "○ ${event.error}"
                    is WsEvent.Message -> {
                        val botMessage = ChatMessage(
                            sessionId = currentSessionId,
                            message = event.text,
                            isUser = false
                        )
                        openClawService.saveMessageToLocal(botMessage)
                        database.chatDao().updateSessionTime(currentSessionId, System.currentTimeMillis())
                    }
                }
            }
        }
    }

    private fun sendMessage() {
        val messageText = binding.messageEditText.text.toString().trim()
        if (messageText.isEmpty()) return
        binding.messageEditText.text?.clear()

        lifecycleScope.launch {
            val userMessage = ChatMessage(
                sessionId = currentSessionId,
                message = messageText,
                isUser = true
            )
            openClawService.saveMessageToLocal(userMessage)
            database.chatDao().updateSessionTime(currentSessionId, System.currentTimeMillis())

            // Fall back to HTTP if WebSocket is not connected
            if (!webSocketManager.isConnected()) {
                val response = openClawService.sendMessage(currentSessionId, messageText)
                if (response.success && response.response != null) {
                    openClawService.saveMessageToLocal(
                        ChatMessage(
                            sessionId = currentSessionId,
                            message = response.response,
                            isUser = false
                        )
                    )
                } else if (!response.success) {
                    Toast.makeText(this@MainActivity,
                        "Error: ${response.error}", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    private fun startVoiceInput() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak your message...")
        }
        try {
            startActivityForResult(intent, VOICE_REQUEST_CODE)
        } catch (e: Exception) {
            Toast.makeText(this, "Voice input not available on this device", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VOICE_REQUEST_CODE && resultCode == RESULT_OK) {
            val results = data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
            results?.firstOrNull()?.let {
                binding.messageEditText.setText(it)
            }
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            R.id.action_new_chat -> {
                startActivity(Intent(this, MainActivity::class.java))
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        webSocketManager.disconnect()
    }
}

