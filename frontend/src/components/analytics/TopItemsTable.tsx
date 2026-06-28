interface TopItemsTableProps {
  title: string;
  items: Array<{
    knowledge_item_id: string;
    title: string | null;
    retrieval_count: number;
    effectiveness_rate?: number | null;
  }>;
  emptyText?: string;
}

export function TopItemsTable({ title, items, emptyText = 'No data' }: TopItemsTableProps) {
  return (
    <div>
      <p className="text-xs font-medium text-slate-500 mb-2">{title}</p>
      {items.length === 0 ? (
        <p className="text-xs text-slate-600 italic">{emptyText}</p>
      ) : (
        <div className="space-y-1">
          {items.map((item) => (
            <div
              key={item.knowledge_item_id}
              className="flex items-center justify-between text-xs border-b border-[color:var(--oc-border-soft)] py-1 last:border-0"
            >
              <span className="text-slate-400 truncate max-w-[65%]">
                {item.title || item.knowledge_item_id}
              </span>
              <div className="flex gap-3 text-right">
                <span className="text-slate-500 tabular-nums">{item.retrieval_count}×</span>
                {item.effectiveness_rate != null && (
                  <span className="text-white tabular-nums">
                    {Math.round(item.effectiveness_rate * 100)}%
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
