import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { tasksAPI, projectsAPI } from '@/api/client';
import type { Task, Project } from '@/types/api';
import { 
  CheckCircle2, 
  PlayCircle, 
  XCircle, 
  Clock, 
  Search,
  Filter,
  ListTodo,
  GitBranch
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, EmptyState } from '@/components/ui';

type TaskStatus = 'pending' | 'running' | 'done' | 'failed';

function TasksList() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projects, setProjects] = useState<Record<number, Project>>({});
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all');

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
    
    const matchesStatus = statusFilter === 'all' || task.status === statusFilter;
    
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
          <h1 className="text-lg font-semibold text-white">Tasks</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {tasks.length} task{tasks.length !== 1 ? 's' : ''} · {Object.keys(projects).length} project{Object.keys(projects).length !== 1 ? 's' : ''}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Filter className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as TaskStatus | 'all')}
              className="pl-8 pr-3 py-1.5 bg-slate-800 border border-slate-700 rounded-md text-xs text-slate-300 focus:outline-none focus:border-slate-600 hover:border-slate-600"
            >
              <option value="all">All</option>
              <option value="pending">Pending</option>
              <option value="running">Running</option>
              <option value="done">Done</option>
              <option value="failed">Failed</option>
            </select>
          </div>

          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
            <input
              type="text"
              placeholder="Search..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-8 pr-3 py-1.5 bg-slate-800 border border-slate-700 rounded-md text-xs text-white placeholder-slate-500 focus:outline-none focus:border-slate-600 w-44"
            />
          </div>
        </div>
      </div>

      {/* Tasks Grid */}
      {filteredTasks.length === 0 ? (
        <EmptyState
          icon={ListTodo}
          title={searchQuery || statusFilter !== 'all' ? 'No matching tasks' : 'No tasks yet'}
          description={
            searchQuery || statusFilter !== 'all'
              ? 'Try adjusting your filters or search query'
              : 'Tasks will appear here when you start working on projects'
          }
        />
      ) : (
        <div className="bg-slate-800 rounded-lg border border-slate-700 divide-y divide-slate-700/60">
          {filteredTasks.map((task) => {
            const project = projects[task.project_id || 0];
            return (
              <Link
                key={task.id}
                to={`/projects/${task.project_id}/tasks/${task.id}`}
                className="flex items-center gap-4 px-4 py-3 hover:bg-slate-700/40 transition-colors group"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-200 group-hover:text-white transition-colors line-clamp-1">
                    {task.title}
                  </p>
                  {task.description && (
                    <p className="text-xs text-slate-400 mt-0.5 line-clamp-1">
                      {task.description}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-1 text-xs text-slate-500">
                    {project && (
                      <span className="flex items-center gap-1">
                        <GitBranch className="h-3 w-3" />
                        {project.name}
                      </span>
                    )}
                    {task.created_at && (
                      <span>{new Date(task.created_at).toLocaleDateString()}</span>
                    )}
                    {task.error_message && (
                      <span className="text-red-500 flex items-center gap-1">
                        <XCircle className="h-3 w-3" />
                        Error
                      </span>
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
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-4">
            <p className="text-xs text-slate-400 mb-1.5">Pending</p>
            <p className="text-xl font-semibold text-white">{tasks.filter(t => t.status === 'pending').length}</p>
          </div>
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-4">
            <p className="text-xs text-slate-400 mb-1.5">Running</p>
            <p className="text-xl font-semibold text-sky-400">{tasks.filter(t => t.status === 'running').length}</p>
          </div>
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-4">
            <p className="text-xs text-slate-400 mb-1.5">Done</p>
            <p className="text-xl font-semibold text-emerald-400">{tasks.filter(t => t.status === 'done').length}</p>
          </div>
          <div className="bg-slate-800 rounded-lg border border-slate-700 p-4">
            <p className="text-xs text-slate-400 mb-1.5">Failed</p>
            <p className="text-xl font-semibold text-red-400">{tasks.filter(t => t.status === 'failed').length}</p>
          </div>
        </div>
      )}
    </div>
  );
}

export default TasksList;
