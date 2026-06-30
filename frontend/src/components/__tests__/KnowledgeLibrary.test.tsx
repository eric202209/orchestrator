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
    getRevisions: vi.fn(),
    getEvents: vi.fn(),
    patch: vi.fn(),
    retire: vi.fn(),
    restore: vi.fn(),
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
    expect(container.textContent).toContain('retired');
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
