package com.yourname.openclawassistant.ui

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.yourname.openclawassistant.data.ChatMessage
import com.yourname.openclawassistant.databinding.ItemMessageBinding

class ChatAdapter : ListAdapter<ChatMessage, ChatAdapter.MessageViewHolder>(MessageDiffCallback()) {
    
    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): MessageViewHolder {
        val binding = ItemMessageBinding.inflate(
            LayoutInflater.from(parent.context),
            parent,
            false
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
            binding.apply {
                messageText.text = message.message
                isUser = message.isUser
                timestampText.text = java.text.SimpleDateFormat(
                    "HH:mm", java.util.Locale.getDefault()
                ).format(java.util.Date(message.timestamp))
                
                // Visual styling
                root.setBackgroundResource(
                    if (message.isUser) {
                        android.R.drawable.ic_dialog_info
                    } else {
                        android.R.drawable.ic_menu_compass
                    }
                )
            }
        }
    }
    
    class MessageDiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem.id == newItem.id
        }
        
        override fun areContentsTheSame(oldItem: ChatMessage, newItem: ChatMessage): Boolean {
            return oldItem == newItem
        }
    }
}
