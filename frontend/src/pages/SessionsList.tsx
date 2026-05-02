import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { sessionsAPI, projectsAPI } from '../api/client';
import type { Session, Project } from '../types/api';
import {
  Terminal,
  Plus,
  Clock,
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
        const projectsResponse = await projectsAPI.getAll();
        const allProjects = projectsResponse.data || [];

        const projectMap: Record<number, Project> = {};
        allProjects.forEach(project => {
          projectMap[project.id] = project;
        });
        setProjects(projectMap);

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
        setSessions(allSessionsArrays.flat());
      } catch (error) {
        console.error('Failed to fetch sessions:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchSessions();
  }, []);

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-white">Sessions</h1>
        <Link
          to="/sessions/new"
          className="bg-sky-600 hover:bg-sky-500 text-white text-sm px-3 py-1.5 rounded-md transition-colors flex items-center gap-1.5"
        >
          <Plus className="h-4 w-4" />
          New Session
        </Link>
      </div>

      {/* Sessions List */}
      {loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="bg-slate-800 rounded-lg border border-slate-700 p-4">
              <Skeleton className="h-5 w-3/4 mb-2" />
              <Skeleton className="h-4 w-1/2 mb-3" />
              <Skeleton className="h-8 w-full" />
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
            onClick: () => { window.location.href = '/sessions/new'; }
          }}
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {sessions.map((session) => {
            const project = projects[session.project_id || 0];
            return (
              <Link
                key={session.id}
                to={`/sessions/${session.id}`}
                className="bg-slate-800 rounded-lg border border-slate-700 p-4 hover:border-slate-600 transition-colors group"
              >
                <div className="flex items-start justify-between mb-3">
                  <StatusBadge status={session.status} size="sm" />
                  {project && (
                    <span className="text-xs text-slate-500">{project.name}</span>
                  )}
                </div>

                <h3 className="text-sm font-medium text-slate-200 mb-1 group-hover:text-white transition-colors line-clamp-1">
                  {session.name}
                </h3>

                {session.description && (
                  <p className="text-xs text-slate-400 mb-3 line-clamp-2">
                    {session.description}
                  </p>
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
