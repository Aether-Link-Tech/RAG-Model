// Holds the signed-in user's tokens.
// Kept in memory and in sessionStorage (cleared when the tab closes) so a page
// refresh doesn't log the user out. Never stored in localStorage.
// The access token is short-lived; the refresh token renews it (FR-002).
import type { User } from '../types';

const STORAGE_KEY = 'aether.session';

interface StoredSession {
  token: string;
  refreshToken: string | null;
  user: User;
  expiresAt: number; // epoch ms (access token)
}

let current: StoredSession | null = load();
const listeners = new Set<() => void>();

function load(): StoredSession | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredSession;
    // An expired access token is still useful while a refresh token exists.
    if (!parsed.token || (parsed.expiresAt <= Date.now() && !parsed.refreshToken)) {
      sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return { ...parsed, refreshToken: parsed.refreshToken ?? null };
  } catch {
    return null;
  }
}

function persist() {
  try {
    if (current) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(current));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private mode: memory only */
  }
}

function notify() {
  listeners.forEach((listener) => listener());
}

export const session = {
  /** Current access token, or null when signed out or expired. */
  get token(): string | null {
    if (!current) return null;
    if (current.expiresAt <= Date.now()) {
      if (!current.refreshToken) session.clear();
      return null;
    }
    return current.token;
  },

  get refreshToken(): string | null {
    return current?.refreshToken ?? null;
  },

  get user(): User | null {
    return current?.user ?? null;
  },

  /** Signed in: a valid access token, or a refresh token that can renew it. */
  isAuthenticated(): boolean {
    return session.token !== null || session.refreshToken !== null;
  },

  start(token: string, user: User, expiresInSeconds: number, refreshToken: string | null = null) {
    current = { token, user, refreshToken, expiresAt: Date.now() + expiresInSeconds * 1000 };
    persist();
    notify();
  },

  clear() {
    if (!current) return;
    current = null;
    persist();
    notify();
  },

  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};
