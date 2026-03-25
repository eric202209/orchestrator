package com.user.ui.activities

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
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
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.R
import com.user.data.PrefsManager
import com.user.databinding.ActivityMainBinding
import com.user.service.AgentInfo
import com.user.ui.ChatAdapter
import com.user.ui.tasks.TaskListActivity
import com.user.ui.tools.GitHubActivity
import com.user.viewmodel.ChatViewModel

/**
 * Main chat activity - primary interface for user interaction
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private val viewModel: ChatViewModel by viewModels()
    private var selectedFileUri: Uri? = null  // Stores selected file/image for preview

    private val voiceLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == RESULT_OK) {
            result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                ?.firstOrNull()?.let { binding.messageEditText.setText(it) }
        }
    }

    private val filePickerLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == RESULT_OK) {
            result.data?.data?.let { uri ->
                selectedFileUri = uri
                showFilePreview(uri)
                binding.attachButton.setImageResource(android.R.drawable.ic_delete)
                binding.attachButton.contentDescription = "Remove file"
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        if (PrefsManager(this).gatewayToken.isEmpty()) {
            Toast.makeText(this, "Please enter your Gateway Token", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
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
            viewModel.loadSession(sessionId)
        } else {
            viewModel.startNewSession()
            requestNotificationPermission()
            viewModel.startService(this)
        }
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
                    binding.statusText.text = status
                    binding.statusText.visibility = View.VISIBLE
                }
                status.startsWith("●") -> {
                    binding.statusText.text = status
                    binding.statusText.visibility = View.VISIBLE
                    binding.statusText.postDelayed({
                        binding.statusText.visibility = View.GONE
                    }, 1500)
                }
                else -> {
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
        viewModel.agents.observe(this)         { setupAgentSpinner(it) }
        viewModel.isSending.observe(this)      { binding.sendButton.isEnabled = !it }
        viewModel.showTyping.observe(this)     { show ->
            binding.typingIndicator.visibility = if (show) View.VISIBLE else View.GONE
        }
        viewModel.pairingRequired.observe(this) { showPairingDialog(it) }
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

        // Use the new paperclip icon
        binding.attachButton.setImageResource(R.drawable.ic_attach_file)
        binding.attachButton.setOnClickListener {
            if (selectedFileUri != null) {
                // Remove selected file
                selectedFileUri = null
                binding.attachButton.setImageResource(R.drawable.ic_attach_file)
                binding.attachButton.contentDescription = "Attach file"
                binding.imagePreviewLayout.visibility = View.GONE
            } else {
                openFilePicker()
            }
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
                "Run on your GX10 device:\n\n" +
                        "1. Run: openclaw gateway call device.pair.list --json\n\n" +
                        "2. Find your device ID from the list above\n\n" +
                        "3. Run:\n" +
                        "   openclaw gateway call device.pair.approve \\\n" +
                        "   --params '{\"requestID\":\"<device-id>\"}' --json\n\n" +
                        "Device ID to approve:\n${deviceId.take(12)}…\n\n" +
                        "After approving, tap Retry to reconnect."
            )
            .setPositiveButton("Retry") { _, _ ->
                // Clear the pairing dialog state and retry connection
                viewModel.clearPairingDialog()
                viewModel.connect()
                Toast.makeText(this, "Retrying connection...", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun sendMessage() {
        val text = binding.messageEditText.text.toString().trim()

        // If there's a selected file, send it with the message
        if (selectedFileUri != null) {
            viewModel.sendFile(this, selectedFileUri!!, text)
            // Clear the selected file after sending
            selectedFileUri = null
            binding.attachButton.setImageResource(R.drawable.ic_attach_file)
            binding.attachButton.contentDescription = "Attach file"
            binding.imagePreviewLayout.visibility = View.GONE
            binding.messageEditText.text?.clear()
            return
        }

        // No file, send text message only
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
        try { voiceLauncher.launch(intent) }
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

    // Show file preview when a file is selected
    private fun showFilePreview(uri: Uri) {
        try {
            val mimeType = contentResolver.getType(uri)
            val fileName = getFileName(uri)

            binding.imagePreviewText.text = fileName ?: "File attached"
            binding.imagePreviewLayout.visibility = View.VISIBLE

            if (mimeType?.startsWith("image/") == true) {
                contentResolver.openInputStream(uri)?.use { inputStream ->
                    val bitmap = android.graphics.BitmapFactory.decodeStream(inputStream)
                    binding.imagePreviewImage.setImageBitmap(bitmap)
                    binding.imagePreviewImage.visibility = View.VISIBLE
                }
            } else {
                // For non-image files, show a generic file icon
                binding.imagePreviewImage.setImageResource(android.R.drawable.ic_menu_save)
                binding.imagePreviewImage.visibility = View.VISIBLE
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    private fun getFileName(uri: Uri): String? {
        var result: String? = null
        if (uri.scheme == "content") {
            contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val index = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                    if (index != -1) {
                        result = cursor.getString(index)
                    }
                }
            }
        }
        if (result == null) {
            result = uri.path
            val cut = result?.lastIndexOf('/') ?: -1
            if (cut != -1) {
                result = result?.substring(cut + 1)
            }
        }
        return result
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
            R.id.action_new_chat      -> { startActivity(Intent(this, MainActivity::class.java)); true }
            R.id.action_history       -> { startActivity(Intent(this, SessionsActivity::class.java)); true }
            R.id.action_tasks         -> { startActivity(Intent(this, TaskListActivity::class.java)); true }
            R.id.action_github        -> { startActivity(Intent(this, GitHubActivity::class.java)); true }
            R.id.action_settings      -> { startActivity(Intent(this, SettingsActivity::class.java)); true }
            else -> super.onOptionsItemSelected(item)
        }
    }

    private fun openFilePicker() {
        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "*/*"
            val mimetypes = arrayOf(
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-powerpoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "text/plain",
                "text/markdown",
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/bmp",
                "image/gif",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/json",
                "text/csv",
                "text/tab-separated-values"
            )
            putExtra(Intent.EXTRA_MIME_TYPES, mimetypes)
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        filePickerLauncher.launch(Intent.createChooser(intent, "Select File"))
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}