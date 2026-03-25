package com.user.ui.tools

import android.os.Bundle
import android.view.View
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.user.ClawMobileApplication
import com.user.data.GitConnection
import com.user.data.GitConnectionDao
import com.user.data.PrefsManager
import com.user.databinding.ActivityGithubBinding
import com.user.service.GitHubIntegrationManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * GitHub integration activity for viewing PRs, issues, and commits
 */
class GitHubActivity : AppCompatActivity() {

    private lateinit var binding: ActivityGithubBinding
    private lateinit var gitDao: GitConnectionDao
    private lateinit var prefs: PrefsManager
    private var gitManager: GitHubIntegrationManager? = null
    private val connections = mutableListOf<GitConnection>()
    private var currentRepo: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityGithubBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "GitHub Integration"

        gitDao = (application as ClawMobileApplication).gitConnectionDao
        prefs = PrefsManager(this)

        val token = prefs.githubToken
        if (token.isNotEmpty()) {
            gitManager = GitHubIntegrationManager(token)
        }

        setupRecyclerView()
        loadConnections()
        setupListeners()
    }

    private fun setupListeners() {
        binding.showPrsButton.setOnClickListener {
            showPullRequests()
        }

        binding.showIssuesButton.setOnClickListener {
            showIssues()
        }

        binding.commitIdInput.setOnEditorActionListener { _, _, _ ->
            showCommit(binding.commitIdInput.text.toString())
            true
        }

        binding.showCommitButton.setOnClickListener {
            showCommit(binding.commitIdInput.text.toString())
        }

        binding.refreshButton.setOnClickListener {
            loadConnections()
        }
    }

    private fun setupRecyclerView() {
        binding.repoRecyclerView.apply {
            layoutManager = LinearLayoutManager(this@GitHubActivity)
            adapter = RepoAdapter(connections) { gitConnection ->
                currentRepo = gitConnection.defaultRepo
                binding.repoInfo.text = "Selected: ${gitConnection.defaultRepo ?: "No repo selected"}"
                Toast.makeText(this@GitHubActivity, "Selected: ${gitConnection.defaultRepo ?: "default"}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun loadConnections() {
        CoroutineScope(Dispatchers.IO).launch {
            val allConnections = gitDao.getAllConnections().first()
            withContext(Dispatchers.Main) {
                connections.clear()
                connections.addAll(allConnections)
                binding.emptyView.visibility = if (allConnections.isEmpty()) View.VISIBLE else View.GONE
            }
        }
    }

    private fun showPullRequests() {
        val manager = gitManager ?: run {
            Toast.makeText(this, "GitHub token not configured", Toast.LENGTH_SHORT).show()
            return
        }

        val repo = currentRepo ?: run {
            Toast.makeText(this, "Select a repository first", Toast.LENGTH_SHORT).show()
            return
        }

        val parts = repo.split("/")
        if (parts.size != 2) {
            Toast.makeText(this, "Invalid repo format (expected owner/repo)", Toast.LENGTH_SHORT).show()
            return
        }
        val owner = parts[0]
        val repoName = parts[1]

        binding.loadingIndicator.visibility = View.VISIBLE

        CoroutineScope(Dispatchers.IO).launch {
            val prs = manager.getPullRequests(owner, repoName, "open")
            withContext(Dispatchers.Main) {
                binding.loadingIndicator.visibility = View.GONE
                binding.prList.setText("")
                prs.forEach { pr ->
                    val card = "${pr.title} (#${pr.number}) - ${pr.user} - ${pr.state}"
                    binding.prList.append("$card\n\n")
                }
                if (prs.isEmpty()) {
                    binding.prList.append("No open PRs found\n")
                }
            }
        }
    }

    private fun showIssues() {
        val manager = gitManager ?: run {
            Toast.makeText(this, "GitHub token not configured", Toast.LENGTH_SHORT).show()
            return
        }

        val repo = currentRepo ?: run {
            Toast.makeText(this, "Select a repository first", Toast.LENGTH_SHORT).show()
            return
        }

        binding.loadingIndicator.visibility = View.VISIBLE

        CoroutineScope(Dispatchers.IO).launch {
            val searchResult = manager.searchGitHub("repo:$repo is:issue is:open")
            val issues = searchResult.issues
            withContext(Dispatchers.Main) {
                binding.loadingIndicator.visibility = View.GONE
                binding.issueList.setText("")
                issues.forEach { issue ->
                    val card = "${issue.title} (#${issue.number}) - ${issue.user} - ${issue.state}"
                    binding.issueList.append("$card\n\n")
                }
                if (issues.isEmpty()) {
                    binding.issueList.append("No open issues found\n")
                }
            }
        }
    }

    private fun showCommit(commitId: String) {
        if (commitId.isBlank()) {
            Toast.makeText(this, "Enter a commit ID", Toast.LENGTH_SHORT).show()
            return
        }

        val manager = gitManager ?: run {
            Toast.makeText(this, "GitHub token not configured", Toast.LENGTH_SHORT).show()
            return
        }

        val repo = currentRepo ?: run {
            Toast.makeText(this, "Select a repository first", Toast.LENGTH_SHORT).show()
            return
        }

        val parts = repo.split("/")
        if (parts.size != 2) {
            Toast.makeText(this, "Invalid repo format (expected owner/repo)", Toast.LENGTH_SHORT).show()
            return
        }
        val owner = parts[0]
        val repoName = parts[1]

        binding.loadingIndicator.visibility = View.VISIBLE

        CoroutineScope(Dispatchers.IO).launch {
            val commit = manager.getCommit(owner, repoName, commitId)
            withContext(Dispatchers.Main) {
                binding.loadingIndicator.visibility = View.GONE
                binding.commitInfo.setText("")
                if (commit != null) {
                    binding.commitInfo.append("Author: ${commit.author}\n")
                    binding.commitInfo.append("Message: ${commit.message}\n")
                    binding.commitInfo.append("Date: ${commit.committed_at}\n")
                    binding.commitInfo.append("SHA: ${commit.sha}\n")
                } else {
                    binding.commitInfo.append("Commit not found\n")
                }
            }
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }

    class RepoAdapter(
        private val repos: List<GitConnection>,
        private val onClick: (GitConnection) -> Unit
    ) : androidx.recyclerview.widget.RecyclerView.Adapter<RepoAdapter.RepoViewHolder>() {

        class RepoViewHolder(itemView: android.view.View) :
            androidx.recyclerview.widget.RecyclerView.ViewHolder(itemView) {
            val repoText = itemView.findViewById<TextView>(android.R.id.text1)
            val apiUrlText = itemView.findViewById<TextView>(android.R.id.text2)
        }

        override fun onCreateViewHolder(parent: android.view.ViewGroup, viewType: Int): RepoViewHolder {
            val view = android.view.LayoutInflater.from(parent.context)
                .inflate(android.R.layout.simple_list_item_2, parent, false)
            return RepoViewHolder(view)
        }

        override fun onBindViewHolder(holder: RepoViewHolder, position: Int) {
            val connection = repos[position]
            holder.repoText.text = connection.defaultRepo ?: "No repository"
            holder.apiUrlText.text = connection.apiUrl
            holder.itemView.setOnClickListener { onClick(connection) }
        }

        override fun getItemCount() = repos.size
    }
}
