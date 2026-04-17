/**
 * ProjectIsolationStatus.tsx
 * 
 * Displays project isolation status and safety information
 */

import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../api/client';

interface IsolationStatus {
  project_id: number;
  project_name: string;
  workspace_path: string;
  project_root: string | null;
  status: 'active' | 'warning' | 'error';
  message: string;
  isolation_enabled: boolean;
}

interface ValidateResponse {
  valid: boolean;
  requested_path: string;
  resolved_path: string;
  project_root: string;
  is_within_bounds: boolean;
  message: string;
}

export const ProjectIsolationStatus: React.FC<{ projectId: number }> = ({ projectId }) => {
  const [status, setStatus] = useState<IsolationStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [testPath, setTestPath] = useState('src/index.ts');
  const [validationResult, setValidationResult] = useState<ValidateResponse | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      const response = await api.get<IsolationStatus>(`/isolation/projects/${projectId}/isolation/status`);
      setStatus(response.data);
    } catch (error) {
      console.error('Failed to load isolation status:', error);
      setStatus({
        project_id: projectId,
        project_name: 'Unknown',
        workspace_path: 'not set',
        project_root: null,
        status: 'error',
        message: 'Failed to load isolation status',
        isolation_enabled: false,
      });
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  const validatePath = useCallback(async () => {
    try {
      const response = await api.post<ValidateResponse>(`/isolation/projects/${projectId}/isolation/validate`, {
        path: testPath,
      });
      setValidationResult(response.data);
    } catch (error: unknown) {
      setValidationResult({
        valid: false,
        requested_path: testPath,
        resolved_path: '',
        project_root: '',
        is_within_bounds: false,
        message: (error as { response?: { data?: { detail?: string } } }).response?.data?.detail || 'Validation failed',
      });
    }
  }, [projectId, testPath]);

  if (loading) {
    return (
      <div className="p-4 bg-gray-800 rounded-lg">
        <p className="text-gray-400">Loading isolation status...</p>
      </div>
    );
  }

  if (!status) {
    return null;
  }

  const statusColors = {
    active: 'bg-green-500',
    warning: 'bg-yellow-500',
    error: 'bg-red-500',
  };

  return (
    <div className="space-y-4">
      {/* Status Card */}
      <div className="p-4 bg-gray-800 rounded-lg border-l-4 border-green-500">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-lg font-semibold text-white">🛡️ Project Isolation</h3>
          <div className="flex items-center gap-2">
            <span className={`w-3 h-3 rounded-full ${statusColors[status.status]}`}></span>
            <span className="text-sm text-gray-400">
              {status.isolation_enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
        </div>

        {status.project_root && (
          <div className="mt-2 text-sm">
            <p className="text-gray-300">
              <strong>Workspace:</strong>{' '}
              <code className="bg-gray-700 px-2 py-1 rounded">{status.project_root}</code>
            </p>
            <p className="text-gray-400 mt-1">{status.message}</p>
          </div>
        )}

        {!status.project_root && (
          <p className="text-yellow-500 mt-2">⚠️ Workspace path not configured</p>
        )}
      </div>

      {/* Path Validator */}
      <div className="p-4 bg-gray-800 rounded-lg">
        <h4 className="text-md font-semibold text-white mb-3">🔍 Path Validator</h4>

        <div className="flex gap-2 mb-3">
          <input
            type="text"
            value={testPath}
            onChange={(e) => setTestPath(e.target.value)}
            placeholder="Enter path (e.g., src/main.py)"
            className="flex-1 px-3 py-2 bg-gray-700 text-white rounded-lg border border-gray-600 focus:border-green-500 focus:outline-none"
          />
          <button
            onClick={validatePath}
            className="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg transition"
          >
            Validate
          </button>
        </div>

        {validationResult && (
          <div
            className={`p-3 rounded-lg ${
              validationResult.valid ? 'bg-green-900/30 border border-green-500' : 'bg-red-900/30 border border-red-500'
            }`}
          >
            <div className="flex items-center gap-2 mb-2">
              <span className="text-lg">
                {validationResult.valid ? '✅' : '❌'}
              </span>
              <span className="font-semibold text-white">
                {validationResult.valid ? 'Valid' : 'Invalid'}
              </span>
            </div>

            <div className="text-sm space-y-1 text-gray-300">
              <p>
                <strong>Requested:</strong> <code>{validationResult.requested_path}</code>
              </p>
              <p>
                <strong>Resolved:</strong>{' '}
                <code className="text-xs">{validationResult.resolved_path}</code>
              </p>
              <p className="mt-2 text-gray-400">{validationResult.message}</p>
            </div>
          </div>
        )}
      </div>

      {/* Safety Info */}
      <div className="p-4 bg-blue-900/20 rounded-lg border border-blue-500">
        <h4 className="text-md font-semibold text-blue-400 mb-2">📋 Safety Rules</h4>
        <ul className="text-sm text-gray-300 space-y-1">
          <li>✅ Files within workspace: ALLOWED</li>
          <li>❌ Files outside workspace: BLOCKED</li>
          <li>⚠️ External resources: Ask user first</li>
        </ul>
      </div>
    </div>
  );
};

export default ProjectIsolationStatus;
