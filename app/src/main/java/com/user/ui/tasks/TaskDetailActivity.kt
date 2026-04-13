package com.user.ui.tasks

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModelProvider
import com.user.ClawMobileApplication
import com.user.R
import com.user.data.Task
import com.user.data.TaskStatus
import com.user.databinding.ActivityTaskDetailBinding
import com.user.ui.OutputHighlighter
import com.user.viewmodel.TaskViewModel
import com.user.viewmodel.TaskViewModelFactory

/**
 * Activity for displaying task details and performing actions
 */
class TaskDetailActivity : AppCompatActivity() {

    private lateinit var binding: ActivityTaskDetailBinding
    private lateinit var viewModel: TaskViewModel
    private var currentTask: Task? = null
    private var sessionId: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityTaskDetailBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.title = "Task"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        val taskId = intent.getStringExtra("task_id") ?: ""
        sessionId = intent.getStringExtra("session_id") ?: ""

        setupViewModel(sessionId)
        loadTask(taskId)
    }

    private fun setupViewModel(sessionId: String) {
        val app = application as ClawMobileApplication
        val factory = TaskViewModelFactory(
            repository = app.repository,
            sessionId = sessionId,
            orchestratorClient = if (app.prefsManager.isOrchestratorConfigured()) {
                com.user.service.OrchestratorApiClient(
                    prefs = app.prefsManager,
                    gatewayToken = app.prefsManager.gatewayToken
                )
            } else null
        )
        viewModel = ViewModelProvider(this, factory)[TaskViewModel::class.java]
    }

    private fun loadTask(taskId: String) {
        viewModel.getTask(taskId)
        viewModel.currentTask.observe(this) { task ->
            currentTask = task
            if (task != null) {
                supportActionBar?.title = task.title
                bindTask(task)
            } else {
                supportActionBar?.title = "Task"
                binding.taskTitle.text = "Task unavailable"
                binding.taskDescription.text = "This task could not be loaded from local storage or Orchestrator."
                binding.taskMetaRow.visibility = android.view.View.GONE
                binding.taskResult.visibility = android.view.View.GONE
                binding.taskResultLabel.visibility = android.view.View.GONE
                binding.taskError.visibility = android.view.View.GONE
                binding.taskErrorLabel.visibility = android.view.View.GONE
                binding.approveButton.visibility = android.view.View.GONE
                binding.rejectButton.visibility = android.view.View.GONE
                binding.startButton.visibility = android.view.View.GONE
            }
        }
    }

    private fun bindTask(task: Task) {
        // Update UI elements
        binding.taskTitle.text = task.title
        binding.taskDescription.text = task.description
        binding.taskTime.text = formatTime(task.createdAt)

        if (task.priority > 0) {
            binding.taskPriority.text = task.priority.toString()
            binding.taskPriorityLabel.visibility = android.view.View.VISIBLE
            binding.taskPriority.visibility = android.view.View.VISIBLE
        } else {
            binding.taskPriorityLabel.visibility = android.view.View.GONE
            binding.taskPriority.visibility = android.view.View.GONE
        }

        if (task.createdAt > 0L) {
            binding.taskTime.text = formatTime(task.createdAt)
            binding.taskTime.visibility = android.view.View.VISIBLE
        } else {
            binding.taskTime.visibility = android.view.View.GONE
        }

        val showMetaRow =
            binding.taskPriorityLabel.visibility == android.view.View.VISIBLE ||
                    binding.taskTime.visibility == android.view.View.VISIBLE
        binding.taskMetaRow.visibility = if (showMetaRow) android.view.View.VISIBLE else android.view.View.GONE

        // Update status badge
        updateStatusBadge(task.status)

        // Update result and error
        val hasResult = !task.result.isNullOrBlank()
        binding.taskResult.text = OutputHighlighter.render(
            this,
            task.result ?: "",
            isError = false
        )
        binding.taskResultLabel.visibility = if (hasResult) android.view.View.VISIBLE else android.view.View.GONE
        binding.taskResult.visibility = if (hasResult) android.view.View.VISIBLE else android.view.View.GONE

        val hasError = !task.error.isNullOrBlank()
        binding.taskError.text = OutputHighlighter.render(
            this,
            task.error ?: "",
            isError = true
        )
        binding.taskErrorLabel.visibility = if (hasError) android.view.View.VISIBLE else android.view.View.GONE
        binding.taskError.visibility = if (hasError) android.view.View.VISIBLE else android.view.View.GONE

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

        if (sessionId.isBlank()) {
            approveBtn.visibility = android.view.View.GONE
            rejectBtn.visibility = android.view.View.GONE
            startBtn.visibility = android.view.View.GONE
            return
        }

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