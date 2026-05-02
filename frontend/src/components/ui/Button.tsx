import { cn } from '@/lib/utils';

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'default' | 'ghost' | 'outline' | 'destructive';
  size?: 'sm' | 'md' | 'lg';
}

export function Button({ 
  variant = 'default', 
  size = 'md',
  className,
  children,
  disabled,
  ...props 
}: ButtonProps) {
  const variants = {
    default: 'bg-primary-600 hover:bg-primary-500 text-white',
    ghost: 'text-slate-400 hover:bg-slate-800 hover:text-slate-100',
    outline: 'border border-slate-700 hover:border-slate-600 hover:bg-slate-800 text-slate-300 hover:text-white',
    destructive: 'bg-red-600 hover:bg-red-500 text-white'
  };

  const sizes = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2 text-sm',
    lg: 'px-6 py-3 text-base'
  };

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center font-medium rounded-md transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-1 focus:ring-offset-slate-900 disabled:opacity-40 disabled:cursor-not-allowed',
        variants[variant],
        sizes[size],
        className
      )}
      disabled={disabled}
      {...props}
    >
      {children}
    </button>
  );
}
