import { useSyncExternalStore } from 'react';
import { session } from '../services/session';

// Re-renders when the user signs in or out (or the token expires).
export function useSession() {
  const user = useSyncExternalStore(session.subscribe, () => session.user, () => null);
  return { user, isAuthenticated: user !== null };
}
