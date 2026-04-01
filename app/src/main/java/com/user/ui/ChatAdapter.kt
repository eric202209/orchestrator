package com.user.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.text.format.DateUtils
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
import java.net.HttpURLConnection
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale
import kotlin.math.abs

class ChatAdapter : ListAdapter<ChatMessage, ChatAdapter.MessageViewHolder>(DiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): MessageViewHolder {
        val binding = ItemMessageBinding.inflate(
            LayoutInflater.from(parent.context),
            parent,
            false
        )
        return MessageViewHolder(binding)
    }

    override fun onBindViewHolder(holder: MessageViewHolder, position: Int) {
        holder.bind(
            message = getItem(position),
            previousMessage = currentList.getOrNull(position - 1)
        )
    }

    inner class MessageViewHolder(
        private val binding: ItemMessageBinding
    ) : RecyclerView.ViewHolder(binding.root) {

        fun bind(message: ChatMessage, previousMessage: ChatMessage?) {
            val time = SimpleDateFormat("HH:mm", Locale.getDefault())
                .format(Date(message.timestamp))
            val showDateDivider = previousMessage == null || !isSameDay(
                previousMessage.timestamp,
                message.timestamp
            )
            val groupedWithPrevious = previousMessage != null &&
                    previousMessage.isUser == message.isUser &&
                    isSameDay(previousMessage.timestamp, message.timestamp) &&
                    abs(message.timestamp - previousMessage.timestamp) <= 60_000L

            binding.dateDividerLayout.visibility = if (showDateDivider) View.VISIBLE else View.GONE
            if (showDateDivider) {
                binding.dateDividerText.text = formatDateLabel(message.timestamp)
            }

            applyBubbleWidths()

            if (message.isUser) {
                bindUserMessage(message, time)
            } else {
                bindAiMessage(message, time, groupedWithPrevious)
            }
        }

        private fun bindUserMessage(message: ChatMessage, time: String) {
            binding.userRow.visibility = View.VISIBLE
            binding.aiRow.visibility = View.GONE
            binding.userTimestampText.text = time

            if (message.imageBase64 != null) {
                try {
                    val bytes = Base64.decode(message.imageBase64, Base64.DEFAULT)
                    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    binding.userMessageImage.setImageBitmap(bitmap)
                    binding.userMessageImage.visibility = View.VISIBLE
                    if (message.message != "\uD83D\uDCF7 Image") {
                        binding.userMessageText.text = message.message
                        binding.userMessageText.visibility = View.VISIBLE
                    } else {
                        binding.userMessageText.visibility = View.GONE
                    }
                } catch (_: Exception) {
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
            binding.userMessageImage.setOnLongClickListener {
                showActionDialog(binding.root.context, message.message)
                true
            }
        }

        private fun bindAiMessage(message: ChatMessage, time: String, groupedWithPrevious: Boolean) {
            binding.aiRow.visibility = View.VISIBLE
            binding.userRow.visibility = View.GONE
            binding.timestampText.text = time
            binding.avatarText.visibility = if (groupedWithPrevious) View.INVISIBLE else View.VISIBLE

            when {
                hasMarkdownImage(message.message) -> {
                    val base64 = extractBase64(message.message)
                    val textOnly = message.message.substringBefore("![").trim()

                    if (textOnly.isNotEmpty()) {
                        binding.messageText.text = MarkdownRenderer.render(
                            binding.root.context,
                            textOnly
                        )
                        binding.messageText.visibility = View.VISIBLE
                    } else {
                        binding.messageText.visibility = View.GONE
                    }

                    if (base64 != null) {
                        showBase64Image(base64)
                    } else {
                        val imageUrl = extractImageUrl(message.message)
                        if (imageUrl != null) {
                            binding.messageImage.visibility = View.VISIBLE
                            loadImageFromUrl(imageUrl)
                        } else {
                            binding.messageImage.visibility = View.GONE
                        }
                    }
                }

                isBase64Image(message.message) && message.status == MessageStatus.FINAL -> {
                    val textOnly = message.message.substringBefore("data:image/").trim()

                    if (textOnly.isNotEmpty()) {
                        binding.messageText.text = MarkdownRenderer.render(
                            binding.root.context,
                            textOnly
                        )
                        binding.messageText.visibility = View.VISIBLE
                    } else {
                        binding.messageText.visibility = View.GONE
                    }

                    showBase64Image(
                        message.message.substring(message.message.indexOf("data:image/"))
                    )
                }

                else -> {
                    binding.messageImage.visibility = View.GONE
                    binding.messageText.visibility = View.VISIBLE
                    binding.messageText.text = MarkdownRenderer.render(
                        binding.root.context,
                        message.message
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

        private fun applyBubbleWidths() {
            val screenWidth = binding.root.resources.displayMetrics.widthPixels
            val userMaxWidth = (screenWidth * 0.75f).toInt()
            val aiMaxWidth = (screenWidth * 0.85f).toInt()

            binding.userMessageText.maxWidth = userMaxWidth
            binding.userMessageImage.maxWidth = userMaxWidth
            binding.messageText.maxWidth = aiMaxWidth
            binding.messageImage.maxWidth = aiMaxWidth
        }

        private fun hasMarkdownImage(text: String): Boolean {
            return Regex("""!\[.*?]\((data:image/|https?://).*?""").containsMatchIn(text)
        }

        private fun isBase64Image(text: String): Boolean {
            return text.contains("data:image/")
        }

        private fun extractBase64(text: String): String? {
            val mdMatch = Regex("""!\[.*?]\((data:image/[^)]+)\)""").find(text)
            if (mdMatch != null) return mdMatch.groupValues[1]
            val idx = text.indexOf("data:image/")
            if (idx >= 0) return text.substring(idx)
            return null
        }

        private fun extractImageUrl(text: String): String? {
            val match = Regex("""!\[.*?]\((https?://[^)]+)\)""").find(text)
            return match?.groupValues?.get(1)
        }

        private fun showBase64Image(base64: String) {
            try {
                val data = if (base64.contains(",")) {
                    base64.substringAfter(",")
                } else {
                    base64
                }

                val bytes = Base64.decode(data, Base64.DEFAULT)
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)

                if (bitmap != null) {
                    binding.messageImage.setImageBitmap(bitmap)
                    binding.messageImage.visibility = View.VISIBLE
                } else {
                    binding.messageText.text = "[Image could not be decoded]"
                    binding.messageText.visibility = View.VISIBLE
                    binding.messageImage.visibility = View.GONE
                }
            } catch (e: Exception) {
                binding.messageText.text = "[Image error: ${e.message}]"
                binding.messageText.visibility = View.VISIBLE
                binding.messageImage.visibility = View.GONE
            }
        }

        // Track the last loaded image URL for this ViewHolder to detect recycling
        private var currentImageUrl: String? = null

        private fun loadImageFromUrl(url: String) {
            val viewHolder = this@MessageViewHolder
            val messagePosition = viewHolder.getAdapterPosition()

            // If ViewHolder has been recycled, skip loading
            if (messagePosition == RecyclerView.NO_POSITION) return

            // Cancel previous request if same ViewHolder is being reused for different item
            if (currentImageUrl != null && currentImageUrl != url) {
                currentImageUrl = null
            }

            currentImageUrl = url

            // Load image in background thread
            Thread {
                var connection: HttpURLConnection? = null
                try {
                    val imageUrl = url  // Capture URL for this specific load operation
                    connection = java.net.URL(imageUrl).openConnection() as HttpURLConnection
                    connection.connectTimeout = 10000
                    connection.readTimeout = 10000
                    connection.requestMethod = "GET"

                    val bitmap = BitmapFactory.decodeStream(connection.inputStream)

                    // Check if ViewHolder is still valid and not recycled
                    binding.root.post {
                        if (getAdapterPosition() != RecyclerView.NO_POSITION &&
                            currentImageUrl == imageUrl) {
                            binding.messageImage.setImageBitmap(bitmap)
                            binding.messageImage.visibility = View.VISIBLE
                        } else {
                            binding.messageImage.visibility = View.GONE
                        }
                    }
                } catch (e: Exception) {
                    binding.root.post {
                        if (getAdapterPosition() != RecyclerView.NO_POSITION &&
                            currentImageUrl == url) {
                            // Only show error if this ViewHolder is still showing the same item
                            binding.messageImage.visibility = View.GONE
                        }
                    }
                } finally {
                    connection?.disconnect()
                }
            }.start()
        }

        private fun showActionDialog(context: Context, text: String) {
            AlertDialog.Builder(context)
                .setItems(arrayOf("\uD83D\uDCCB  Copy", "↗  Share")) { _, which ->
                    when (which) {
                        0 -> copyToClipboard(context, text)
                        1 -> shareText(context, text)
                    }
                }
                .show()
        }

        private fun copyToClipboard(context: Context, text: String) {
            val clipboard =
                context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
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

        private fun isSameDay(firstTimestamp: Long, secondTimestamp: Long): Boolean {
            val first = Calendar.getInstance().apply { timeInMillis = firstTimestamp }
            val second = Calendar.getInstance().apply { timeInMillis = secondTimestamp }
            return first.get(Calendar.YEAR) == second.get(Calendar.YEAR) &&
                    first.get(Calendar.DAY_OF_YEAR) == second.get(Calendar.DAY_OF_YEAR)
        }

        private fun formatDateLabel(timestamp: Long): CharSequence {
            return when {
                DateUtils.isToday(timestamp) -> "Today"
                DateUtils.isToday(timestamp + DateUtils.DAY_IN_MILLIS) -> "Yesterday"
                else -> SimpleDateFormat("EEE, MMM d", Locale.getDefault()).format(Date(timestamp))
            }
        }
    }

    class DiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean =
            oldItem.id == newItem.id

        override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean =
            oldItem == newItem
    }
}

