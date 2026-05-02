import { cn } from '@/lib/utils';

interface StatusBadgeProps {
  status: string;
  size?: 'sm' | 'md' | 'lg';
  variant?: 'default' | 'outline';
  className?: string;
}

const statusColors: Record<string, { bg: string; text: string; dot: string }> = {
  'active':      { bg: 'bg-emerald-500/10', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  'pending':     { bg: 'bg-amber-500/10',   text: 'text-amber-400',   dot: 'bg-amber-400' },
  'stopped':     { bg: 'bg-slate-500/10',   text: 'text-slate-400',   dot: 'bg-slate-500' },
  'running':     { bg: 'bg-sky-500/10',     text: 'text-sky-400',     dot: 'bg-sky-400' },
  'paused':      { bg: 'bg-amber-500/10',   text: 'text-amber-400',   dot: 'bg-amber-400' },
  'failed':      { bg: 'bg-red-500/10',     text: 'text-red-400',     dot: 'bg-red-400' },
  'completed':   { bg: 'bg-emerald-500/10', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  'cancelled':   { bg: 'bg-slate-500/10',   text: 'text-slate-400',   dot: 'bg-slate-500' },
  'todo':        { bg: 'bg-slate-500/10',   text: 'text-slate-400',   dot: 'bg-slate-500' },
  'in_progress': { bg: 'bg-sky-500/10',     text: 'text-sky-400',     dot: 'bg-sky-400' },
  'done':        { bg: 'bg-emerald-500/10', text: 'text-emerald-400', dot: 'bg-emerald-400' },
  'default':     { bg: 'bg-slate-500/10',   text: 'text-slate-400',   dot: 'bg-slate-500' },
};

export function StatusBadge({
  status,
  size = 'md',
  variant = 'default',
  className,
}: StatusBadgeProps) {
  const statusLower = status?.toLowerCase();
  const colors = statusColors[statusLower] || statusColors['default'];

  const formatStatusText = (s: string | undefined): string => {
    if (!s) return 'unknown';
    const special: Record<string, string> = {
      in_progress: 'In Progress',
      todo: 'To Do',
      done: 'Done',
    };
    return special[s] ?? s.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  };

  const sizeClasses = {
    sm: 'px-1.5 py-0.5 text-xs gap-1',
    md: 'px-2 py-0.5 text-xs gap-1.5',
    lg: 'px-2.5 py-1 text-sm gap-1.5',
  };

  const dotSize = size === 'lg' ? 'h-2 w-2' : 'h-1.5 w-1.5';

  const borderClasses = variant === 'outline'
    ? `border border-current`
    : '';

  return (
    <span
      className={cn(
        'inline-flex items-center font-medium rounded-md whitespace-nowrap',
        sizeClasses[size],
        variant === 'default' ? `${colors.bg} ${colors.text}` : `${colors.text} ${borderClasses}`,
        className
      )}
    >
      <span className={cn('rounded-full flex-shrink-0', dotSize, colors.dot)} />
      <span>{formatStatusText(status)}</span>
    </span>
  );
}
