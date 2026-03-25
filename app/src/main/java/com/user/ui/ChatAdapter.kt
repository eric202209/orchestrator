package com.user.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.util.Base64
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.user.data.ChatMessage
import com.user.data.MessageStatus
import com.user.databinding.ItemMessageBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class ChatAdapter : ListAdapter<ChatMessage, ChatAdapter.MessageViewHolder>(DiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): MessageViewHolder {
        val binding = ItemMessageBinding.inflate(
            LayoutInflater.from(parent.context), parent, false
        )
        return MessageViewHolder(binding)
    }

    override fun onBindViewHolder(holder: MessageViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class MessageViewHolder(
        private val binding: ItemMessageBinding
    ) : RecyclerView.ViewHolder(binding.root) {

        fun bind(message: ChatMessage) {
            val time = SimpleDateFormat("HH:mm", Locale.getDefault())
                .format(Date(message.timestamp))

            if (message.isUser) {
                binding.userRow.visibility = View.VISIBLE
                binding.aiRow.visibility   = View.GONE
                binding.userTimestampText.text = time

                if (message.imageBase64 != null) {
                    try {
                        val bytes  = android.util.Base64.decode(message.imageBase64, android.util.Base64.DEFAULT)
                        val bitmap = android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                        binding.userMessageImage.setImageBitmap(bitmap)
                        binding.userMessageImage.visibility = View.VISIBLE
                        if (message.message != "📷 Image") {
                            binding.userMessageText.text = message.message
                            binding.userMessageText.visibility = View.VISIBLE
                        } else {
                            binding.userMessageText.visibility = View.GONE
                        }
                    } catch (e: Exception) {
                        binding.userMessageText.text = message.message
                        binding.userMessageText.visibility = View.VISIBLE
                        binding.userMessageImage.visibility = View.GONE
                    }
                } else {
                    binding.userMessageText.text = message.message
                    binding.userMessageText.visibility = View.VISIBLE
                    binding.userMessageImage.visibility = View.GONE
                }

                binding.userMessageText.setOnLongClickListener {
                    showActionDialog(binding.root.context, message.message)
                    true
                }
            } else {
                binding.aiRow.visibility   = View.VISIBLE
                binding.userRow.visibility = View.GONE
                binding.timestampText.text = time

                when {
                    // Markdown image format: ![alt](data:image/... or https://...)
                    hasMarkdownImage(message.message) -> {
                        val base64   = extractBase64(message.message)
                        val textOnly = message.message
                            .substringBefore("![")
                            .trim()

                        if (textOnly.isNotEmpty()) {
                            binding.messageText.text = MarkdownRenderer.render(
                                binding.root.context, textOnly
                            )
                            binding.messageText.visibility = View.VISIBLE
                        } else {
                            binding.messageText.visibility = View.GONE
                        }

                        if (base64 != null) {
                            showBase64Image(base64)
                        } else {
                            // Try URL image
                            val imageUrl = extractImageUrl(message.message)
                            if (imageUrl != null) {
                                binding.messageImage.visibility = View.VISIBLE
                                loadImageFromUrl(imageUrl)
                            } else {
                                binding.messageImage.visibility = View.GONE
                            }
                        }
                    }

                    // Raw base64 image (StreamFinal only)
                    isBase64Image(message.message) &&
                            message.status == MessageStatus.FINAL -> {
                        val textOnly = message.message
                            .substringBefore("data:image/")
                            .trim()

                        if (textOnly.isNotEmpty()) {
                            binding.messageText.text = MarkdownRenderer.render(
                                binding.root.context, textOnly
                            )
                            binding.messageText.visibility = View.VISIBLE
                        } else {
                            binding.messageText.visibility = View.GONE
                        }

                        showBase64Image(
                            message.message.substring(
                                message.message.indexOf("data:image/")
                            )
                        )
                    }

                    // Plain text
                    else -> {
                        binding.messageImage.visibility = View.GONE
                        binding.messageText.visibility  = View.VISIBLE
                        binding.messageText.text = MarkdownRenderer.render(
                            binding.root.context, message.message
                        )
                    }
                }

                binding.messageText.setOnLongClickListener {
                    showActionDialog(binding.root.context, message.message)
                    true
                }
                binding.messageImage.setOnLongClickListener {
                    showActionDialog(binding.root.context, message.message)
                    true
                }
            }
        }

        // ── Image detection ───────────────────────────────────────

        private fun hasMarkdownImage(text: String): Boolean {
            return Regex("""!\[.*?]\((data:image/|https?://).*?""").containsMatchIn(text)
        }

        private fun isBase64Image(text: String): Boolean {
            return text.contains("data:image/")
        }

        // ── Image extraction ──────────────────────────────────────

        private fun extractBase64(text: String): String? {
            // From markdown: ![alt](data:image/jpeg;base64,...)
            val mdMatch = Regex("""!\[.*?]\((data:image/[^)]+)\)""").find(text)
            if (mdMatch != null) return mdMatch.groupValues[1]
            // Raw in text
            val idx = text.indexOf("data:image/")
            if (idx >= 0) return text.substring(idx)
            return null
        }

        private fun extractImageUrl(text: String): String? {
            val match = Regex("""!\[.*?]\((https?://[^)]+)\)""").find(text)
            return match?.groupValues?.get(1)
        }

        // ── Image rendering ───────────────────────────────────────

        private fun showBase64Image(base64: String) {
            try {
                val data = if (base64.contains(",")) {
                    base64.substringAfter(",")
                } else base64

                val bytes  = Base64.decode(data, Base64.DEFAULT)
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)

                if (bitmap != null) {
                    binding.messageImage.setImageBitmap(bitmap)
                    binding.messageImage.visibility = View.VISIBLE
                } else {
                    binding.messageText.text = "[Image could not be decoded]"
                    binding.messageText.visibility  = View.VISIBLE
                    binding.messageImage.visibility = View.GONE
                }
            } catch (e: Exception) {
                binding.messageText.text = "[Image error: ${e.message}]"
                binding.messageText.visibility  = View.VISIBLE
                binding.messageImage.visibility = View.GONE
            }
        }

        private fun loadImageFromUrl(url: String) {
            Thread {
                try {
                    val connection = java.net.URL(url).openConnection()
                    connection.connectTimeout = 10000
                    connection.readTimeout    = 10000
                    connection.connect()
                    val bitmap = BitmapFactory.decodeStream(connection.getInputStream())
                    binding.root.post {
                        if (bitmap != null) {
                            binding.messageImage.setImageBitmap(bitmap)
                        } else {
                            binding.messageImage.visibility = View.GONE
                        }
                    }
                } catch (e: Exception) {
                    binding.root.post {
                        binding.messageImage.visibility = View.GONE
                    }
                }
            }.start()
        }

        // ── Long press actions ────────────────────────────────────

        private fun showActionDialog(context: Context, text: String) {
            AlertDialog.Builder(context)
                .setItems(arrayOf("📋  Copy", "↗  Share")) { _, which ->
                    when (which) {
                        0 -> copyToClipboard(context, text)
                        1 -> shareText(context, text)
                    }
                }
                .show()
        }

        private fun copyToClipboard(context: Context, text: String) {
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE)
                    as ClipboardManager
            clipboard.setPrimaryClip(ClipData.newPlainText("OpenClaw", text))
            Toast.makeText(context, "Copied", Toast.LENGTH_SHORT).show()
        }

        private fun shareText(context: Context, text: String) {
            val intent = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_TEXT, text)
            }
            context.startActivity(Intent.createChooser(intent, "Share via"))
        }
    }

    class DiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage) =
            oldItem.id == newItem.id
        override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage) =
            oldItem.message == newItem.message && oldItem.status == newItem.status
    }
}


