interface MetricCardProps {
  label: string;
  value: string;
  sub?: string;
  hint?: string;
}

export function MetricCard({ label, value, sub, hint }: MetricCardProps) {
  return (
    <div className="flex flex-col gap-0.5 py-2">
      <span className="text-xl font-semibold text-white tabular-nums">{value}</span>
      <span className="text-xs text-slate-400">{label}</span>
      {sub && <span className="text-[10px] text-slate-600">{sub}</span>}
      {hint && <span className="text-[10px] text-slate-600 italic">{hint}</span>}
    </div>
  );
}
