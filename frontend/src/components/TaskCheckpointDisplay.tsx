import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';

interface TaskCheckpoint {
  id: number;
  checkpoint_type: string;
  step_number?: number;
  description?: string;
  created_at: string;
}

interface CheckpointResumeData {
  checkpoint_id: number;
  checkpoint_type: string;
  step_number?: number;
  description?: string;
  state_snapshot?: Record<string, unknown>;
  logs_snapshot?: unknown[];
  error_info?: Record<string, unknown>;
}

interface TaskCheckpointDisplayProps {
  taskId: number;
  sessionId?: number;
}

export default function TaskCheckpointDisplay({ taskId }: TaskCheckpointDisplayProps) {
  const [checkpoints, setCheckpoints] = useState<TaskCheckpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resumeData, setResumeData] = useState<CheckpointResumeData | null>(null);

  const fetchCheckpoints = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await axios.get(`/api/v1/context/checkpoints/${taskId}`);
      setCheckpoints(response.data.checkpoints);
    } catch (err: unknown) {
      setError((err as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Failed to load checkpoints');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchCheckpoints();
  }, [fetchCheckpoints]);

  const handleResume = async (checkpointId?: number) => {
    try {
      const response = await axios.post<{ resume_data: CheckpointResumeData }>(`/api/v1/context/resume/${taskId}`, {}, {
        params: { checkpoint_id: checkpointId },
      });

      setResumeData(response.data.resume_data);
    } catch (err: unknown) {
      const error = err as { response?: { data?: { detail?: string } } };
      setError(error.response?.data?.detail || 'Failed to resume from checkpoint');
    }
  };

  const getCheckpointIcon = (type: string) => {
    switch (type) {
      case 'before':
        return '▶️';
      case 'after':
        return '✅';
      case 'error':
        return '⚠️';
      default:
        return '📌';
    }
  };

  const getCheckpointColor = (type: string) => {
    switch (type) {
      case 'before':
        return 'border-blue-500 bg-blue-50';
      case 'after':
        return 'border-green-500 bg-green-50';
      case 'error':
        return 'border-red-500 bg-red-50';
      default:
        return 'border-gray-500 bg-gray-50';
    }
  };

  if (loading) {
    return (
      <div className="bg-white p-6 rounded-lg shadow-md">
        <div className="animate-pulse space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-12 bg-gray-200 rounded"></div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white p-6 rounded-lg shadow-md">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900">Task Checkpoints</h3>
        <span className="text-xs text-gray-500">{checkpoints.length} checkpoints</span>
      </div>

      {/* Checkpoints List */}
      <div className="space-y-2 mb-4">
        {checkpoints.length === 0 ? (
          <div className="text-center text-gray-500 py-8">
            <p>No checkpoints yet</p>
            <p className="text-sm">Checkpoints will appear as task progresses</p>
          </div>
        ) : (
          checkpoints.map((checkpoint) => (
            <div
              key={checkpoint.id}
              className={`p-3 rounded-lg border-l-4 ${getCheckpointColor(checkpoint.checkpoint_type)} cursor-pointer hover:opacity-80 transition-opacity`}
              onClick={() => handleResume(checkpoint.id)}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <span className="text-lg">{getCheckpointIcon(checkpoint.checkpoint_type)}</span>
                  <div>
                    <p className="text-sm font-medium text-gray-900">
                      {checkpoint.description || `${checkpoint.checkpoint_type} checkpoint`}
                    </p>
                    {checkpoint.step_number && (
                      <p className="text-xs text-gray-600">
                        Step {checkpoint.step_number}
                      </p>
                    )}
                  </div>
                </div>
                <span className="text-xs text-gray-500">
                  {new Date(checkpoint.created_at).toLocaleTimeString()}
                </span>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Resume Data Display */}
      {resumeData && (
        <div className="bg-gray-50 p-4 rounded-lg border border-gray-200">
          <h4 className="text-sm font-semibold text-gray-900 mb-2">
            Resume Data (Checkpoint #{resumeData.checkpoint_id})
          </h4>
          <div className="text-xs text-gray-600 space-y-1">
            <p><strong>Type:</strong> {resumeData.checkpoint_type}</p>
            {resumeData.step_number && (
              <p><strong>Step:</strong> {resumeData.step_number}</p>
            )}
            {resumeData.description && (
              <p><strong>Description:</strong> {resumeData.description}</p>
            )}
            {resumeData.error_info && (
              <p className="text-red-600">
                <strong>Error:</strong> {JSON.stringify(resumeData.error_info)}
              </p>
            )}
          </div>
          <div className="mt-3 flex space-x-2">
            <button
              onClick={() => handleResume()}
              className="px-3 py-1 bg-green-500 text-white text-xs rounded hover:bg-green-600"
            >
              Resume from Last Checkpoint
            </button>
            <button
              onClick={() => setResumeData(null)}
              className="px-3 py-1 bg-gray-300 text-gray-700 text-xs rounded hover:bg-gray-400"
            >
              Close
            </button>
          </div>
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-2 rounded-lg text-sm mt-3">
          {error}
        </div>
      )}
    </div>
  );
}
