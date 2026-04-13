import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { projectsAPI } from '../api/client';
import type { Project } from '../types/api';
import { 
  GitBranch, 
  Plus,
  FileText,
  XCircle,
  ExternalLink,
  ArrowLeft
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
  const [creatingProject, setCreatingProject] = useState(false);
  const [updatingProject, setUpdatingProject] = useState(false);

  useEffect(() => {
    fetchProjects();
  }, []);

  const handleCreateProject = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newProjectName.trim()) return;

    setCreatingProject(true);
    try {
      await projectsAPI.create({ 
        name: newProjectName,
        branch: 'main'
      });
      setNewProjectName('');
      setShowCreateProject(false);
      fetchProjects();
    } catch (error) {
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

    try {
      const token = localStorage.getItem('access_token');
      const apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8080/api/v1';
      console.log('🗑️ Deleting project', projectId);
      console.log('🔍 VITE_API_URL:', apiUrl);
      
      // Delete the project (backend should handle cascading deletes)
      const deleteResponse = await fetch(`${apiUrl}/projects/${projectId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });
      
      console.log('Delete response status:', deleteResponse.status);
      const responseText = await deleteResponse.text();
      console.log('Delete response text:', responseText);

      if (!deleteResponse.ok) {
        let errorMessage = 'Failed to delete project';
        try {
          const errorData = JSON.parse(responseText);
          errorMessage = errorData.detail || errorMessage;
        } catch {
          if (responseText) errorMessage = responseText;
        }
        throw new Error(errorMessage);
      }

      fetchProjects();
      alert('Project deleted successfully!');
    } catch (error: unknown) {
      const err = error as { message?: string };
      console.error('❌ Failed to delete project:', err.message || error);
      alert(`Failed to delete project: ${err.message || 'Unknown error'}`);
    }
  };

  const startEditProject = (project: Project) => {
    setEditingProjectId(project.id);
    setEditingName(project.name);
  };

  const handleUpdateProject = async (projectId: number) => {
    if (!editingName.trim()) return;

    setUpdatingProject(true);
    try {
      await projectsAPI.update(projectId, { name: editingName });
      setEditingProjectId(null);
      fetchProjects();
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: unknown } } };
      console.error('Failed to update project:', error);
      alert(`Failed to update project: ${err.response?.data?.detail || error.message}`);
    } finally {
      setUpdatingProject(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-slate-900">
        <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex items-center justify-between h-16">
              <div className="flex items-center gap-2">
                <Skeleton className="h-6 w-6" />
                <Skeleton className="h-6 w-32" />
              </div>
              <div className="flex items-center gap-4">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-4 w-24" />
              </div>
            </div>
          </div>
        </nav>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
          <div className="flex items-center justify-between mb-8">
            <div className="flex items-center gap-4">
              <Skeleton className="h-5 w-5" />
              <Skeleton className="h-8 w-32" />
            </div>
            <div className="flex items-center gap-3">
              <Skeleton className="h-10 w-28" />
            </div>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            <Skeleton className="h-48 w-full" />
            <Skeleton className="h-48 w-full" />
            <Skeleton className="h-48 w-full" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-900">
      {/* Navbar */}
      <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-2">
              <GitBranch className="h-6 w-6 text-primary-500" />
              <span className="text-xl font-bold text-white">Orchestrator</span>
            </div>
            
            <div className="flex items-center gap-4">
              <Link
                to="/"
                className="text-sm text-slate-400 hover:text-white transition-colors"
              >
                Back to Dashboard
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-4">
            <Link
              to="/"
              className="text-slate-400 hover:text-white transition-colors"
              title="Back to Dashboard"
            >
              <ArrowLeft className="h-5 w-5" />
            </Link>
            <h1 className="text-3xl font-bold text-white">Projects</h1>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setShowCreateProject(true)}
              className="flex items-center gap-2 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all"
            >
              <Plus className="h-4 w-4" />
              New Project
            </button>
          </div>
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
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {projects.map((project) => (
              <div
                key={project.id}
                onClick={() => navigate(`/projects/${project.id}`)}
                className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-primary-500/50 transition-all group cursor-pointer"
              >
                <div className="flex items-start justify-between mb-4">
                  <GitBranch className="h-6 w-6 text-primary-500" />
                  <div className="flex gap-2">
                    {project.github_url && (
                      <a
                        href={project.github_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-slate-400 hover:text-primary-400 transition-colors"
                        title="View GitHub"
                      >
                        <ExternalLink className="h-4 w-4" />
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
                        className="text-green-500 hover:text-green-600 transition-colors disabled:opacity-50"
                        title="Save changes"
                      >
                        {updatingProject ? (
                          <div className="h-4 w-4 border-2 border-white/30 border-t-green-500 rounded-full animate-spin" />
                        ) : (
                          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
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
                        className="text-slate-400 hover:text-blue-400 transition-colors"
                        title="Rename project"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
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
                      className="text-slate-400 hover:text-red-400 transition-colors"
                      title="Delete project"
                    >
                      <XCircle className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {editingProjectId === project.id ? (
                  <div className="relative">
                    <input
                      type="text"
                      value={editingName}
                      onChange={(e) => setEditingName(e.target.value)}
                      className="w-full bg-slate-900/50 border border-primary-500 rounded-lg px-3 py-1 text-white mb-2 focus:outline-none focus:ring-2 focus:ring-primary-500"
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
                  <h3 className="text-lg font-semibold text-white mb-2 group-hover:text-primary-400 transition-colors">
                    {project.name}
                  </h3>
                )}
                {project.description && (
                  <p className="text-sm text-slate-400 mb-4 line-clamp-2">{project.description}</p>
                )}
                <div className="flex items-center justify-between text-sm text-slate-400">
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
      </div>

      {/* Create Project Modal */}
      {showCreateProject && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50">
          <div className="bg-slate-800 rounded-xl border border-slate-700 p-6 w-full max-w-md mx-4">
            <h3 className="text-lg font-semibold text-white mb-4">Create New Project</h3>
            <form onSubmit={handleCreateProject}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-2">
                    Project Name
                  </label>
                  <input
                    type="text"
                    value={newProjectName}
                    onChange={(e) => setNewProjectName(e.target.value)}
                    className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-2 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
                    placeholder="My Awesome Project"
                    autoFocus
                  />
                </div>
                <div className="flex gap-3">
                  <button
                    type="button"
                    onClick={() => setShowCreateProject(false)}
                    className="flex-1 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg transition-all"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={!newProjectName.trim() || creatingProject}
                    className="flex-1 bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
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
