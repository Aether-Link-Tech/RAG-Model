import { afterEach, describe, expect, it, vi } from 'vitest';
import api from './api';
import { session } from './session';
import type { User } from '../types';

const user: User = { id: 'u1', email: 'a@b.com', role: 'user', created_at: '', is_deleted: false };

afterEach(() => {
  session.clear();
  vi.useRealTimers();
});

describe('session', () => {
  it('starts, persists for the tab and clears', () => {
    session.start('tok', user, 60);
    expect(session.isAuthenticated()).toBe(true);
    expect(sessionStorage.getItem('aether.session')).toContain('tok');
    session.clear();
    expect(session.token).toBeNull();
    expect(sessionStorage.getItem('aether.session')).toBeNull();
  });

  it('expires after expires_in seconds', () => {
    vi.useFakeTimers();
    session.start('tok', user, 10);
    vi.advanceTimersByTime(11_000);
    expect(session.isAuthenticated()).toBe(false);
  });

  it('notifies subscribers on sign in / out', () => {
    const listener = vi.fn();
    const unsubscribe = session.subscribe(listener);
    session.start('tok', user, 60);
    session.clear();
    unsubscribe();
    expect(listener).toHaveBeenCalledTimes(2);
  });
});

describe('api client', () => {
  it('sends the Bearer token on every request', async () => {
    session.start('secret-token', user, 60);
    let seen: string | undefined;
    await api.get('/documents', {
      adapter: async (config) => {
        seen = String(config.headers.Authorization);
        return { data: [], status: 200, statusText: 'OK', headers: {}, config };
      },
    });
    expect(seen).toBe('Bearer secret-token');
  });

  it('ends the session and announces it on 401', async () => {
    session.start('stale', user, 60);
    const onExpired = vi.fn();
    window.addEventListener('auth:expired', onExpired);
    await expect(
      api.get('/documents', {
        adapter: async (config) => {
          throw Object.assign(new Error('401'), { isAxiosError: true, config, response: { status: 401, data: {} } });
        },
      }),
    ).rejects.toBeTruthy();
    window.removeEventListener('auth:expired', onExpired);
    expect(session.isAuthenticated()).toBe(false);
    expect(onExpired).toHaveBeenCalledOnce();
  });
});
