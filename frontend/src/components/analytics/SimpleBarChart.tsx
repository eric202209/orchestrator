interface BarEntry {
  label: string;
  value: number | null;
}

interface SimpleBarChartProps {
  title?: string;
  bars: BarEntry[];
  max?: number;
  formatValue?: (v: number | null) => string;
  emptyText?: string;
  labelClassName?: string;
}

function defaultFmt(v: number | null): string {
  return v == null ? '—' : String(v);
}

export function SimpleBarChart({
  title,
  bars,
  max,
  formatValue = defaultFmt,
  emptyText = 'No data',
  labelClassName = 'w-9 text-right',
}: SimpleBarChartProps) {
  const allNull = bars.every((b) => b.value == null);
  if (bars.length === 0 || allNull) {
    return (
      <div>
        {title && <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>}
        <p className="text-xs text-slate-600 italic">{emptyText}</p>
      </div>
    );
  }

  const computedMax =
    max !== undefined
      ? max
      : Math.max(...bars.map((b) => b.value ?? 0));
  const effectiveMax = computedMax > 0 ? computedMax : 1;

  return (
    <div>
      {title && <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>}
      <div className="space-y-1.5">
        {bars.map(({ label, value }) => {
          const pct = value != null ? Math.min((value / effectiveMax) * 100, 100) : 0;
          return (
            <div key={label} className="flex items-center gap-2">
              <span className={`text-xs text-slate-400 shrink-0 truncate ${labelClassName}`} title={label}>
                {label}
              </span>
              <div className="flex-1 h-3 bg-slate-800 rounded-sm overflow-hidden">
                {value != null && pct > 0 && (
                  <div
                    className="h-full rounded-sm bg-[color:var(--oc-accent)]"
                    style={{ width: `${pct}%` }}
                    role="presentation"
                  />
                )}
              </div>
              <span className="text-xs text-slate-300 tabular-nums w-9 shrink-0 text-right">
                {formatValue(value)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
