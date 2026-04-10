import { Modal } from './ui/Modal';

interface HelpModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function HelpModal({ isOpen, onClose }: HelpModalProps) {
  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Keyboard Shortcuts">
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="flex items-center gap-2">
            <kbd className="px-2 py-1 bg-slate-700 rounded text-xs font-mono">Ctrl+K</kbd>
            <span>Open session search</span>
          </div>
          <div className="flex items-center gap-2">
            <kbd className="px-2 py-1 bg-slate-700 rounded text-xs font-mono">Ctrl+S</kbd>
            <span>New session</span>
          </div>
          <div className="flex items-center gap-2">
            <kbd className="px-2 py-1 bg-slate-700 rounded text-xs font-mono">Ctrl+D</kbd>
            <span>Toggle dark mode</span>
          </div>
          <div className="flex items-center gap-2">
            <kbd className="px-2 py-1 bg-slate-700 rounded text-xs font-mono">Esc</kbd>
            <span>Close modal</span>
          </div>
        </div>
        <p className="text-sm text-slate-400">
          Use these shortcuts to navigate faster. Press <kbd className="px-1 bg-slate-700 rounded">?</kbd> to show this modal again.
        </p>
      </div>
    </Modal>
  );
}

export default HelpModal;
