import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import type { Project, ProjectFilters } from '@/types';

const projectsSlice = createSlice({
  name: 'projects',
  initialState: {
    items: [] as Project[],
    status: 'idle' as 'idle' | 'loading' | 'succeeded' | 'failed',
    error: null as string | null,
    filters: {} as ProjectFilters,
  },
  reducers: {
    // Optimistic operations
    addProjectOptimistic: (state, action: PayloadAction<Project>) => {
      state.items.unshift(action.payload);
    },
    deleteProjectOptimistic: (state, action: PayloadAction<number>) => {
      state.items = state.items.filter((p) => p.id !== action.payload);
    },
    updateProjectOptimistic: (state, action: PayloadAction<{ id: number; updates: Partial<Project> }>) => {
      const project = state.items.find((p) => p.id === action.payload.id);
      if (project) {
        Object.assign(project, action.payload.updates);
      }
    },
    // Rollback
    rollbackProjectDelete: () => {
      console.log('Rolling back project deletion');
    },
    rollbackProjectUpdate: () => {
      console.log('Rolling back project update');
    },
    clearError: (state) => {
      state.error = null;
    },
  },
});

export const {
  addProjectOptimistic,
  deleteProjectOptimistic,
  updateProjectOptimistic,
  rollbackProjectDelete,
  rollbackProjectUpdate,
  clearError,
} = projectsSlice.actions;

export default projectsSlice.reducer;
