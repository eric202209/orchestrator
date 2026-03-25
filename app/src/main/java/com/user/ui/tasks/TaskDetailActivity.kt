package com.user.ui.tasks

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModelProvider
import com.user.ClawMobileApplication
import com.user.R
import com.user.data.Task
import com.user.data.TaskStatus
import com.user.databinding.ActivityTaskDetailBinding
import com.user.viewmodel.TaskViewModel
import com.user.viewmodel.TaskViewModelFactory

/**
 * Activity for displaying task details and performing actions
 */
class TaskDetailActivity : AppCompatActivity() {

    private lateinit var binding: ActivityTaskDetailBinding
    private lateinit var viewModel: TaskViewModel
    private var currentTask: Task? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityTaskDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        val taskId = intent.getStringExtra("task_id") ?: ""
        val sessionId = intent.getStringExtra("session_id") ?: ""

        setupViewModel(sessionId)
        loadTask(taskId)
    }

    private fun setupViewModel(sessionId: String) {
        val app = application as ClawMobileApplication
        val factory = TaskViewModelFactory(repository = app.repository, sessionId = sessionId)
        viewModel = ViewModelProvider(this, factory)[TaskViewModel::class.java]
    }

    private fun loadTask(taskId: String) {
        viewModel.getTask(taskId)
        viewModel.currentTask.observe(this) { task ->
            currentTask = task
            task?.let { bindTask(it) }
        }
    }

    private fun bindTask(task: Task) {
        // Update UI elements
        binding.taskTitle.text = task.title
        binding.taskDescription.text = task.description
        binding.taskPriority.text = task.priority.toString()
        binding.taskTime.text = formatTime(task.createdAt)

        // Update status badge
        updateStatusBadge(task.status)

        // Update result and error
        binding.taskResult.text = task.result ?: "No result yet"
        binding.taskResult.visibility = if (task.result.isNullOrEmpty()) android.view.View.GONE else android.view.View.VISIBLE
        binding.taskError.text = task.error ?: ""
        binding.taskError.visibility = if (task.error.isNullOrEmpty()) android.view.View.GONE else android.view.View.VISIBLE

        // Show/hide action buttons based on status
        updateActionButtons(task.status)
    }

    private fun updateStatusBadge(status: TaskStatus) {
        val badge = binding.taskStatusBadge
        badge.text = status.name

        val drawableRes = when (status) {
            TaskStatus.PENDING -> R.drawable.badge_pending
            TaskStatus.APPROVED -> R.drawable.badge_approved
            TaskStatus.IN_PROGRESS -> R.drawable.badge_running
            TaskStatus.COMPLETED -> R.drawable.badge_completed
            TaskStatus.FAILED -> R.drawable.badge_failed
            TaskStatus.REJECTED -> R.drawable.badge_rejected
            TaskStatus.TIMEOUT -> R.drawable.badge_timeout
        }
        badge.setBackgroundResource(drawableRes)
    }

    private fun updateActionButtons(status: TaskStatus) {
        val approveBtn = binding.approveButton
        val rejectBtn = binding.rejectButton
        val startBtn = binding.startButton

        when (status) {
            TaskStatus.PENDING -> {
                approveBtn.visibility = android.view.View.VISIBLE
                rejectBtn.visibility = android.view.View.VISIBLE
                startBtn.visibility = android.view.View.GONE
                approveBtn.setOnClickListener { viewModel.approveTask(currentTask?.taskId ?: "") }
                rejectBtn.setOnClickListener {
                    android.app.AlertDialog.Builder(this)
                        .setTitle("Reject Task")
                        .setMessage("Enter reason for rejection (optional):")
                        .setPositiveButton("Reject") { _, _ ->
                            viewModel.rejectTask(currentTask?.taskId ?: "", null)
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                }
            }
            TaskStatus.APPROVED -> {
                approveBtn.visibility = android.view.View.GONE
                rejectBtn.visibility = android.view.View.GONE
                startBtn.visibility = android.view.View.VISIBLE
                startBtn.setOnClickListener { viewModel.startTask(currentTask?.taskId ?: "") }
            }
            TaskStatus.IN_PROGRESS -> {
                approveBtn.visibility = android.view.View.GONE
                rejectBtn.visibility = android.view.View.GONE
                startBtn.visibility = android.view.View.GONE
            }
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED, TaskStatus.TIMEOUT -> {
                approveBtn.visibility = android.view.View.GONE
                rejectBtn.visibility = android.view.View.GONE
                startBtn.visibility = android.view.View.GONE
            }
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

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }
}
