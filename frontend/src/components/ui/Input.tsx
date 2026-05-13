import { cn } from '@/lib/utils';

type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

export function Input({ className, ...props }: InputProps) {
  return (
    <input
      className={cn(
        'w-full rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-white placeholder-slate-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/35',
        className
      )}
      {...props}
    />
  );
}
