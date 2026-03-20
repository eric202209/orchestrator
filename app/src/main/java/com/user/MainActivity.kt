package com.user

import android.content.Intent
import android.os.Bundle
import android.speech.RecognizerIntent
import android.view.KeyEvent
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.data.ChatDatabase
import com.user.data.ChatMessage
import com.user.data.ChatSession
import com.user.data.PrefsManager
import com.user.databinding.ActivityMainBinding
import com.user.service.GatewayClient
import com.user.service.GatewayEvent
import com.user.service.OpenClawService
import com.user.service.Ed25519Manager
import com.user.ui.ChatAdapter
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private lateinit var openClawService: OpenClawService
    private lateinit var gatewayClient: GatewayClient
    private lateinit var prefs: PrefsManager
    private lateinit var database: ChatDatabase
    private lateinit var ed25519: Ed25519Manager

    private var currentSessionId = UUID.randomUUID().toString()

    // Track streaming bot message row
    private var streamingMsgId: Long = -1L

    companion object {
        private const val VOICE_REQUEST_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        prefs    = PrefsManager(this)
        database = ChatDatabase.getDatabase(application)
        ed25519  = Ed25519Manager(this)

        if (prefs.gatewayToken.isEmpty()) {
            Toast.makeText(this, "Please enter your Gateway Token", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }

        setupRecyclerView()
        setupServices()
        setupSession()
        loadMessages()
        connectGateway()

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
            layoutManager = LinearLayoutManager(this@MainActivity)
                .also { it.stackFromEnd = true }
            adapter = chatAdapter
        }
    }

    private fun setupServices() {
        openClawService = OpenClawService(
            database.chatDao(), prefs.serverUrl, prefs.gatewayToken
        )
        gatewayClient = GatewayClient(prefs.serverUrl, prefs.gatewayToken, ed25519)
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
                        title = "Chat ${SimpleDateFormat("MMM dd HH:mm",
                            Locale.getDefault()).format(Date())}"
                    )
                )
            }
        }
    }

    private fun loadMessages() {
        lifecycleScope.launch {
            database.chatDao().getMessagesBySession(currentSessionId).collect { messages ->
                chatAdapter.submitList(messages.toList())
                if (messages.isNotEmpty()) {
                    binding.recyclerView.scrollToPosition(messages.size - 1)
                }
            }
        }
    }

    private fun connectGateway() {
        gatewayClient.connect()
        lifecycleScope.launch {
            gatewayClient.events.collect { event ->
                when (event) {
                    is GatewayEvent.Connecting ->
                        binding.statusText.text = "○ Connecting…"

                    is GatewayEvent.HandshakeStarted ->
                        binding.statusText.text = "● Handshaking…"

                    is GatewayEvent.Ready -> {
                        binding.statusText.text = "● Connected"
                        binding.sendButton.isEnabled = true
                    }

                    is GatewayEvent.PairingRequired ->
                        showPairingDialog(event.deviceId)

                    is GatewayEvent.AuthError -> {
                        binding.statusText.text = "✕ Auth failed"
                        Toast.makeText(this@MainActivity,
                            event.message, Toast.LENGTH_LONG).show()
                    }

                    // ── Streaming: first delta → create placeholder ──
                    is GatewayEvent.StreamDelta -> {
                        binding.typingIndicator.visibility = View.GONE
                        if (streamingMsgId == -1L) {
                            val placeholder = ChatMessage(
                                sessionId = currentSessionId,
                                message   = event.text,
                                isUser    = false
                            )
                            streamingMsgId = openClawService.saveMessageToLocal(placeholder)
                        } else {
                            val current = chatAdapter.currentList
                                .firstOrNull { it.id == streamingMsgId }
                            if (current != null) {
                                openClawService.updateMessageContent(
                                    streamingMsgId,
                                    current.message + event.text
                                )
                            }
                        }
                    }

                    // ── Stream complete ──────────────────────────────
                    is GatewayEvent.StreamFinal -> {
                        if (streamingMsgId != -1L) {
                            openClawService.updateMessageContent(
                                streamingMsgId, event.fullText
                            )
                            streamingMsgId = -1L
                        } else {
                            openClawService.saveMessageToLocal(
                                ChatMessage(
                                    sessionId = currentSessionId,
                                    message   = event.fullText,
                                    isUser    = false
                                )
                            )
                        }
                        database.chatDao().updateSessionTime(
                            currentSessionId, System.currentTimeMillis()
                        )
                        binding.sendButton.isEnabled = true
                    }

                    is GatewayEvent.ToolCall -> {
                        val icon = if (event.done) "✅" else "⚙️"
                        binding.statusText.text = "$icon ${event.name}"
                    }

                    is GatewayEvent.Disconnected ->
                        binding.statusText.text = "○ Disconnected"

                    is GatewayEvent.Error -> {
                        binding.statusText.text = "✕ ${event.message}"
                        binding.typingIndicator.visibility = View.GONE
                        binding.sendButton.isEnabled = true
                        Toast.makeText(this@MainActivity,
                            event.message, Toast.LENGTH_SHORT).show()
                    }
                }
            }
        }
    }

    private fun sendMessage() {
        val text = binding.messageEditText.text.toString().trim()
        if (text.isEmpty()) return
        binding.messageEditText.text?.clear()
        binding.sendButton.isEnabled = false
        binding.root.visibility = View.VISIBLE

        lifecycleScope.launch {
            openClawService.saveMessageToLocal(
                ChatMessage(sessionId = currentSessionId, message = text, isUser = true)
            )
            database.chatDao().updateSessionTime(currentSessionId, System.currentTimeMillis())
            gatewayClient.sendMessage(text)
        }
    }

    private fun showPairingDialog(deviceId: String) {
        binding.statusText.text = "⚠ Pairing required"
        AlertDialog.Builder(this)
            .setTitle("Device Pairing Required")
            .setMessage(
                "Run on GX10:\n\n" +
                        "1. openclaw gateway call device.pair.list --json\n\n" +
                        "2. openclaw gateway call device.pair.approve \\\n" +
                        "   --params '{\"requestId\":\"<id>\"}' --json\n\n" +
                        "Device ID (first 12 chars):\n${deviceId.take(12)}…\n\n" +
                        "Then restart the app."
            )
            .setPositiveButton("OK", null)
            .show()
    }

    private fun startVoiceInput() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak your message…")
        }
        try { startActivityForResult(intent, VOICE_REQUEST_CODE) }
        catch (e: Exception) {
            Toast.makeText(this, "Voice input not available", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VOICE_REQUEST_CODE && resultCode == RESULT_OK) {
            data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                ?.firstOrNull()?.let { binding.messageEditText.setText(it) }
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java)); true
            }
            R.id.action_new_chat -> {
                startActivity(Intent(this, MainActivity::class.java)); true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        gatewayClient.disconnect()
    }
}
