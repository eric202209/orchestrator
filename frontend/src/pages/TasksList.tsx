import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { tasksAPI, projectsAPI } from '@/api/client';
import type { Task, Project, Page } from '@/types/api';
import {
  Search,
  ListTodo,
  GitBranch,
  AlertTriangle
} from 'lucide-react';
import { StatusBadge, LoadingSpinner, EmptyState } from '@/components/ui';
import { PaginationControls } from '@/components/PaginationControls';

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

function filterToParams(filter: TaskStatusFilter): Record<string, unknown> {
  switch (filter) {
    case 'review': return { needs_review: true };
    case 'pending': return { status: 'pending' };
    case 'running': return { status: 'running' };
    case 'done': return { status: 'done' };
    case 'failed': return { status: 'failed' };
    case 'cancelled': return { status: 'cancelled' };
    default: return {};
  }
}

const taskNeedsReview = (task: Task): boolean =>
  task.workspace_status === 'ready' || task.workspace_status === 'changes_requested';

const PER_PAGE = 25;

function TasksList() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [projects, setProjects] = useState<Record<number, Project>>({});
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<TaskStatusFilter>('all');
  const [page, setPage] = useState(1);
  const [pageData, setPageData] = useState<Omit<Page<Task>, 'items'>>({
    page: 1,
    per_page: PER_PAGE,
    total: 0,
    total_pages: 1,
    has_next: false,
    has_previous: false,
  });

  const fetchTasks = useCallback(async (currentPage: number, currentFilter: TaskStatusFilter, currentSearch: string) => {
    try {
      const filterParams = filterToParams(currentFilter);
      const res = await tasksAPI.getAll({
        page: currentPage,
        per_page: PER_PAGE,
        order_by: 'created_at',
        order_dir: 'desc',
        ...(currentSearch.trim() ? { search: currentSearch.trim() } : {}),
        ...filterParams,
      } as Record<string, unknown>);
      const data = res.data as Page<Task>;
      setTasks(data.items ?? []);
      setPageData({
        page: data.page,
        per_page: data.per_page,
        total: data.total,
        total_pages: data.total_pages,
        has_next: data.has_next,
        has_previous: data.has_previous,
      });
    } catch (error) {
      console.error('Failed to fetch tasks:', error);
    }
  }, []);

  // Projects fetch (small, used for project name display)
  useEffect(() => {
    projectsAPI.getAll({ page: 1, per_page: 200, order_by: 'name', order_dir: 'asc' }).then((res) => {
      const data = res.data;
      const list = Array.isArray(data) ? data : (data as Page<Project>).items ?? [];
      const map: Record<number, Project> = {};
      list.forEach((p) => { map[p.id] = p; });
      setProjects(map);
    }).catch(() => {});
  }, []);

  // Reset to page 1 when filter or search changes
  useEffect(() => {
    setPage(1);
  }, [statusFilter, searchQuery]);

  // Fetch tasks on page / filter / search change
  useEffect(() => {
    const load = async () => {
      setLoading(true);
      await fetchTasks(page, statusFilter, searchQuery);
      setLoading(false);
    };
    load();
  }, [page, statusFilter, searchQuery, fetchTasks]);

  const handleFilterChange = (f: TaskStatusFilter) => setStatusFilter(f);
  const handleSearchChange = (q: string) => setSearchQuery(q);

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
            {pageData.total} task{pageData.total !== 1 ? 's' : ''}
          </p>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
            <input
              type="text"
              placeholder="Search..."
              value={searchQuery}
              onChange={(e) => handleSearchChange(e.target.value)}
              className="w-44 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] py-1.5 pl-8 pr-3 text-xs text-white placeholder-slate-400 hover:border-[color:var(--oc-border)] focus:border-primary-500 focus:outline-none"
            />
          </div>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {statusFilters.map((filter) => {
          const selected = statusFilter === filter.key;
          return (
            <button
              key={filter.key}
              type="button"
              onClick={() => handleFilterChange(filter.key)}
              className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                selected
                  ? 'border-primary-500 bg-primary-500/10 text-white'
                  : 'border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
              }`}
            >
              {filter.label}
              {selected && pageData.total > 0 && (
                <span className="ml-1 text-primary-200/80">{pageData.total}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Tasks list */}
      {tasks.length === 0 ? (
        statusFilter === 'review' && !searchQuery ? (
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-8 text-center">
            <ListTodo className="mx-auto mb-3 h-8 w-8 text-emerald-500/50" />
            <h2 className="text-sm font-medium text-white">No tasks need review</h2>
            <p className="mt-1 text-sm text-slate-400">
              Task outputs appear here when ready for operator approval.{' '}
              <button
                type="button"
                onClick={() => handleFilterChange('all')}
                className="text-primary-400 hover:text-primary-300 transition-colors"
              >
                View all tasks
              </button>
            </p>
          </div>
        ) : (
          <EmptyState
            icon={ListTodo}
            title={
              searchQuery || statusFilter !== 'all'
                ? 'No matching tasks'
                : 'No tasks yet'
            }
            description={
              searchQuery || statusFilter !== 'all'
                ? 'Try adjusting your filters or search query'
                : 'Tasks will appear here when you start working on projects'
            }
          />
        )
      ) : (
        <>
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] divide-y divide-[color:var(--oc-border-soft)]">
            {tasks.map((task) => {
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
          {pageData.total_pages > 1 && (
            <PaginationControls
              page={pageData.page}
              total_pages={pageData.total_pages}
              has_next={pageData.has_next}
              has_previous={pageData.has_previous}
              total={pageData.total}
              per_page={pageData.per_page}
              onPrev={() => setPage((p) => p - 1)}
              onNext={() => setPage((p) => p + 1)}
            />
          )}
        </>
      )}
    </div>
  );
}

export default TasksList;
