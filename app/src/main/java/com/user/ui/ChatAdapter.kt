package com.user.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.user.data.ChatMessage
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
                binding.userMessageText.text = message.message
                binding.userTimestampText.text = time
                binding.userMessageText.setOnLongClickListener {
                    showActionDialog(binding.root.context, message.message)
                    true
                }
            } else {
                binding.aiRow.visibility   = View.VISIBLE
                binding.userRow.visibility = View.GONE
                binding.messageText.text = MarkdownRenderer.render(
                    binding.root.context, message.message
                )
                binding.timestampText.text = time
                binding.messageText.setOnLongClickListener {
                    showActionDialog(binding.root.context, message.message)
                    true
                }
            }
        }

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

