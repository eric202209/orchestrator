package com.user.ui.tasks

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ProgressBar
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.user.R
import com.user.data.Project

/**
 * Adapter for displaying project progress cards with real task counts from Orchestrator API
 */
class ProjectProgressAdapter(
    private val onProjectClickListener: ((Project) -> Unit)? = null
) : ListAdapter<Project, ProjectProgressAdapter.ProjectViewHolder>(ProjectDiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ProjectViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_project_progress, parent, false)
        return ProjectViewHolder(view)
    }

    override fun onBindViewHolder(holder: ProjectViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    inner class ProjectViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val projectName: TextView = itemView.findViewById(R.id.projectName)
        private val projectRunningCount: TextView = itemView.findViewById(R.id.projectRunningCount)
        private val projectPendingCount: TextView = itemView.findViewById(R.id.projectPendingCount)
        private val projectCompletedCount: TextView = itemView.findViewById(R.id.projectCompletedCount)
        private val projectProgressBar: ProgressBar = itemView.findViewById(R.id.projectProgressBar)

        fun bind(project: Project) {
            projectName.text = project.name

            // Set empty state initially (will be updated by loadProjectTaskCounts)
            projectRunningCount.text = "0"
            projectPendingCount.text = "0"
            projectCompletedCount.text = "0"

            // Hide progress bar for static view
            projectProgressBar.visibility = View.GONE

            // Click handler
            itemView.setOnClickListener {
                onProjectClickListener?.invoke(project)
            }
        }

        fun updateWithStats(running: Int, pending: Int, completed: Int, total: Int) {
            // Using a temporary variable to avoid re-appending if called multiple times
            val baseName = projectName.text.toString().substringBefore(" (")
            projectName.text = "$baseName ($total tasks)"
            projectRunningCount.text = running.toString()
            projectPendingCount.text = pending.toString()
            projectCompletedCount.text = completed.toString()

            // Calculate completion percentage if we have total tasks
            if (total > 0) {
                val percent = ((completed.toFloat()) / total * 100).toInt()
                projectProgressBar.progress = percent
                projectProgressBar.visibility = View.VISIBLE
            } else {
                projectProgressBar.visibility = View.GONE
            }
        }

        fun showEmptyState() {
            val baseName = projectName.text.toString().substringBefore(" (")
            projectName.text = "$baseName (No tasks)"
            projectRunningCount.text = "0"
            projectPendingCount.text = "0"
            projectCompletedCount.text = "0"
            projectProgressBar.visibility = View.GONE
        }
    }

    class ProjectDiffCallback : DiffUtil.ItemCallback<Project>() {
        override fun areItemsTheSame(oldItem: Project, newItem: Project): Boolean {
            return oldItem.getProjectId() == newItem.getProjectId()
        }

        override fun areContentsTheSame(oldItem: Project, newItem: Project): Boolean {
            return oldItem == newItem
        }
    }
}
