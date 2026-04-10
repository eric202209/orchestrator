import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { Task, TaskFilters } from '@/types';

const tasksSlice = createSlice({
  name: 'tasks',
  initialState: {
    items: [] as Task[],
    status: 'idle' as 'idle' | 'loading' | 'succeeded' | 'failed',
    error: null as string | null,
    filters: {} as TaskFilters,
  },
  reducers: {
    // Optimistic updates
    updateTaskStatusOptimistic: (state, action: PayloadAction<{ taskId: number; status: string }>) => {
      const task = state.items.find((t) => t.id === action.payload.taskId);
      if (task) {
        task.status = action.payload.status;
      }
    },
    addTaskOptimistic: (state, action: PayloadAction<Task>) => {
      state.items.unshift(action.payload);
    },
    deleteTaskOptimistic: (state, action: PayloadAction<number>) => {
      state.items = state.items.filter((t) => t.id !== action.payload);
    },
    // Rollback
    rollbackTaskStatusUpdate: () => {
      console.log('Rolling back task status update');
    },
    rollbackTaskAdd: () => {
      console.log('Rolling back task addition');
    },
    rollbackTaskDelete: () => {
      console.log('Rolling back task deletion');
    },
    clearError: (state) => {
      state.error = null;
    },
  },
});

export const {
  updateTaskStatusOptimistic,
  addTaskOptimistic,
  deleteTaskOptimistic,
  rollbackTaskStatusUpdate,
  rollbackTaskAdd,
  rollbackTaskDelete,
  clearError,
} = tasksSlice.actions;

export default tasksSlice.reducer;
