package com.user.ui.activities

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Rect
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.speech.RecognizerIntent
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
import androidx.core.view.ViewCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.bottomsheet.BottomSheetDialog
import com.user.R
import com.user.data.PrefsManager
import com.user.databinding.ActivityMainBinding
import com.user.databinding.BottomSheetAttachmentBinding
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
    private var selectedFileUri: Uri? = null

    private val voiceLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) {
                result.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)
                    ?.firstOrNull()
                    ?.let { binding.messageEditText.setText(it) }
            }
        }

    private val galleryPickerLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) {
                result.data?.data?.let(::setSelectedFile)
            }
        }

    private val documentPickerLauncher =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) {
                result.data?.data?.let(::setSelectedFile)
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
        updateAttachmentUi()

        val sessionId = intent.getStringExtra("session_id")
        val sessionTitle = intent.getStringExtra("session_title")
        if (sessionId != null) {
            supportActionBar?.title = sessionTitle ?: getString(R.string.toolbar_title)
            supportActionBar?.setDisplayHomeAsUpEnabled(true)
            viewModel.loadSession(sessionId)
        } else {
            supportActionBar?.title = getString(R.string.toolbar_title)
            viewModel.startNewSession()
            requestNotificationPermission()
            viewModel.startService(this)
        }

        ViewCompat.requestApplyInsets(binding.root)
    }

    private fun setupRecyclerView() {
        chatAdapter = ChatAdapter()
        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity).also { it.stackFromEnd = true }
            adapter = chatAdapter
            if (itemDecorationCount == 0) {
                addItemDecoration(MessageSpacingDecoration(dpToPx(4)))
            }
        }
    }

    private fun setupObservers() {
        viewModel.status.observe(this) { status ->
            binding.statusText.text = status.removePrefix("● ").removePrefix("○ ").removePrefix("✕ ")
            binding.statusText.visibility = View.VISIBLE
            binding.statusText.setTextColor(resolveStatusColor(status))

            if (status.startsWith("●")) {
                binding.statusText.postDelayed({
                    if (!isFinishing) {
                        binding.statusText.visibility = View.GONE
                    }
                }, 1500)
            }
        }

        viewModel.messages.observe(this) { messages ->
            chatAdapter.submitList(messages.toList())
            if (messages.isNotEmpty()) {
                binding.recyclerView.scrollToPosition(messages.size - 1)
            }
        }

        viewModel.agents.observe(this) { setupAgentSpinner(it) }
        viewModel.isSending.observe(this) { binding.sendButton.isEnabled = !it }
        viewModel.showTyping.observe(this) { show ->
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
        binding.sendButton.setOnClickListener { sendMessage() }
        binding.messageEditText.setOnKeyListener { _, keyCode, event ->
            if (keyCode == KeyEvent.KEYCODE_ENTER &&
                event.action == KeyEvent.ACTION_DOWN &&
                !event.isShiftPressed
            ) {
                sendMessage()
                true
            } else {
                false
            }
        }
        binding.attachButton.setOnClickListener {
            if (selectedFileUri != null) {
                clearSelectedFile()
            } else {
                showAttachmentSheet()
            }
        }
        binding.voiceButton.setOnClickListener { startVoiceInput() }
        binding.removePreviewButton.setOnClickListener { clearSelectedFile() }
    }

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
                parent: AdapterView<*>?,
                view: View?,
                position: Int,
                id: Long
            ) {
                viewModel.switchAgent(agents[position].agentId)
            }

            override fun onNothingSelected(parent: AdapterView<*>?) = Unit
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
                viewModel.clearPairingDialog()
                viewModel.connect()
                Toast.makeText(this, "Retrying connection...", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun sendMessage() {
        val text = binding.messageEditText.text.toString().trim()

        if (selectedFileUri != null) {
            viewModel.sendFile(this, selectedFileUri!!, text)
            clearSelectedFile(clearInput = false)
            binding.messageEditText.text?.clear()
            return
        }

        if (text.isEmpty()) return
        binding.messageEditText.text?.clear()
        viewModel.sendMessage(text)
    }

    private fun showAttachmentSheet() {
        val bottomSheet = BottomSheetDialog(this)
        val sheetBinding = BottomSheetAttachmentBinding.inflate(layoutInflater)
        bottomSheet.setContentView(sheetBinding.root)

        sheetBinding.galleryOptionButton.setOnClickListener {
            bottomSheet.dismiss()
            openGalleryPicker()
        }
        sheetBinding.documentOptionButton.setOnClickListener {
            bottomSheet.dismiss()
            openDocumentPicker()
        }

        bottomSheet.show()
    }

    private fun openGalleryPicker() {
        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "image/*"
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        galleryPickerLauncher.launch(Intent.createChooser(intent, getString(R.string.attachment_gallery)))
    }

    private fun openDocumentPicker() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(
                Intent.EXTRA_MIME_TYPES,
                arrayOf(
                    "application/pdf",
                    "text/plain",
                    "text/markdown",
                    "application/json",
                    "text/csv",
                    "application/xml",
                    "text/xml"
                )
            )
        }
        documentPickerLauncher.launch(
            Intent.createChooser(intent, getString(R.string.attachment_documents))
        )
    }

    private fun startVoiceInput() {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
            )
            putExtra(RecognizerIntent.EXTRA_PROMPT, "Speak your message…")
        }
        try {
            voiceLauncher.launch(intent)
        } catch (_: Exception) {
            Toast.makeText(this, "Voice input not available", Toast.LENGTH_SHORT).show()
        }
    }

    private fun setSelectedFile(uri: Uri) {
        selectedFileUri = uri
        showFilePreview(uri)
        updateAttachmentUi()
    }

    private fun clearSelectedFile(clearInput: Boolean = false) {
        selectedFileUri = null
        binding.imagePreviewLayout.visibility = View.GONE
        binding.imagePreviewImage.setImageDrawable(null)
        if (clearInput) {
            binding.messageEditText.text?.clear()
        }
        updateAttachmentUi()
    }

    private fun updateAttachmentUi() {
        val hasAttachment = selectedFileUri != null
        binding.attachButton.setImageResource(
            if (hasAttachment) android.R.drawable.ic_delete else R.drawable.ic_add
        )
        binding.attachButton.contentDescription = getString(
            if (hasAttachment) R.string.remove_attachment else R.string.more_options
        )
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                200
            )
        }
    }

    private fun showFilePreview(uri: Uri) {
        try {
            val mimeType = contentResolver.getType(uri)
            val fileName = getFileName(uri)

            binding.imagePreviewText.text = fileName ?: getString(R.string.image_attached)
            binding.imagePreviewLayout.visibility = View.VISIBLE

            if (mimeType?.startsWith("image/") == true) {
                contentResolver.openInputStream(uri)?.use { inputStream ->
                    val bitmap = android.graphics.BitmapFactory.decodeStream(inputStream)
                    binding.imagePreviewImage.setImageBitmap(bitmap)
                }
            } else {
                binding.imagePreviewImage.setImageResource(android.R.drawable.ic_menu_save)
            }
        } catch (_: Exception) {
            binding.imagePreviewLayout.visibility = View.GONE
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
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_new_chat -> {
                startActivity(Intent(this, MainActivity::class.java))
                true
            }
            R.id.action_history -> {
                startActivity(Intent(this, SessionsActivity::class.java))
                true
            }
            R.id.action_tasks -> {
                startActivity(Intent(this, TaskListActivity::class.java))
                true
            }
            R.id.action_github -> {
                startActivity(Intent(this, GitHubActivity::class.java))
                true
            }
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    private fun resolveStatusColor(status: String): Int {
        val colorRes = when {
            status.startsWith("✕") -> R.color.status_failed
            status.startsWith("●") -> R.color.status_connected
            status.contains("error", ignoreCase = true) -> R.color.status_failed
            else -> R.color.status_disconnected
        }
        return ContextCompat.getColor(this, colorRes)
    }

    private fun dpToPx(dp: Int): Int =
        (dp * resources.displayMetrics.density).toInt()

    private class MessageSpacingDecoration(
        private val spacingPx: Int
    ) : RecyclerView.ItemDecoration() {
        override fun getItemOffsets(
            outRect: Rect,
            view: View,
            parent: RecyclerView,
            state: RecyclerView.State
        ) {
            outRect.top = spacingPx / 2
            outRect.bottom = spacingPx / 2
        }
    }
}
