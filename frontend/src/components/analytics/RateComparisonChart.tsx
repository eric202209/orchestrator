interface ComparisonGroup {
  label: string;
  a: number | null;
  b: number | null;
}

interface RateComparisonChartProps {
  title?: string;
  groups: ComparisonGroup[];
  labelA: string;
  labelB: string;
  formatValue?: (v: number | null) => string;
  emptyText?: string;
}

function defaultFmt(v: number | null): string {
  return v == null ? '—' : String(v);
}

export function RateComparisonChart({
  title,
  groups,
  labelA,
  labelB,
  formatValue = defaultFmt,
  emptyText = 'No data',
}: RateComparisonChartProps) {
  const allNull = groups.every((g) => g.a == null && g.b == null);
  if (groups.length === 0 || allNull) {
    return (
      <div>
        {title && <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>}
        <p className="text-xs text-slate-600 italic">{emptyText}</p>
      </div>
    );
  }

  const allValues = groups.flatMap((g) => [g.a, g.b]).filter((v): v is number => v != null);
  const maxVal = allValues.length > 0 ? Math.max(...allValues) : 1;
  const effectiveMax = maxVal > 0 ? maxVal : 1;

  return (
    <div>
      {title && <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>}
      <div className="flex gap-4 mb-2">
        <div className="flex items-center gap-1.5">
          <div className="w-2.5 h-2.5 rounded-sm bg-[color:var(--oc-accent)]" />
          <span className="text-[10px] text-slate-500">{labelA}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2.5 h-2.5 rounded-sm bg-slate-500" />
          <span className="text-[10px] text-slate-500">{labelB}</span>
        </div>
      </div>
      <div className="space-y-3">
        {groups.map(({ label, a, b }) => {
          const pctA = a != null ? Math.min((a / effectiveMax) * 100, 100) : 0;
          const pctB = b != null ? Math.min((b / effectiveMax) * 100, 100) : 0;
          return (
            <div key={label}>
              <span className="text-[10px] text-slate-500 block mb-0.5">{label}</span>
              <div className="flex items-center gap-2 mb-0.5">
                <div className="flex-1 h-2 bg-slate-800 rounded-sm overflow-hidden">
                  {a != null && pctA > 0 && (
                    <div
                      className="h-full rounded-sm bg-[color:var(--oc-accent)]"
                      style={{ width: `${pctA}%` }}
                      role="presentation"
                    />
                  )}
                </div>
                <span className="text-xs text-slate-400 tabular-nums w-10 shrink-0 text-right">
                  {formatValue(a)}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <div className="flex-1 h-2 bg-slate-800 rounded-sm overflow-hidden">
                  {b != null && pctB > 0 && (
                    <div
                      className="h-full rounded-sm bg-slate-500"
                      style={{ width: `${pctB}%` }}
                      role="presentation"
                    />
                  )}
                </div>
                <span className="text-xs text-slate-400 tabular-nums w-10 shrink-0 text-right">
                  {formatValue(b)}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
