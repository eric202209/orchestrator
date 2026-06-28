interface AnalyticsCardProps {
  title: string;
  children: React.ReactNode;
}

export function AnalyticsCard({ title, children }: AnalyticsCardProps) {
  return (
    <section className="bg-[color:var(--oc-surface)] rounded-lg border border-[color:var(--oc-border-soft)] p-4">
      <h2 className="text-sm font-semibold text-slate-200 mb-4 uppercase tracking-wide">
        {title}
      </h2>
      {children}
    </section>
  );
}
