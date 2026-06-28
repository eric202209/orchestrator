interface DistributionBarChartProps {
  title: string;
  data: Record<string, number>;
  emptyText?: string;
}

export function DistributionBarChart({
  title,
  data,
  emptyText = 'No data',
}: DistributionBarChartProps) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, count]) => sum + count, 0);

  return (
    <div>
      <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>
      {entries.length === 0 ? (
        <p className="text-xs text-slate-600 italic">{emptyText}</p>
      ) : (
        <div className="space-y-1.5">
          {entries.map(([key, count]) => {
            const pct = total > 0 ? (count / total) * 100 : 0;
            return (
              <div key={key} className="flex items-center gap-2">
                <span className="text-xs text-slate-400 truncate w-24 shrink-0">{key}</span>
                <div className="flex-1 h-3 bg-slate-800 rounded-sm overflow-hidden">
                  <div
                    className="h-full rounded-sm bg-slate-500"
                    style={{ width: `${pct}%` }}
                    role="presentation"
                  />
                </div>
                <span className="text-xs text-slate-400 tabular-nums w-8 shrink-0 text-right">
                  {Math.round(pct)}%
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
