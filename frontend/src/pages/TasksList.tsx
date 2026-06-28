import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { tasksAPI, projectsAPI } from '@/api/client';
import type { Task, Project } from '@/types/api';
import { 
  Search,
  ListTodo,
  GitBranch,
  AlertTriangle
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, EmptyState } from '@/components/ui';

type TaskStatusFilter = 'all' | 'review' | 'pending' | 'running' | 'done' | 'failed' | 'cancelled';

const statusFilters: Array<{ key: TaskStatusFilter; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'review', label: 'Needs Review' },
  { key: 'failed', label: 'Failed' },
  { key: 'pending', label: 'Pending' },
  { key: 'running', label: 'Running' },
  { key: 'done', label: 'Done' },
  { key: 'cancelled', label: 'Cancelled' },
];

const taskNeedsReview = (task: Task): boolean =>
  task.workspace_status === 'ready' || task.workspace_status === 'changes_requested';

function TasksList() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projects, setProjects] = useState<Record<number, Project>>({});
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<TaskStatusFilter>('all');

  useEffect(() => {
    fetchTasks();
    fetchProjects();
  }, []);

  const fetchTasks = async () => {
    try {
      const response = await tasksAPI.getAll();
      setTasks(response.data || []);
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    } finally {
      setLoading(false);
    }
  };

  const fetchProjects = async () => {
    try {
      const response = await projectsAPI.getAll();
      const projectMap: Record<number, Project> = {};
      response.data?.forEach((project) => {
        projectMap[project.id] = project;
      });
      setProjects(projectMap);
    } catch (error) {
      console.error('Failed to fetch projects:', error);
    }
  };

  const filteredTasks = tasks.filter((task) => {
    const matchesSearch = 
      task.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      task.description?.toLowerCase().includes(searchQuery.toLowerCase());
    
    const matchesStatus =
      statusFilter === 'all' ||
      (statusFilter === 'review' ? taskNeedsReview(task) : task.status === statusFilter);
    
    return matchesSearch && matchesStatus;
  });

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-white">Review Queue</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            {tasks.length} task{tasks.length !== 1 ? 's' : ''} · {Object.keys(projects).length} project{Object.keys(projects).length !== 1 ? 's' : ''}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
            <input
              type="text"
              placeholder="Search..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-44 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] py-1.5 pl-8 pr-3 text-xs text-white placeholder-slate-400 hover:border-[color:var(--oc-border)] focus:border-primary-500 focus:outline-none"
            />
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {statusFilters.map((filter) => {
          const count =
            filter.key === 'all'
              ? tasks.length
              : filter.key === 'review'
                ? tasks.filter(taskNeedsReview).length
              : tasks.filter((task) => task.status === filter.key).length;
          return (
            <button
              key={filter.key}
              type="button"
              onClick={() => setStatusFilter(filter.key)}
              className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                statusFilter === filter.key
                  ? 'border-primary-500 bg-primary-500/10 text-white'
                  : 'border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
              }`}
            >
              {filter.label}
              <span className="ml-1 text-slate-400">{count}</span>
            </button>
          );
        })}
      </div>

      {/* Tasks Grid */}
      {filteredTasks.length === 0 ? (
        <EmptyState
          icon={ListTodo}
          title={
            statusFilter === 'review' && !searchQuery
              ? 'No tasks need review'
              : searchQuery || statusFilter !== 'all'
                ? 'No matching tasks'
                : 'No tasks yet'
          }
          description={
            statusFilter === 'review' && !searchQuery
              ? 'Task outputs appear here when ready for operator approval.'
              : searchQuery || statusFilter !== 'all'
                ? 'Try adjusting your filters or search query'
                : 'Tasks will appear here when you start working on projects'
          }
        />
      ) : (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] divide-y divide-[color:var(--oc-border-soft)]">
          {filteredTasks.map((task) => {
            const project = projects[task.project_id || 0];
            return (
              <Link
                key={task.id}
                to={`/projects/${task.project_id}/tasks/${task.id}`}
                className="flex items-center gap-4 px-4 py-3 hover:bg-[color:var(--oc-surface-raised)] transition-colors group"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <p className="min-w-0 text-sm font-medium text-slate-200 group-hover:text-white transition-colors line-clamp-1">
                      {task.title}
                    </p>
                    {taskNeedsReview(task) && (
                      <span className="inline-flex shrink-0 items-center gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-xs font-medium text-amber-200">
                        <AlertTriangle className="h-3 w-3" />
                        Needs review
                      </span>
                    )}
                  </div>
                  {task.description && (
                    <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">
                      {task.description}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-1 text-xs text-slate-400">
                    {project && (
                      <span className="flex items-center gap-1">
                        <GitBranch className="h-3 w-3" />
                        {project.name}
                      </span>
                    )}
                    {task.created_at && (
                      <span>{new Date(task.created_at).toLocaleDateString()}</span>
                    )}
                  </div>
                </div>
                <StatusBadge status={task.status} size="sm" />
              </Link>
            );
          })}
        </div>
      )}

      {/* Stats Footer */}
      {tasks.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
            <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-1.5">Pending</p>
            <p className="text-xl font-semibold text-white">{tasks.filter(t => t.status === 'pending').length}</p>
          </div>
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
            <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-1.5">Running</p>
            <p className="text-xl font-semibold text-primary-400">{tasks.filter(t => t.status === 'running').length}</p>
          </div>
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
            <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-1.5">Done</p>
            <p className="text-xl font-semibold text-emerald-400">{tasks.filter(t => t.status === 'done').length}</p>
          </div>
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4">
            <p className="text-[10px] font-medium uppercase tracking-wider text-slate-400 mb-1.5">Failed</p>
            <p className="text-xl font-semibold text-red-400">{tasks.filter(t => t.status === 'failed').length}</p>
          </div>
        </div>
      )}
    </div>
  );
}

export default TasksList;
