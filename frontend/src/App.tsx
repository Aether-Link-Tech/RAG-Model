import { useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import AuthLayout from './layouts/AuthLayout';
import AppLayout from './layouts/AppLayout';
import SignupPage from './pages/signup';
import LoginPage from './pages/login';
import DocumentsPage from './pages/documents';
import VerifyEmailPage from './pages/verify-email';
import AuditLogPage from './pages/audit-log';
import { GuestOnly, RequireAdmin, RequireAuth } from './routes/guards';
import { ToastProvider, useToast } from './components/Toast';

// When the token expires mid-session, go back to login with a clear message.
function SessionWatcher() {
  const navigate = useNavigate();
  const toast = useToast();
  useEffect(() => {
    const onExpired = () => {
      toast.info('Session expired', 'Please sign in again.');
      navigate('/login', { replace: true });
    };
    window.addEventListener('auth:expired', onExpired);
    return () => window.removeEventListener('auth:expired', onExpired);
  }, [navigate, toast]);
  return null;
}

export function AppRoutes() {
  return (
    <>
      <SessionWatcher />
      <Routes>
        <Route
          element={
            <GuestOnly>
              <AuthLayout />
            </GuestOnly>
          }
        >
          <Route path="/login" element={<LoginPage />} />
          <Route path="/signup" element={<SignupPage />} />
        </Route>

        {/* Opened from the verification email; works signed in or out. */}
        <Route element={<AuthLayout />}>
          <Route path="/verify-email" element={<VerifyEmailPage />} />
        </Route>

        <Route
          element={
            <RequireAuth>
              <AppLayout />
            </RequireAuth>
          }
        >
          <Route path="/documents" element={<DocumentsPage />} />
          <Route
            path="/admin/audit"
            element={
              <RequireAdmin>
                <AuditLogPage />
              </RequireAdmin>
            }
          />
        </Route>

        <Route path="*" element={<Navigate to="/documents" replace />} />
      </Routes>
    </>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </ToastProvider>
  );
}
