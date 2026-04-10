import { cn } from '@/lib/utils';

type AlertVariant = 'default' | 'destructive';

interface AlertProps {
  variant?: AlertVariant;
  title?: string;
  description?: string;
  children?: React.ReactNode;
  className?: string;
}

export function Alert({ 
  variant = 'default', 
  title, 
  description, 
  children,
  className 
}: AlertProps) {
  const variants = {
    default: 'bg-blue-500/10 border-blue-500/20 text-blue-400',
    destructive: 'bg-red-500/10 border-red-500/20 text-red-400'
  };

  return (
    <div className={cn(
      'p-4 rounded-lg border flex items-start gap-3',
      variants[variant],
      className
    )}>
      {variant === 'destructive' ? (
        <svg 
          className="h-5 w-5 flex-shrink-0 mt-0.5" 
          viewBox="0 0 20 20" 
          fill="currentColor"
        >
          <path 
            fillRule="evenodd" 
            d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" 
            clipRule="evenodd" 
          />
        </svg>
      ) : (
        <svg 
          className="h-5 w-5 flex-shrink-0 mt-0.5" 
          viewBox="0 0 20 20" 
          fill="currentColor"
        >
          <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" />
        </svg>
      )}
      <div className="flex-1">
        {title && <h3 className="text-sm font-medium mb-1">{title}</h3>}
        {description && <p className="text-sm">{description}</p>}
        {children && <div className="mt-2">{children}</div>}
      </div>
    </div>
  );
}
