import { createSlice, PayloadAction } from '@reduxjs/toolkit';
import { Log, LogFilters } from '@/types';

const logsSlice = createSlice({
  name: 'logs',
  initialState: {
    items: [] as Log[],
    status: 'idle' as 'idle' | 'loading' | 'succeeded' | 'failed',
    error: null as string | null,
    filters: {} as LogFilters,
    searchQuery: '',
  },
  reducers: {
    addLogs: (state, action: PayloadAction<Log[]>) => {
      state.items.push(...action.payload);
    },
    setLogs: (state, action: PayloadAction<Log[]>) => {
      state.items = action.payload;
    },
    clearLogs: (state) => {
      state.items = [];
    },
    setSearchQuery: (state, action: PayloadAction<string>) => {
      state.searchQuery = action.payload;
    },
    setFilters: (state, action: PayloadAction<LogFilters>) => {
      state.filters = action.payload;
    },
    setError: (state, action: PayloadAction<string | null>) => {
      state.error = action.payload;
    },
  },
});

export const { addLogs, setLogs, clearLogs, setSearchQuery, setFilters, setError } = logsSlice.actions;
export default logsSlice.reducer;
