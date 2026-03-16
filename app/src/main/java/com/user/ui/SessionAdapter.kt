package com.user.ui

import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.user.data.ChatSession
import com.user.databinding.ItemSessionBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class SessionAdapter(
    private val onClick: (ChatSession) -> Unit,
    private val onDelete: (ChatSession) -> Unit
) : ListAdapter<ChatSession, SessionAdapter.SessionViewHolder>(SessionDiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): SessionViewHolder {
        val binding = ItemSessionBinding.inflate(
            LayoutInflater.from(parent.context), parent, false
        )
        return SessionViewHolder(binding)
    }

    override fun onBindViewHolder(holder: SessionViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class SessionViewHolder(
        private val binding: ItemSessionBinding
    ) : RecyclerView.ViewHolder(binding.root) {

        fun bind(session: ChatSession) {
            binding.sessionTitle.text = session.title
            binding.sessionTime.text = SimpleDateFormat("MMM dd, HH:mm", Locale.getDefault())
                .format(Date(session.lastMessageAt))
            binding.root.setOnClickListener { onClick(session) }
            binding.deleteButton.setOnClickListener { onDelete(session) }
        }
    }

    class SessionDiffCallback : DiffUtil.ItemCallback<ChatSession>() {
        override fun areItemsTheSame(oldItem: ChatSession, newItem: ChatSession) =
            oldItem.sessionId == newItem.sessionId
        override fun areContentsTheSame(oldItem: ChatSession, newItem: ChatSession) =
            oldItem == newItem
    }
}


