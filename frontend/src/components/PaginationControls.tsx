import { ChevronLeft, ChevronRight } from 'lucide-react';

interface PaginationControlsProps {
  page: number;
  total_pages: number;
  has_next: boolean;
  has_previous: boolean;
  total: number;
  per_page: number;
  onPrev: () => void;
  onNext: () => void;
}

export function PaginationControls({
  page,
  total_pages,
  has_next,
  has_previous,
  total,
  per_page,
  onPrev,
  onNext,
}: PaginationControlsProps) {
  const start = total === 0 ? 0 : (page - 1) * per_page + 1;
  const end = Math.min(page * per_page, total);

  return (
    <div className="flex items-center justify-between px-1 py-2">
      <p className="text-xs text-slate-400">
        {total === 0 ? 'No results' : `Showing ${start}–${end} of ${total}`}
      </p>
      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={!has_previous}
          onClick={onPrev}
          className="flex items-center gap-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft className="h-3.5 w-3.5" />
          Previous
        </button>
        <span className="text-xs text-slate-400">
          Page {page} of {total_pages}
        </span>
        <button
          type="button"
          disabled={!has_next}
          onClick={onNext}
          className="flex items-center gap-1 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-3 py-1.5 text-xs text-slate-300 transition-colors hover:border-[color:var(--oc-border)] hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
          <ChevronRight className="h-3.5 w-3.5" />
        </button>
      </div>
    </div>
  );
}
