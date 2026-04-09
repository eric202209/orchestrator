import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Login from './pages/Login';
import Register from './pages/Register';
import Dashboard from './pages/Dashboard';
import ProjectsList from './pages/ProjectsList';
import ProjectDetail from './pages/ProjectDetail';
import SessionDashboard from './pages/SessionDashboard';
import NewSession from './pages/NewSession';
import SessionsList from './pages/SessionsList';
import { AppShell } from './layouts';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public routes */}
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        
        {/* Protected routes with AppShell layout */}
        <Route element={<AppShell />}>
          <Route 
            path="/" 
            element={
              localStorage.getItem('access_token') ? (
                <Dashboard />
              ) : (
                <Navigate to="/login" replace />
              )
            } 
          />
          <Route path="/projects" element={<ProjectsList />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/sessions" element={<SessionsList />} />
          <Route path="/sessions/new" element={<NewSession />} />
          <Route path="/sessions/:id" element={<SessionDashboard />} />
        </Route>
        
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
