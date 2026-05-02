import { useEffect, useState } from 'react';
import { Eye, KeyRound, Save, Shield, FolderCog, RefreshCw, UserCircle2 } from 'lucide-react';
import { settingsAPI } from '../api/client';
import type { AppSettings } from '../types/api';

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingProfile, setSavingProfile] = useState(false);
  const [savingSystem, setSavingSystem] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);
  const [revealingSecret, setRevealingSecret] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [workspaceRoot, setWorkspaceRoot] = useState('');
  const [mobileApiKey, setMobileApiKey] = useState('');
  const [agentBackend, setAgentBackend] = useState('');
  const [agentModelFamily, setAgentModelFamily] = useState('');
  const [adaptationProfile, setAdaptationProfile] = useState('');
  const [policyProfile, setPolicyProfile] = useState('');
  const [revealedMobileSecret, setRevealedMobileSecret] = useState<string | null>(null);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');

  const loadSettings = async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await settingsAPI.get();
      const data = response.data;
      setSettings(data);
      setName(data.account.name || '');
      setWorkspaceRoot(data.system.workspace_root || '');
      setAgentBackend(data.system.agent_backend || '');
      setAgentModelFamily(data.system.agent_model_family || '');
      setAdaptationProfile(data.system.agent_adaptation_profile || '');
      setPolicyProfile(data.system.orchestration_policy_profile || '');
      setMobileApiKey('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadSettings();
  }, []);

  const refreshFromResponse = (data: AppSettings) => {
    setSettings(data);
    setName(data.account.name || '');
    setWorkspaceRoot(data.system.workspace_root || '');
    setAgentBackend(data.system.agent_backend || '');
    setAgentModelFamily(data.system.agent_model_family || '');
    setAdaptationProfile(data.system.agent_adaptation_profile || '');
    setPolicyProfile(data.system.orchestration_policy_profile || '');
    setMobileApiKey('');
  };

  const handleProfileSave = async () => {
    setSavingProfile(true);
    setError(null);
    setMessage(null);
    try {
      const response = await settingsAPI.updateProfile({ name: name.trim() || null });
      refreshFromResponse(response.data);
      setMessage('Profile updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update profile');
    } finally {
      setSavingProfile(false);
    }
  };

  const handleSystemSave = async (rotateMobileKey = false) => {
    setSavingSystem(true);
    setError(null);
    setMessage(null);
    try {
      const response = await settingsAPI.updateSystem({
        workspace_root: workspaceRoot.trim(),
        mobile_api_key: mobileApiKey.trim() || undefined,
        rotate_mobile_api_key: rotateMobileKey,
        agent_backend: agentBackend || undefined,
        agent_model_family: agentModelFamily.trim() || undefined,
        agent_adaptation_profile: adaptationProfile || undefined,
        orchestration_policy_profile: policyProfile || undefined,
      });
      refreshFromResponse(response.data);
      setRevealedMobileSecret(null);
      setMessage(rotateMobileKey ? 'Mobile API key rotated.' : 'System settings updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update system settings');
    } finally {
      setSavingSystem(false);
    }
  };

  const handlePasswordChange = async () => {
    setChangingPassword(true);
    setError(null);
    setMessage(null);
    try {
      await settingsAPI.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword('');
      setNewPassword('');
      setMessage('Password updated successfully.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to change password');
    } finally {
      setChangingPassword(false);
    }
  };

  const handleRevealSecret = async () => {
    setRevealingSecret(true);
    setError(null);
    try {
      const response = await settingsAPI.revealMobileSecret();
      setRevealedMobileSecret(
        response.data.api_key_preview || response.data.detail || 'Secret is intentionally redacted by the API.'
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reveal mobile API key');
    } finally {
      setRevealingSecret(false);
    }
  };

  if (loading) {
    return <div className="text-slate-300">Loading settings...</div>;
  }

  if (!settings) {
    return <div className="text-red-400">{error || 'Settings unavailable'}</div>;
  }

  const selectedBackendDescriptor =
    settings.system.supported_backends.find((backend) => backend.name === agentBackend) ||
    settings.system.supported_backends[0];
  const activeBackendCapabilities =
    selectedBackendDescriptor?.capabilities || settings.system.backend_capabilities;
  const activeBackendHealth =
    selectedBackendDescriptor?.health || settings.system.backend_health;
  const activeBackendConfig = selectedBackendDescriptor?.config;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-white">Settings</h1>
        <p className="text-sm text-slate-400 mt-0.5">
          Manage your account, workspace path, and ClawMobile/OpenClaw connection details.
        </p>
      </div>

      {message && <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-emerald-300">{message}</div>}
      {error && <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-red-300">{error}</div>}

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-6">
        <div className="flex items-center gap-3 mb-4">
          <UserCircle2 className="h-5 w-5 text-sky-400" />
          <h2 className="text-sm font-semibold text-white">Account</h2>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="block text-sm text-slate-400 mb-2">Email</label>
            <input value={settings.account.email} disabled className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-slate-400" />
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">Display Name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
          </div>
        </div>
        <button onClick={handleProfileSave} disabled={savingProfile} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500 disabled:opacity-50">
          <Save className="h-4 w-4" />
          {savingProfile ? 'Saving...' : 'Save Profile'}
        </button>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-6">
        <div className="flex items-center gap-3 mb-4">
          <FolderCog className="h-5 w-5 text-sky-400" />
          <h2 className="text-sm font-semibold text-white">System</h2>
        </div>
        <div className="space-y-4">
          <div>
            <label className="block text-sm text-slate-400 mb-2">OpenClaw Workspace Root</label>
            <input value={workspaceRoot} onChange={(e) => setWorkspaceRoot(e.target.value)} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
            <p className="mt-2 text-xs text-slate-400">This becomes the root path used for project workspaces and isolation checks.</p>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
              <div className="text-sm text-slate-400">OpenClaw Gateway URL</div>
              <div className="mt-2 text-sm text-white break-all">{settings.system.openclaw_gateway_url}</div>
            </div>
            <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
              <div className="text-sm text-slate-400">Recommended Mobile Base URL</div>
              <div className="mt-2 text-sm text-white break-all">{settings.system.mobile_base_url}</div>
            </div>
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <div>
              <label className="block text-sm text-slate-400 mb-2">Agent Backend</label>
              <select
                value={agentBackend}
                onChange={(e) => {
                  const nextBackend = e.target.value;
                  setAgentBackend(nextBackend);
                  const selected = settings.system.supported_backends.find(
                    (backend) => backend.name === nextBackend
                  );
                  if (selected) {
                    setAgentModelFamily(selected.default_model_family);
                    setAdaptationProfile(selected.config.adaptation_profiles[0] || '');
                  }
                }}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white"
              >
                {settings.system.supported_backends.map((backend) => (
                  <option key={backend.name} value={backend.name}>
                    {backend.display_name} {backend.available ? '' : '(Unavailable)'}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-slate-400 mb-2">Model Family</label>
              <input
                value={agentModelFamily}
                onChange={(e) => setAgentModelFamily(e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white"
              />
            </div>
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">Adaptation Profile</label>
            <select
              value={adaptationProfile}
              onChange={(e) => setAdaptationProfile(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white"
            >
              {settings.system.available_adaptation_profiles
                .filter((profile) => {
                  const backend = selectedBackendDescriptor;
                  if (!backend) {
                    return true;
                  }
                  return backend.config.adaptation_profiles.includes(profile.name);
                })
                .map((profile) => (
                  <option key={profile.name} value={profile.name}>
                    {profile.display_name}
                  </option>
                ))}
            </select>
            <p className="mt-2 text-xs text-slate-400">
              {settings.system.available_adaptation_profiles.find((profile) => profile.name === adaptationProfile)?.description}
            </p>
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">Policy Profile</label>
            <select
              value={policyProfile}
              onChange={(e) => setPolicyProfile(e.target.value)}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white"
            >
              {settings.system.available_policy_profiles.map((profile) => (
                <option key={profile.name} value={profile.name}>
                  {profile.display_name}
                </option>
              ))}
            </select>
            <p className="mt-2 text-xs text-slate-400">
              {settings.system.available_policy_profiles.find((profile) => profile.name === policyProfile)?.description}
            </p>
            <p className="mt-2 text-xs text-slate-400">
              {settings.system.available_policy_profiles.find((profile) => profile.name === policyProfile)?.effects?.restore_behavior_label}
            </p>
          </div>
          <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
            <div className="text-sm text-slate-400">Backend Capabilities</div>
            <div className="mt-2 text-xs text-slate-300">
              {Object.entries(activeBackendCapabilities)
                .filter(([, value]) => value === true)
                .map(([key]) => key.replace(/^supports_/, '').replace(/_/g, ' '))
                .join(' • ') || 'No capabilities reported'}
            </div>
            <div className="mt-2 text-xs text-slate-400">
              MCP support: {activeBackendCapabilities.mcp_capable ? 'supported by descriptor' : 'not declared'}
            </div>
          </div>
          {activeBackendConfig && (
            <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
              <div className="text-sm text-slate-400">Backend Configuration</div>
              <div className="mt-2 space-y-1 text-xs text-slate-300">
                <div>Transport: {activeBackendConfig.transport_mode}</div>
                <div>Auth: {activeBackendConfig.auth_mode}</div>
                <div>Prompt format: {activeBackendConfig.supported_prompt_format}</div>
                <div>Streaming: {activeBackendConfig.streaming_mode}</div>
                <div>
                  Required env vars:{' '}
                  {activeBackendConfig.required_env_vars.length > 0
                    ? activeBackendConfig.required_env_vars.join(', ')
                    : 'None'}
                </div>
              </div>
            </div>
          )}
          <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
            <div className="text-sm text-slate-400">Backend Readiness</div>
            <div className="mt-2 text-sm text-white">
              {activeBackendHealth.ready ? 'Ready' : 'Unavailable'} ({activeBackendHealth.status})
            </div>
            <div className="mt-2 text-xs text-slate-400">
              {selectedBackendDescriptor?.implemented
                ? 'This backend has a runtime adapter in the codebase.'
                : 'This backend is listed for architecture planning only and cannot be selected for live execution yet.'}
            </div>
            {activeBackendHealth.errors.length > 0 && (
              <div className="mt-2 text-xs text-red-300">
                {activeBackendHealth.errors.join(' ')}
              </div>
            )}
            {activeBackendHealth.warnings.length > 0 && (
              <div className="mt-2 text-xs text-amber-300">
                {activeBackendHealth.warnings.join(' ')}
              </div>
            )}
          </div>
        </div>
        <button onClick={() => handleSystemSave(false)} disabled={savingSystem} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500 disabled:opacity-50">
          <Save className="h-4 w-4" />
          {savingSystem ? 'Saving...' : 'Save System Settings'}
        </button>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-6">
        <div className="flex items-center gap-3 mb-4">
          <Shield className="h-5 w-5 text-sky-400" />
          <h2 className="text-sm font-semibold text-white">Mobile API Key</h2>
        </div>
        <div className="space-y-4">
          <div className="rounded-lg border border-slate-700 bg-slate-950 p-4">
            <div className="text-sm text-slate-400">Current Key</div>
            <div className="mt-2 text-sm text-white">{settings.system.mobile_api_key_preview || 'Not configured'}</div>
            <div className="mt-1 text-xs text-slate-400">Source: {settings.system.mobile_api_key_source || 'none'}</div>
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">Set Custom Mobile API Key</label>
            <input value={mobileApiKey} onChange={(e) => setMobileApiKey(e.target.value)} placeholder="Leave blank to keep current key" className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
          </div>
          {revealedMobileSecret && (
            <div className="rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-amber-200 break-all">
              <div className="font-medium">Current X-OpenClaw-API-Key</div>
              <div className="mt-2 text-sm">{revealedMobileSecret}</div>
            </div>
          )}
          <div className="flex flex-wrap gap-3">
            <button onClick={handleRevealSecret} disabled={revealingSecret} className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-4 py-2 text-slate-200 hover:bg-slate-800 disabled:opacity-50">
              <Eye className="h-4 w-4" />
              {revealingSecret ? 'Revealing...' : 'Reveal Current Key'}
            </button>
            <button onClick={() => handleSystemSave(true)} disabled={savingSystem} className="inline-flex items-center gap-2 rounded-lg border border-slate-700 px-4 py-2 text-slate-200 hover:bg-slate-800 disabled:opacity-50">
              <RefreshCw className="h-4 w-4" />
              Rotate Key
            </button>
            <button onClick={() => handleSystemSave(false)} disabled={savingSystem} className="inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500 disabled:opacity-50">
              <KeyRound className="h-4 w-4" />
              Save Mobile Key
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-slate-700 bg-slate-800 p-6">
        <div className="flex items-center gap-3 mb-4">
          <Shield className="h-5 w-5 text-sky-400" />
          <h2 className="text-sm font-semibold text-white">Password</h2>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="block text-sm text-slate-400 mb-2">Current Password</label>
            <input type="password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
          </div>
          <div>
            <label className="block text-sm text-slate-400 mb-2">New Password</label>
            <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-white" />
          </div>
        </div>
        <button onClick={handlePasswordChange} disabled={changingPassword || !currentPassword || !newPassword} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-sky-600 px-4 py-2 text-white hover:bg-sky-500 disabled:opacity-50">
          <Save className="h-4 w-4" />
          {changingPassword ? 'Updating...' : 'Change Password'}
        </button>
      </section>
    </div>
  );
}
