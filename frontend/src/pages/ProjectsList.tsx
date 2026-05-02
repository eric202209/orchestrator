import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { projectsAPI } from '../api/client';
import type { Project } from '../types/api';
import {
  GitBranch,
  Plus,
  FileText,
  XCircle,
  ExternalLink,
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { EmptyState, Skeleton } from '../components/ui';

function ProjectsList() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreateProject, setShowCreateProject] = useState(false);
  const [editingProjectId, setEditingProjectId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState('');
  const [newProjectName, setNewProjectName] = useState('');
  const [newProjectDescription, setNewProjectDescription] = useState('');
  const [newProjectRules, setNewProjectRules] = useState('');
  const [creatingProject, setCreatingProject] = useState(false);
  const [updatingProject, setUpdatingProject] = useState(false);

  useEffect(() => {
    fetchProjects();
  }, []);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedName = newProjectName.trim();
    if (!trimmedName) return;

    setCreatingProject(true);
    const tempId = -Date.now();
    const now = new Date().toISOString();
    const optimisticProject: Project = {
      id: tempId,
      name: trimmedName,
      description: newProjectDescription.trim() || null,
      project_rules: newProjectRules.trim() || null,
      github_url: null,
      branch: 'main',
      created_at: now,
      updated_at: now,
    };

    setProjects((current) => [optimisticProject, ...current]);
    setNewProjectName('');
    setNewProjectDescription('');
    setNewProjectRules('');
    setShowCreateProject(false);

    try {
      const response = await projectsAPI.create({ 
        name: trimmedName,
        description: newProjectDescription.trim() || undefined,
        project_rules: newProjectRules.trim() || undefined,
      });
      setProjects((current) =>
        current.map((project) => (project.id === tempId ? response.data : project))
      );
    } catch (error) {
      setProjects((current) => current.filter((project) => project.id !== tempId));
      setNewProjectName(trimmedName);
      setNewProjectDescription(optimisticProject.description || '');
      setNewProjectRules(optimisticProject.project_rules || '');
      setShowCreateProject(true);
      console.error('Failed to create project:', error);
      alert('Failed to create project. Please try again.');
    } finally {
      setCreatingProject(false);
    }
  };

  const fetchProjects = async () => {
    try {
      const response = await projectsAPI.getAll();
      setProjects(response.data);
    } catch (error) {
      console.error('Failed to fetch projects:', error);
    } finally {
      setLoading(false);
    }
  };

  

  const handleDeleteProject = async (projectId: number): Promise<void> => {
    if (!window.confirm('Are you sure you want to delete this project? This cannot be undone.')) {
      return;
    }

    const previousProjects = projects;
    setProjects((current) => current.filter((project) => project.id !== projectId));

    try {
      await projectsAPI.delete(projectId);
      alert('Project deleted successfully!');
    } catch (error: unknown) {
      setProjects(previousProjects);
      const err = error as { response?: { data?: { detail?: unknown } }; message?: string };
      const message =
        typeof err.response?.data?.detail === 'string'
          ? err.response.data.detail
          : err.message || 'Unknown error';
      console.error('❌ Failed to delete project:', message);
      alert(`Failed to delete project: ${message}`);
    }
  };

  const startEditProject = (project: Project) => {
    setEditingProjectId(project.id);
    setEditingName(project.name);
  };

  const handleUpdateProject = async (projectId: number) => {
    const trimmedName = editingName.trim();
    if (!trimmedName) return;

    setUpdatingProject(true);
    const previousProjects = projects;
    setProjects((current) =>
      current.map((project) =>
        project.id === projectId ? { ...project, name: trimmedName } : project
      )
    );
    setEditingProjectId(null);

    try {
      const response = await projectsAPI.update(projectId, { name: trimmedName });
      setProjects((current) =>
        current.map((project) => (project.id === projectId ? response.data : project))
      );
    } catch (error: unknown) {
      setProjects(previousProjects);
      setEditingProjectId(projectId);
      const err = error as { response?: { data?: { detail?: unknown } } };
      console.error('Failed to update project:', error);
      const message =
        typeof err.response?.data?.detail === 'string'
          ? err.response.data.detail
          : error instanceof Error
            ? error.message
            : 'Unknown error';
      alert(`Failed to update project: ${message}`);
    } finally {
      setUpdatingProject(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-5">
        <div className="flex items-center justify-between">
          <Skeleton className="h-6 w-24" />
          <Skeleton className="h-9 w-28" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          <Skeleton className="h-36 w-full" />
          <Skeleton className="h-36 w-full" />
          <Skeleton className="h-36 w-full" />
        </div>
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-5">
        <h1 className="text-lg font-semibold text-white">Projects</h1>
        <button
          onClick={() => setShowCreateProject(true)}
          className="flex items-center gap-1.5 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-1.5 rounded-md transition-colors"
        >
          <Plus className="h-4 w-4" />
          New Project
        </button>
      </div>

        {/* Projects Grid */}
        {projects.length === 0 ? (
          <EmptyState
            icon={GitBranch}
            title="No projects yet"
            description="Create your first project to start orchestrating AI development tasks"
            action={{
              label: 'Create Project',
              onClick: () => setShowCreateProject(true)
            }}
          />
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {projects.map((project) => (
              <div
                key={project.id}
                onClick={() => navigate(`/projects/${project.id}`)}
                className="bg-slate-800 rounded-lg border border-slate-700 p-4 hover:border-slate-600 transition-colors group cursor-pointer"
              >
                <div className="flex items-start justify-between mb-3">
                  <GitBranch className="h-4 w-4 text-slate-500 mt-0.5" />
                  <div className="flex gap-1.5">
                    {project.github_url && (
                      <a
                        href={project.github_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-slate-500 hover:text-slate-300 transition-colors"
                        title="View GitHub"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    )}
                    {editingProjectId === project.id ? (
                      <button
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          handleUpdateProject(project.id);
                        }}
                        disabled={updatingProject}
                        className="text-emerald-500 hover:text-emerald-400 transition-colors disabled:opacity-50"
                        title="Save changes"
                      >
                        {updatingProject ? (
                          <div className="h-3.5 w-3.5 border-2 border-white/30 border-t-emerald-500 rounded-full animate-spin" />
                        ) : (
                          <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                          </svg>
                        )}
                      </button>
                    ) : (
                      <button
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          startEditProject(project);
                        }}
                        className="text-slate-500 hover:text-slate-300 transition-colors"
                        title="Rename project"
                      >
                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                        </svg>
                      </button>
                    )}
                    <button
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        handleDeleteProject(project.id);
                      }}
                      className="text-slate-500 hover:text-red-400 transition-colors"
                      title="Delete project"
                    >
                      <XCircle className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
                {editingProjectId === project.id ? (
                  <div className="relative">
                    <input
                      type="text"
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      className="w-full bg-slate-950 border border-sky-500 rounded-md px-2.5 py-1 text-sm text-white mb-2 focus:outline-none focus:ring-1 focus:ring-sky-500"
                      autoFocus
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault();
                          handleUpdateProject(project.id);
                        } else if (e.key === 'Escape') {
                          setEditingProjectId(null);
                        }
                      }}
                    />
                  </div>
                ) : (
                  <h3 className="text-sm font-semibold text-white mb-1 group-hover:text-slate-200 transition-colors">
                    {project.name}
                  </h3>
                )}
                {project.description && (
                  <p className="text-xs text-slate-400 mb-3 line-clamp-2">{project.description}</p>
                )}
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span className="flex items-center gap-1">
                    <FileText className="h-3 w-3" />
                    {project.branch}
                  </span>
                  <span>{formatDistanceToNow(new Date(project.created_at), { addSuffix: true })}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      {/* Create Project Modal */}
      {showCreateProject && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-900 rounded-lg border border-slate-700 p-5 w-full max-w-md mx-4 shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">New Project</h3>
            <form onSubmit={handleCreateProject}>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Project Name
                  </label>
                  <input
                    type="text"
                    value={newProjectName}
                    onChange={(e) => setNewProjectName(e.target.value)}
                    className="w-full bg-slate-950 border border-slate-700 rounded-md px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500"
                    placeholder="My Project"
                    autoFocus
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Project Brief
                  </label>
                  <textarea
                    value={newProjectDescription}
                    onChange={(e) => setNewProjectDescription(e.target.value)}
                    className="min-h-[80px] w-full resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500"
                    placeholder="What this project is for, scope, expected deliverable..."
                  />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-400 mb-1.5">
                    Project Rules
                  </label>
                  <textarea
                    value={newProjectRules}
                    onChange={(e) => setNewProjectRules(e.target.value)}
                    className="min-h-[96px] w-full resize-y rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500"
                    placeholder="Constraints, style rules, forbidden tools, must-keep architecture..."
                  />
                </div>
                <div className="flex gap-2 pt-1">
                  <button
                    type="button"
                    onClick={() => {
                      setShowCreateProject(false);
                      setNewProjectName('');
                      setNewProjectDescription('');
                      setNewProjectRules('');
                    }}
                    className="flex-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm px-3 py-2 rounded-md transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newProjectName.trim() || creatingProject}
                    className="flex-1 bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-2 rounded-md transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2"
                  >
                    {creatingProject ? (
                      <>
                        <div className="h-4 w-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Creating...
                      </>
                    ) : (
                      'Create'
                    )}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

export default ProjectsList;
