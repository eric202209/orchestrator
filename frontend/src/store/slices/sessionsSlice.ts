import { createSlice, PayloadAction, createAsyncThunk } from '@reduxjs/toolkit';
import axios from 'axios';
import { Session, SessionFilters } from '@/types';

const API_BASE = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1';
const MOBILE_API_KEY = import.meta.env.VITE_MOBILE_API_KEY || '52a14f2e39c90d24ff8eef703492234b762ffb228e56999f7562be1a167b0456';

export const fetchSessions = createAsyncThunk<Session[], SessionFilters>(
  'sessions/fetch',
  async (filters = {}, { rejectWithValue }) => {
    try {
      const response = await axios.get(`${API_BASE}/mobile/sessions`, { 
        params: filters,
        headers: {
          'X-OpenClaw-API-Key': MOBILE_API_KEY,
        },
      });
      return response.data.sessions || [];
    } catch (error: any) {
      return rejectWithValue(error.response?.data?.message || 'Failed to fetch sessions');
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
    rollbackSessionDelete: (state) => {
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
