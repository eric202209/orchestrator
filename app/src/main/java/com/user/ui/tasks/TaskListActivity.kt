package com.user.ui.tasks

import android.content.Intent
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.Toolbar
import androidx.lifecycle.ViewModelProvider
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
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
        viewModel.pendingTasks.observe(this) { tasks ->
            binding.pendingCount.text = tasks.size.toString()
            updatePendingTasks(tasks)
            updateEmptyState()
        }

        viewModel.activeTasks.observe(this) { tasks ->
            updateActiveTasks(tasks)
            updateEmptyState()
        }
    }

    private fun updatePendingTasks(tasks: List<Task>) {
        taskAdapter.submitList(tasks.filter { it.status == TaskStatus.PENDING })
    }

    private fun updateActiveTasks(tasks: List<Task>) {
        taskAdapter.submitList(tasks.filter {
            it.status == TaskStatus.APPROVED || it.status == TaskStatus.IN_PROGRESS
        })
    }

    private fun updateEmptyState() {
        val pendingTasks = viewModel.pendingTasks.value?.filter { it.status == TaskStatus.PENDING }?.isEmpty() ?: true
        val activeTasks = viewModel.activeTasks.value?.filter {
            it.status == TaskStatus.APPROVED || it.status == TaskStatus.IN_PROGRESS
        }?.isEmpty() ?: true

        binding.emptyView.visibility = if (pendingTasks && activeTasks) TextView.VISIBLE else TextView.GONE
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
                taskAdapter.submitList(viewModel.pendingTasks.value?.filter { it.status == TaskStatus.PENDING } ?: emptyList())
                true
            }
            R.id.action_filter_active -> {
                taskAdapter.submitList(viewModel.activeTasks.value?.filter {
                    it.status == TaskStatus.APPROVED || it.status == TaskStatus.IN_PROGRESS
                } ?: emptyList())
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}
