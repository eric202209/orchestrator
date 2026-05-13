import { useState, useEffect, useCallback } from 'react';
import { useSearchParams, useNavigate, Link } from 'react-router-dom';
import { sessionsAPI, projectsAPI } from '../api/client';
import { 
  ArrowLeft, 
  Plus, 
  Terminal,
  X,
  Settings
} from 'lucide-react';
import type { ExecutionProfile } from '../types/api';

const executionProfiles: Array<{ value: ExecutionProfile; label: string; description: string }> = [
  { value: 'full_lifecycle', label: 'Full Lifecycle', description: 'Plan, implement, verify, and summarize by default.' },
  { value: 'execute_only', label: 'Execute Only', description: 'Bias toward implementation work with minimal planning overhead.' },
  { value: 'test_only', label: 'Test Only', description: 'Focus on verification, tests, and failure reporting.' },
  { value: 'debug_only', label: 'Debug Only', description: 'Reproduce, diagnose, fix, and verify issues.' },
  { value: 'review_only', label: 'Review Only', description: 'Inspect and report findings without implementation-first behavior.' },
];

function NewSession() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [projects, setProjects] = useState<Array<{ id: number; name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [sessionName, setSessionName] = useState('');
  const [sessionDescription, setSessionDescription] = useState('');
  const [executionMode, setExecutionMode] = useState<'automatic' | 'manual'>('automatic');
  const [defaultExecutionProfile, setDefaultExecutionProfile] = useState<ExecutionProfile>('full_lifecycle');
  const [submitting, setSubmitting] = useState(false);

  const fetchProjects = useCallback(async () => {
    try {
      setLoading(true); // Show loading while fetching
      const response = await projectsAPI.getAll();
      setProjects(response.data);
      
      // Check if we have a project_id in the URL
      const projectId = searchParams.get('project_id');
      if (projectId && response.data) {
        const project = response.data.find((p: { id: number }) => p.id === Number(projectId));
        if (!project) {
          console.error(`Project with ID ${projectId} not found`);
          alert(`Project with ID ${projectId} not found. Please select a valid project.`);
        }
      }
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: unknown } }; message?: string };
      console.error('Failed to fetch projects:', error);
      
      // Show user-friendly error
      const errorMsg = err.response?.data?.detail || 
                      err.message || 
                      'Failed to load projects. Please check your connection.';
      alert(errorMsg);
    } finally {
      setLoading(false);
    }
  }, [searchParams]);

  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!sessionName.trim()) {
      alert('Please enter a session name');
      return;
    }

    const projectId = searchParams.get('project_id');
    if (!projectId) {
      alert('Please select a project');
      return;
    }

    try {
      setSubmitting(true);
      const response = await sessionsAPI.create({
        project_id: Number(projectId),
        name: sessionName,
        description: sessionDescription || undefined,
        execution_mode: executionMode,
        default_execution_profile: defaultExecutionProfile,
      });
      
      // Redirect to the new session
      navigate(`/sessions/${response.data.id}`);
    } catch (error: unknown) {
      const err = error as { response?: { data?: { detail?: unknown } } };
      console.error('Failed to create session:', error);
      alert(err.response?.data?.detail || 'Failed to create session. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="h-8 w-8 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin" />
      </div>
    );
  }

  // Show error if no projects loaded
  if (projects.length === 0 && !loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-900">
        <div className="text-center">
          <div className="h-16 w-16 bg-red-500/10 rounded-full flex items-center justify-center mx-auto mb-4">
            <X className="h-8 w-8 text-red-500" />
          </div>
          <h2 className="text-xl font-semibold text-white mb-2">No Projects Found</h2>
          <p className="text-slate-400 mb-6">Please create a project first or check your connection.</p>
          <Link
            to="/projects"
            className="bg-primary-500 hover:border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white px-6 py-2 rounded-lg transition-colors"
          >
            Go to Projects
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-900">
      {/* Navbar */}
      <nav className="bg-slate-800/50 backdrop-blur border-b border-slate-700">
        <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-4">
              <Link
                to="/projects"
                className="flex items-center gap-2 text-slate-400 hover:text-white transition-colors"
              >
                <ArrowLeft className="h-4 w-4" />
                <span>Back to Projects</span>
              </Link>
              <div className="flex items-center gap-2">
                <Terminal className="h-6 w-6 text-primary-500" />
                <h1 className="text-xl font-bold text-white">New Session</h1>
              </div>
            </div>
            <button
              onClick={() => navigate(-1)}
              className="p-2 text-slate-400 hover:text-white transition-colors"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <form onSubmit={handleSubmit} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-8">
          <div className="space-y-6">
            {/* Project Selection */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Project <span className="text-red-400">*</span>
              </label>
              <select
                value={searchParams.get('project_id') || ''}
                onChange={(e) => navigate(`/sessions/new?project_id=${e.target.value}`)}
                className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
                required
              >
                <option value="">Select a project...</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            </div>

            {/* Session Name */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Session Name <span className="text-red-400">*</span>
              </label>
              <input
                type="text"
                value={sessionName}
                onChange={(e) => setSessionName(e.target.value)}
                placeholder="e.g., Vite Website Development"
                className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500"
                required
              />
            </div>

            {/* Session Description */}
            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Description (Optional)
              </label>
              <textarea
                value={sessionDescription}
                onChange={(e) => setSessionDescription(e.target.value)}
                placeholder="Describe what you want the AI session to accomplish..."
                rows={4}
                className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Execution Mode
              </label>
              <div className="grid grid-cols-2 gap-3">
                <button
                  type="button"
                  onClick={() => setExecutionMode('automatic')}
                  className={`rounded-lg border px-4 py-3 text-left transition-colors ${
                    executionMode === 'automatic'
                      ? 'border-primary-500 bg-primary-500/10 text-white'
                      : 'border-slate-700 bg-slate-900/50 text-slate-300 hover:border-slate-600'
                  }`}
                >
                  <div className="font-medium">Automatic</div>
                  <div className="mt-1 text-xs text-slate-400">Follow task order and continue to the next pending task automatically.</div>
                </button>
                <button
                  type="button"
                  onClick={() => setExecutionMode('manual')}
                  className={`rounded-lg border px-4 py-3 text-left transition-colors ${
                    executionMode === 'manual'
                      ? 'border-primary-500 bg-primary-500/10 text-white'
                      : 'border-slate-700 bg-slate-900/50 text-slate-300 hover:border-slate-600'
                  }`}
                >
                  <div className="font-medium">Manual</div>
                  <div className="mt-1 text-xs text-slate-400">Start the session, then choose each task yourself from the session task list.</div>
                </button>
              </div>
            </div>

            <div>
              <label className="block text-sm font-medium text-slate-300 mb-2">
                Default Task Profile
              </label>
              <select
                value={defaultExecutionProfile}
                onChange={(e) => setDefaultExecutionProfile(e.target.value as ExecutionProfile)}
                className="w-full bg-slate-900/50 border border-slate-700 rounded-lg px-4 py-3 text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
              >
                {executionProfiles.map((profile) => (
                  <option key={profile.value} value={profile.value}>
                    {profile.label} - {profile.description}
                  </option>
                ))}
              </select>
            </div>

            {/* Submit Button */}
            <button
              type="submit"
              disabled={submitting || !sessionName.trim()}
              className="w-full bg-primary-500 hover:border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white px-6 py-3 rounded-lg transition-all font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
            >
              {submitting ? (
                <>
                  <div className="h-5 w-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Creating Session...
                </>
              ) : (
                <>
                  <Plus className="h-5 w-5" />
                  Create Session
                </>
              )}
            </button>
          </div>
        </form>

        {/* Help Text */}
        <div className="mt-6 bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
          <h3 className="text-lg font-semibold text-white mb-2">What's Next?</h3>
          <p className="text-slate-400 mb-4">
            Once your session is created, you'll be able to:
          </p>
          <ul className="space-y-2 text-slate-300">
            <li className="flex items-center gap-2">
              <Terminal className="h-4 w-4 text-primary-500" />
              Execute development tasks
            </li>
            <li className="flex items-center gap-2">
              <Settings className="h-4 w-4 text-primary-500" />
              Monitor real-time logs
            </li>
            <li className="flex items-center gap-2">
              <Plus className="h-4 w-4 text-primary-500" />
              Control session lifecycle (start, pause, resume, stop)
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}

export default NewSession;
