import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { useSession } from '../hooks/useSession';

// Pages that need a signed-in user; others are sent to /login and come back after.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useSession();
  const location = useLocation();
  if (!isAuthenticated) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

// Login / signup are skipped when already signed in.
export function GuestOnly({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useSession();
  if (isAuthenticated) return <Navigate to="/documents" replace />;
  return <>{children}</>;
}

// Admin-only pages (FR-004). The API enforces this too; this only hides the page.
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { user } = useSession();
  if (user?.role !== 'admin') return <Navigate to="/documents" replace />;
  return <>{children}</>;
}
