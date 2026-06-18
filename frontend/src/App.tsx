import { Suspense, lazy } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { LoadingSpinner } from '@/components/ui';

const AppShell = lazy(() => import('@/layouts/AppShell'));
const Dashboard = lazy(() => import('@/pages/Dashboard'));
const ProjectsList = lazy(() => import('@/pages/ProjectsList'));
const ProjectDetail = lazy(() => import('@/pages/ProjectDetail'));
const SessionsList = lazy(() => import('@/pages/SessionsList'));
const SessionDetail = lazy(() => import('@/pages/SessionDetail'));
const NewSession = lazy(() => import('@/pages/NewSession'));
const TasksList = lazy(() => import('@/pages/TasksList'));
const TaskDetail = lazy(() => import('@/pages/TaskDetail'));
const Login = lazy(() => import('@/pages/Login'));
const Register = lazy(() => import('@/pages/Register'));
const SettingsPage = lazy(() => import('@/pages/SettingsPage'));
const AdminPilotDashboard = lazy(() => import('@/pages/AdminPilotDashboard'));

function RouteFallback() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-900 text-slate-100">
      <LoadingSpinner size="lg" />
    </div>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/" element={<AppShell />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="projects" element={<ProjectsList />} />
            <Route path="projects/:projectId" element={<ProjectDetail />} />
            <Route path="sessions" element={<SessionsList />} />
            <Route path="sessions/new" element={<NewSession />} />
            <Route path="sessions/:sessionId" element={<SessionDetail />} />
            <Route path="tasks" element={<TasksList />} />
            <Route path="projects/:projectId/tasks/:taskId" element={<TaskDetail />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="admin/pilot-dashboard" element={<AdminPilotDashboard />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}

export default App;
