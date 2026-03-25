package com.user.ui.tasks

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import com.user.R
import com.user.data.Task
import com.user.data.TaskStatus

/**
 * Adapter for displaying tasks in a RecyclerView
 */
class TaskAdapter(
    private val onApproveClick: (Task) -> Unit,
    private val onRejectClick: (Task) -> Unit,
    private val onStartClick: (Task) -> Unit,
    private val onViewClick: (Task) -> Unit
) : ListAdapter<Task, TaskAdapter.TaskViewHolder>(TaskDiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): TaskViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_task, parent, false)
        return TaskViewHolder(view)
    }

    override fun onBindViewHolder(holder: TaskViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class TaskViewHolder(itemView: View) : ViewHolder(itemView) {
        private val titleText: TextView = itemView.findViewById(R.id.taskTitle)
        private val descriptionText: TextView = itemView.findViewById(R.id.taskDescription)
        private val statusText: TextView = itemView.findViewById(R.id.taskStatus)
        private val priorityText: TextView = itemView.findViewById(R.id.taskPriority)
        private val timeText: TextView = itemView.findViewById(R.id.taskTime)

        private val approveButton: Button? = itemView.findViewById(R.id.approveButton)
        private val rejectButton: Button? = itemView.findViewById(R.id.rejectButton)
        private val startButton: Button? = itemView.findViewById(R.id.startButton)
        private val viewButton: Button = itemView.findViewById(R.id.viewButton)

        fun bind(task: Task) {
            titleText.text = task.title
            descriptionText.text = task.description.take(100) +
                    if (task.description.length > 100) "..." else ""
            statusText.text = task.status.name
            statusText.setBackgroundResource(getStatusDrawable(task.status))
            priorityText.text = "Priority: ${task.priority}"
            timeText.text = formatTime(task.createdAt)

            // Show/hide action buttons based on task status
            approveButton?.visibility = if (task.status == TaskStatus.PENDING) View.VISIBLE else View.GONE
            rejectButton?.visibility = if (task.status == TaskStatus.PENDING) View.VISIBLE else View.GONE
            startButton?.visibility = if (task.status == TaskStatus.APPROVED) View.VISIBLE else View.GONE

            viewButton.setOnClickListener { onViewClick(task) }

            approveButton?.setOnClickListener { onApproveClick(task) }
            rejectButton?.setOnClickListener { onRejectClick(task) }
            startButton?.setOnClickListener { onStartClick(task) }
        }

        private fun getStatusDrawable(status: TaskStatus): Int {
            return when (status) {
                TaskStatus.PENDING -> R.drawable.badge_pending
                TaskStatus.APPROVED -> R.drawable.badge_approved
                TaskStatus.IN_PROGRESS -> R.drawable.badge_running
                TaskStatus.COMPLETED -> R.drawable.badge_completed
                TaskStatus.FAILED -> R.drawable.badge_failed
                TaskStatus.REJECTED -> R.drawable.badge_rejected
                TaskStatus.TIMEOUT -> R.drawable.badge_timeout
            }
        }

        private fun formatTime(timestamp: Long): String {
            val now = System.currentTimeMillis()
            val diff = now - timestamp
            return when {
                diff < 60_000 -> "${diff / 1000}s ago"
                diff < 3600_000 -> "${diff / 60_000}m ago"
                diff < 86400_000 -> "${diff / 3600_000}h ago"
                else -> "${diff / 86400_000}d ago"
            }
        }
    }

    abstract class ViewHolder(itemView: View) : androidx.recyclerview.widget.RecyclerView.ViewHolder(itemView)

    class TaskDiffCallback : DiffUtil.ItemCallback<Task>() {
        override fun areItemsTheSame(oldItem: Task, newItem: Task): Boolean {
            return oldItem.taskId == newItem.taskId
        }

        override fun areContentsTheSame(oldItem: Task, newItem: Task): Boolean {
            return oldItem == newItem
        }
    }
}