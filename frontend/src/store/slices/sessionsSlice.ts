import { createSlice, PayloadAction, createAsyncThunk } from '@reduxjs/toolkit';
import type { Session, SessionFilters } from '@/types';
import { sessionsAPI } from '@/api/client';

export const fetchSessions = createAsyncThunk<Session[], SessionFilters>(
  'sessions/fetch',
  async (filters = {}, { rejectWithValue }) => {
    try {
      const response = await sessionsAPI.getAll(filters);
      return response.data || [];
    } catch (error: unknown) {
      const axiosError = error as {
        response?: { data?: { message?: string } };
      };
      return rejectWithValue(
        axiosError.response?.data?.message || 'Failed to fetch sessions'
      );
    }
  }
);

export interface SessionState {
  items: Session[];
  status: 'idle' | 'loading' | 'succeeded' | 'failed';
  error: string | null;
  filters: SessionFilters;
}

const initialState: SessionState = {
  items: [],
  status: 'idle',
  error: null,
  filters: {},
};

const sessionsSlice = createSlice({
  name: 'sessions',
  initialState,
  reducers: {
    setSessions: (state, action: PayloadAction<Session[]>) => {
      state.items = action.payload;
    },
    addSession: (state, action: PayloadAction<Session>) => {
      state.items.unshift(action.payload);
    },
    deleteSession: (state, action: PayloadAction<number>) => {
      state.items = state.items.filter((s) => s.id !== action.payload);
    },
    updateSession: (state, action: PayloadAction<{ id: number; updates: Partial<Session> }>) => {
      const index = state.items.findIndex((s) => s.id === action.payload.id);
      if (index !== -1) {
        state.items[index] = { ...state.items[index], ...action.payload.updates };
      }
    },
    setFilters: (state, action: PayloadAction<SessionFilters>) => {
      state.filters = action.payload;
    },
    setError: (state, action: PayloadAction<string | null>) => {
      state.error = action.payload;
    },
    clearError: (state) => {
      state.error = null;
    },
    // Optimistic delete
    deleteSessionOptimistic: (state, action: PayloadAction<number>) => {
      state.items = state.items.filter((s) => s.id !== action.payload);
    },
    // Rollback
    rollbackSessionDelete: () => {
      console.log('Rolling back session deletion');
    },
  },
});

export const {
  setSessions,
  addSession,
  deleteSession,
  updateSession,
  setFilters,
  setError,
  clearError,
  deleteSessionOptimistic,
  rollbackSessionDelete,
} = sessionsSlice.actions;

export default sessionsSlice.reducer;
