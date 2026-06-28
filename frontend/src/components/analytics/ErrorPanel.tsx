interface ErrorPanelProps {
  message?: string;
}

export function ErrorPanel({ message = 'Failed to load data' }: ErrorPanelProps) {
  return (
    <div className="rounded border border-red-500/20 bg-red-500/5 px-3 py-2">
      <p className="text-xs text-red-400">{message}</p>
    </div>
  );
}
