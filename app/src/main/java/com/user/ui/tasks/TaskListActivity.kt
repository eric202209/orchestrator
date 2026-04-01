package com.user.ui.tasks

import android.content.Intent
import android.os.Bundle
import android.util.Log
import android.view.Menu
import android.view.MenuItem
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.ViewModelProvider
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.snackbar.Snackbar
import com.user.ClawMobileApplication
import com.user.R
import com.user.data.DashboardSummary
import com.user.data.OrchestTask
import com.user.data.PrefsManager
import com.user.data.Project
import com.user.data.Task
import com.user.data.TaskStatus
import com.user.databinding.ActivityTaskListBinding
import com.user.service.OrchestratorApiClient
import com.user.ui.tasks.ProjectProgressAdapter
import com.user.viewmodel.TaskViewModel
import com.user.viewmodel.TaskViewModelFactory
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

/**
 * Activity for displaying and managing tasks
 */
class TaskListActivity : AppCompatActivity() {

    private lateinit var binding: ActivityTaskListBinding
    private lateinit var taskAdapter: TaskAdapter
    private lateinit var viewModel: TaskViewModel
    private var orchestratorApiClient: OrchestratorApiClient? = null
    private lateinit var prefsManager: PrefsManager
    private lateinit var projectProgressAdapter: ProjectProgressAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityTaskListBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.title = "Tasks"
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        setupRecyclerView()
        setupProjectRecyclerView()
        setupViewModel()
        setupObservers()
    }

    private fun setupViewModel() {
        val sessionId = intent.getStringExtra("session_id") ?: ""
        val app = application as ClawMobileApplication
        prefsManager = app.prefsManager

        // Log configuration status for debugging
        Log.d("TaskListActivity", "Session ID: '$sessionId'")
        Log.d("TaskListActivity", "Orchestrator configured: ${prefsManager.isOrchestratorConfigured()}")
        if (prefsManager.isOrchestratorConfigured()) {
            Log.d("TaskListActivity", "Orchestrator URL: ${prefsManager.orchestratorServerUrl}")
            Log.d("TaskListActivity", "Gateway token length: ${prefsManager.gatewayToken.length}")
        }

        // Create Orchestrator API client if configured
        if (prefsManager.isOrchestratorConfigured()) {
            orchestratorApiClient = OrchestratorApiClient(
                prefs = prefsManager,
                gatewayToken = prefsManager.gatewayToken
            )
            Log.d("TaskListActivity", "OrchestratorApiClient created successfully")
        } else {
            Log.w("TaskListActivity", "Orchestrator not configured - will use local tasks only")
        }

        val factory = TaskViewModelFactory(
            repository = app.repository,
            sessionId = sessionId,
            orchestratorClient = orchestratorApiClient
        )
        viewModel = ViewModelProvider(this, factory)[TaskViewModel::class.java]
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

    private fun setupProjectRecyclerView() {
        projectProgressAdapter = ProjectProgressAdapter(
            onProjectClickListener = { project ->
                // TODO: Navigate to project details or show more info
            }
        )
        binding.projectsRecyclerView.apply {
            layoutManager = LinearLayoutManager(this@TaskListActivity)
            adapter = projectProgressAdapter
        }
    }

    private fun setupObservers() {
        Log.d("TaskListActivity", "Setting up observers for tasks")

        // Single observer for all tasks to avoid duplicate processing
        viewModel.allTasks.observe(this) { tasks ->
            Log.d("TaskListActivity", "Received ${tasks.size} tasks from ViewModel")
            submitTasks(tasks)
            updateEmptyState(tasks)
            updateLocalStats(tasks)  // Update stats from local tasks as fallback

            // If no tasks loaded, show a helpful message about potential database access issues
            if (tasks.isEmpty()) {
                Log.d("TaskListActivity", "No tasks loaded - checking for database access issues")
                binding.emptyView.visibility = TextView.VISIBLE

                // Show empty state view with custom text if database might be inaccessible
                val sessionId = intent.getStringExtra("session_id") ?: ""
                val emptyText = when {
                    sessionId.isBlank() && orchestratorApiClient == null ->
                        "Orchestrator not configured. Please configure it in Settings."
                    sessionId.isBlank() ->
                        "No tasks found from Orchestrator. Check if the backend is running and accessible."
                    else -> "No tasks found for this session. Tasks may not have been created yet, or there could be a database access issue."
                }
                (binding.emptyView as TextView).text = emptyText
            }
        }

        viewModel.pendingTasks.observe(this) { tasks ->
            binding.pendingCountNew.text = tasks.size.toString()
            Log.d("TaskListActivity", "Pending tasks: ${tasks.size}")
        }

        viewModel.runningTasks.observe(this) { tasks ->
            // Update running count in the layout
            binding.runningCount.text = tasks.size.toString()
            Log.d("TaskListActivity", "Running/In-progress tasks: ${tasks.size}")
        }

        // Observe Orchestrator stats for comprehensive dashboard view
        viewModel.orchestratorStats.observe(this) { stats ->
            updateOrchestratorDashboard(stats)
        }

        // Load and display projects from Orchestrator with task counts
        loadProjectsFromOrchestrator()
    }

    private fun updateOrchestratorDashboard(stats: DashboardSummary?) {
        if (stats == null) return

        // Update all dashboard stats
        binding.totalProjectsCount.text = stats.projects.toString()
        binding.totalTasksCount.text = stats.tasks.total.toString()
        binding.runningCount.text = stats.tasks.inProgress.toString()

        // Calculate pending and completed from Orchestrator data
        val pending = stats.tasks.pending + stats.tasks.approved
        binding.pendingCountNew.text = pending.toString()
        binding.completedCount.text = stats.tasks.done.toString()

        // Show comprehensive stats summary
        val statsText = getString(
            R.string.orchestrator_dashboard_stats,
            stats.projects,
            stats.tasks.total,
            stats.tasks.inProgress,
            pending,
            stats.tasks.done,
            stats.tasks.failed
        )

        binding.orchestratorStatsView.text = statsText
        binding.orchestratorStatsView.visibility = TextView.VISIBLE
    }

    private fun updateLocalStats(tasks: List<Task>) {
        // Update counts based on local task data
        val totalTasks = tasks.size
        val running = tasks.count { it.status == TaskStatus.IN_PROGRESS || it.status == TaskStatus.APPROVED }
        val pending = tasks.count { it.status == TaskStatus.PENDING }
        val completed = tasks.count { it.status == TaskStatus.COMPLETED }

        binding.totalTasksCount.text = totalTasks.toString()
        if (running > 0) {
            binding.runningCount.text = running.toString()
        }
        if (pending > 0) {
            binding.pendingCountNew.text = pending.toString()
        }
        if (completed > 0) {
            binding.completedCount.text = completed.toString()
        }

        // Show local stats if no Orchestrator data yet
        if (binding.orchestratorStatsView.visibility == TextView.GONE) {
            val statsText = getString(
                R.string.local_stats,
                totalTasks, running, pending, completed
            )
            binding.orchestratorStatsView.text = statsText
            binding.orchestratorStatsView.visibility = TextView.VISIBLE
        }
    }

    private fun loadProjectsFromOrchestrator() {
        if (orchestratorApiClient == null || !prefsManager.isOrchestratorConfigured()) {
            Log.d("TaskListActivity", "Skipping Orchestrator projects - not configured")
            return
        }

        // Load projects in a coroutine - failures are handled silently
        CoroutineScope(Dispatchers.Main).launch {
            orchestratorApiClient?.getProjects()?.onSuccess { projects ->
                runOnUiThread {
                    Log.d("TaskListActivity", "Loaded ${projects.size} projects from Orchestrator")
                    projectProgressAdapter.submitList(projects)

                    // Fetch task counts for each project after adapter is updated
                    loadProjectTaskCounts(projects)
                }
            }?.onFailure { error ->
                // Silent failure - just log, don't show error to user
                Log.d("TaskListActivity", "Orchestrator projects unavailable (normal when not on same network): ${error.message}")
                runOnUiThread {
                    projectProgressAdapter.submitList(emptyList())
                }
            }
        }
    }

    /**
     * Load task counts for each project by calling the status endpoint
     */
    private fun loadProjectTaskCounts(projects: List<Project>) {
        if (projects.isEmpty()) return

        CoroutineScope(Dispatchers.Main).launch {
            projects.forEach { project ->
                val projectId = project.getProjectId()

                // Call /mobile/projects/{project_id}/status to get task counts
                orchestratorApiClient?.getProjectStatus(projectId)?.onSuccess { status ->
                    runOnUiThread {
                        val position = projectProgressAdapter.currentList.indexOfFirst {
                            it.getProjectId() == projectId
                        }

                        if (position >= 0) {
                            val holder = binding.projectsRecyclerView.findViewHolderForAdapterPosition(position)
                            if (holder is ProjectProgressAdapter.ProjectViewHolder) {
                                status.tasks?.let { tasks ->
                                    Log.d("TaskListActivity", "Loaded stats for project $projectId: $tasks")
                                    holder.updateWithStats(
                                        running = tasks.running ?: 0,
                                        pending = tasks.pending ?: 0,
                                        completed = tasks.done ?: 0,
                                        total = tasks.total ?: 0
                                    )
                                } ?: holder.showEmptyState()
                            }
                        }
                    }
                }?.onFailure { error ->
                    // Silent failure - just log for debugging
                    Log.d("TaskListActivity", "Orchestrator status unavailable for project $projectId: ${error.message}")
                    runOnUiThread {
                        val position = projectProgressAdapter.currentList.indexOfFirst {
                            it.getProjectId() == projectId
                        }
                        if (position >= 0) {
                            binding.projectsRecyclerView.findViewHolderForAdapterPosition(position)
                                ?.let { holder ->
                                    if (holder is ProjectProgressAdapter.ProjectViewHolder) {
                                        holder.showEmptyState()
                                    }
                                }
                        }
                    }
                }
            }
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
                val usingOrchestrator = orchestratorApiClient != null && prefsManager.isOrchestratorConfigured()

                if (usingOrchestrator) {
                    // Refresh from Orchestrator API
                    viewModel.refreshAll()
                    Snackbar.make(binding.root, "Syncing with Orchestrator...", Snackbar.LENGTH_SHORT).show()
                } else {
                    // Just refresh local data
                    viewModel.loadTasks()
                    Snackbar.make(binding.root, "Refreshing tasks (local)...", Snackbar.LENGTH_SHORT).show()
                }
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