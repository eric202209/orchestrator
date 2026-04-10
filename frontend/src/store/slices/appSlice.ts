import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { Session, Project, Task } from '@/types';

interface AppState {
  sessions: Session[];
  projects: Project[];
  tasks: Task[];
  isLoading: boolean;
  error: string | null;
}

const initialState: AppState = {
  sessions: [],
  projects: [],
  tasks: [],
  isLoading: false,
  error: null,
};

const appSlice = createSlice({
  name: 'app',
  initialState,
  reducers: {
    // Optimistic updates
    addSessionOptimistic: (state, action: PayloadAction<Session>) => {
      state.sessions.unshift(action.payload);
    },
    deleteSessionOptimistic: (state, action: PayloadAction<number>) => {
      state.sessions = state.sessions.filter((s) => s.id !== action.payload);
    },
    updateTaskStatusOptimistic: (state, action: PayloadAction<{ taskId: number; status: string }>) => {
      const task = state.tasks.find((t) => t.id === action.payload.taskId);
      if (task) {
        task.status = action.payload.status;
      }
    },
    deleteProjectOptimistic: (state, action: PayloadAction<number>) => {
      state.projects = state.projects.filter((p) => p.id !== action.payload);
    },
    
    // Real updates
    setSessions: (state, action: PayloadAction<Session[]>) => {
      state.sessions = action.payload;
    },
    setProjects: (state, action: PayloadAction<Project[]>) => {
      state.projects = action.payload;
    },
    setTasks: (state, action: PayloadAction<Task[]>) => {
      state.tasks = action.payload;
    },
    setLoading: (state, action: PayloadAction<boolean>) => {
      state.isLoading = action.payload;
    },
    setError: (state, action: PayloadAction<string | null>) => {
      state.error = action.payload;
    },
    rollbackSessionDelete: () => {
      // Rollback would be handled by re-fetching
      console.log('Rolling back session delete');
    },
    rollbackTaskUpdate: () => {
      console.log('Rolling back task update');
    },
  },
});

export const {
  addSessionOptimistic,
  deleteSessionOptimistic,
  updateTaskStatusOptimistic,
  deleteProjectOptimistic,
  setSessions,
  setProjects,
  setTasks,
  setLoading,
  setError,
  rollbackSessionDelete,
  rollbackTaskUpdate,
} = appSlice.actions;

export default appSlice.reducer;
