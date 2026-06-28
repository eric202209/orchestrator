interface MetricGridProps {
  children: React.ReactNode;
  cols?: 2 | 3 | 4;
}

export function MetricGrid({ children, cols = 4 }: MetricGridProps) {
  const colClass =
    cols === 2 ? 'grid-cols-2'
    : cols === 3 ? 'grid-cols-2 sm:grid-cols-3'
    : 'grid-cols-2 sm:grid-cols-4';
  return (
    <div className={`grid ${colClass} gap-x-6 gap-y-0 divide-x divide-[color:var(--oc-border-soft)]`}>
      {children}
    </div>
  );
}
