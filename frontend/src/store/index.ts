import { configureStore } from '@reduxjs/toolkit';
import appReducer from './slices/appSlice';
import sessionsReducer from './slices/sessionsSlice';
import projectsReducer from './slices/projectsSlice';
import tasksReducer from './slices/tasksSlice';

export const store = configureStore({
  reducer: {
    app: appReducer,
    sessions: sessionsReducer,
    projects: projectsReducer,
    tasks: tasksReducer,
  },
  middleware: (getDefaultMiddleware) =>
    getDefaultMiddleware({
      serializableCheck: {
        ignoredActions: ['sessions/fetch/pending', 'sessions/fetch/fulfilled'],
      },
    }),
});

export type RootState = ReturnType<typeof store.getState>;
export type AppDispatch = typeof store.dispatch;

export default store;
