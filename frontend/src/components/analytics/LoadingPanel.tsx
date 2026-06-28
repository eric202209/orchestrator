import { Skeleton } from '@/components/ui';

interface LoadingPanelProps {
  rows?: number;
}

export function LoadingPanel({ rows = 3 }: LoadingPanelProps) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-4 w-full" />
      ))}
    </div>
  );
}
