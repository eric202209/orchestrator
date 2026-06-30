import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import KnowledgeLibrary from '@/pages/KnowledgeLibrary';
import { knowledgeLibraryAPI } from '@/api/client';

vi.mock('@/api/client', () => ({
  knowledgeLibraryAPI: {
    list: vi.fn(),
    getById: vi.fn(),
    getUsageSummary: vi.fn(),
    getUsageList: vi.fn(),
    getRevisions: vi.fn(),
    getEvents: vi.fn(),
    patch: vi.fn(),
    retire: vi.fn(),
    restore: vi.fn(),
    sync: vi.fn(),
  },
}));

// ── fixtures ──────────────────────────────────────────────────────────────────

function makeItem(overrides: Partial<{
  id: string;
  title: string;
  knowledge_type: string;
  is_active: boolean;
  priority: number;
  version: number;
  sync_status: 'synced' | 'dirty' | 'syncing' | 'failed';
  sync_required_at: string | null;
  last_synced_at: string | null;
  last_sync_error: string | null;
}> = {}) {
  return {
    id: 'item-1',
    title: 'Format Guide Item',
    content: 'Some detailed content here.',
    source_path: null,
    knowledge_type: 'format_guide',
    tags: ['tag1'],
    project_scope: null,
    applies_to: ['planning'],
    failure_signature: null,
    tool_name: null,
    priority: 0,
    is_active: true,
    version: 1,
    checksum: 'abc123def456abc123def456',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    sync_status: 'synced' as const,
    sync_required_at: null,
    last_synced_at: '2026-06-01T00:00:00Z',
    last_sync_error: null,
    ...overrides,
  };
}

function makeEmptyPage() {
  return { items: [], total: 0, page: 1, page_size: 20 };
}

function makePage(items: ReturnType<typeof makeItem>[]) {
  return { items, total: items.length, page: 1, page_size: 20 };
}

function makeUsageSummary(overrides: Partial<{
  retrieval_count: number;
  used_in_prompt_count: number;
  effective_count: number;
  knowledge_hit_rate: number | null;
  effectiveness_rate: number | null;
  avg_confidence: number | null;
}> = {}) {
  return {
    knowledge_item_id: 'item-1',
    retrieval_count: 0,
    used_in_prompt_count: 0,
    effective_count: 0,
    knowledge_hit_rate: null,
    effectiveness_rate: null,
    avg_confidence: null,
    phase_distribution: {},
    recent_sessions: [],
    recent_tasks: [],
    ...overrides,
  };
}

function makeRevisionsPage(items: unknown[] = []) {
  return { items, total: items.length, page: 1, page_size: 10 };
}

function makeEventsPage(items: unknown[] = []) {
  return { items, total: items.length, page: 1, page_size: 10 };
}

// ── harness ───────────────────────────────────────────────────────────────────

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
  vi.clearAllMocks();
});

async function render() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/knowledge']}>
        <KnowledgeLibrary />
      </MemoryRouter>,
    );
  });
}

function setupEmptyList() {
  (knowledgeLibraryAPI.list as Mock).mockResolvedValue({ data: makeEmptyPage() });
}

function setupList(items: ReturnType<typeof makeItem>[]) {
  (knowledgeLibraryAPI.list as Mock).mockResolvedValue({ data: makePage(items) });
}

function setupDetail(item: ReturnType<typeof makeItem>) {
  (knowledgeLibraryAPI.getById as Mock).mockResolvedValue({ data: item });
}

function setupUsageSummary(summary: ReturnType<typeof makeUsageSummary>) {
  (knowledgeLibraryAPI.getUsageSummary as Mock).mockResolvedValue({ data: summary });
}

function setupRevisions(items: unknown[] = []) {
  (knowledgeLibraryAPI.getRevisions as Mock).mockResolvedValue({ data: makeRevisionsPage(items) });
}

function setupEvents(items: unknown[] = []) {
  (knowledgeLibraryAPI.getEvents as Mock).mockResolvedValue({ data: makeEventsPage(items) });
}

// ── tests ─────────────────────────────────────────────────────────────────────

describe('KnowledgeLibrary — route renders', () => {
  it('renders the Knowledge Library heading', async () => {
    setupEmptyList();
    await render();
    expect(container.textContent).toContain('Knowledge Library');
  });

  it('calls knowledgeLibraryAPI.list on mount', async () => {
    setupEmptyList();
    await render();
    expect(knowledgeLibraryAPI.list).toHaveBeenCalledTimes(1);
  });
});

describe('KnowledgeLibrary — nav item', () => {
  it('renders a Knowledge nav link in AppShell', async () => {
    const { default: AppShell } = await import('@/layouts/AppShell');
    const shellContainer = document.createElement('div');
    document.body.appendChild(shellContainer);
    const shellRoot = createRoot(shellContainer);
    await act(async () => {
      shellRoot.render(
        <MemoryRouter initialEntries={['/knowledge']}>
          <AppShell />
        </MemoryRouter>,
      );
    });
    const links = shellContainer.querySelectorAll('a[href="/knowledge"]');
    expect(links.length).toBeGreaterThan(0);
    act(() => { shellRoot.unmount(); });
    shellContainer.remove();
  });
});

describe('KnowledgeLibrary — list renders active items', () => {
  it('shows item titles in the list', async () => {
    setupList([makeItem({ title: 'My Format Guide' })]);
    await render();
    expect(container.textContent).toContain('My Format Guide');
  });

  it('shows empty state when list is empty', async () => {
    setupEmptyList();
    await render();
    expect(container.textContent).toContain('No knowledge items found');
  });

  it('renders loading skeletons while fetching', () => {
    (knowledgeLibraryAPI.list as Mock).mockReturnValue(new Promise(() => {}));
    act(() => {
      root.render(
        <MemoryRouter>
          <KnowledgeLibrary />
        </MemoryRouter>,
      );
    });
    const skeletons = container.querySelectorAll('[class*="animate-pulse"]');
    expect(skeletons.length).toBeGreaterThan(0);
  });

  it('shows active badge for active items', async () => {
    setupList([makeItem({ is_active: true })]);
    await render();
    expect(container.textContent).toContain('Active');
  });

  it('shows retired badge for inactive items', async () => {
    setupList([makeItem({ is_active: false }), makeItem({ id: 'item-2', title: 'Retired Item', is_active: false })]);
    await render();
    expect(container.textContent).toContain('Retired');
  });
});

describe('KnowledgeLibrary — selecting item loads detail', () => {
  it('shows empty state prompt before item selection', async () => {
    setupList([makeItem()]);
    await render();
    expect(container.textContent).toContain('Select a knowledge item to inspect it');
  });

  it('loads detail when item is clicked', async () => {
    const item = makeItem({ title: 'Detailed Item' });
    setupList([item]);
    setupDetail(item);
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Detailed Item')
    );
    expect(btn).toBeTruthy();

    await act(async () => { btn!.click(); });
    expect(knowledgeLibraryAPI.getById).toHaveBeenCalledWith(item.id);
  });
});

describe('KnowledgeLibrary — usage summary renders', () => {
  it('shows usage counts after clicking Usage tab', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary({ retrieval_count: 10, used_in_prompt_count: 8, effective_count: 5 }));
    (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 15 } });
    setupRevisions();
    setupEvents();
    await render();

    // Click item
    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    // Click Usage tab
    const usageTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Usage'
    );
    await act(async () => { usageTab!.click(); });

    expect(knowledgeLibraryAPI.getUsageSummary).toHaveBeenCalledWith(item.id);
    expect(container.textContent).toContain('10');
    expect(container.textContent).toContain('8');
    expect(container.textContent).toContain('5');
  });

  it('shows "No usage data" when retrieval count is 0', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary({ retrieval_count: 0 }));
    (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 15 } });
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    const usageTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Usage'
    );
    await act(async () => { usageTab!.click(); });

    expect(container.textContent).toContain('No usage data');
  });
});

describe('KnowledgeLibrary — revisions render', () => {
  it('shows "No revisions yet" when empty', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupRevisions([]);
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    const revTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Revisions'
    );
    await act(async () => { revTab!.click(); });

    expect(container.textContent).toContain('No revisions yet');
  });

  it('renders revision items', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupRevisions([{
      id: 1,
      knowledge_item_id: item.id,
      version: 2,
      previous_version: 1,
      changed_fields: ['title'],
      before_snapshot: {},
      after_snapshot: {},
      change_reason: 'Updated title',
      created_by: 'admin@example.com',
      created_at: '2026-06-01T00:00:00Z',
    }]);
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    const revTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Revisions'
    );
    await act(async () => { revTab!.click(); });

    expect(knowledgeLibraryAPI.getRevisions).toHaveBeenCalledWith(item.id, expect.any(Object));
    expect(container.textContent).toContain('v2');
    expect(container.textContent).toContain('Updated title');
  });
});

describe('KnowledgeLibrary — audit events render', () => {
  it('shows "No lifecycle events yet" when empty', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupEvents([]);
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    const evTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Audit Events'
    );
    await act(async () => { evTab!.click(); });

    expect(container.textContent).toContain('No lifecycle events yet');
  });

  it('renders event items', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupEvents([{
      id: 5,
      knowledge_item_id: item.id,
      event_type: 'retired',
      payload: null,
      actor: 'admin@example.com',
      reason: 'Outdated content',
      created_at: '2026-06-15T00:00:00Z',
    }]);
    await render();

    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { btn!.click(); });

    const evTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Audit Events'
    );
    await act(async () => { evTab!.click(); });

    expect(knowledgeLibraryAPI.getEvents).toHaveBeenCalledWith(item.id, expect.any(Object));
    expect(container.textContent).toContain('Retired');
    expect(container.textContent).toContain('Outdated content');
  });
});

describe('KnowledgeLibrary — retire action', () => {
  it('calls retire API when Retire button is clicked and refreshes item', async () => {
    const item = makeItem({ is_active: true });
    const retiredItem = { ...item, is_active: false };
    setupList([item]);
    setupDetail(item);
    (knowledgeLibraryAPI.retire as Mock).mockResolvedValue({ data: retiredItem });
    await render();

    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { listBtn!.click(); });

    const retireBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Retire')
    );
    expect(retireBtn).toBeTruthy();

    await act(async () => { retireBtn!.click(); });
    expect(knowledgeLibraryAPI.retire).toHaveBeenCalledWith(item.id);
  });
});

describe('KnowledgeLibrary — restore action', () => {
  it('calls restore API when Restore button is clicked and refreshes item', async () => {
    const item = makeItem({ is_active: false });
    const restoredItem = { ...item, is_active: true };
    setupList([item]);
    setupDetail(item);
    (knowledgeLibraryAPI.restore as Mock).mockResolvedValue({ data: restoredItem });
    await render();

    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Format Guide Item')
    );
    await act(async () => { listBtn!.click(); });

    const restoreBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Restore')
    );
    expect(restoreBtn).toBeTruthy();

    await act(async () => { restoreBtn!.click(); });
    expect(knowledgeLibraryAPI.restore).toHaveBeenCalledWith(item.id);
  });
});

describe('KnowledgeLibrary — empty states', () => {
  it('shows empty list state when no items', async () => {
    setupEmptyList();
    await render();
    expect(container.textContent).toContain('No knowledge items found');
  });

  it('shows select-item prompt on the right panel when nothing selected', async () => {
    setupList([makeItem()]);
    await render();
    expect(container.textContent).toContain('Select a knowledge item to inspect it');
  });
});

describe('KnowledgeLibrary — failed endpoint states', () => {
  it('shows error message when list fetch fails', async () => {
    (knowledgeLibraryAPI.list as Mock).mockRejectedValue(new Error('Network error'));
    await render();
    expect(container.textContent).toContain('Failed to load knowledge items');
  });
});

// ── helpers for edit tests ────────────────────────────────────────────────────

async function openDetail(item: ReturnType<typeof makeItem>) {
  setupList([item]);
  setupDetail(item);
  await render();
  const btn = Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes(item.title)
  );
  await act(async () => { btn!.click(); });
}

function getEditButton() {
  return Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes('Edit Knowledge')
  );
}

function getSaveButton() {
  return Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes('Save Changes')
  );
}

function getCancelButton() {
  return Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes('Cancel')
  );
}

function getEditFieldInput(labelStartsWith: string): HTMLInputElement | HTMLTextAreaElement | null {
  for (const label of container.querySelectorAll('label')) {
    if (label.textContent?.trim().startsWith(labelStartsWith)) {
      const parent = label.closest('div');
      return parent?.querySelector('input[type="text"], input[type="number"], textarea') as HTMLInputElement | null;
    }
  }
  return null;
}

function setInputValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, value);
  // Call React's onChange prop directly via React 19 internal props key
  const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
  if (propsKey) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (el as any)[propsKey]?.onChange?.({ target: el, currentTarget: el });
  }
}

describe('KnowledgeLibrary — edit button', () => {
  it('shows Edit Knowledge button on active items', async () => {
    const item = makeItem({ is_active: true });
    await openDetail(item);
    expect(getEditButton()).toBeTruthy();
  });

  it('shows Edit Knowledge button on retired items', async () => {
    const item = makeItem({ is_active: false });
    await openDetail(item);
    expect(getEditButton()).toBeTruthy();
  });

  it('opens edit form when Edit Knowledge is clicked', async () => {
    const item = makeItem();
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });
    expect(getSaveButton()).toBeTruthy();
    expect(getCancelButton()).toBeTruthy();
  });
});

describe('KnowledgeLibrary — edit form prefill', () => {
  it('prefills title from current item', async () => {
    const item = makeItem({ title: 'My Test Title' });
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });
    const titleInput = getEditFieldInput('Title');
    expect(titleInput?.value).toBe('My Test Title');
  });

  it('does not show immutable fields as editable (checksum, version, created_at)', async () => {
    const item = makeItem();
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });
    const inputs = Array.from(container.querySelectorAll('input, textarea'));
    const labels = Array.from(container.querySelectorAll('label')).map(l => l.textContent?.toLowerCase() ?? '');
    expect(labels.some(l => l.includes('checksum'))).toBe(false);
    expect(labels.some(l => l.includes('version'))).toBe(false);
    // id should not be editable either
    expect(inputs.filter(i => (i as HTMLInputElement).name === 'id').length).toBe(0);
  });
});

describe('KnowledgeLibrary — edit save', () => {
  it('calls PATCH with changed fields and reason on save', async () => {
    const item = makeItem({ title: 'Original Title' });
    const updated = { ...item, title: 'New Title', version: 2 };
    (knowledgeLibraryAPI.patch as Mock).mockResolvedValue({ data: updated });
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });

    await act(async () => { setInputValue(getEditFieldInput('Title')!, 'New Title'); });
    await act(async () => { setInputValue(getEditFieldInput('Reason')!, 'Fixing title'); });
    await act(async () => { getSaveButton()!.click(); });

    expect(knowledgeLibraryAPI.patch).toHaveBeenCalledWith(
      item.id,
      expect.objectContaining({ reason: 'Fixing title' })
    );
  });

  it('does not call PATCH when no fields changed', async () => {
    const item = makeItem();
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });

    // Click save without changing anything
    await act(async () => { getSaveButton()!.click(); });

    expect(knowledgeLibraryAPI.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain('No changes to save');
  });

  it('requires reason field — shows error when missing', async () => {
    const item = makeItem({ title: 'Original' });
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });

    await act(async () => { setInputValue(getEditFieldInput('Title')!, 'Changed Title'); });
    await act(async () => { getSaveButton()!.click(); });

    expect(knowledgeLibraryAPI.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain('Reason for change is required');
  });

  it('exits edit mode and shows success after save', async () => {
    const item = makeItem({ title: 'Old' });
    const updated = { ...item, title: 'New', version: 2 };
    (knowledgeLibraryAPI.patch as Mock).mockResolvedValue({ data: updated });
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });

    await act(async () => { setInputValue(getEditFieldInput('Title')!, 'New'); });
    await act(async () => { setInputValue(getEditFieldInput('Reason')!, 'Updated'); });
    await act(async () => { getSaveButton()!.click(); });

    expect(getSaveButton()).toBeUndefined();
    expect(container.textContent).toContain('Changes saved');
  });

  it('shows API error message on PATCH failure', async () => {
    const item = makeItem({ title: 'A' });
    (knowledgeLibraryAPI.patch as Mock).mockRejectedValue({
      response: { data: { detail: 'Immutable field rejected.' } },
    });
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });

    await act(async () => { setInputValue(getEditFieldInput('Title')!, 'B'); });
    await act(async () => { setInputValue(getEditFieldInput('Reason')!, 'Reason'); });
    await act(async () => { getSaveButton()!.click(); });

    expect(container.textContent).toContain('Immutable field rejected.');
  });
});

describe('KnowledgeLibrary — edit cancel', () => {
  it('exits edit mode on cancel without saving', async () => {
    const item = makeItem();
    await openDetail(item);
    await act(async () => { getEditButton()!.click(); });
    expect(getSaveButton()).toBeTruthy();

    await act(async () => { getCancelButton()!.click(); });

    expect(getSaveButton()).toBeUndefined();
    expect(knowledgeLibraryAPI.patch).not.toHaveBeenCalled();
  });
});

// ── helpers for URL param tests ───────────────────────────────────────────────

async function renderWithParams(search: string) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/knowledge${search}`]}>
        <KnowledgeLibrary />
      </MemoryRouter>,
    );
  });
}

describe('KnowledgeLibrary — URL param ?item= loads item directly', () => {
  it('calls getById with the item id from ?item= param on mount', async () => {
    const item = makeItem({ id: 'abc-123' });
    setupList([item]);
    setupDetail(item);
    await renderWithParams('?item=abc-123');
    expect(knowledgeLibraryAPI.getById).toHaveBeenCalledWith('abc-123');
  });

  it('shows the detail panel for the item from URL param', async () => {
    const item = makeItem({ id: 'abc-123', title: 'URL Selected Item' });
    setupList([item]);
    setupDetail(item);
    await renderWithParams('?item=abc-123');
    expect(container.textContent).toContain('URL Selected Item');
  });

  it('shows "Item not found" when ?item= ID does not resolve', async () => {
    setupList([]);
    (knowledgeLibraryAPI.getById as Mock).mockRejectedValue(new Error('Not found'));
    await renderWithParams('?item=nonexistent-id');
    expect(container.textContent).toContain('Item not found');
  });

  it('does not show "Item not found" when no item param', async () => {
    setupEmptyList();
    await render();
    expect(container.textContent).not.toContain('Item not found');
  });
});

describe('KnowledgeLibrary — decision context banner', () => {
  it('shows decision banner when ?source=decision is in URL', async () => {
    const item = makeItem({ id: 'abc-123' });
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary());
    (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 15 } });
    await renderWithParams('?item=abc-123&source=decision');
    expect(container.textContent).toContain('Opened from Decision Intelligence');
    expect(container.textContent).toContain('improvement opportunity');
  });

  it('does not show decision banner without ?source=decision', async () => {
    const item = makeItem({ id: 'abc-123' });
    setupList([item]);
    setupDetail(item);
    await renderWithParams('?item=abc-123');
    expect(container.textContent).not.toContain('Opened from Decision Intelligence');
  });

  it('does not show decision banner when opened without params', async () => {
    setupEmptyList();
    await render();
    expect(container.textContent).not.toContain('Opened from Decision Intelligence');
  });

  it('shows action hint when ?source=decision and item is loaded', async () => {
    const item = makeItem({ id: 'abc-123' });
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary());
    (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 15 } });
    await renderWithParams('?item=abc-123&source=decision');
    expect(container.textContent).toContain('Recommended actions');
  });

  it('does not show action hint without source=decision', async () => {
    const item = makeItem({ id: 'abc-123' });
    setupList([item]);
    setupDetail(item);
    await renderWithParams('?item=abc-123');
    expect(container.textContent).not.toContain('Recommended actions');
  });
});

describe('KnowledgeLibrary — direct nav behavior unchanged', () => {
  it('shows empty detail panel when opened with no params', async () => {
    setupList([makeItem()]);
    await render();
    expect(container.textContent).toContain('Select a knowledge item to inspect it');
  });

  it('allows manual item selection after direct nav', async () => {
    const item = makeItem({ title: 'Manual Pick' });
    setupList([item]);
    setupDetail(item);
    await render();
    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Manual Pick')
    );
    await act(async () => { btn!.click(); });
    expect(knowledgeLibraryAPI.getById).toHaveBeenCalledWith(item.id);
  });
});

describe('KnowledgeLibrary — edit/retire/restore work after URL selection', () => {
  it('retire works after item loaded from URL param', async () => {
    const item = makeItem({ id: 'url-item', is_active: true });
    const retired = { ...item, is_active: false };
    setupList([item]);
    setupDetail(item);
    (knowledgeLibraryAPI.retire as Mock).mockResolvedValue({ data: retired });
    await renderWithParams('?item=url-item');

    const retireBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Retire')
    );
    expect(retireBtn).toBeTruthy();
    await act(async () => { retireBtn!.click(); });
    expect(knowledgeLibraryAPI.retire).toHaveBeenCalledWith(item.id);
  });

  it('restore works after item loaded from URL param', async () => {
    const item = makeItem({ id: 'url-item', is_active: false });
    const restored = { ...item, is_active: true };
    setupList([item]);
    setupDetail(item);
    (knowledgeLibraryAPI.restore as Mock).mockResolvedValue({ data: restored });
    await renderWithParams('?item=url-item');

    const restoreBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes('Restore')
    );
    expect(restoreBtn).toBeTruthy();
    await act(async () => { restoreBtn!.click(); });
    expect(knowledgeLibraryAPI.restore).toHaveBeenCalledWith(item.id);
  });
});

// ── usage drilldown fixtures ──────────────────────────────────────────────────

function makeUsageRecord(overrides: Partial<{
  id: string;
  session_id: number;
  task_id: number | null;
  trigger_phase: string;
  retrieval_reason: string;
  retrieval_query: string | null;
  confidence: number;
  rank: number;
  used_in_prompt: boolean;
  was_effective: boolean | null;
  created_at: string | null;
}> = {}) {
  return {
    id: 'rec-1',
    session_id: 42,
    task_id: 7,
    trigger_phase: 'planning',
    retrieval_reason: 'Matched format guide query',
    retrieval_query: 'how to format output',
    confidence: 0.85,
    rank: 1,
    used_in_prompt: true,
    was_effective: true,
    created_at: '2026-06-01T10:00:00Z',
    ...overrides,
  };
}

function makeUsageLogPage(items: ReturnType<typeof makeUsageRecord>[], total?: number) {
  return { items, total: total ?? items.length, page: 1, page_size: 15 };
}

function setupUsageList(items: ReturnType<typeof makeUsageRecord>[], total?: number) {
  (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({
    data: makeUsageLogPage(items, total),
  });
}

async function openUsageTab(item: ReturnType<typeof makeItem>) {
  setupList([item]);
  setupDetail(item);
  setupUsageSummary(makeUsageSummary());
  await render();
  const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes(item.title)
  );
  await act(async () => { listBtn!.click(); });
  const usageTab = Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.trim().startsWith('Usage')
  );
  await act(async () => { usageTab!.click(); });
}

function setSelectValue(el: HTMLSelectElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(el, value);
  const propsKey = Object.keys(el).find(k => k.startsWith('__reactProps$'));
  if (propsKey) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (el as any)[propsKey]?.onChange?.({ target: el, currentTarget: el });
  }
}

// ── usage drilldown tests ─────────────────────────────────────────────────────

describe('KnowledgeLibrary — usage drilldown loads', () => {
  it('calls getUsageList when usage tab is clicked', async () => {
    setupUsageList([]);
    const item = makeItem();
    await openUsageTab(item);
    expect(knowledgeLibraryAPI.getUsageList).toHaveBeenCalledWith(item.id, expect.objectContaining({ page: 1 }));
  });

  it('shows "No usage records" empty state when list is empty', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('No usage records for this item yet');
  });
});

describe('KnowledgeLibrary — usage drilldown table', () => {
  it('renders record rows with phase, session, task, confidence, rank', async () => {
    const rec = makeUsageRecord({ session_id: 99, task_id: 5, trigger_phase: 'execution', confidence: 0.92, rank: 2 });
    setupUsageList([rec]);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('execution');
    expect(container.textContent).toContain('99');
    expect(container.textContent).toContain('5');
    expect(container.textContent).toContain('0.92');
  });

  it('renders session ID as a link to /sessions/{id}', async () => {
    const rec = makeUsageRecord({ session_id: 42 });
    setupUsageList([rec]);
    await openUsageTab(makeItem());
    const link = container.querySelector('a[href="/sessions/42"]');
    expect(link).not.toBeNull();
  });

  it('shows Yes badge for used_in_prompt=true records', async () => {
    setupUsageList([makeUsageRecord({ used_in_prompt: true })]);
    await openUsageTab(makeItem());
    const cells = Array.from(container.querySelectorAll('td'));
    expect(cells.some(c => c.textContent?.trim() === 'Yes')).toBe(true);
  });

  it('shows No badge for was_effective=false records', async () => {
    setupUsageList([makeUsageRecord({ was_effective: false, used_in_prompt: false })]);
    await openUsageTab(makeItem());
    const cells = Array.from(container.querySelectorAll('td'));
    expect(cells.some(c => c.textContent?.trim() === 'No')).toBe(true);
  });

  it('shows — for was_effective=null records', async () => {
    setupUsageList([makeUsageRecord({ was_effective: null })]);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('—');
  });

  it('shows retrieval_reason in table', async () => {
    setupUsageList([makeUsageRecord({ retrieval_reason: 'Matched special pattern' })]);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('Matched special pattern');
  });

  it('shows — for task_id=null records', async () => {
    setupUsageList([makeUsageRecord({ task_id: null })]);
    await openUsageTab(makeItem());
    const cells = Array.from(container.querySelectorAll('td'));
    expect(cells.some(c => c.textContent?.trim() === '—')).toBe(true);
  });
});

describe('KnowledgeLibrary — usage drilldown pagination', () => {
  it('shows pagination controls when total exceeds page size', async () => {
    const recs = Array.from({ length: 15 }, (_, i) =>
      makeUsageRecord({ id: `rec-${i}`, session_id: i + 1 })
    );
    setupUsageList(recs, 30);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('Page 1 of 2');
  });

  it('shows total record count when single page', async () => {
    setupUsageList([makeUsageRecord()]);
    await openUsageTab(makeItem());
    expect(container.textContent).toContain('1 record');
  });
});

describe('KnowledgeLibrary — usage drilldown filters', () => {
  it('renders all filter inputs', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    expect(container.querySelector('[aria-label="Filter by phase"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by used in prompt"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by effective"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by session ID"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by task ID"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by created after"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Filter by created before"]')).not.toBeNull();
  });

  it('calls getUsageList with trigger_phase when phase filter is set', async () => {
    setupUsageList([]);
    const item = makeItem();
    await openUsageTab(item);
    const phaseInput = container.querySelector('[aria-label="Filter by phase"]') as HTMLInputElement;
    await act(async () => { setInputValue(phaseInput, 'execution'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ trigger_phase: 'execution' });
  });

  it('calls getUsageList with used_in_prompt=true when Yes selected', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const sel = container.querySelector('[aria-label="Filter by used in prompt"]') as HTMLSelectElement;
    await act(async () => { setSelectValue(sel, 'true'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ used_in_prompt: true });
  });

  it('calls getUsageList with was_effective=false when No selected', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const sel = container.querySelector('[aria-label="Filter by effective"]') as HTMLSelectElement;
    await act(async () => { setSelectValue(sel, 'false'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ was_effective: false });
  });

  it('calls getUsageList with session_id when session filter is set', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const sessionInput = container.querySelector('[aria-label="Filter by session ID"]') as HTMLInputElement;
    await act(async () => { setInputValue(sessionInput, '42'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ session_id: 42 });
  });

  it('calls getUsageList with task_id when task filter is set', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const taskInput = container.querySelector('[aria-label="Filter by task ID"]') as HTMLInputElement;
    await act(async () => { setInputValue(taskInput, '7'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ task_id: 7 });
  });

  it('calls getUsageList with created_after date filter', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const afterInput = container.querySelector('[aria-label="Filter by created after"]') as HTMLInputElement;
    await act(async () => { setInputValue(afterInput, '2026-01-01'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ created_after: '2026-01-01' });
  });

  it('calls getUsageList with created_before date filter', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const beforeInput = container.querySelector('[aria-label="Filter by created before"]') as HTMLInputElement;
    await act(async () => { setInputValue(beforeInput, '2026-06-30'); });
    const calls = (knowledgeLibraryAPI.getUsageList as Mock).mock.calls;
    expect(calls[calls.length - 1][1]).toMatchObject({ created_before: '2026-06-30' });
  });

  it('shows "No records matching current filters" when filtered result is empty', async () => {
    setupUsageList([]);
    await openUsageTab(makeItem());
    const phaseInput = container.querySelector('[aria-label="Filter by phase"]') as HTMLInputElement;
    (knowledgeLibraryAPI.getUsageList as Mock).mockResolvedValue({ data: makeUsageLogPage([]) });
    await act(async () => { setInputValue(phaseInput, 'nonexistent_phase'); });
    expect(container.textContent).toContain('No records matching current filters');
  });
});

describe('KnowledgeLibrary — decision context activates usage tab', () => {
  it('auto-activates usage tab when opened with ?source=decision', async () => {
    const item = makeItem({ id: 'dec-item' });
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary());
    setupUsageList([]);
    await renderWithParams('?item=dec-item&source=decision');
    expect(knowledgeLibraryAPI.getUsageList).toHaveBeenCalledWith('dec-item', expect.any(Object));
  });

  it('does not call getUsageList when no fromDecision and usage tab not clicked', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupUsageSummary(makeUsageSummary());
    await render();
    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });
    expect(knowledgeLibraryAPI.getUsageList).not.toHaveBeenCalled();
  });
});

// ── sync helpers ──────────────────────────────────────────────────────────────

async function openDetailItem(item: ReturnType<typeof makeItem>) {
  setupList([item]);
  setupDetail(item);
  setupRevisions();
  setupEvents();
  await render();
  const btn = Array.from(container.querySelectorAll('button')).find(b =>
    b.textContent?.includes(item.title)
  );
  await act(async () => { btn!.click(); });
}

function getSyncNowButton() {
  return Array.from(container.querySelectorAll('button')).find(b =>
    b.getAttribute('aria-label') === 'Sync Now'
  );
}

// ── sync badge in list ────────────────────────────────────────────────────────

describe('KnowledgeLibrary — sync badge in list', () => {
  it('shows Dirty badge in list for dirty items on initial load', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    setupList([item]);
    await render();
    // Verify badge appears without needing to click/select the item
    expect(container.textContent).toContain('Dirty');
  });

  it('shows Failed badge in list for failed items on initial load', async () => {
    const item = makeItem({ sync_status: 'failed' });
    setupList([item]);
    await render();
    expect(container.textContent).toContain('Failed');
  });

  it('shows Syncing badge in list for syncing items on initial load', async () => {
    const item = makeItem({ sync_status: 'syncing' });
    setupList([item]);
    await render();
    expect(container.textContent).toContain('Syncing');
  });

  it('does not show sync badge in list for synced items', async () => {
    const item = makeItem({ sync_status: 'synced' });
    setupList([item]);
    await render();
    // The list should not render a sync badge for synced (clean state)
    const badges = Array.from(container.querySelectorAll('span')).filter(s =>
      s.textContent?.trim() === 'Synced' && s.className?.includes('emerald')
    );
    expect(badges.length).toBe(0);
  });

  it('shows dirty badge in list without requiring item selection', async () => {
    const item = makeItem({ sync_status: 'dirty', title: 'Dirty Knowledge Item' });
    setupList([item]);
    await render();
    // Item is not selected — dirty badge should still be visible in the list row
    expect(container.querySelector('[class*="amber"]')).not.toBeNull();
  });
});

// ── sync status in detail ─────────────────────────────────────────────────────

describe('KnowledgeLibrary — sync status in detail panel', () => {
  it('shows Synced badge in detail header for synced items', async () => {
    const item = makeItem({ sync_status: 'synced' });
    await openDetailItem(item);
    const spans = Array.from(container.querySelectorAll('span')).filter(s =>
      s.textContent?.trim() === 'Synced'
    );
    expect(spans.length).toBeGreaterThan(0);
  });

  it('shows Dirty badge in detail header for dirty items', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    await openDetailItem(item);
    const spans = Array.from(container.querySelectorAll('span')).filter(s =>
      s.textContent?.trim() === 'Dirty'
    );
    expect(spans.length).toBeGreaterThan(0);
  });

  it('shows sync metadata section in Metadata tab', async () => {
    const item = makeItem({
      sync_status: 'synced',
      last_synced_at: '2026-06-15T00:00:00Z',
    });
    await openDetailItem(item);
    expect(container.textContent).toContain('Sync Status');
    expect(container.textContent).toContain('Last Synced');
  });
});

// ── dirty warning ─────────────────────────────────────────────────────────────

describe('KnowledgeLibrary — dirty warning', () => {
  it('shows dirty warning when sync_status is dirty', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    await openDetailItem(item);
    expect(container.textContent).toContain('unsynchronized changes');
    expect(container.textContent).toContain('stale embeddings');
  });

  it('does not show dirty warning when sync_status is synced', async () => {
    const item = makeItem({ sync_status: 'synced' });
    await openDetailItem(item);
    expect(container.textContent).not.toContain('unsynchronized changes');
  });
});

// ── failed error ──────────────────────────────────────────────────────────────

describe('KnowledgeLibrary — failed error', () => {
  it('shows failed error message when sync_status is failed', async () => {
    const item = makeItem({ sync_status: 'failed', last_sync_error: 'Qdrant timeout' });
    await openDetailItem(item);
    expect(container.textContent).toContain('Last sync failed');
    expect(container.textContent).toContain('Qdrant timeout');
  });

  it('shows generic failed message without last_sync_error', async () => {
    const item = makeItem({ sync_status: 'failed', last_sync_error: null });
    await openDetailItem(item);
    expect(container.textContent).toContain('Last sync failed');
  });

  it('does not show failed error when sync_status is synced', async () => {
    const item = makeItem({ sync_status: 'synced' });
    await openDetailItem(item);
    expect(container.textContent).not.toContain('Last sync failed');
  });
});

// ── Sync Now visibility ───────────────────────────────────────────────────────

describe('KnowledgeLibrary — Sync Now button visibility', () => {
  it('shows Sync Now for dirty items', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    await openDetailItem(item);
    expect(getSyncNowButton()).toBeTruthy();
  });

  it('shows Sync Now for failed items', async () => {
    const item = makeItem({ sync_status: 'failed' });
    await openDetailItem(item);
    expect(getSyncNowButton()).toBeTruthy();
  });

  it('hides Sync Now for synced items', async () => {
    const item = makeItem({ sync_status: 'synced' });
    await openDetailItem(item);
    expect(getSyncNowButton()).toBeUndefined();
  });

  it('shows disabled Syncing button when sync_status is syncing', async () => {
    const item = makeItem({ sync_status: 'syncing' });
    await openDetailItem(item);
    const btn = getSyncNowButton();
    expect(btn).toBeTruthy();
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(btn?.textContent).toContain('Syncing');
  });
});

// ── Sync Now action ───────────────────────────────────────────────────────────

describe('KnowledgeLibrary — Sync Now action', () => {
  it('calls sync API when Sync Now is clicked', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    const synced = { ...item, sync_status: 'synced' as const };
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents();
    (knowledgeLibraryAPI.sync as Mock).mockResolvedValue({ data: synced });
    await render();
    const btn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { btn!.click(); });

    const syncBtn = getSyncNowButton();
    expect(syncBtn).toBeTruthy();
    await act(async () => { syncBtn!.click(); });

    expect(knowledgeLibraryAPI.sync).toHaveBeenCalledWith(item.id);
  });

  it('shows success message and refreshes detail after sync succeeds', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    const synced = { ...item, sync_status: 'synced' as const };
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents();
    (knowledgeLibraryAPI.sync as Mock).mockResolvedValue({ data: synced });
    await render();
    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });
    await act(async () => { getSyncNowButton()!.click(); });

    expect(container.textContent).toContain('Sync completed');
    // unsynchronized changes warning should be gone (item refreshed to synced)
    expect(container.textContent).not.toContain('unsynchronized changes');
  });

  it('shows error message when sync fails', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    const failed = { ...item, sync_status: 'failed' as const, last_sync_error: 'Qdrant unavailable' };
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents();
    (knowledgeLibraryAPI.sync as Mock).mockRejectedValue(new Error('503'));
    (knowledgeLibraryAPI.getById as Mock)
      .mockResolvedValueOnce({ data: item })
      .mockResolvedValueOnce({ data: failed });
    await render();
    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });
    await act(async () => { getSyncNowButton()!.click(); });

    expect(container.textContent).toContain('Sync failed');
  });

  it('refreshes audit events after sync succeeds', async () => {
    const item = makeItem({ sync_status: 'dirty' });
    const synced = { ...item, sync_status: 'synced' as const };
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents();
    (knowledgeLibraryAPI.sync as Mock).mockResolvedValue({ data: synced });
    await render();
    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });
    await act(async () => { getSyncNowButton()!.click(); });

    // Navigate to Audit Events tab to trigger reload with new key
    const evTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Audit Events'
    );
    await act(async () => { evTab!.click(); });
    expect(knowledgeLibraryAPI.getEvents).toHaveBeenCalled();
  });
});

// ── edit result shows dirty state ─────────────────────────────────────────────

describe('KnowledgeLibrary — edit result shows dirty state', () => {
  it('shows dirty warning after successful edit returns dirty sync_status', async () => {
    const item = makeItem({ sync_status: 'synced', title: 'Original' });
    const updated = { ...item, title: 'Updated', sync_status: 'dirty' as const };
    (knowledgeLibraryAPI.patch as Mock).mockResolvedValue({ data: updated });
    await openDetailItem(item);

    await act(async () => { getEditButton()!.click(); });
    await act(async () => { setInputValue(getEditFieldInput('Title')!, 'Updated'); });
    await act(async () => { setInputValue(getEditFieldInput('Reason')!, 'Changed title'); });
    await act(async () => { getSaveButton()!.click(); });

    expect(container.textContent).toContain('unsynchronized changes');
  });
});

// ── audit event labels ────────────────────────────────────────────────────────

describe('KnowledgeLibrary — audit event labels for sync events', () => {
  it('shows "Synced" label for synced events', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents([{
      id: 10,
      knowledge_item_id: item.id,
      event_type: 'synced',
      payload: null,
      actor: null,
      reason: null,
      created_at: '2026-06-20T00:00:00Z',
    }]);
    await render();

    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });

    const evTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Audit Events'
    );
    await act(async () => { evTab!.click(); });

    expect(container.textContent).toContain('Synced');
  });

  it('shows "Sync Failed" label for sync_failed events', async () => {
    const item = makeItem();
    setupList([item]);
    setupDetail(item);
    setupRevisions();
    setupEvents([{
      id: 11,
      knowledge_item_id: item.id,
      event_type: 'sync_failed',
      payload: null,
      actor: null,
      reason: 'Qdrant timeout',
      created_at: '2026-06-20T00:00:00Z',
    }]);
    await render();

    const listBtn = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.includes(item.title)
    );
    await act(async () => { listBtn!.click(); });

    const evTab = Array.from(container.querySelectorAll('button')).find(b =>
      b.textContent?.trim() === 'Audit Events'
    );
    await act(async () => { evTab!.click(); });

    expect(container.textContent).toContain('Sync Failed');
    expect(container.textContent).toContain('Qdrant timeout');
  });
});
