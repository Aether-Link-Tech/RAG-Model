// Signed-in shell: top bar with navigation, user chip and logout (FR-003 UI).
import { useState } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { FileText, LogOut, Search, ShieldAlert, ShieldCheck } from 'lucide-react';
import Logo from '../components/Logo';
import { useToast } from '../components/Toast';
import { useSession } from '../hooks/useSession';
import { logout } from '../services/authService';

export default function AppLayout() {
  const { user } = useSession();
  const navigate = useNavigate();
  const toast = useToast();
  const [leaving, setLeaving] = useState(false);

  async function handleLogout() {
    setLeaving(true);
    await logout();
    toast.info('Signed out', 'See you soon.');
    navigate('/login', { replace: true });
  }

  const initials = (user?.email ?? '?').slice(0, 2).toUpperCase();

  return (
    <div className="app-shell">
      <header className="app-bar">
        <div className="app-bar__inner">
          <Logo />
          <nav className="app-nav" aria-label="Main">
            <NavLink to="/documents" className={({ isActive }) => `app-nav__link${isActive ? ' is-active' : ''}`}>
              <FileText size={16} /> Documents
            </NavLink>
            {user?.role === 'admin' && (
              <NavLink to="/admin/audit" className={({ isActive }) => `app-nav__link${isActive ? ' is-active' : ''}`}>
                <ShieldAlert size={16} /> Audit log
              </NavLink>
            )}
            <span className="app-nav__link is-disabled" title="Coming in Sprint 2 (hybrid search)">
              <Search size={16} /> Search <small>soon</small>
            </span>
          </nav>
          <div className="app-user">
            {user?.role === 'admin' && (
              <span className="badge badge--admin">
                <ShieldCheck size={12} /> Admin
              </span>
            )}
            <span className="app-user__avatar" aria-hidden="true">
              {initials}
            </span>
            <span className="app-user__email" title={user?.email}>
              {user?.email}
            </span>
            <button type="button" className="button button--ghost app-user__logout" onClick={handleLogout} disabled={leaving}>
              {leaving ? <span className="spinner" /> : <LogOut size={16} />} Log out
            </button>
          </div>
        </div>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
