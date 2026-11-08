// Shared frame for auth pages: background, header, welcome text and the form card
import { Link, Outlet, useLocation } from 'react-router-dom';
import { ArrowRight, Check, ShieldCheck } from 'lucide-react';
import Logo from '../components/Logo';

export default function AuthLayout() {
  const location = useLocation();
  const isLogin = location.pathname === '/login';

  return (
    <main className="auth-page">
      <div className="auth-page__glow auth-page__glow--one" aria-hidden="true" />
      <div className="auth-page__glow auth-page__glow--two" aria-hidden="true" />

      <header className="auth-header">
        <Logo />
        <Link to={isLogin ? '/signup' : '/login'} className="button button--glass">
          {isLogin ? 'Create account' : 'Sign in'}
          <ArrowRight size={17} className="button__arrow" />
        </Link>
      </header>

      <section className="auth-layout">
        <div className="auth-copy">
          <span className="eyebrow fade-up" style={{ animationDelay: '60ms' }}>
            <ShieldCheck size={15} /> Private by design
          </span>
          <h1 className="fade-up" style={{ animationDelay: '140ms' }}>
            Your knowledge.
            <br />
            <span>Clear answers.</span>
          </h1>
          <p className="fade-up" style={{ animationDelay: '220ms' }}>
            Search sensitive company knowledge with a secure AI workspace that runs on your own
            infrastructure — every answer backed by a source you can check.
          </p>
          <div className="trust-row fade-up" style={{ animationDelay: '300ms' }}>
            <span>
              <Check size={14} /> Offline capable
            </span>
            <span>
              <Check size={14} /> Source citations
            </span>
            <span>
              <Check size={14} /> Role-based access
            </span>
          </div>
        </div>

        {/* key replays the card animation when the route changes */}
        <div key={location.pathname} className="auth-card">
          <Outlet />
        </div>
      </section>

      <footer className="auth-footer">
        <span>© {new Date().getFullYear()} Aether Link Tech</span>
        <span>Private infrastructure · Encrypted at rest</span>
      </footer>
    </main>
  );
}
