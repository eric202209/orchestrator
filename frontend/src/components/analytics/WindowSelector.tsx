import type { AnalyticsWindow } from '@/types/api';

const WINDOWS: { label: string; value: AnalyticsWindow }[] = [
  { label: '7d', value: '7d' },
  { label: '30d', value: '30d' },
  { label: 'All Time', value: 'all_time' },
];

interface WindowSelectorProps {
  value: AnalyticsWindow;
  onChange: (w: AnalyticsWindow) => void;
}

export function WindowSelector({ value, onChange }: WindowSelectorProps) {
  return (
    <div className="flex items-center gap-1 bg-[color:var(--oc-surface)] border border-[color:var(--oc-border-soft)] rounded-md p-0.5">
      {WINDOWS.map((w) => (
        <button
          key={w.value}
          onClick={() => onChange(w.value)}
          className={`text-xs px-3 py-1 rounded transition-colors ${
            value === w.value
              ? 'bg-[color:var(--oc-surface-raised)] text-white font-medium'
              : 'text-slate-500 hover:text-slate-300'
          }`}
        >
          {w.label}
        </button>
      ))}
    </div>
  );
}
