import { useState, useEffect } from 'react';
import { useSearchParams, useNavigate, Link } from 'react-router-dom';
import { sessionsAPI, projectsAPI } from '../api/client';
import { 
  ArrowLeft, 
  Plus, 
  Terminal,
  X
} from 'lucide-react';

function NewSession() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [projects, setProjects] = useState<Array<{ id: number; name: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [sessionName, setSessionName] = useState('');
  const [sessionDescription, setSessionDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    fetchProjects();
  }, []);

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

            {/* Submit Button */}
            <button
              type="submit"
              disabled={submitting || !sessionName.trim()}
              className="w-full bg-primary-500 hover:bg-primary-600 text-white px-6 py-3 rounded-lg transition-all font-medium disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
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
