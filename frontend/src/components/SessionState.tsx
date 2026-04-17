import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

interface SessionState {
  session_id: number;
  project_id: number;
  current_step: number;
  total_steps: number;
  completion_percent: number;
  state_version: number;
  last_snapshot_at?: string;
}

// ConversationMessage intentionally unused - kept for future conversation display features
// eslint-disable-next-line @typescript-eslint/no-unused-vars
interface ConversationMessage {
  id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  metadata?: Record<string, unknown>;
  created_at: string;
}

interface SessionStateProps {
  sessionId: number;
  projectId?: number;
}

export default function SessionStateDisplay({ sessionId }: SessionStateProps) {
  const [state, setState] = useState<SessionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSessionState = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get(`/api/v1/context/state/${sessionId}`);
      if (response.data.exists) {
        setState(response.data);
      }
    } catch (err: unknown) {
      setError((err as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Failed to load session state');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    fetchSessionState();
    const interval = setInterval(fetchSessionState, 5000); // Auto-refresh every 5 seconds
    return () => clearInterval(interval);
  }, [fetchSessionState]);

  if (loading) {
    return (
      <div className="bg-white p-6 rounded-lg shadow-md">
        <div className="animate-pulse flex space-x-4">
          <div className="flex-1 space-y-4 py-1">
            <div className="h-4 bg-gray-200 rounded w-3/4"></div>
            <div className="space-y-2">
              <div className="h-4 bg-gray-200 rounded"></div>
              <div className="h-4 bg-gray-200 rounded w-5/6"></div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
        <p className="font-semibold">Error</p>
        <p className="text-sm">{error}</p>
      </div>
    );
  }

  if (!state) {
    return (
      <div className="bg-gray-50 border border-gray-200 text-gray-600 px-4 py-3 rounded-lg text-center">
        <p>No session state available</p>
        <p className="text-sm mt-1">Start a task to begin tracking progress</p>
      </div>
    );
  }

  const progressColor = state.completion_percent >= 100 
    ? 'bg-green-500' 
    : state.completion_percent >= 50 
      ? 'bg-blue-500' 
      : 'bg-yellow-500';

  return (
    <div className="bg-white p-6 rounded-lg shadow-md">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900">Session Progress</h3>
        <span className="text-xs text-gray-500">v{state.state_version}</span>
      </div>

      {/* Progress Bar */}
      <div className="mb-4">
        <div className="flex justify-between text-sm mb-1">
          <span className="text-gray-600">Step {state.current_step} of {state.total_steps}</span>
          <span className="text-gray-600">{state.completion_percent.toFixed(1)}%</span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2.5">
          <div
            className={`h-2.5 rounded-full ${progressColor} transition-all duration-500`}
            style={{ width: `${state.completion_percent}%` }}
          ></div>
        </div>
      </div>

      {/* State Details */}
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-600">Last Snapshot:</span>
          <span className="text-gray-900 font-medium">
            {state.last_snapshot_at 
              ? new Date(state.last_snapshot_at).toLocaleTimeString()
              : 'Never'}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-600">Session:</span>
          <span className="text-gray-900 font-medium">{state.session_id}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-600">Project:</span>
          <span className="text-gray-900 font-medium">{state.project_id}</span>
        </div>
      </div>

      {/* Auto-refresh indicator */}
      <div className="mt-4 flex items-center justify-center">
        <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse"></div>
        <span className="ml-2 text-xs text-gray-500">Auto-refreshing...</span>
      </div>
    </div>
  );
}
