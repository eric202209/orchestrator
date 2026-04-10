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
    default: 'bg-primary-600 hover:bg-primary-700 text-white',
    ghost: 'hover:bg-slate-700 text-slate-300 hover:text-white',
    outline: 'border border-slate-600 hover:bg-slate-700 text-slate-300',
    destructive: 'bg-red-600 hover:bg-red-700 text-white'
  };

  const sizes = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2',
    lg: 'px-6 py-3 text-lg'
  };

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center font-medium rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 focus:ring-offset-slate-900 disabled:opacity-50 disabled:cursor-not-allowed',
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
