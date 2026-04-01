package com.user.viewmodel

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import com.user.repository.ChatRepository
import com.user.service.OrchestratorApiClient

/**
 * Factory for creating TaskViewModel with required dependencies
 */
class TaskViewModelFactory(
    private val repository: ChatRepository,
    private val sessionId: String,
    private val orchestratorClient: OrchestratorApiClient? = null
) : ViewModelProvider.Factory {
    override fun <T : ViewModel> create(modelClass: Class<T>): T {
        if (modelClass.isAssignableFrom(TaskViewModel::class.java)) {
            @Suppress("UNCHECKED_CAST")
            return TaskViewModel(repository, sessionId, orchestratorClient) as T
        }
        throw IllegalArgumentException("Unknown ViewModel class")
    }
}
