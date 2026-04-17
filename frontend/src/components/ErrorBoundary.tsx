import { ErrorBoundary as ReactErrorBoundary } from 'react-error-boundary';
import type { FallbackProps } from 'react-error-boundary';

function ErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  const normalizedError = error instanceof Error ? error : new Error(String(error));
  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-900 text-slate-100 p-4">
      <div className="max-w-md w-full bg-slate-800 rounded-lg p-6 space-y-4">
        <h2 className="text-xl font-semibold text-red-400">Something went wrong</h2>
        <p className="text-slate-400 text-sm">{normalizedError.message}</p>
        <details className="bg-slate-900 rounded p-3 text-xs font-mono text-slate-500">
          <summary className="cursor-pointer mb-2">Stack Trace</summary>
          {normalizedError.stack}
        </details>
        <div className="flex gap-3">
          <button
            onClick={resetErrorBoundary}
            className="flex-1 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg transition-colors"
          >
            Try again
          </button>
          <button
            onClick={() => window.location.href = '/'}
            className="flex-1 bg-slate-700 hover:bg-slate-600 text-white px-4 py-2 rounded-lg transition-colors"
          >
            Go home
          </button>
        </div>
      </div>
    </div>
  );
}

export function ErrorBoundary({ children }: { children: React.ReactNode }) {
  return (
    <ReactErrorBoundary
      FallbackComponent={ErrorFallback}
      onError={(error, errorInfo) => {
        console.error('ErrorBoundary caught an error:', error, errorInfo);
      }}
      onReset={() => {
        console.log('ErrorBoundary reset');
      }}
    >
      {children}
    </ReactErrorBoundary>
  );
}

export default ErrorBoundary;
