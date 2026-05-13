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
    /* border matches the button family (blue), not the surface family (navy) */
    default: 'border border-[color:var(--oc-action-hover)] bg-[color:var(--oc-action)] text-white hover:bg-[color:var(--oc-action-hover)]',
    ghost: 'text-slate-400 hover:bg-[color:var(--oc-surface-raised)] hover:text-slate-200',
    outline: 'border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white',
    destructive: 'border border-red-700 bg-red-700 hover:bg-red-600 hover:border-red-500 text-white'
  };

  const sizes = {
    sm: 'px-3 py-1.5 text-sm',
    md: 'px-4 py-2 text-sm',
    lg: 'px-6 py-3 text-base'
  };

  return (
    <button
      className={cn(
        'inline-flex items-center justify-center rounded-md font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-primary-500/60 focus:ring-offset-1 focus:ring-offset-[color:var(--oc-canvas)] disabled:cursor-not-allowed disabled:opacity-40',
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
