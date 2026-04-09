import { LucideIcon } from 'lucide-react';

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description: string;
  action?: {
    label: string;
    onClick: () => void;
  };
  className?: string;
}

export default function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className
}: EmptyStateProps) {
  return (
    <div className={`flex flex-col items-center justify-center py-12 ${className || ''}`}>
      <div className="mb-4 text-slate-600">
        {Icon ? (
          <Icon className="h-16 w-16" />
        ) : (
          <div className="h-16 w-16 bg-slate-800 rounded-full flex items-center justify-center">
            <span className="text-2xl">📭</span>
          </div>
        )}
      </div>
      <h3 className="text-xl font-semibold text-white mb-2">{title}</h3>
      <p className="text-slate-400 text-center mb-6 max-w-md">{description}</p>
      {action && (
        <button
          onClick={action.onClick}
          className="bg-primary-500 hover:bg-primary-600 text-white px-6 py-2 rounded-lg transition-all"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
