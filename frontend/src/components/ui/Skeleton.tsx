export default function Skeleton({ 
  className,
  width,
  height
}: { 
  className?: string;
  width?: string | number;
  height?: string | number;
}) {
  return (
    <div
      className={`animate-pulse bg-slate-700/50 rounded ${className || ''}`}
      style={{
        minWidth: width,
        minHeight: height,
      }}
    />
  );
}
