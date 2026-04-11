import { useCallback, useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { tasksAPI } from '@/api/client';
import type { Task } from '@/types/api';
import { 
  ArrowLeft, 
  Edit3, 
  Save, 
  X, 
  Clock, 
  CheckCircle2, 
  PlayCircle, 
  XCircle as XCircleIcon,
  Calendar,
  AlertCircle,
  FileJson
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, Button, TextArea, Alert } from '@/components/ui';
import { cn } from '@/lib/utils';

function TaskDetail() {
  const { taskId } = useParams<{ projectId: string; taskId: string }>();
  const navigate = useNavigate();
  const [task, setTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    title: '',
    description: '',
    steps: '',
    current_step: 0
  });

  const fetchTask = useCallback(async () => {
    try {
      const response = await tasksAPI.getById(Number(taskId));
      setTask(response.data);
      const steps = response.data?.steps ? JSON.stringify(JSON.parse(response.data.steps), null, 2) : '';
      setEditForm({
        title: response.data?.title || '',
        description: response.data?.description || '',
        steps: steps,
        current_step: response.data?.current_step || 0
      });
    } catch (error) {
      console.error('Failed to fetch task:', error);
      setSaveError('Failed to load task details');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    fetchTask();
  }, [fetchTask]);

  const handleSave = async () => {
    try {
      setSaveError(null);
      
      // Parse steps JSON
      let stepsJson = null;
      if (editForm.steps.trim()) {
        try {
          stepsJson = JSON.stringify(JSON.parse(editForm.steps));
        } catch {
          setSaveError('Invalid JSON format for steps. Please check your syntax.');
          return;
        }
      }
      
      await tasksAPI.update(Number(taskId), {
        title: editForm.title,
        description: editForm.description,
        steps: stepsJson,
        current_step: editForm.current_step
      });
      await fetchTask();
      setEditing(false);
    } catch (error) {
      console.error('Failed to update task:', error);
      setSaveError('Failed to save changes');
    }
  };

  const handleCancel = () => {
    setEditing(false);
    fetchTask();
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'done':
        return <CheckCircle2 className="h-5 w-5 text-emerald-400" />;
      case 'running':
        return <PlayCircle className="h-5 w-5 text-blue-400 animate-pulse" />;
      case 'failed':
        return <XCircleIcon className="h-5 w-5 text-red-400" />;
      default:
        return <Clock className="h-5 w-5 text-slate-400" />;
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'done':
        return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
      case 'running':
        return 'bg-blue-500/10 text-blue-400 border-blue-500/20';
      case 'failed':
        return 'bg-red-500/10 text-red-400 border-red-500/20';
      default:
        return 'bg-slate-500/10 text-slate-400 border-slate-500/20';
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  if (!task) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Alert
          variant="destructive"
          title="Task not found"
          description="The task you're looking for doesn't exist or has been removed."
        />
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate('/tasks')}
            className="gap-2"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to Tasks
          </Button>
          {/* Hidden back button for reliable navigation */}
          <button
            onClick={() => navigate('/tasks')}
            className="sr-only"
            aria-label="Back to tasks"
            style={{ position: 'absolute', left: '-9999px', width: '1px', height: '1px', overflow: 'hidden' }}
          >
            Back to tasks
          </button>
          <h1 className="text-2xl font-bold text-slate-100">Task Details</h1>
        </div>
        {!editing ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setEditing(true)}
            className="gap-2"
          >
            <Edit3 className="h-4 w-4" />
            Edit Task
          </Button>
        ) : (
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleCancel}
              className="gap-2"
            >
              <X className="h-4 w-4" />
              Cancel
            </Button>
            <Button
              variant="default"
              size="sm"
              onClick={handleSave}
              className="gap-2"
            >
              <Save className="h-4 w-4" />
              Save Changes
            </Button>
          </div>
        )}
      </div>

      {/* Error Alert */}
      {saveError && (
        <Alert
          variant="destructive"
          title="Error"
          description={saveError}
        />
      )}

      {/* Task Content */}
      <div className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-8">
        <div className="flex items-start justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className={cn('p-3 rounded-lg', getStatusColor(task.status))}>
              {getStatusIcon(task.status)}
            </div>
            <div>
              <h2 className="text-xl font-bold text-white">
                {task.title}
              </h2>
              <StatusBadge status={task.status} />
            </div>
          </div>
          {task.created_at && (
            <div className="flex items-center gap-2 text-sm text-slate-400">
              <Calendar className="h-4 w-4" />
              <span>
                Created {new Date(task.created_at).toLocaleDateString()}
              </span>
            </div>
          )}
        </div>

        {/* Edit Mode */}
        {editing ? (
          <div className="space-y-6">
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Title
              </label>
              <input
                value={editForm.title}
                onChange={(e) => setEditForm({ ...editForm, title: e.target.value })}
                placeholder="Task title"
                className="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Description
              </label>
              <TextArea
                value={editForm.description}
                onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                placeholder="Task description"
                className="bg-slate-900 border-slate-700 min-h-[150px]"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Step-by-Step Plan (JSON)
              </label>
              <p className="text-xs text-slate-500 mb-2">
                Define task steps as a JSON array. Each step should have: <code className="bg-slate-700 px-1 rounded">{"{"} "action": "...", "description": "..." {"}"}</code>
              </p>
              <TextArea
                value={editForm.steps}
                onChange={(e) => setEditForm({ ...editForm, steps: e.target.value })}
                placeholder='[{"action": "setup", "description": "Initialize project"}, {"action": "code", "description": "Write main code"}]'
                className="bg-slate-900 border-slate-700 min-h-[300px] font-mono text-sm"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Current Step
              </label>
              <input
                type="number"
                value={editForm.current_step}
                onChange={(e) => setEditForm({ ...editForm, current_step: parseInt(e.target.value) || 0 })}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
            </div>
          </div>
        ) : (
          /* View Mode */
          <div className="space-y-6">
            {task.description && (
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-2">Description</h3>
                <p className="text-slate-400 whitespace-pre-wrap">{task.description}</p>
              </div>
            )}

            {task.error_message && (
              <div className="flex items-start gap-3 p-4 bg-red-500/10 border border-red-500/20 rounded-lg">
                <AlertCircle className="h-5 w-5 text-red-400 mt-0.5" />
                <div>
                  <h3 className="text-sm font-medium text-red-400 mb-1">Error</h3>
                  <p className="text-sm text-red-300">{task.error_message}</p>
                </div>
              </div>
            )}

            {task.steps && (
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-2 flex items-center gap-2">
                  <FileJson className="h-4 w-4" />
                  Step-by-Step Plan
                </h3>
                <pre className="bg-slate-900 p-4 rounded-lg text-sm text-slate-300 overflow-x-auto font-mono">
                  {typeof task.steps === 'string' ? task.steps : JSON.stringify(task.steps, null, 2)}
                </pre>
              </div>
            )}

            <div className="grid grid-cols-2 gap-4 pt-4 border-t border-slate-700">
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Project ID</h3>
                <p className="text-slate-400 text-sm">{task.project_id || 'N/A'}</p>
              </div>
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Linked Session ID</h3>
                <p className="text-slate-400 text-sm">{task.session_id || 'N/A'}</p>
              </div>
              <div>
                <h3 className="text-sm font-medium text-slate-300 mb-1">Current Step</h3>
                <p className="text-slate-400 text-sm">
                  {task.current_step > 0 ? `Step ${task.current_step}` : 'Not started'}
                </p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default TaskDetail;

