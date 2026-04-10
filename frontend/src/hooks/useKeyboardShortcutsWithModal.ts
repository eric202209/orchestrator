import { useNavigate } from 'react-router-dom';
import { useKeyboardShortcuts } from '@/lib/hooks/useKeyboardShortcuts';

export function useKeyboardShortcutsWithModal() {
  const navigate = useNavigate();

  const handleSearch = () => {
    // TODO: Implement session search modal
    console.log('Open search');
  };

  const handleNewSession = () => {
    navigate('/sessions/new');
  };

  const handleToggleTheme = () => {
    // TODO: Implement theme toggle
    console.log('Toggle theme');
  };

  const handleCloseModal = () => {
    console.log('Close modal');
  };

  useKeyboardShortcuts({
    onSearch: handleSearch,
    onNewSession: handleNewSession,
    onToggleTheme: handleToggleTheme,
    onCloseModal: handleCloseModal,
  });

  return {
    setIsHelpOpen: () => {},
  };
}

export default useKeyboardShortcutsWithModal;
