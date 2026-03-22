package com.user

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.speech.RecognizerIntent
import android.text.SpannableString
import android.view.KeyEvent
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.activity.viewModels
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.data.PrefsManager
import com.user.databinding.ActivityMainBinding
import com.user.service.AgentInfo
import com.user.ui.ChatAdapter
import com.user.viewmodel.ChatViewModel

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private val viewModel: ChatViewModel by viewModels()

    companion object {
        private const val VOICE_REQUEST_CODE = 100
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        if (PrefsManager(this).gatewayToken.isEmpty()) {
            Toast.makeText(this, "Please enter your Gateway Token", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }

        setupRecyclerView()
        setupObservers()
        setupInputHandlers()

        val sessionId    = intent.getStringExtra("session_id")
        val sessionTitle = intent.getStringExtra("session_title")
        if (sessionId != null) {
            title = sessionTitle ?: "Chat"
            supportActionBar?.setDisplayHomeAsUpEnabled(true)
            viewModel.loadSession(sessionId, sessionTitle)
            // When you open Chat History mode, it does not initiate new connections
        } else {
            viewModel.startNewSession()
            requestNotificationPermission()
            viewModel.startService(this)
        }

        requestNotificationPermission()
        viewModel.startService(this)
    }

    // ── Setup ─────────────────────────────────────────────────

    private fun setupRecyclerView() {
        chatAdapter = ChatAdapter()
        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity)
                .also { it.stackFromEnd = true }
            adapter = chatAdapter
        }
    }

    private fun setupObservers() {
        viewModel.status.observe(this) { status ->
            when {
                status.startsWith("✕") || status.startsWith("○") -> {
                    // Display only when there is a disconnection or error.
                    binding.statusText.text = status
                    binding.statusText.visibility = View.VISIBLE
                }
                status.startsWith("●") -> {
                    // Connection successful → Hide in 1.5 seconds
                    binding.statusText.text = status
                    binding.statusText.visibility = View.VISIBLE
                    binding.statusText.postDelayed({
                        binding.statusText.visibility = View.GONE
                    }, 1500)
                }
                else -> {
                    // Connecting / Handshaking → Displayed but not persistent.
                    binding.statusText.text = status
                    binding.statusText.visibility = View.VISIBLE
                }
            }
        }
        viewModel.messages.observe(this) { messages ->
            chatAdapter.submitList(messages.toList())
            if (messages.isNotEmpty())
                binding.recyclerView.scrollToPosition(messages.size - 1)
        }
        viewModel.agents.observe(this)          { setupAgentSpinner(it) }
        viewModel.isSending.observe(this)        { binding.sendButton.isEnabled = !it }
        viewModel.showTyping.observe(this)       { show ->
            binding.typingIndicator.visibility = if (show) View.VISIBLE else View.GONE
        }
        viewModel.pairingRequired.observe(this)  { showPairingDialog(it) }

        binding.statusText.visibility = View.GONE

        viewModel.status.observe(this) { status ->
            if (status.isNullOrEmpty()) {
                binding.statusText.visibility = View.GONE
            } else {
                binding.statusText.text = status
                binding.statusText.visibility = View.VISIBLE
            }
        }

        viewModel.toast.observe(this) { message ->
            message ?: return@observe
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
            viewModel.clearToast()
        }
    }

    private fun setupInputHandlers() {
        binding.sendButton.setOnClickListener    { sendMessage() }
        binding.voiceButton.setOnClickListener   { startVoiceInput() }
        binding.historyButton.setOnClickListener {
            startActivity(Intent(this, SessionsActivity::class.java))
        }
        binding.messageEditText.setOnKeyListener { _, keyCode, event ->
            if (keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN) {
                sendMessage(); true
            } else false
        }
    }

    // ── Helpers ───────────────────────────────────────────────

    private fun setupAgentSpinner(agents: List<AgentInfo>) {
        if (agents.isEmpty()) return
        val adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_item,
            agents.map { it.name }
        ).also { it.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item) }

        binding.agentSpinner.adapter = adapter
        binding.agentSpinner.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(
                parent: AdapterView<*>?, view: View?, position: Int, id: Long
            ) { viewModel.switchAgent(agents[position].agentId) }
            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
    }

    private fun showPairingDialog(deviceId: String?) {
        deviceId ?: return
        AlertDialog.Builder(this)
            .setTitle("Device Pairing Required")
            .setMessage(
                "Run on GX10:\n\n" +
                        "1. openclaw gateway call device.pair.list --json\n\n" +
                        "2. openclaw gateway call device.pair.approve \\\n" +
                        "   --params '{\"requestId\":\"<id>\"}' --json\n\n" +
                        "Device ID:\n${deviceId.take(12)}…\n\nThen restart the app."
            )
            .setPositiveButton("OK", null)
            .show()
    }

    private fun sendMessage() {
        val text = binding.messageEditText.text.toString().trim()
        if (text.isEmpty()) return
        binding.messageEditText.text?.clear()
        viewModel.sendMessage(text)
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

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 200
                )
            }
        }
    }

    // ── Overrides ─────────────────────────────────────────────

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == VOICE_REQUEST_CODE && resultCode == RESULT_OK) {
            data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                ?.firstOrNull()?.let { binding.messageEditText.setText(it) }
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        for (i in 0 until menu.size()) {
            val item  = menu.getItem(i)
            val title = SpannableString(item.title)
            title.setSpan(
                android.text.style.ForegroundColorSpan(android.graphics.Color.BLACK),
                0, title.length, android.text.Spannable.SPAN_INCLUSIVE_INCLUSIVE
            )
            item.title = title
        }
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_new_chat  -> { startActivity(Intent(this, MainActivity::class.java)); true }
            R.id.action_history   -> { startActivity(Intent(this, SessionsActivity::class.java)); true }
            R.id.action_settings  -> { startActivity(Intent(this, SettingsActivity::class.java)); true }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
