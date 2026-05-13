import { cn } from '@/lib/utils';

type TextAreaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

export function TextArea({ className, ...props }: TextAreaProps) {
  return (
    <textarea
      className={cn(
        'min-h-[100px] w-full resize-y rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-white placeholder-slate-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/35',
        className
      )}
      {...props}
    />
  );
}
