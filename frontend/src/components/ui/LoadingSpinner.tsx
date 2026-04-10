import { ReactNode } from 'react';

interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg';
  className?: string;
  children?: ReactNode;
}

export function LoadingSpinner({ size = 'md', className = '', children }: LoadingSpinnerProps) {
  const sizeClasses = {
    sm: 'w-4 h-4',
    md: 'w-8 h-8',
    lg: 'w-12 h-12',
  };

  return (
    <div className={`flex flex-col items-center justify-center ${className}`}>
      <div
        className={`animate-spin rounded-full border-2 border-slate-600 border-t-blue-500 ${sizeClasses[size]}`}
      />
      {children && <p className="mt-3 text-slate-400 text-sm">{children}</p>}
    </div>
  );
}

export default LoadingSpinner;
