import { afterEach, describe, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';
import type { ProxyOptions } from 'vite';
import viteConfig from './vite.config';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

async function proxy() {
  const config = typeof viteConfig === 'function'
    ? await viteConfig({ command: 'serve', mode: 'test' })
    : await viteConfig;
  return config.server?.proxy?.['/api'] as ProxyOptions;
}

describe('development API routing', () => {
  it('preserves the conventional target and WebSocket proxy by default', async () => {
    vi.stubEnv('ORCHESTRATOR_DEV_API_TARGET', '');
    expect(await proxy()).toMatchObject({ target: 'http://localhost:8080', ws: true });
  });

  it('uses an explicit server-side target for HTTP authentication and WebSockets', async () => {
    vi.stubEnv('ORCHESTRATOR_DEV_API_TARGET', 'http://127.0.0.1:18740');
    vi.stubEnv('DEV', true);
    vi.stubEnv('VITE_API_URL', '');
    vi.stubEnv('VITE_API_WS_HOST', '');
    const { default: api, authAPI, sessionsAPI } = await import('./src/api/client');
    const requests: Array<{ baseURL?: string; url?: string }> = [];
    api.defaults.adapter = async (config) => {
      requests.push({ baseURL: config.baseURL, url: config.url });
      return { data: { ticket: 'fixture-ticket' }, status: 200, statusText: 'OK', headers: {}, config };
    };
    await authAPI.getMe();
    expect(requests[0]).toEqual({ baseURL: '/api/v1', url: '/auth/me' });
    expect((await proxy()).target).toBe('http://127.0.0.1:18740');
    const socket = vi.fn();
    vi.stubGlobal('WebSocket', socket);
    await sessionsAPI.getStatusStream(7);
    const expectedHost = window.location.host.replace(/^localhost(?=:|$)/, '127.0.0.1');
    expect(socket).toHaveBeenCalledWith(`ws://${expectedHost}/api/v1/sessions/7/status?ticket=fixture-ticket`);
    expect(requests[1]).toEqual({ baseURL: '/api/v1', url: '/auth/ws-ticket' });
  });

  it('preserves an explicit direct API and WebSocket override', async () => {
    vi.stubEnv('DEV', true);
    vi.stubEnv('VITE_API_URL', 'http://127.0.0.1:18740');
    vi.stubEnv('VITE_API_WS_HOST', '127.0.0.1:19000');
    const { default: api, sessionsAPI } = await import('./src/api/client');
    expect(api.defaults.baseURL).toBe('http://127.0.0.1:18740/api/v1');
    api.defaults.adapter = async config => ({ data: { ticket: 'fixture' }, status: 200, statusText: 'OK', headers: {}, config });
    const socket = vi.fn();
    vi.stubGlobal('WebSocket', socket);
    await sessionsAPI.getStatusStream(7);
    expect(socket).toHaveBeenCalledWith('ws://127.0.0.1:19000/api/v1/sessions/7/status?ticket=fixture');
  });

  it('rejects a missing backend instead of returning an authenticated result', async () => {
    const { default: api, authAPI } = await import('./src/api/client');
    api.defaults.adapter = async () => { throw new AxiosError('Network Error', 'ERR_NETWORK'); };
    await expect(authAPI.getMe()).rejects.toMatchObject({ code: 'ERR_NETWORK' });
    await expect(authAPI.login('eval@local.dev', 'fixture-only')).rejects.toMatchObject({ code: 'ERR_NETWORK' });
  });

  it.each(['redis://127.0.0.1:6379', 'http://user:secret@localhost:18740', 'http://localhost:18740/api/v1'])('refuses unsafe proxy configuration %s', async target => {
    vi.stubEnv('ORCHESTRATOR_DEV_API_TARGET', target);
    await expect(proxy()).rejects.toThrow('HTTP(S) origin without credentials');
  });
});
