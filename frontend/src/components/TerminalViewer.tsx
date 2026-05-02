import { useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { cn } from '@/lib/utils';

export interface TerminalLogEntry {
  message: string;
  timestamp?: string;
}

interface TerminalViewerProps {
  logs: Array<string | TerminalLogEntry>;
  autoScroll?: boolean;
  className?: string;
  height?: string;
}

export function TerminalViewer({ 
  logs, 
  autoScroll = true, 
  className = '',
  height = '400px'
}: TerminalViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [showScrollIndicator, setShowScrollIndicator] = useState(false);

  // Handle scroll detection
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const handleScroll = () => {
      const scrollTop = container.scrollTop;
      const scrollHeight = container.scrollHeight;
      const clientHeight = container.clientHeight;
      
      const isAtBottomPos = scrollHeight - scrollTop <= clientHeight + 10;
      setShowScrollIndicator(!isAtBottomPos && logs.length > 0);
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, [logs.length]);

  // Auto-scroll to bottom
  useEffect(() => {
    if (!autoScroll) return;
    
    const container = containerRef.current;
    if (!container) return;

    container.scrollTo({
      top: container.scrollHeight,
      behavior: 'smooth'
    });
  }, [logs, autoScroll]);

  const scrollToBottom = () => {
    const container = containerRef.current;
    if (container) {
      container.scrollTo({
        top: container.scrollHeight,
        behavior: 'smooth'
      });
    }
  };

  // Colorize logs based on content
  const colorizeLog = (log: string | TerminalLogEntry) => {
    const message = typeof log === 'string' ? log : log.message;
    const timestamp = typeof log === 'string' ? undefined : log.timestamp;
    const lines = message.split('\n');
    return lines.map((line, idx) => {
      const prefix = idx === 0 && timestamp
        ? (
          <span className="text-slate-600 mr-2 shrink-0 select-none">
            {timestamp}
          </span>
        )
        : null;

      // Check for different log levels
      if (line.includes('✓') || line.includes('success') || line.includes('Success')) {
        return (
          <div key={idx} className="flex">
            {prefix}
            <span className="text-emerald-400">{line}</span>
          </div>
        );
      }
      if (line.includes('✗') || line.includes('error') || line.includes('Error') || line.includes('failed')) {
        return (
          <div key={idx} className="flex">
            {prefix}
            <span className="text-red-400">{line}</span>
          </div>
        );
      }
      if (line.includes('warning') || line.includes('Warning')) {
        return (
          <div key={idx} className="flex">
            {prefix}
            <span className="text-yellow-400">{line}</span>
          </div>
        );
      }
      if (line.includes('info') || line.includes('Info') || line.includes('INFO')) {
        return (
          <div key={idx} className="flex">
            {prefix}
            <span className="text-blue-400">{line}</span>
          </div>
        );
      }
      if (line.includes('[') && line.includes(']')) {
        // Timestamp lines
        return (
          <div key={idx} className="flex">
            {prefix}
            <span className="text-slate-400">{line}</span>
          </div>
        );
      }
      // Default log line
      return (
        <div key={idx} className="flex">
          {prefix}
          <span className="text-slate-200">{line}</span>
        </div>
      );
    });
  };

  return (
    <div className={cn("relative", className)}>
      <div
        ref={containerRef}
        className={cn(
          "overflow-y-auto rounded-lg border border-slate-700 bg-slate-950",
          "scrollbar-thin scrollbar-thumb-slate-600 scrollbar-track-slate-800"
        )}
        style={{ height }}
      >
        {logs.length === 0 ? (
          <div className="flex items-center justify-center h-full text-slate-600">
            <div className="text-center">
              <p className="text-xs">No logs yet</p>
              <p className="text-xs mt-0.5 text-slate-700">Logs appear when the session starts</p>
            </div>
          </div>
        ) : (
          <div className="p-3 font-mono text-[12px] leading-5">
            {logs.map((log, index) => (
              <div
                key={index}
                className="whitespace-pre-wrap break-words py-px"
              >
                {colorizeLog(log)}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Scroll indicator */}
      {showScrollIndicator && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-3 right-3 p-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded-md shadow-lg transition-colors"
          title="Scroll to bottom"
        >
          <ChevronDown className="h-4 w-4" />
        </button>
      )}

      {/* Log count badge */}
      {logs.length > 0 && (
        <div className="absolute top-2 right-2 px-1.5 py-0.5 bg-slate-800/90 text-slate-500 text-[10px] rounded font-mono">
          {logs.length}
        </div>
      )}
    </div>
  );
}

export default TerminalViewer;

