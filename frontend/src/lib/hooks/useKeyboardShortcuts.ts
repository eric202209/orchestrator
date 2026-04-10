import { useEffect } from 'react';

interface UseKeyboardShortcutsOptions {
  onSearch?: () => void;
  onNewSession?: () => void;
  onToggleTheme?: () => void;
  onCloseModal?: () => void;
}

export function useKeyboardShortcuts(options: UseKeyboardShortcutsOptions = {}) {
  const { onSearch, onNewSession, onToggleTheme, onCloseModal } = options;

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ctrl/Cmd + K: Open search
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        onSearch?.();
      }
      
      // Ctrl/Cmd + S: Navigate to new session
      if ((e.ctrlKey || e.metaKey) && e.key === 's') {
        e.preventDefault();
        onNewSession?.();
      }
      
      // Ctrl/Cmd + D: Toggle theme
      if ((e.ctrlKey || e.metaKey) && e.key === 'd') {
        e.preventDefault();
        onToggleTheme?.();
      }
      
      // Escape: Close modals
      if (e.key === 'Escape') {
        onCloseModal?.();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onSearch, onNewSession, onToggleTheme, onCloseModal]);
}

export default useKeyboardShortcuts;
