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
    <div className={`flex flex-col items-center justify-center py-10 ${className || ''}`}>
      {Icon && (
        <div className="mb-3 text-slate-600">
          <Icon className="h-8 w-8" />
        </div>
      )}
      <p className="text-sm font-medium text-slate-300">{title}</p>
      <p className="text-xs text-slate-500 text-center mt-1 max-w-xs">{description}</p>
      {action && (
        <button
          onClick={action.onClick}
          className="mt-4 bg-sky-600 hover:bg-sky-500 text-white text-sm px-4 py-1.5 rounded-md transition-colors"
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
