package com.user.ui.tasks

import android.content.Intent
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModelProvider
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.ClawMobileApplication
import com.user.R
import com.user.data.Task
import com.user.data.TaskStatus
import com.user.databinding.ActivityTaskListBinding
import com.user.viewmodel.TaskViewModel
import com.user.viewmodel.TaskViewModelFactory

/**
 * Activity for displaying and managing tasks
 */
class TaskListActivity : AppCompatActivity() {

    private lateinit var binding: ActivityTaskListBinding
    private lateinit var taskAdapter: TaskAdapter
    private lateinit var viewModel: TaskViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityTaskListBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.title = "Tasks"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        setupRecyclerView()
        setupViewModel()
        setupObservers()
    }

    private fun setupRecyclerView() {
        taskAdapter = TaskAdapter(
            onApproveClick = { task -> approveTask(task) },
            onRejectClick = { task -> rejectTask(task) },
            onStartClick = { task -> startTask(task) },
            onViewClick = { task -> viewTaskDetails(task) }
        )

        binding.recyclerView.apply {
            layoutManager = LinearLayoutManager(this@TaskListActivity)
            adapter = taskAdapter
        }
    }

    private fun setupViewModel() {
        val sessionId = intent.getStringExtra("session_id") ?: ""
        val app = application as ClawMobileApplication
        val factory = TaskViewModelFactory(repository = app.repository, sessionId = sessionId)
        viewModel = ViewModelProvider(this, factory)[TaskViewModel::class.java]
    }

    private fun setupObservers() {
        viewModel.allTasks.observe(this) { tasks ->
            submitTasks(tasks)
            updateEmptyState(tasks)
        }

        viewModel.pendingTasks.observe(this) { tasks ->
            binding.pendingCount.text = tasks.size.toString()
        }

        viewModel.runningTasks.observe(this) { tasks ->
            binding.activeCount.text = tasks.size.toString()
        }
    }

    private fun dashboardSortRank(status: TaskStatus): Int {
        return when (status) {
            TaskStatus.PENDING -> 0
            TaskStatus.IN_PROGRESS -> 1
            TaskStatus.APPROVED -> 2
            TaskStatus.COMPLETED -> 3
            TaskStatus.FAILED -> 4
            TaskStatus.REJECTED -> 5
            TaskStatus.TIMEOUT -> 6
        }
    }

    private fun submitTasks(tasks: List<Task>) {
        taskAdapter.submitList(
            tasks.sortedWith(
                compareBy<Task> { dashboardSortRank(it.status) }
                    .thenByDescending { it.priority }
                    .thenByDescending { it.createdAt }
            )
        )
    }

    private fun updateEmptyState(tasks: List<Task>) {
        binding.emptyView.visibility = if (tasks.isEmpty()) TextView.VISIBLE else TextView.GONE
    }

    private fun approveTask(task: Task) {
        viewModel.approveTask(task.taskId)
    }

    private fun rejectTask(task: Task) {
        AlertDialog.Builder(this)
            .setTitle("Reject Task")
            .setMessage("Enter reason for rejection (optional):")
            .setPositiveButton("Reject") { _, _ ->
                viewModel.rejectTask(task.taskId, null)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun startTask(task: Task) {
        viewModel.startTask(task.taskId)
    }

    private fun viewTaskDetails(task: Task) {
        val intent = Intent(this, TaskDetailActivity::class.java).apply {
            putExtra("task_id", task.taskId)
            putExtra("session_id", intent.getStringExtra("session_id"))
        }
        startActivity(intent)
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.task_list_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            android.R.id.home -> {
                finish()
                true
            }
            R.id.action_refresh -> {
                viewModel.loadTasks()
                true
            }
            R.id.action_filter_pending -> {
                submitTasks(viewModel.pendingTasks.value.orEmpty())
                true
            }
            R.id.action_filter_active -> {
                submitTasks(viewModel.runningTasks.value.orEmpty())
                true
            }
            R.id.action_filter_all -> {
                submitTasks(viewModel.allTasks.value.orEmpty())
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}
