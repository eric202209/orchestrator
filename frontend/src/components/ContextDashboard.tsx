import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import SessionState from './SessionState';
import ConversationHistory from './ConversationHistory';
import TaskCheckpointDisplay from './TaskCheckpointDisplay';

interface ContextExportResponse {
  context: Record<string, unknown>;
}

interface ContextDashboardProps {
  sessionId: number;
  projectId: number;
  taskId?: number;
}

export default function ContextDashboard({ sessionId, projectId, taskId }: ContextDashboardProps) {
  const [contextSummary, setContextSummary] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchContextSummary = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get(`/api/v1/context/summary/${sessionId}`);
      setContextSummary(response.data.context);
    } catch (err: unknown) {
      setError((err as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Failed to load context summary');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    fetchContextSummary();
  }, [sessionId, fetchContextSummary]);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
      {/* Left Column */}
      <div className="space-y-6">
        {/* Session State */}
        <SessionState sessionId={sessionId} projectId={projectId} />

        {/* Context Summary */}
        <div className="bg-white p-6 rounded-lg shadow-md">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900">Context Summary</h3>
            <button
              onClick={fetchContextSummary}
              className="px-3 py-1 text-sm bg-gray-200 rounded hover:bg-gray-300"
              title="Refresh"
            >
              🔄
            </button>
          </div>

          {loading ? (
            <div className="animate-pulse space-y-2">
              <div className="h-4 bg-gray-200 rounded"></div>
              <div className="h-4 bg-gray-200 rounded w-5/6"></div>
              <div className="h-4 bg-gray-200 rounded w-4/6"></div>
            </div>
          ) : error ? (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
              <p className="font-semibold">Error</p>
              <p className="text-sm">{error}</p>
            </div>
          ) : (
            <div className="prose prose-sm max-w-none">
              <p className="text-gray-700 whitespace-pre-wrap">{contextSummary || 'No context available'}</p>
            </div>
          )}
        </div>
      </div>

      {/* Right Column */}
      <div className="space-y-6">
        {/* Conversation History */}
        <ConversationHistory sessionId={sessionId} limit={10} />

        {/* Task Checkpoints */}
        {taskId && (
          <TaskCheckpointDisplay taskId={taskId} sessionId={sessionId} />
        )}

        {/* Export Context */}
        <div className="bg-white p-6 rounded-lg shadow-md">
          <h3 className="text-lg font-semibold text-gray-900 mb-4">Export Context</h3>
          <p className="text-sm text-gray-600 mb-4">
            Export complete session context including state, conversation, and checkpoints.
          </p>
          <button
            onClick={async () => {
              try {
                const response = await axios.post<ContextExportResponse>(`/api/v1/context/export/${sessionId}`);
                const context = response.data.context;
                const blob = new Blob([JSON.stringify(context, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `context-${sessionId}-${new Date().toISOString()}.json`;
                a.click();
                URL.revokeObjectURL(url);
              } catch (err: unknown) {
                const error = err as { response?: { data?: { detail?: string } } };
                setError(error.response?.data?.detail || 'Failed to export context');
              }
            }}
            className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 text-sm font-medium"
          >
            📥 Export Context
          </button>
        </div>
      </div>
    </div>
  );
}
