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
          <span className="text-slate-500 mr-3 shrink-0">
            [{timestamp}]
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
          "overflow-y-auto rounded-lg border border-slate-700 bg-slate-900",
          "scrollbar-thin scrollbar-thumb-slate-600 scrollbar-track-slate-800",
          "scrollbar-hover"
        )}
        style={{ height }}
      >
        {logs.length === 0 ? (
          <div className="flex items-center justify-center h-full text-slate-500">
            <div className="text-center">
              <p className="text-sm">No logs yet</p>
              <p className="text-xs mt-1">Logs will appear here when the session starts</p>
            </div>
          </div>
        ) : (
          <div className="p-4 font-mono text-sm leading-relaxed">
            {logs.map((log, index) => (
              <div
                key={index}
                className={cn(
                  "whitespace-pre-wrap break-words",
                  "last:scroll-into-view"
                )}
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
          className="absolute bottom-4 right-4 p-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg shadow-lg transition-colors"
          title="Scroll to bottom"
        >
          <ChevronDown className="h-5 w-5 animate-bounce" />
        </button>
      )}

      {/* Log count badge */}
      {logs.length > 0 && (
        <div className="absolute top-2 right-2 px-2 py-1 bg-slate-800 text-slate-400 text-xs rounded">
          {logs.length} logs
        </div>
      )}
    </div>
  );
}

export default TerminalViewer;

