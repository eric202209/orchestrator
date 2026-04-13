import { Provider } from 'react-redux';
import { store } from '@/store';
import { AppProviders } from '@/components/AppProviders';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import AppShell from '@/layouts/AppShell';
import Dashboard from '@/pages/Dashboard';
import ProjectsList from '@/pages/ProjectsList';
import ProjectDetail from '@/pages/ProjectDetail';
import SessionsList from '@/pages/SessionsList';
import SessionDetail from '@/pages/SessionDetail';
import NewSession from '@/pages/NewSession';
import TasksList from '@/pages/TasksList';
import TaskDetail from '@/pages/TaskDetail';
import Login from '@/pages/Login';
import Register from '@/pages/Register';
import SettingsPage from '@/pages/SettingsPage';

function App() {
  return (
    <Provider store={store}>
      <AppProviders>
        <BrowserRouter>
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
            </Route>
          </Routes>
        </BrowserRouter>
      </AppProviders>
    </Provider>
  );
}

export default App;
