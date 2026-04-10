import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { sessionsAPI, projectsAPI } from '../api/client';
import type { Session, Project } from '../types/api';
import { 
  Terminal, 
  Plus, 
  Clock, 
  Activity,
  ArrowLeft
} from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { EmptyState, StatusBadge, Skeleton } from '../components/ui';

function SessionsList() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Record<number, Project>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchSessions = async () => {
      try {
        // Fetch all projects first
        const projectsResponse = await projectsAPI.getAll();
        const allProjects = projectsResponse.data || [];
        
        // Build a map of project IDs
        const projectMap: Record<number, Project> = {};
        allProjects.forEach(project => {
          projectMap[project.id] = project;
        });
        setProjects(projectMap);
        
        // Fetch sessions for each project and combine them
        const sessionPromises = allProjects.map(async (project) => {
          try {
            const sessionsResponse = await sessionsAPI.getByProject(project.id);
            return sessionsResponse.data || [];
          } catch (error) {
            console.error(`Failed to fetch sessions for project ${project.id}:`, error);
            return [];
          }
        });
        
        const allSessionsArrays = await Promise.all(sessionPromises);
        const allSessions = allSessionsArrays.flat();
        setSessions(allSessions);
      } catch (error) {
        console.error('Failed to fetch sessions:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchSessions();
  }, []);

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'running': return 'text-green-400 bg-green-400/10';
      case 'paused': return 'text-yellow-400 bg-yellow-400/10';
      case 'stopped': return 'text-slate-400 bg-slate-400/10';
      default: return 'text-blue-400 bg-blue-400/10';
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <Link to="/" className="text-slate-400 hover:text-white">
              <ArrowLeft className="h-4 w-4" />
            </Link>
            <h1 className="text-2xl font-bold text-white">Sessions</h1>
          </div>
          <p className="text-slate-400">
            View and manage all AI development sessions
          </p>
        </div>
        <Link
          to="/sessions/new"
          className="bg-primary-500 hover:bg-primary-600 text-white px-4 py-2 rounded-lg transition-all flex items-center gap-2"
        >
          <Plus className="h-5 w-5" />
          New Session
        </Link>
      </div>

      {/* Sessions List */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div key={i} className="bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6">
              <Skeleton className="h-6 w-3/4 mb-3" />
              <Skeleton className="h-4 w-1/2 mb-4" />
              <Skeleton className="h-10 w-full" />
            </div>
          ))}
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          icon={Terminal}
          title="No sessions yet"
          description="Create your first AI session to start orchestrating development tasks"
          action={{
            label: 'Create Session',
            onClick: () => {
              window.location.href = '/sessions/new';
            }
          }}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {sessions.map((session) => {
            const project = projects[session.project_id || 0];
            return (
              <Link
                key={session.id}
                to={`/sessions/${session.id}`}
                className={`bg-slate-800/50 backdrop-blur rounded-xl border border-slate-700 p-6 hover:border-primary-500/50 transition-all group ${
                  session.status === 'running' ? 'active-session-pulse' : ''
                }`}
              >
                <div className="flex items-start justify-between mb-3">
                  <div className={`p-2 rounded-lg ${getStatusColor(session.status)}`}>
                    {session.status === 'running' ? (
                      <Activity className="h-5 w-5 animate-pulse" />
                    ) : session.status === 'paused' ? (
                      <Clock className="h-5 w-5" />
                    ) : (
                      <Terminal className="h-5 w-5" />
                    )}
                  </div>
                  <StatusBadge status={session.status} size="sm" />
                </div>
                
                <h3 className="font-semibold text-white mb-1 group-hover:text-primary-400 transition-colors">
                  {session.name}
                </h3>
                
                {session.description && (
                  <p className="text-sm text-slate-400 mb-3 line-clamp-2">
                    {session.description}
                  </p>
                )}
                
                {project && (
                  <div className="flex items-center gap-2 text-xs text-slate-500 mb-3">
                    <span className="text-primary-400">•</span>
                    <span>{project.name}</span>
                  </div>
                )}
                
                <div className="flex items-center justify-between text-xs text-slate-500 pt-3 border-t border-slate-700">
                  <span>{formatDistanceToNow(new Date(session.created_at), { addSuffix: true })}</span>
                  {session.started_at && (
                    <span className="flex items-center gap-1">
                      <Clock className="h-3 w-3" />
                      Started
                    </span>
                  )}
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default SessionsList;
