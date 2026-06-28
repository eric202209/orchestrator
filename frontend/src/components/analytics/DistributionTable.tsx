interface DistributionTableProps {
  title: string;
  data: Record<string, number>;
  emptyText?: string;
}

export function DistributionTable({ title, data, emptyText = 'No data' }: DistributionTableProps) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  return (
    <div>
      <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>
      {entries.length === 0 ? (
        <p className="text-xs text-slate-600 italic">{emptyText}</p>
      ) : (
        <div className="space-y-1">
          {entries.map(([key, count]) => (
            <div
              key={key}
              className="flex items-center justify-between text-xs border-b border-[color:var(--oc-border-soft)] py-1 last:border-0"
            >
              <span className="text-slate-400 truncate max-w-[70%]">{key}</span>
              <span className="text-white tabular-nums font-medium">{count}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
