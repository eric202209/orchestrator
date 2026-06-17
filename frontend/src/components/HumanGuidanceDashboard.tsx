import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Eye,
  Plus,
  Settings,
  Shield,
  Trash2,
  XCircle,
} from 'lucide-react';
import { guidanceAPI } from '@/api/client';
import { Button } from '@/components/ui';
import type {
  HumanGuidanceActivation,
  HumanGuidanceConflict,
  HumanGuidanceEntry,
  HumanGuidanceReadiness,
  HumanGuidanceRendered,
} from '@/types/api';

// ── constants ──────────────────────────────────────────────────────────────────

const BACKEND_OPTIONS = ['all', 'direct_ollama', 'local_openclaw'];
const MODEL_OPTIONS = ['all', 'qwen', 'claude', 'llama', 'deepseek'];
const PURPOSE_OPTIONS = ['all', 'planning', 'execution', 'repair'];
const PURPOSE_RESERVED = 'validation';

const DEFAULT_ACTIVATION: HumanGuidanceActivation = {
  id: null,
  scope: 'project',
  project_id: null,
  session_id: null,
  table_enabled: false,
  persistence_enabled: false,
  render_enabled: false,
  injection_enabled: false,
  conflict_detection_enabled: false,
  status: 'disabled',
  enabled_by: null,
  disabled_at: null,
  disabled_by: null,
  created_at: null,
  updated_at: null,
};

// ── helpers ────────────────────────────────────────────────────────────────────

const renderTargets = (targets: string[]) => {
  if (!targets || targets.length === 0 || (targets.length === 1 && targets[0] === 'all')) {
    return <span className="text-slate-500">all</span>;
  }
  return (
    <span className="font-mono text-xs text-primary-300">
      {targets.join(', ')}
    </span>
  );
};

const purposeLabel = (p: string) =>
  p === PURPOSE_RESERVED ? `${p} (reserved)` : p;

// ── sub-components ─────────────────────────────────────────────────────────────

interface ActivationFlagsProps {
  activation: HumanGuidanceActivation;
  saving: boolean;
  onSave: (flags: Omit<HumanGuidanceActivation, 'id' | 'scope' | 'project_id' | 'session_id' | 'status' | 'enabled_by' | 'disabled_at' | 'disabled_by' | 'created_at' | 'updated_at'>) => void;
  onDisableAll: () => void;
}

function ActivationFlags({ activation, saving, onSave, onDisableAll }: ActivationFlagsProps) {
  const [flags, setFlags] = useState({
    table_enabled: activation.table_enabled,
    persistence_enabled: activation.persistence_enabled,
    render_enabled: activation.render_enabled,
    injection_enabled: activation.injection_enabled,
    conflict_detection_enabled: activation.conflict_detection_enabled,
  });

  useEffect(() => {
    setFlags({
      table_enabled: activation.table_enabled,
      persistence_enabled: activation.persistence_enabled,
      render_enabled: activation.render_enabled,
      injection_enabled: activation.injection_enabled,
      conflict_detection_enabled: activation.conflict_detection_enabled,
    });
  }, [activation]);

  const toggle = (key: keyof typeof flags) =>
    setFlags((f) => ({ ...f, [key]: !f[key] }));

  const enableAll = () => {
    const all = {
      table_enabled: true,
      persistence_enabled: true,
      render_enabled: true,
      injection_enabled: true,
      conflict_detection_enabled: true,
    };
    setFlags(all);
    onSave(all);
  };

  const flagRows: Array<{ key: keyof typeof flags; label: string; description: string }> = [
    { key: 'table_enabled', label: 'Table', description: 'Enable guidance storage and retrieval' },
    { key: 'persistence_enabled', label: 'Persistence', description: 'Persist guidance to working memory' },
    { key: 'render_enabled', label: 'Render', description: 'Render guidance block in prompts' },
    { key: 'injection_enabled', label: 'Injection', description: 'Inject guidance into orchestrator context' },
    { key: 'conflict_detection_enabled', label: 'Conflict detection', description: 'Scan task descriptions for pattern conflicts' },
  ];

  return (
    <div className="space-y-3">
      <div className="grid gap-2">
        {flagRows.map(({ key, label, description }) => (
          <label
            key={key}
            className="flex items-center gap-3 rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2 cursor-pointer hover:border-[color:var(--oc-border)] transition-colors"
          >
            <input
              type="checkbox"
              checked={flags[key]}
              onChange={() => toggle(key)}
              className="h-4 w-4 rounded border-[color:var(--oc-border)] bg-[color:var(--oc-shell)] accent-blue-500"
            />
            <div className="flex-1">
              <span className="text-sm text-slate-200">{label}</span>
              <p className="text-xs text-slate-500">{description}</p>
            </div>
            {flags[key] ? (
              <CheckCircle className="h-4 w-4 text-emerald-400 shrink-0" />
            ) : (
              <XCircle className="h-4 w-4 text-slate-600 shrink-0" />
            )}
          </label>
        ))}
      </div>
      <div className="flex gap-2">
        <Button
          variant="default"
          size="sm"
          onClick={() => onSave(flags)}
          disabled={saving}
        >
          {saving ? 'Saving…' : 'Save'}
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={enableAll}
          disabled={saving}
        >
          Enable all
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={onDisableAll}
          disabled={saving}
          className="text-red-400 hover:text-red-300 ml-auto"
        >
          Disable all
        </Button>
      </div>
    </div>
  );
}

// ── guidance form ─────────────────────────────────────────────────────────────

interface GuidanceFormState {
  message: string;
  scope: string;
  priority: number;
  backendTargets: string[];
  modelTargets: string[];
  purposeTargets: string[];
  expiresAt: string;
}

const defaultFormState = (): GuidanceFormState => ({
  message: '',
  scope: 'project',
  priority: 0,
  backendTargets: ['all'],
  modelTargets: ['all'],
  purposeTargets: ['all'],
  expiresAt: '',
});

interface GuidanceFormProps {
  initial?: HumanGuidanceEntry | null;
  advancedMode: boolean;
  saving: boolean;
  onSubmit: (state: GuidanceFormState) => void;
  onCancel: () => void;
}

function GuidanceForm({ initial, advancedMode, saving, onSubmit, onCancel }: GuidanceFormProps) {
  const [form, setForm] = useState<GuidanceFormState>(() => {
    if (!initial) return defaultFormState();
    return {
      message: initial.message,
      scope: initial.scope,
      priority: initial.priority,
      backendTargets: initial.backend_targets?.length ? initial.backend_targets : ['all'],
      modelTargets: initial.model_targets?.length ? initial.model_targets : ['all'],
      purposeTargets: initial.purpose_targets?.length ? initial.purpose_targets : ['all'],
      expiresAt: initial.expires_at ? initial.expires_at.slice(0, 10) : '',
    };
  });

  const toggleTarget = (
    key: 'backendTargets' | 'modelTargets' | 'purposeTargets',
    value: string,
  ) => {
    setForm((f) => {
      const current = f[key];
      if (value === 'all') return { ...f, [key]: ['all'] };
      const without = current.filter((v) => v !== 'all' && v !== value);
      const next = current.includes(value) ? without : [...without, value];
      return { ...f, [key]: next.length === 0 ? ['all'] : next };
    });
  };

  const allPurposeOptions = advancedMode
    ? [...PURPOSE_OPTIONS, PURPOSE_RESERVED]
    : PURPOSE_OPTIONS;

  return (
    <div className="space-y-4">
      <div>
        <label className="mb-1.5 block text-xs font-medium text-slate-300">
          Message <span className="text-red-400">*</span>
        </label>
        <textarea
          value={form.message}
          onChange={(e) => setForm((f) => ({ ...f, message: e.target.value }))}
          rows={3}
          maxLength={500}
          className="w-full resize-y rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white placeholder-slate-600 focus:outline-none focus:ring-1 focus:ring-primary-500/60"
          placeholder="e.g., Never use mutable default arguments in Python function signatures."
          autoFocus
        />
        <p className="mt-0.5 text-right text-xs text-slate-600">{form.message.length}/500</p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Scope</label>
          <select
            value={form.scope}
            onChange={(e) => setForm((f) => ({ ...f, scope: e.target.value }))}
            className="w-full rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-primary-500/60"
          >
            <option value="project">Project</option>
            <option value="global">Global</option>
          </select>
        </div>
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Priority (0–100)</label>
          <input
            type="number"
            min={0}
            max={100}
            value={form.priority}
            onChange={(e) => setForm((f) => ({ ...f, priority: Number(e.target.value) }))}
            className="w-full rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-primary-500/60"
          />
        </div>
      </div>

      {advancedMode && (
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Expires at (optional)</label>
          <input
            type="date"
            value={form.expiresAt}
            onChange={(e) => setForm((f) => ({ ...f, expiresAt: e.target.value }))}
            className="rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-3 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-primary-500/60"
          />
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-3">
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Backend</label>
          <div className="space-y-1">
            {BACKEND_OPTIONS.map((opt) => (
              <label key={opt} className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.backendTargets.includes(opt)}
                  onChange={() => toggleTarget('backendTargets', opt)}
                  className="h-3.5 w-3.5 rounded accent-blue-500"
                />
                {opt}
              </label>
            ))}
          </div>
        </div>
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Model family</label>
          <div className="space-y-1">
            {MODEL_OPTIONS.map((opt) => (
              <label key={opt} className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.modelTargets.includes(opt)}
                  onChange={() => toggleTarget('modelTargets', opt)}
                  className="h-3.5 w-3.5 rounded accent-blue-500"
                />
                {opt}
              </label>
            ))}
          </div>
        </div>
        <div>
          <label className="mb-1.5 block text-xs font-medium text-slate-300">Purpose</label>
          <div className="space-y-1">
            {allPurposeOptions.map((opt) => (
              <label key={opt} className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.purposeTargets.includes(opt)}
                  onChange={() => toggleTarget('purposeTargets', opt)}
                  className="h-3.5 w-3.5 rounded accent-blue-500"
                />
                {opt === PURPOSE_RESERVED ? (
                  <span>
                    {opt}{' '}
                    <span className="text-slate-500">(reserved / future runtime support)</span>
                  </span>
                ) : (
                  opt
                )}
              </label>
            ))}
          </div>
        </div>
      </div>

      <div className="flex gap-2 pt-1">
        <Button variant="outline" size="sm" onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button
          variant="default"
          size="sm"
          onClick={() => onSubmit(form)}
          disabled={saving || !form.message.trim()}
        >
          {saving ? 'Saving…' : initial ? 'Update' : 'Add guidance'}
        </Button>
      </div>
    </div>
  );
}

// ── preview panel ──────────────────────────────────────────────────────────────

interface PreviewPanelProps {
  projectId: number;
}

function PreviewPanel({ projectId }: PreviewPanelProps) {
  const [backend, setBackend] = useState('all');
  const [modelFamily, setModelFamily] = useState('all');
  const [purpose, setPurpose] = useState('all');
  const [preview, setPreview] = useState<HumanGuidanceRendered | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await guidanceAPI.getRendered(projectId, {
        backend,
        model_family: modelFamily,
        purpose,
      });
      setPreview(res.data);
    } catch {
      setError('Failed to load preview');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-2">
        {[
          { label: 'Backend', value: backend, setter: setBackend, options: BACKEND_OPTIONS },
          { label: 'Model family', value: modelFamily, setter: setModelFamily, options: MODEL_OPTIONS },
          {
            label: 'Purpose',
            value: purpose,
            setter: setPurpose,
            options: ['all', ...PURPOSE_OPTIONS.filter((p) => p !== 'all'), PURPOSE_RESERVED],
          },
        ].map(({ label, value, setter, options }) => (
          <div key={label}>
            <label className="mb-1 block text-xs text-slate-500">{label}</label>
            <select
              value={value}
              onChange={(e) => setter(e.target.value)}
              className="rounded-md border border-[color:var(--oc-border)] bg-[color:var(--oc-surface-deep)] px-2 py-1.5 text-xs text-white focus:outline-none"
            >
              {options.map((o) => (
                <option key={o} value={o}>
                  {o === PURPOSE_RESERVED ? `${o} (reserved)` : o}
                </option>
              ))}
            </select>
          </div>
        ))}
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <Eye className="mr-1.5 h-3.5 w-3.5" />
          {loading ? 'Loading…' : 'Preview'}
        </Button>
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}

      {preview && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-3 text-xs text-slate-400">
            <span>{preview.selected_count} selected</span>
            <span>{preview.rendered_chars} / {preview.max_chars} chars</span>
            {preview.trimmed && (
              <span className="text-amber-300">trimmed</span>
            )}
            {preview.filtered_target_ids.length > 0 && (
              <span className="text-slate-500">
                {preview.filtered_target_ids.length} filtered by backend/model (IDs: {preview.filtered_target_ids.join(', ')})
              </span>
            )}
            {preview.filtered_purpose_ids.length > 0 && (
              <span className="text-slate-500">
                {preview.filtered_purpose_ids.length} filtered by purpose (IDs: {preview.filtered_purpose_ids.join(', ')})
              </span>
            )}
          </div>
          {preview.block ? (
            <pre className="max-h-64 overflow-y-auto rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-shell)] p-3 font-mono text-xs text-slate-300 whitespace-pre-wrap break-words">
              {preview.block}
            </pre>
          ) : (
            <p className="text-xs text-slate-500 italic">No guidance selected for this combination.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── conflict panel ────────────────────────────────────────────────────────────

interface ConflictPanelProps {
  projectId: number;
  conflicts: HumanGuidanceConflict[];
  onResolve: (conflictId: number, status: 'resolved' | 'ignored') => void;
}

function ConflictPanel({ projectId: _projectId, conflicts, onResolve }: ConflictPanelProps) {
  if (conflicts.length === 0) {
    return (
      <p className="text-xs text-slate-500 italic">No open conflicts detected.</p>
    );
  }

  return (
    <div className="space-y-2">
      {conflicts.map((c) => (
        <div
          key={c.id ?? `${c.guidance_id}-${c.task_id}`}
          className="rounded-md border border-amber-500/20 bg-amber-500/5 p-3"
        >
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs font-medium text-amber-200 line-clamp-1">
                {c.guidance_message}
              </p>
              {c.task_title && (
                <p className="mt-0.5 text-xs text-slate-400">
                  Task: {c.task_title}
                </p>
              )}
              {c.conflict_excerpt && (
                <p className="mt-1 font-mono text-xs text-slate-500 line-clamp-2">
                  {c.conflict_excerpt}
                </p>
              )}
              {c.conflict_patterns.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {c.conflict_patterns.map((p) => (
                    <span
                      key={p}
                      className="rounded border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 font-mono text-[10px] text-amber-300"
                    >
                      {p}
                    </span>
                  ))}
                </div>
              )}
            </div>
            <div className="flex shrink-0 gap-1.5">
              <button
                onClick={() => c.id !== null && onResolve(c.id, 'resolved')}
                className="rounded border border-emerald-500/25 bg-emerald-500/10 px-2 py-1 text-xs text-emerald-300 hover:bg-emerald-500/20 transition-colors"
              >
                Resolve
              </button>
              <button
                onClick={() => c.id !== null && onResolve(c.id, 'ignored')}
                className="rounded border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-2 py-1 text-xs text-slate-400 hover:text-slate-200 transition-colors"
              >
                Ignore
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── main dashboard ────────────────────────────────────────────────────────────

interface HumanGuidanceDashboardProps {
  projectId: number;
}

export function HumanGuidanceDashboard({ projectId }: HumanGuidanceDashboardProps) {
  const [readiness, setReadiness] = useState<HumanGuidanceReadiness | null>(null);
  const [guidance, setGuidance] = useState<HumanGuidanceEntry[]>([]);
  const [conflicts, setConflicts] = useState<HumanGuidanceConflict[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [advancedMode, setAdvancedMode] = useState(false);
  const [listFilter, setListFilter] = useState<'active' | 'disabled' | 'all'>('active');

  // Modal state
  const [showAddModal, setShowAddModal] = useState(false);
  const [editingEntry, setEditingEntry] = useState<HumanGuidanceEntry | null>(null);
  const [formSaving, setFormSaving] = useState(false);

  // Activation saving
  const [activationSaving, setActivationSaving] = useState(false);

  // Collapsible sections
  const [showActivation, setShowActivation] = useState(false);
  const [showPreview, setShowPreview] = useState(false);
  const [showConflicts, setShowConflicts] = useState(false);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [readinessRes, guidanceRes, conflictsRes] = await Promise.all([
        guidanceAPI.getReadiness(projectId),
        guidanceAPI.list(projectId, { status: listFilter, limit: 50 }),
        guidanceAPI.listConflicts(projectId, { status: 'open', limit: 50 }),
      ]);
      setReadiness(readinessRes.data);
      setGuidance(guidanceRes.data.items);
      setConflicts(conflictsRes.data.items);
    } catch {
      setError('Failed to load guidance data');
    } finally {
      setLoading(false);
    }
  }, [projectId, listFilter]);

  useEffect(() => {
    setLoading(true);
    loadAll();
  }, [loadAll]);

  const handleSaveActivation = async (flags: {
    table_enabled: boolean;
    persistence_enabled: boolean;
    render_enabled: boolean;
    injection_enabled: boolean;
    conflict_detection_enabled: boolean;
  }) => {
    setActivationSaving(true);
    try {
      await guidanceAPI.patchActivation(projectId, flags);
      const res = await guidanceAPI.getReadiness(projectId);
      setReadiness(res.data);
    } catch {
      alert('Failed to save activation settings.');
    } finally {
      setActivationSaving(false);
    }
  };

  const handleDisableAll = async () => {
    if (!window.confirm('Disable all Human Guidance features for this project?')) return;
    setActivationSaving(true);
    try {
      await guidanceAPI.disableActivation(projectId);
      const res = await guidanceAPI.getReadiness(projectId);
      setReadiness(res.data);
    } catch {
      alert('Failed to disable guidance.');
    } finally {
      setActivationSaving(false);
    }
  };

  const handleFormSubmit = async (state: GuidanceFormState) => {
    setFormSaving(true);
    try {
      const payload = {
        message: state.message.trim(),
        scope: state.scope,
        priority: state.priority,
        backend_targets: state.backendTargets,
        model_targets: state.modelTargets,
        purpose_targets: state.purposeTargets,
        expires_at: state.expiresAt ? new Date(state.expiresAt).toISOString() : null,
      };

      if (editingEntry) {
        await guidanceAPI.patch(editingEntry.id, {
          message: payload.message,
          priority: payload.priority,
          expires_at: payload.expires_at,
        });
      } else {
        await guidanceAPI.create(projectId, payload);
      }

      setShowAddModal(false);
      setEditingEntry(null);
      await loadAll();
    } catch {
      alert('Failed to save guidance entry.');
    } finally {
      setFormSaving(false);
    }
  };

  const handleToggleStatus = async (entry: HumanGuidanceEntry) => {
    const next = entry.status === 'active' ? 'disabled' : 'active';
    try {
      await guidanceAPI.patch(entry.id, { status: next });
      await loadAll();
    } catch {
      alert('Failed to update status.');
    }
  };

  const handleArchive = async (entry: HumanGuidanceEntry) => {
    if (!window.confirm(`Archive guidance #${entry.id}? It will no longer affect planning.`)) return;
    try {
      await guidanceAPI.archive(entry.id);
      await loadAll();
    } catch {
      alert('Failed to archive guidance entry.');
    }
  };

  const handleResolveConflict = async (conflictId: number, status: 'resolved' | 'ignored') => {
    try {
      await guidanceAPI.patchConflict(projectId, conflictId, { status });
      setConflicts((c) => c.filter((row) => row.id !== conflictId));
    } catch {
      alert('Failed to update conflict.');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[200px]">
        <div className="h-6 w-6 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-300">
        {error}
      </div>
    );
  }

  const activation = readiness?.activation ?? DEFAULT_ACTIVATION;
  const isReady = readiness?.is_ready ?? false;
  const purposeStats = readiness?.purpose_statistics;
  const openConflicts = conflicts.filter((c) => !c.resolved);

  return (
    <div className="space-y-5">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Shield className="h-4 w-4 text-primary-400" />
          <h2 className="text-sm font-medium text-white">Human Guidance</h2>
          {isReady ? (
            <span className="flex items-center gap-1 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300">
              <CheckCircle className="h-3 w-3" /> Active
            </span>
          ) : (
            <span className="flex items-center gap-1 rounded-full border border-slate-600 bg-slate-800 px-2 py-0.5 text-xs text-slate-400">
              <XCircle className="h-3 w-3" /> Inactive
            </span>
          )}
          {openConflicts.length > 0 && (
            <span className="flex items-center gap-1 rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-300">
              <AlertTriangle className="h-3 w-3" />
              {openConflicts.length} conflict{openConflicts.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <button
          onClick={() => setAdvancedMode((v) => !v)}
          className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition-colors"
        >
          <Settings className="h-3.5 w-3.5" />
          {advancedMode ? 'Basic mode' : 'Advanced mode'}
        </button>
      </div>

      {/* Section 1: Readiness panel */}
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-4 space-y-3">
        <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500">
          Readiness
        </h3>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5">
          {[
            { label: 'Active entries', value: readiness?.active_guidance_count ?? 0 },
            { label: 'Planning', value: purposeStats?.planning ?? 0 },
            { label: 'Execution', value: purposeStats?.execution ?? 0 },
            { label: 'Repair', value: purposeStats?.repair ?? 0 },
            {
              label: advancedMode ? 'Validation (reserved)' : 'Global (all)',
              value: advancedMode ? (purposeStats?.validation ?? 0) : (purposeStats?.all ?? 0),
            },
          ].map(({ label, value }) => (
            <div key={label} className="rounded-md border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface-deep)] px-3 py-2">
              <p className="text-[10px] uppercase tracking-wider text-slate-500">{label}</p>
              <p className="mt-0.5 text-lg font-semibold text-slate-200">{value}</p>
            </div>
          ))}
        </div>

        {readiness?.warnings && readiness.warnings.length > 0 && (
          <div className="space-y-1">
            {readiness.warnings.map((w) => (
              <p key={w} className="flex items-center gap-1.5 text-xs text-amber-300">
                <AlertTriangle className="h-3 w-3 shrink-0" />
                {w}
              </p>
            ))}
          </div>
        )}

        {advancedMode && readiness?.backend_statistics && (
          <div className="text-xs text-slate-500">
            Backend filter: {readiness.backend_statistics.backend} / {readiness.backend_statistics.model_family} —{' '}
            {readiness.backend_statistics.matching_guidance} matching,{' '}
            {readiness.backend_statistics.filtered_guidance} filtered
          </div>
        )}
      </div>

      {/* Section 2: Activation controls (collapsible) */}
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)]">
        <button
          className="flex w-full items-center justify-between px-4 py-3 text-left"
          onClick={() => setShowActivation((v) => !v)}
        >
          <span className="text-xs font-medium uppercase tracking-wider text-slate-500">
            Activation controls
          </span>
          {showActivation ? (
            <ChevronUp className="h-4 w-4 text-slate-500" />
          ) : (
            <ChevronDown className="h-4 w-4 text-slate-500" />
          )}
        </button>
        {showActivation && (
          <div className="border-t border-[color:var(--oc-border-soft)] px-4 pb-4 pt-3">
            <ActivationFlags
              activation={activation}
              saving={activationSaving}
              onSave={handleSaveActivation}
              onDisableAll={handleDisableAll}
            />
          </div>
        )}
      </div>

      {/* Section 3: Guidance list */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium text-white">Guidance entries</h3>
            <div className="flex rounded-md border border-[color:var(--oc-border-soft)] overflow-hidden text-xs">
              {(['active', 'disabled', 'all'] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setListFilter(f)}
                  className={`px-2.5 py-1 transition-colors ${
                    listFilter === f
                      ? 'bg-primary-600/20 text-primary-300'
                      : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>
          <Button
            variant="default"
            size="sm"
            onClick={() => {
              setEditingEntry(null);
              setShowAddModal(true);
            }}
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            Add guidance
          </Button>
        </div>

        {guidance.length === 0 ? (
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] py-10 text-center">
            <Shield className="mx-auto mb-2 h-8 w-8 text-slate-700" />
            <p className="text-sm text-slate-400">No guidance entries yet.</p>
            <p className="mt-1 text-xs text-slate-600">
              Add rules to shape how the orchestrator approaches this project.
            </p>
          </div>
        ) : (
          <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] divide-y divide-[color:var(--oc-border-soft)]">
            {guidance.map((entry) => (
              <div key={entry.id} className="px-4 py-3 hover:bg-[color:var(--oc-surface-raised)] transition-colors">
                <div className="flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-slate-200">{entry.message}</p>
                    <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                      <span className="text-slate-500">{entry.scope}</span>
                      <span className="text-slate-600">·</span>
                      <span className="text-slate-500">p={entry.priority}</span>
                      <span className="text-slate-600">·</span>
                      {renderTargets(entry.backend_targets)}
                      {' / '}
                      {renderTargets(entry.model_targets)}
                      {' / '}
                      {renderTargets(entry.purpose_targets.map(purposeLabel))}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <span
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-medium ${
                        entry.status === 'active'
                          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                          : 'border-slate-600 bg-slate-800 text-slate-400'
                      }`}
                    >
                      {entry.status}
                    </span>
                    <button
                      onClick={() => {
                        setEditingEntry(entry);
                        setShowAddModal(true);
                      }}
                      className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                      title="Edit"
                    >
                      <Settings className="h-3.5 w-3.5" />
                    </button>
                    <button
                      onClick={() => handleToggleStatus(entry)}
                      className="rounded p-1 text-slate-500 hover:text-slate-300 transition-colors"
                      title={entry.status === 'active' ? 'Disable' : 'Enable'}
                    >
                      {entry.status === 'active' ? (
                        <XCircle className="h-3.5 w-3.5" />
                      ) : (
                        <CheckCircle className="h-3.5 w-3.5" />
                      )}
                    </button>
                    <button
                      onClick={() => handleArchive(entry)}
                      className="rounded p-1 text-slate-500 hover:text-red-400 transition-colors"
                      title="Archive"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Section 5: Rendered preview (advanced only, collapsible) */}
      {advancedMode && (
        <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)]">
          <button
            className="flex w-full items-center justify-between px-4 py-3 text-left"
            onClick={() => setShowPreview((v) => !v)}
          >
            <span className="text-xs font-medium uppercase tracking-wider text-slate-500">
              Rendered preview
            </span>
            {showPreview ? (
              <ChevronUp className="h-4 w-4 text-slate-500" />
            ) : (
              <ChevronDown className="h-4 w-4 text-slate-500" />
            )}
          </button>
          {showPreview && (
            <div className="border-t border-[color:var(--oc-border-soft)] px-4 pb-4 pt-3">
              <PreviewPanel projectId={projectId} />
            </div>
          )}
        </div>
      )}

      {/* Section 6: Conflict panel (collapsible) */}
      <div className="rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)]">
        <button
          className="flex w-full items-center justify-between px-4 py-3 text-left"
          onClick={() => setShowConflicts((v) => !v)}
        >
          <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-slate-500">
            Conflicts
            {openConflicts.length > 0 && (
              <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300 normal-case tracking-normal">
                {openConflicts.length} open
              </span>
            )}
          </span>
          {showConflicts ? (
            <ChevronUp className="h-4 w-4 text-slate-500" />
          ) : (
            <ChevronDown className="h-4 w-4 text-slate-500" />
          )}
        </button>
        {showConflicts && (
          <div className="border-t border-[color:var(--oc-border-soft)] px-4 pb-4 pt-3">
            <ConflictPanel
              projectId={projectId}
              conflicts={openConflicts}
              onResolve={handleResolveConflict}
            />
          </div>
        )}
      </div>

      {/* Section 4: Add/Edit Modal */}
      {showAddModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
          <div className="w-full max-w-xl rounded-lg border border-[color:var(--oc-border-soft)] bg-[color:var(--oc-surface)] p-5 shadow-2xl max-h-[90vh] overflow-y-auto">
            <h3 className="mb-4 text-sm font-semibold text-white">
              {editingEntry ? `Edit guidance #${editingEntry.id}` : 'Add guidance'}
            </h3>
            <GuidanceForm
              initial={editingEntry}
              advancedMode={advancedMode}
              saving={formSaving}
              onSubmit={handleFormSubmit}
              onCancel={() => {
                setShowAddModal(false);
                setEditingEntry(null);
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
