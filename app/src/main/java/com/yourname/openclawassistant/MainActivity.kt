package com.yourname.openclawassistant

import android.os.Bundle
import android.view.KeyEvent
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.yourname.openclawassistant.data.ChatDatabase
import com.yourname.openclawassistant.databinding.ActivityMainBinding
import com.yourname.openclawassistant.ui.ChatAdapter
import com.yourname.openclawassistant.service.OpenClawService

class MainActivity : AppCompatActivity() {
    
    private lateinit var binding: ActivityMainBinding
    private lateinit var chatAdapter: ChatAdapter
    private lateinit var openClawService: OpenClawService
    private var currentSessionId = "default-session"
    
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        
        setupRecyclerView()
        setupOpenClawService()
        loadMessages()
        
        // Setup send button
        binding.sendButton.setOnClickListener {
            sendMessage()
        }
        
        // Setup Enter key to send
        binding.messageEditText.setOnKeyListener { _, keyCode, event ->
            if (keyCode == KeyEvent.KEYCODE_ENTER && event.action == KeyEvent.ACTION_DOWN) {
                sendMessage()
                true
            } else {
                false
            }
        }
    }
    
    private fun setupRecyclerView() {
        chatAdapter = ChatAdapter()
        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@MainActivity)
            adapter = chatAdapter
        }
    }
    
    private fun setupOpenClawService() {
        val database = ChatDatabase.getDatabase(application)
        openClawService = OpenClawService(database.chatDao())
    }
    
    private fun loadMessages() {
        // TODO: Load messages from Room database
        // This will be implemented in the next iteration
    }
    
    private fun sendMessage() {
        val messageText = binding.messageEditText.text.toString().trim()
        if (messageText.isEmpty()) return
        
        // TODO: Add message to UI
        
        binding.messageEditText.text?.clear()
        
        // TODO: Send to OpenClaw service
        // openClawService.sendMessage(currentSessionId, messageText)
    }
}
