import { useEffect } from 'react';
import { useKeyboardShortcutsWithModal } from '@/hooks/useKeyboardShortcutsWithModal';
import HelpModal from '@/components/HelpModal';

export default function KeyboardShortcutsController() {
  const { isHelpOpen, setIsHelpOpen } = useKeyboardShortcutsWithModal();

  useEffect(() => {
    const handleGlobalKeyDown = (e: KeyboardEvent) => {
      if (e.key === '?' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        setIsHelpOpen(true);
      }
    };

    window.addEventListener('keydown', handleGlobalKeyDown);
    return () => window.removeEventListener('keydown', handleGlobalKeyDown);
  }, [setIsHelpOpen]);

  return <HelpModal isOpen={isHelpOpen} onClose={() => setIsHelpOpen(false)} />;
}
