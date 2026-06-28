import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { SessionCompleteBlock } from '@/components/SessionDetailSections';

// ── test harness ──────────────────────────────────────────────────────────────

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

type BlockProps = React.ComponentProps<typeof SessionCompleteBlock>;

function render(props: BlockProps) {
  act(() => {
    root.render(
      <MemoryRouter>
        <SessionCompleteBlock {...props} />
      </MemoryRouter>,
    );
  });
}

// ── completed session ─────────────────────────────────────────────────────────

describe('SessionCompleteBlock — completed session', () => {
  it('shows "Session complete" heading', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Session complete');
  });

  it('shows "Status: Completed"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Status: Completed');
  });

  it('shows Review Queue link when reviewCount > 0', () => {
    render({ reviewCount: 3, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Review Queue');
    expect(container.textContent).toContain('3 task outputs');
  });

  it('shows singular "task output" when reviewCount is 1', () => {
    render({ reviewCount: 1, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Review Queue');
    expect(container.textContent).toContain('1 task output');
  });

  it('hides Review Queue link when reviewCount is 0', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).not.toContain('Review Queue');
  });

  it('shows "Check system health"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Check system health');
  });

  it('shows "Start next session" when projectId is provided', () => {
    render({ reviewCount: 0, projectId: 42, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Start next session');
  });

  it('shows "Return to Sessions" instead of start-next-session when projectId is null', () => {
    render({ reviewCount: 0, projectId: null, sessionStatus: 'completed' });
    expect(container.textContent).not.toContain('Start next session');
    expect(container.textContent).toContain('Return to Sessions');
  });

  it('shows "Return to Sessions" instead of start-next-session when projectId is undefined', () => {
    render({ reviewCount: 0, sessionStatus: 'completed' });
    expect(container.textContent).not.toContain('Start next session');
    expect(container.textContent).toContain('Return to Sessions');
  });

  it('does not show "Return to Sessions" when projectId available', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).not.toContain('Return to Sessions');
  });

  it('does not show "Open logs" button', () => {
    const onOpenLogs = vi.fn();
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'completed', onOpenLogs });
    expect(container.textContent).not.toContain('Open logs');
  });
});

// ── failed session ────────────────────────────────────────────────────────────

describe('SessionCompleteBlock — failed session', () => {
  it('shows "Session failed" heading', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).toContain('Session failed');
  });

  it('shows "Status: Failed"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).toContain('Status: Failed');
  });

  it('shows "Return to Sessions"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).toContain('Return to Sessions');
  });

  it('does not show "Start next session"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).not.toContain('Start next session');
  });

  it('does not show "Start next session" even when projectId provided', () => {
    render({ reviewCount: 0, projectId: 99, sessionStatus: 'failed' });
    expect(container.textContent).not.toContain('Start next session');
  });

  it('shows "Open logs" button when onOpenLogs provided', () => {
    const onOpenLogs = vi.fn();
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed', onOpenLogs });
    expect(container.textContent).toContain('Open logs');
  });

  it('does not show "Open logs" when onOpenLogs not provided', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).not.toContain('Open logs');
  });

  it('calls onOpenLogs when "Open logs" button is clicked', () => {
    const onOpenLogs = vi.fn();
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed', onOpenLogs });
    const btn = container.querySelector('button');
    expect(btn).not.toBeNull();
    act(() => { btn!.click(); });
    expect(onOpenLogs).toHaveBeenCalledOnce();
  });

  it('does not show Review Queue even when reviewCount > 0', () => {
    render({ reviewCount: 5, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).not.toContain('Review Queue');
  });

  it('shows "Check system health"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'failed' });
    expect(container.textContent).toContain('Check system health');
  });
});

// ── stopped session ───────────────────────────────────────────────────────────

describe('SessionCompleteBlock — stopped session', () => {
  it('shows "Session stopped" heading', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'stopped' });
    expect(container.textContent).toContain('Session stopped');
  });

  it('shows "Status: Stopped"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'stopped' });
    expect(container.textContent).toContain('Status: Stopped');
  });

  it('shows "Return to Sessions"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'stopped' });
    expect(container.textContent).toContain('Return to Sessions');
  });

  it('does not show "Start next session"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'stopped' });
    expect(container.textContent).not.toContain('Start next session');
  });
});

// ── cancelled session ─────────────────────────────────────────────────────────

describe('SessionCompleteBlock — cancelled session', () => {
  it('shows "Session cancelled" for status "cancelled"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'cancelled' });
    expect(container.textContent).toContain('Session cancelled');
    expect(container.textContent).toContain('Status: Cancelled');
  });

  it('shows "Session cancelled" for US spelling "canceled"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'canceled' });
    expect(container.textContent).toContain('Session cancelled');
    expect(container.textContent).toContain('Status: Cancelled');
  });

  it('does not show "Start next session"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'cancelled' });
    expect(container.textContent).not.toContain('Start next session');
  });

  it('shows "Return to Sessions"', () => {
    render({ reviewCount: 0, projectId: 1, sessionStatus: 'cancelled' });
    expect(container.textContent).toContain('Return to Sessions');
  });
});

// ── Review Queue terminology ──────────────────────────────────────────────────

describe('SessionCompleteBlock — Review Queue terminology', () => {
  it('uses "Review Queue" not "Tasks"', () => {
    render({ reviewCount: 2, projectId: 1, sessionStatus: 'completed' });
    expect(container.textContent).toContain('Review Queue');
    expect(container.textContent).not.toContain('Tasks —');
  });
});

// ── unknown status fallback ───────────────────────────────────────────────────

describe('SessionCompleteBlock — unknown/missing status', () => {
  it('shows "Session ended" heading when sessionStatus is undefined', () => {
    render({ reviewCount: 0, projectId: 1 });
    expect(container.textContent).toContain('Session ended');
  });

  it('does not show status label when sessionStatus is undefined', () => {
    render({ reviewCount: 0, projectId: 1 });
    expect(container.textContent).not.toContain('Status:');
  });
});
