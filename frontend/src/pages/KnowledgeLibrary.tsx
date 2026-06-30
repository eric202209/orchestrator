import { useState, useEffect, useCallback } from 'react';
import { BookOpen, Search, ChevronRight, RefreshCw, Archive, RotateCcw, Pencil, X } from 'lucide-react';
import { knowledgeLibraryAPI } from '@/api/client';
import type {
  KnowledgeLibraryItem,
  KnowledgeUpdatePayload,
  KnowledgeUsageSummary,
  KnowledgeRevision,
  KnowledgeLifecycleEvent,
} from '@/types/api';
import { cn } from '@/lib/utils';

// ── helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

function fmtConf(v: number | null | undefined): string {
  if (v == null) return '—';
  return v.toFixed(2);
}

const TYPE_LABELS: Record<string, string> = {
  format_guide: 'Format Guide',
  debug_case: 'Debug Case',
  tool_guide: 'Tool Guide',
  workflow_guide: 'Workflow Guide',
  project_context: 'Project Context',
  failure_pattern: 'Failure Pattern',
};

function typeLabel(t: string): string {
  return TYPE_LABELS[t] ?? t.split('_').map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
}

// ── sub-components ─────────────────────────────────────────────────────────────

function Badge({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium', className)}>
      {children}
    </span>
  );
}

function ActiveBadge({ isActive }: { isActive: boolean }) {
  return isActive ? (
    <Badge className="bg-emerald-500/15 text-emerald-300">Active</Badge>
  ) : (
    <Badge className="bg-slate-500/10 text-slate-400">Retired</Badge>
  );
}

function SectionHeader({ title }: { title: string }) {
  return (
    <h3 className="mb-3 text-xs font-semibold uppercase tracking-widest text-slate-500">{title}</h3>
  );
}

function MetaRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5 border-b border-[color:var(--oc-border-soft)] last:border-0">
      <span className="w-32 flex-shrink-0 text-xs text-slate-500">{label}</span>
      <span className="min-w-0 flex-1 text-xs text-slate-200 break-words">{value}</span>
    </div>
  );
}

// ── UsageSummaryPanel ────────────────────────────────────────────────────────

function UsageSummaryPanel({ itemId }: { itemId: string }) {
  const [summary, setSummary] = useState<KnowledgeUsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    knowledgeLibraryAPI.getUsageSummary(itemId)
      .then(r => setSummary(r.data))
      .catch(() => setError('Failed to load usage summary.'))
      .finally(() => setLoading(false));
  }, [itemId]);

  if (loading) return <p className="py-4 text-xs text-slate-500">Loading usage summary…</p>;
  if (error) return <p className="py-4 text-xs text-red-400">{error}</p>;
  if (!summary) return null;

  const phases = Object.entries(summary.phase_distribution);

  return (
    <div>
      <div className="grid grid-cols-3 gap-3 mb-4">
        {[
          { label: 'Retrievals', value: summary.retrieval_count },
          { label: 'Used in Prompt', value: summary.used_in_prompt_count },
          { label: 'Effective', value: summary.effective_count },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-md bg-[color:var(--oc-surface)] border border-[color:var(--oc-border-soft)] p-3 text-center">
            <div className="text-lg font-semibold text-slate-100">{value}</div>
            <div className="text-[11px] text-slate-500 mt-0.5">{label}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-3 gap-3 mb-4">
        {[
          { label: 'Hit Rate', value: fmtPct(summary.knowledge_hit_rate) },
          { label: 'Effectiveness', value: fmtPct(summary.effectiveness_rate) },
          { label: 'Avg Confidence', value: fmtConf(summary.avg_confidence) },
        ].map(({ label, value }) => (
          <div key={label} className="rounded-md bg-[color:var(--oc-surface)] border border-[color:var(--oc-border-soft)] p-3 text-center">
            <div className="text-base font-semibold text-slate-100">{value}</div>
            <div className="text-[11px] text-slate-500 mt-0.5">{label}</div>
          </div>
        ))}
      </div>

      {phases.length > 0 && (
        <div className="mb-4">
          <p className="mb-2 text-xs font-medium text-slate-400">Phase Distribution</p>
          <div className="space-y-1">
            {phases.sort((a, b) => b[1] - a[1]).map(([phase, count]) => (
              <div key={phase} className="flex items-center gap-2">
                <span className="w-32 truncate text-[11px] text-slate-400">{phase}</span>
                <div className="flex-1 h-1.5 rounded-full bg-[color:var(--oc-surface)]">
                  <div
                    className="h-1.5 rounded-full bg-[color:var(--oc-accent)]"
                    style={{ width: `${Math.round((count / summary.retrieval_count) * 100)}%` }}
                  />
                </div>
                <span className="w-6 text-right text-[11px] text-slate-400">{count}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {summary.recent_sessions.length > 0 && (
        <div className="mb-2">
          <p className="mb-1 text-xs font-medium text-slate-400">Recent Sessions</p>
          <p className="text-xs text-slate-300">{summary.recent_sessions.join(', ')}</p>
        </div>
      )}

      {summary.recent_tasks.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-medium text-slate-400">Recent Tasks</p>
          <p className="text-xs text-slate-300">{summary.recent_tasks.join(', ')}</p>
        </div>
      )}

      {summary.retrieval_count === 0 && (
        <p className="text-xs text-slate-500">No usage data for this item yet.</p>
      )}
    </div>
  );
}

// ── RevisionsPanel ───────────────────────────────────────────────────────────

function RevisionsPanel({ itemId }: { itemId: string }) {
  const [items, setItems] = useState<KnowledgeRevision[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback((p: number) => {
    setLoading(true);
    setError(null);
    knowledgeLibraryAPI.getRevisions(itemId, { page: p, page_size: 10 })
      .then(r => { setItems(r.data.items); setTotal(r.data.total); })
      .catch(() => setError('Failed to load revisions.'))
      .finally(() => setLoading(false));
  }, [itemId]);

  useEffect(() => { setPage(1); load(1); }, [load]);

  if (loading) return <p className="py-4 text-xs text-slate-500">Loading revisions…</p>;
  if (error) return <p className="py-4 text-xs text-red-400">{error}</p>;
  if (items.length === 0) return <p className="py-4 text-xs text-slate-500">No revisions yet.</p>;

  const totalPages = Math.ceil(total / 10);

  return (
    <div>
      <div className="space-y-2">
        {items.map(rev => (
          <div key={rev.id} className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-3">
            <div className="flex items-center justify-between mb-1.5">
              <span className="text-xs font-medium text-slate-200">
                v{rev.version} <span className="text-slate-500">← v{rev.previous_version}</span>
              </span>
              <span className="text-[11px] text-slate-500">{fmtDate(rev.created_at)}</span>
            </div>
            <div className="flex flex-wrap gap-1 mb-1.5">
              {rev.changed_fields.map(f => (
                <Badge key={f} className="bg-blue-500/10 text-blue-300">{f}</Badge>
              ))}
            </div>
            {rev.change_reason && (
              <p className="text-[11px] text-slate-400 italic">{rev.change_reason}</p>
            )}
            {rev.created_by && (
              <p className="text-[11px] text-slate-500 mt-1">by {rev.created_by}</p>
            )}
          </div>
        ))}
      </div>
      {totalPages > 1 && (
        <div className="mt-3 flex items-center justify-between">
          <button
            disabled={page <= 1}
            onClick={() => { const p = page - 1; setPage(p); load(p); }}
            className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >← Previous</button>
          <span className="text-xs text-slate-500">Page {page} of {totalPages}</span>
          <button
            disabled={page >= totalPages}
            onClick={() => { const p = page + 1; setPage(p); load(p); }}
            className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >Next →</button>
        </div>
      )}
    </div>
  );
}

// ── AuditEventsPanel ─────────────────────────────────────────────────────────

const EVENT_COLORS: Record<string, string> = {
  updated: 'text-blue-300',
  retired: 'text-amber-300',
  restored: 'text-emerald-300',
};

function AuditEventsPanel({ itemId }: { itemId: string }) {
  const [items, setItems] = useState<KnowledgeLifecycleEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback((p: number) => {
    setLoading(true);
    setError(null);
    knowledgeLibraryAPI.getEvents(itemId, { page: p, page_size: 10 })
      .then(r => { setItems(r.data.items); setTotal(r.data.total); })
      .catch(() => setError('Failed to load events.'))
      .finally(() => setLoading(false));
  }, [itemId]);

  useEffect(() => { setPage(1); load(1); }, [load]);

  if (loading) return <p className="py-4 text-xs text-slate-500">Loading events…</p>;
  if (error) return <p className="py-4 text-xs text-red-400">{error}</p>;
  if (items.length === 0) return <p className="py-4 text-xs text-slate-500">No lifecycle events yet.</p>;

  const totalPages = Math.ceil(total / 10);

  return (
    <div>
      <div className="space-y-2">
        {items.map(ev => (
          <div key={ev.id} className="flex items-start gap-3 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-3">
            <span className={cn('mt-0.5 text-xs font-medium capitalize w-16 flex-shrink-0', EVENT_COLORS[ev.event_type] ?? 'text-slate-300')}>
              {ev.event_type}
            </span>
            <div className="min-w-0 flex-1">
              {ev.actor && <p className="text-[11px] text-slate-400">by {ev.actor}</p>}
              {ev.reason && <p className="text-[11px] text-slate-300 italic">{ev.reason}</p>}
            </div>
            <span className="flex-shrink-0 text-[11px] text-slate-500">{fmtDate(ev.created_at)}</span>
          </div>
        ))}
      </div>
      {totalPages > 1 && (
        <div className="mt-3 flex items-center justify-between">
          <button
            disabled={page <= 1}
            onClick={() => { const p = page - 1; setPage(p); load(p); }}
            className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >← Previous</button>
          <span className="text-xs text-slate-500">Page {page} of {totalPages}</span>
          <button
            disabled={page >= totalPages}
            onClick={() => { const p = page + 1; setPage(p); load(p); }}
            className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
          >Next →</button>
        </div>
      )}
    </div>
  );
}

// ── EditForm ─────────────────────────────────────────────────────────────────

interface EditFormProps {
  item: KnowledgeLibraryItem;
  onSave: (updated: KnowledgeLibraryItem) => void;
  onCancel: () => void;
}

function EditForm({ item, onSave, onCancel }: EditFormProps) {
  const [title, setTitle] = useState(item.title);
  const [content, setContent] = useState(item.content);
  const [knowledgeType, setKnowledgeType] = useState(item.knowledge_type);
  const [tags, setTags] = useState(
    Array.isArray(item.tags) ? item.tags.map(String).join(', ') : ''
  );
  const [priority, setPriority] = useState(String(item.priority));
  const [appliesTo, setAppliesTo] = useState(
    Array.isArray(item.applies_to) ? item.applies_to.map(String).join(', ') : ''
  );
  const [toolName, setToolName] = useState(item.tool_name ?? '');
  const [failureSignature, setFailureSignature] = useState(item.failure_signature ?? '');
  const [reason, setReason] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [noChange, setNoChange] = useState(false);

  const handleSave = async () => {
    setError(null);
    setNoChange(false);

    const tagsArray = tags.trim() ? tags.split(',').map(t => t.trim()).filter(Boolean) : [];
    const appliesToArray = appliesTo.trim() ? appliesTo.split(',').map(t => t.trim()).filter(Boolean) : [];
    const priorityNum = parseInt(priority, 10);
    const origTags = Array.isArray(item.tags) ? item.tags.map(String) : [];
    const origAppliesTo = Array.isArray(item.applies_to) ? item.applies_to.map(String) : [];

    const changed: Omit<KnowledgeUpdatePayload, 'reason'> = {};
    if (title !== item.title) changed.title = title;
    if (content !== item.content) changed.content = content;
    if (knowledgeType !== item.knowledge_type) changed.knowledge_type = knowledgeType;
    if (JSON.stringify(tagsArray) !== JSON.stringify(origTags)) changed.tags = tagsArray;
    if (!isNaN(priorityNum) && priorityNum !== item.priority) changed.priority = priorityNum;
    if (JSON.stringify(appliesToArray) !== JSON.stringify(origAppliesTo)) changed.applies_to = appliesToArray;
    const toolNameVal = toolName.trim() || null;
    if (toolNameVal !== item.tool_name) changed.tool_name = toolNameVal;
    const failureSigVal = failureSignature.trim() || null;
    if (failureSigVal !== item.failure_signature) changed.failure_signature = failureSigVal;

    if (Object.keys(changed).length === 0) {
      setNoChange(true);
      return;
    }

    if (!reason.trim()) {
      setError('Reason for change is required.');
      return;
    }

    const payload: KnowledgeUpdatePayload = { ...changed, reason: reason.trim() };

    setSaving(true);
    try {
      const r = await knowledgeLibraryAPI.patch(item.id, payload);
      onSave(r.data);
    } catch (e: unknown) {
      const axiosErr = e as { response?: { data?: { detail?: unknown } } };
      const detail = axiosErr?.response?.data?.detail;
      if (typeof detail === 'string') {
        setError(detail);
      } else {
        setError('Failed to save changes.');
      }
    } finally {
      setSaving(false);
    }
  };

  const inputCls = 'w-full rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-[color:var(--oc-border)]';
  const labelCls = 'block text-xs font-medium text-slate-400 mb-1';

  return (
    <div className="space-y-3">
      <div>
        <label className={labelCls}>Title</label>
        <input type="text" value={title} onChange={e => setTitle(e.target.value)} className={inputCls} />
      </div>

      <div>
        <label className={labelCls}>Content</label>
        <textarea
          value={content}
          onChange={e => setContent(e.target.value)}
          rows={6}
          className={cn(inputCls, 'resize-y font-mono leading-relaxed')}
        />
      </div>

      <div>
        <label className={labelCls}>Type</label>
        <select
          value={knowledgeType}
          onChange={e => setKnowledgeType(e.target.value)}
          className={cn(inputCls, 'text-slate-300')}
        >
          {KNOWLEDGE_TYPES.map(t => (
            <option key={t} value={t}>{typeLabel(t)}</option>
          ))}
        </select>
      </div>

      <div>
        <label className={labelCls}>Priority</label>
        <input
          type="number"
          min={0}
          value={priority}
          onChange={e => setPriority(e.target.value)}
          className={cn(inputCls, 'w-24')}
        />
      </div>

      <div>
        <label className={labelCls}>Tags <span className="text-slate-600">(comma-separated)</span></label>
        <input type="text" value={tags} onChange={e => setTags(e.target.value)} className={inputCls} />
      </div>

      <div>
        <label className={labelCls}>Applies To <span className="text-slate-600">(comma-separated)</span></label>
        <input type="text" value={appliesTo} onChange={e => setAppliesTo(e.target.value)} className={inputCls} />
      </div>

      <div>
        <label className={labelCls}>Tool Name <span className="text-slate-600">(optional)</span></label>
        <input type="text" value={toolName} onChange={e => setToolName(e.target.value)} className={inputCls} />
      </div>

      <div>
        <label className={labelCls}>Failure Signature <span className="text-slate-600">(optional)</span></label>
        <input type="text" value={failureSignature} onChange={e => setFailureSignature(e.target.value)} className={inputCls} />
      </div>

      <div>
        <label className={labelCls}>
          Reason for change <span className="text-red-400">*</span>
        </label>
        <input
          type="text"
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="Describe why you are making this change…"
          className={cn(inputCls, 'placeholder:text-slate-600')}
        />
      </div>

      {noChange && <p className="text-xs text-slate-400">No changes to save.</p>}
      {error && <p className="text-xs text-red-400">{error}</p>}

      <div className="flex gap-2 pt-1">
        <button
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-[color:var(--oc-accent)] px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving…' : 'Save Changes'}
        </button>
        <button
          onClick={onCancel}
          disabled={saving}
          className="rounded-md border border-[color:var(--oc-border-soft)] px-3 py-1.5 text-xs text-slate-400 transition-colors hover:text-white hover:border-[color:var(--oc-border)] disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ── Detail Panel ─────────────────────────────────────────────────────────────

type DetailTab = 'metadata' | 'usage' | 'revisions' | 'events';

const DETAIL_TABS: { id: DetailTab; label: string }[] = [
  { id: 'metadata', label: 'Metadata' },
  { id: 'usage', label: 'Usage' },
  { id: 'revisions', label: 'Revisions' },
  { id: 'events', label: 'Audit Events' },
];

interface DetailPanelProps {
  item: KnowledgeLibraryItem;
  onRefresh: (updated: KnowledgeLibraryItem) => void;
}

function DetailPanel({ item, onRefresh }: DetailPanelProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>('metadata');
  const [actionLoading, setActionLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [revKey, setRevKey] = useState(0);
  const [evKey, setEvKey] = useState(0);

  // Reset edit mode when the selected item changes
  useEffect(() => {
    setIsEditing(false);
    setSuccessMsg(null);
    setActionError(null);
  }, [item.id]);

  const handleRetire = async () => {
    setActionLoading(true);
    setActionError(null);
    setSuccessMsg(null);
    try {
      const r = await knowledgeLibraryAPI.retire(item.id);
      onRefresh(r.data);
    } catch {
      setActionError('Failed to retire item.');
    } finally {
      setActionLoading(false);
    }
  };

  const handleRestore = async () => {
    setActionLoading(true);
    setActionError(null);
    setSuccessMsg(null);
    try {
      const r = await knowledgeLibraryAPI.restore(item.id);
      onRefresh(r.data);
    } catch {
      setActionError('Failed to restore item.');
    } finally {
      setActionLoading(false);
    }
  };

  const handleEditSave = (updated: KnowledgeLibraryItem) => {
    onRefresh(updated);
    setIsEditing(false);
    setSuccessMsg('Changes saved.');
    setRevKey(k => k + 1);
    setEvKey(k => k + 1);
  };

  const tagList = Array.isArray(item.tags) ? item.tags.map(String) : [];
  const appliesToList = Array.isArray(item.applies_to) ? item.applies_to.map(String) : [];

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="mb-4 pb-4 border-b border-[color:var(--oc-border-soft)]">
        <div className="flex items-start justify-between gap-3 mb-2">
          <h2 className="text-sm font-semibold text-slate-100 leading-snug">{item.title}</h2>
          <ActiveBadge isActive={item.is_active} />
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <Badge className="bg-slate-500/10 text-slate-300">{typeLabel(item.knowledge_type)}</Badge>
          <Badge className="bg-slate-500/10 text-slate-400">Priority {item.priority}</Badge>
          <Badge className="bg-slate-500/10 text-slate-400">v{item.version}</Badge>
        </div>

        {/* Actions */}
        <div className="mt-3 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => { setIsEditing(e => !e); setSuccessMsg(null); setActionError(null); }}
            className={cn(
              'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs transition-colors',
              isEditing
                ? 'border-slate-500/40 bg-slate-500/10 text-slate-400 hover:bg-slate-500/20'
                : 'border-[color:var(--oc-border-soft)] text-slate-300 hover:border-[color:var(--oc-border)] hover:text-white'
            )}
          >
            {isEditing ? <X className="h-3.5 w-3.5" /> : <Pencil className="h-3.5 w-3.5" />}
            {isEditing ? 'Close Editor' : 'Edit Knowledge'}
          </button>

          {!isEditing && (
            item.is_active ? (
              <button
                onClick={handleRetire}
                disabled={actionLoading}
                className="flex items-center gap-1.5 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300 transition-colors hover:bg-amber-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Archive className="h-3.5 w-3.5" />
                {actionLoading ? 'Retiring…' : 'Retire'}
              </button>
            ) : (
              <button
                onClick={handleRestore}
                disabled={actionLoading}
                className="flex items-center gap-1.5 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-1.5 text-xs text-emerald-300 transition-colors hover:bg-emerald-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                {actionLoading ? 'Restoring…' : 'Restore'}
              </button>
            )
          )}
        </div>
        {actionError && <p className="mt-2 text-xs text-red-400">{actionError}</p>}
        {successMsg && <p className="mt-2 text-xs text-emerald-400">{successMsg}</p>}
      </div>

      {/* Edit mode */}
      {isEditing ? (
        <div className="flex-1 overflow-y-auto min-h-0">
          <EditForm
            item={item}
            onSave={handleEditSave}
            onCancel={() => setIsEditing(false)}
          />
        </div>
      ) : (
        <>
          {/* Tabs */}
          <div className="flex gap-1 mb-4 border-b border-[color:var(--oc-border-soft)]">
            {DETAIL_TABS.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  'px-3 py-2 text-xs font-medium transition-colors border-b-2 -mb-px',
                  activeTab === tab.id
                    ? 'border-[color:var(--oc-accent)] text-white'
                    : 'border-transparent text-slate-500 hover:text-slate-300'
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto min-h-0">
            {activeTab === 'metadata' && (
              <div>
                <SectionHeader title="Core" />
                <div className="mb-4 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-3">
                  <MetaRow label="Type" value={typeLabel(item.knowledge_type)} />
                  <MetaRow label="Priority" value={item.priority} />
                  <MetaRow label="Version" value={`v${item.version}`} />
                  <MetaRow label="Active" value={item.is_active ? 'Yes' : 'No'} />
                  <MetaRow label="Created" value={fmtDate(item.created_at)} />
                  <MetaRow label="Updated" value={fmtDate(item.updated_at)} />
                </div>

                {tagList.length > 0 && (
                  <>
                    <SectionHeader title="Tags" />
                    <div className="mb-4 flex flex-wrap gap-1.5">
                      {tagList.map(t => (
                        <Badge key={t} className="bg-slate-500/10 text-slate-300">{t}</Badge>
                      ))}
                    </div>
                  </>
                )}

                {appliesToList.length > 0 && (
                  <>
                    <SectionHeader title="Applies To" />
                    <div className="mb-4 flex flex-wrap gap-1.5">
                      {appliesToList.map(a => (
                        <Badge key={a} className="bg-slate-500/10 text-slate-300">{a}</Badge>
                      ))}
                    </div>
                  </>
                )}

                {(item.tool_name || item.failure_signature || item.source_path || item.project_scope) && (
                  <>
                    <SectionHeader title="Context" />
                    <div className="mb-4 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-3">
                      {item.tool_name && <MetaRow label="Tool" value={item.tool_name} />}
                      {item.failure_signature && <MetaRow label="Failure Sig." value={item.failure_signature} />}
                      {item.source_path && <MetaRow label="Source" value={item.source_path} />}
                      {item.project_scope && <MetaRow label="Project Scope" value={item.project_scope} />}
                    </div>
                  </>
                )}

                <SectionHeader title="Content" />
                <pre className="whitespace-pre-wrap rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-3 text-xs text-slate-300 leading-relaxed overflow-x-auto">
                  {item.content}
                </pre>

                <div className="mt-3 text-[11px] text-slate-600 font-mono">
                  checksum: {item.checksum.slice(0, 12)}…
                </div>
              </div>
            )}

            {activeTab === 'usage' && <UsageSummaryPanel itemId={item.id} />}
            {activeTab === 'revisions' && <RevisionsPanel key={revKey} itemId={item.id} />}
            {activeTab === 'events' && <AuditEventsPanel key={evKey} itemId={item.id} />}
          </div>
        </>
      )}
    </div>
  );
}

// ── List Item ────────────────────────────────────────────────────────────────

function KnowledgeListItem({
  item,
  selected,
  onClick,
}: {
  item: KnowledgeLibraryItem;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'w-full text-left rounded-md px-3 py-2.5 transition-colors group',
        selected
          ? 'bg-[color:var(--oc-surface-raised)] border border-[color:var(--oc-border)]'
          : 'hover:bg-[color:var(--oc-surface)] border border-transparent'
      )}
    >
      <div className="flex items-start justify-between gap-2 mb-1">
        <span className="text-xs font-medium text-slate-200 leading-snug line-clamp-2 flex-1">
          {item.title}
        </span>
        <ChevronRight className={cn(
          'h-3.5 w-3.5 flex-shrink-0 mt-0.5 transition-colors',
          selected ? 'text-[color:var(--oc-accent)]' : 'text-slate-600 group-hover:text-slate-400'
        )} />
      </div>
      <div className="flex items-center gap-1.5 flex-wrap">
        <Badge className="bg-slate-500/10 text-slate-400">{typeLabel(item.knowledge_type)}</Badge>
        <ActiveBadge isActive={item.is_active} />
        {item.priority > 0 && (
          <Badge className="bg-slate-500/10 text-slate-500">p{item.priority}</Badge>
        )}
      </div>
    </button>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

const KNOWLEDGE_TYPES = [
  'format_guide',
  'debug_case',
  'tool_guide',
  'workflow_guide',
  'project_context',
  'failure_pattern',
];

export default function KnowledgeLibrary() {
  const [items, setItems] = useState<KnowledgeLibraryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize] = useState(20);
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedItem, setSelectedItem] = useState<KnowledgeLibraryItem | null>(null);

  const load = useCallback((p: number, type: string, query: string) => {
    setLoading(true);
    setError(null);
    knowledgeLibraryAPI.list({
      page: p,
      page_size: pageSize,
      knowledge_type: type || undefined,
      search: query.trim() || undefined,
      include_retired: true,
    })
      .then(r => {
        setItems(r.data.items);
        setTotal(r.data.total);
      })
      .catch(() => setError('Failed to load knowledge items.'))
      .finally(() => setLoading(false));
  }, [pageSize]);

  useEffect(() => {
    setPage(1);
    load(1, typeFilter, search);
  }, [load, typeFilter, search]);

  // Fetch full detail when selectedId changes
  useEffect(() => {
    if (!selectedId) { setSelectedItem(null); return; }
    knowledgeLibraryAPI.getById(selectedId)
      .then(r => setSelectedItem(r.data))
      .catch(() => setSelectedItem(null));
  }, [selectedId]);

  const totalPages = Math.ceil(total / pageSize);

  const handleItemRefresh = (updated: KnowledgeLibraryItem) => {
    setSelectedItem(updated);
    setItems(prev => prev.map(i => i.id === updated.id ? updated : i));
  };

  return (
    <div className="flex flex-col h-full">
      {/* Page header */}
      <div className="mb-5 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <BookOpen className="h-5 w-5 text-[color:var(--oc-accent)]" />
          <div>
            <h1 className="text-base font-semibold text-white">Knowledge Library</h1>
            {!loading && <p className="text-xs text-slate-500">{total} items total</p>}
          </div>
        </div>
        <button
          onClick={() => load(page, typeFilter, search)}
          className="flex items-center gap-1.5 rounded-md border border-[color:var(--oc-border-soft)] px-3 py-1.5 text-xs text-slate-400 transition-colors hover:text-white hover:border-[color:var(--oc-border)]"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-500/30 bg-red-500/10 px-4 py-3 text-xs text-red-400">
          {error}
        </div>
      )}

      {/* Layout: list + detail */}
      <div className="flex gap-5 min-h-0 flex-1">
        {/* Left: list */}
        <div className="w-72 flex-shrink-0 flex flex-col">
          {/* Filters */}
          <div className="mb-3 space-y-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
              <input
                type="text"
                placeholder="Filter by title…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] pl-8 pr-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-[color:var(--oc-border)]"
              />
            </div>
            <select
              value={typeFilter}
              onChange={e => setTypeFilter(e.target.value)}
              className="w-full rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] px-3 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-[color:var(--oc-border)]"
            >
              <option value="">All types</option>
              {KNOWLEDGE_TYPES.map(t => (
                <option key={t} value={t}>{typeLabel(t)}</option>
              ))}
            </select>
          </div>

          {/* List */}
          <div className="flex-1 overflow-y-auto min-h-0 space-y-0.5">
            {loading ? (
              <div className="space-y-2 py-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="h-14 rounded-md bg-[color:var(--oc-surface)] animate-pulse" />
                ))}
              </div>
            ) : items.length === 0 ? (
              <div className="py-8 text-center">
                <p className="text-xs text-slate-500">No knowledge items found.</p>
              </div>
            ) : (
              items.map(item => (
                <KnowledgeListItem
                  key={item.id}
                  item={item}
                  selected={selectedId === item.id}
                  onClick={() => setSelectedId(item.id)}
                />
              ))
            )}
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="mt-3 flex items-center justify-between border-t border-[color:var(--oc-border-soft)] pt-3">
              <button
                disabled={page <= 1}
                onClick={() => { const p = page - 1; setPage(p); load(p, typeFilter, search); }}
                className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
              >← Prev</button>
              <span className="text-xs text-slate-500">{page} / {totalPages}</span>
              <button
                disabled={page >= totalPages}
                onClick={() => { const p = page + 1; setPage(p); load(p, typeFilter, search); }}
                className="text-xs text-slate-400 hover:text-white disabled:opacity-40 disabled:cursor-not-allowed"
              >Next →</button>
            </div>
          )}
        </div>

        {/* Right: detail */}
        <div className="flex-1 min-w-0 rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-raised)] p-5 overflow-y-auto">
          {selectedItem ? (
            <DetailPanel item={selectedItem} onRefresh={handleItemRefresh} />
          ) : (
            <div className="flex h-full items-center justify-center">
              <div className="text-center">
                <BookOpen className="mx-auto mb-3 h-8 w-8 text-slate-700" />
                <p className="text-sm text-slate-500">Select a knowledge item to inspect it.</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
