// Holds the signed-in user's access token.
// Kept in memory and in sessionStorage (cleared when the tab closes) so a page
// refresh doesn't log the user out. Never stored in localStorage.
import type { User } from '../types';

const STORAGE_KEY = 'aether.session';

interface StoredSession {
  token: string;
  user: User;
  expiresAt: number; // epoch ms
}

let current: StoredSession | null = load();
const listeners = new Set<() => void>();

function load(): StoredSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredSession;
    if (!parsed.token || parsed.expiresAt <= Date.now()) {
      sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function notify() {
  listeners.forEach((listener) => listener());
}

export const session = {
  get token(): string | null {
    if (current && current.expiresAt <= Date.now()) session.clear();
    return current?.token ?? null;
  },

  get user(): User | null {
    return session.token ? current!.user : null;
  },

  isAuthenticated(): boolean {
    return session.token !== null;
  },

  start(token: string, user: User, expiresInSeconds: number) {
    current = { token, user, expiresAt: Date.now() + expiresInSeconds * 1000 };
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(current));
    } catch {
      /* private mode: memory only */
    }
    notify();
  },

  clear() {
    if (!current) return;
    current = null;
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    notify();
  },

  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};
