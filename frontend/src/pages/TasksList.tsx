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
import { cn } from '@/lib/utils';

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

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'done':
        return <CheckCircle2 className="h-5 w-5 text-emerald-400" />;
      case 'running':
        return <PlayCircle className="h-5 w-5 text-blue-400 animate-pulse" />;
      case 'failed':
        return <XCircle className="h-5 w-5 text-red-400" />;
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

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-100 flex items-center gap-3">
            <ListTodo className="h-7 w-7 text-primary-500" />
            Tasks
          </h1>
          <p className="text-slate-400 mt-1">
            {tasks.length} task{tasks.length !== 1 ? 's' : ''} across {Object.keys(projects).length} project{Object.keys(projects).length !== 1 ? 's' : ''}
          </p>
        </div>

        {/* Filters */}
        <div className="flex items-center gap-3">
          {/* Status Filter */}
          <div className="relative">
            <Filter className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as TaskStatus | 'all')}
              className="pl-10 pr-8 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="all">All Status</option>
              <option value="pending">Pending</option>
              <option value="running">Running</option>
              <option value="done">Done</option>
              <option value="failed">Failed</option>
            </select>
          </div>

          {/* Search */}
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
            <input
              type="text"
              placeholder="Search tasks..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 pr-4 py-2 bg-slate-800 border border-slate-700 rounded-lg text-sm text-white placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-primary-500 w-64"
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
        <div className="grid grid-cols-1 gap-4">
          {filteredTasks.map((task) => {
            const project = projects[task.project_id || 0];
            return (
              <Link
                key={task.id}
                to={`/projects/${task.project_id}/tasks/${task.id}`}
                className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-primary-500/50 transition-all group"
              >
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-start gap-4 flex-1">
                    <div className={cn('p-2 rounded-lg', getStatusColor(task.status))}>
                      {getStatusIcon(task.status)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <h3 className="font-semibold text-white group-hover:text-primary-400 transition-colors line-clamp-1">
                        {task.title}
                      </h3>
                      {task.description && (
                        <p className="text-sm text-slate-400 mt-1 line-clamp-2">
                          {task.description}
                        </p>
                      )}
                    </div>
                  </div>
                  <StatusBadge status={task.status} />
                </div>

                <div className="flex items-center justify-between text-xs text-slate-500 pt-4 border-t border-slate-700">
                  <div className="flex items-center gap-4">
                    {project && (
                      <div className="flex items-center gap-1.5">
                        <GitBranch className="h-3.5 w-3.5" />
                        <span className="text-primary-400">{project.name}</span>
                      </div>
                    )}
                    {task.created_at && (
                      <div className="flex items-center gap-1.5">
                        <Clock className="h-3.5 w-3.5" />
                        <span>
                          {new Date(task.created_at).toLocaleDateString()}
                        </span>
                      </div>
                    )}
                  </div>
                  
                  {task.error_message && (
                    <div className="flex items-center gap-1.5 text-red-400">
                      <XCircle className="h-3.5 w-3.5" />
                      <span className="text-xs">Error</span>
                    </div>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}

      {/* Stats Footer */}
      {tasks.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 pt-6 border-t border-slate-800">
          <div className="bg-slate-800/30 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <Clock className="h-4 w-4 text-slate-400" />
              <span className="text-sm text-slate-400">Pending</span>
            </div>
            <p className="text-2xl font-bold text-white">
              {tasks.filter(t => t.status === 'pending').length}
            </p>
          </div>
          <div className="bg-slate-800/30 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <PlayCircle className="h-4 w-4 text-blue-400" />
              <span className="text-sm text-blue-400">Running</span>
            </div>
            <p className="text-2xl font-bold text-white">
              {tasks.filter(t => t.status === 'running').length}
            </p>
          </div>
          <div className="bg-slate-800/30 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
              <span className="text-sm text-emerald-400">Done</span>
            </div>
            <p className="text-2xl font-bold text-white">
              {tasks.filter(t => t.status === 'done').length}
            </p>
          </div>
          <div className="bg-slate-800/30 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-2">
              <XCircle className="h-4 w-4 text-red-400" />
              <span className="text-sm text-red-400">Failed</span>
            </div>
            <p className="text-2xl font-bold text-white">
              {tasks.filter(t => t.status === 'failed').length}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

export default TasksList;
