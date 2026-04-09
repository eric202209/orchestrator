import { cn } from '@/lib/utils';

interface StatusBadgeProps {
  status: string;
  size?: 'sm' | 'md' | 'lg';
  variant?: 'default' | 'outline';
  className?: string;
}

const statusColors: Record<string, { bg: string; text: string; icon: string }> = {
  // Project statuses
  'active': { bg: 'bg-green-500/10', text: 'text-green-400', icon: '✓' },
  'pending': { bg: 'bg-yellow-500/10', text: 'text-yellow-400', icon: '⏳' },
  'stopped': { bg: 'bg-slate-500/10', text: 'text-slate-400', icon: '⏸' },
  
  // Session statuses
  'running': { bg: 'bg-blue-500/10', text: 'text-blue-400', icon: '▶' },
  'paused': { bg: 'bg-yellow-500/10', text: 'text-yellow-400', icon: '⏸' },
  'failed': { bg: 'bg-red-500/10', text: 'text-red-400', icon: '✗' },
  'completed': { bg: 'bg-green-500/10', text: 'text-green-400', icon: '✓' },
  'cancelled': { bg: 'bg-slate-500/10', text: 'text-slate-400', icon: '⏹' },
  
  // Task statuses
  'todo': { bg: 'bg-slate-500/10', text: 'text-slate-400', icon: '□' },
  'in_progress': { bg: 'bg-blue-500/10', text: 'text-blue-400', icon: '◐' },
  'done': { bg: 'bg-green-500/10', text: 'text-green-400', icon: '✓' },
  
  // Default fallback
  'default': { bg: 'bg-slate-500/10', text: 'text-slate-400', icon: '•' },
};

export default function StatusBadge({ 
  status, 
  size = 'md', 
  variant = 'default', 
  className 
}: StatusBadgeProps) {
  const statusLower = status?.toLowerCase();
  const colors = statusColors[statusLower] || statusColors['default'];
  
  // Format status text: convert snake_case to Title Case
  const formatStatusText = (statusText: string | undefined): string => {
    if (!statusText) return 'unknown';
    
    // Handle special cases
    const specialCases: Record<string, string> = {
      'in_progress': 'In Progress',
      'todo': 'To Do',
      'done': 'Done',
    };
    
    if (specialCases[statusText]) {
      return specialCases[statusText];
    }
    
    // Convert snake_case to Title Case
    return statusText
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  };
  
  const sizeClasses = {
    sm: 'px-2 py-0.5 text-xs',
    md: 'px-2.5 py-0.5 text-sm',
    lg: 'px-3 py-1 text-base',
  };
  
  const borderClasses = variant === 'outline' 
    ? `border ${colors.bg.replace('/10', '')} ${colors.text}`
    : '';
  
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 font-medium rounded-full',
        sizeClasses[size],
        variant === 'default' ? `${colors.bg} ${colors.text}` : borderClasses,
        className
      )}
    >
      <span className="text-xs">{colors.icon}</span>
      <span className="capitalize">{formatStatusText(status)}</span>
    </span>
  );
}
