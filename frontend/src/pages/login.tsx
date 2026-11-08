// Login page (/login) — AL-122. Flow by Faisal (PR #16), styled with the shared auth design.
import { useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { ArrowRight, LockKeyhole, LogIn, Mail } from 'lucide-react';
import InputField from '../components/InputField';
import Button from '../components/Button';
import Alert from '../components/Alert';
import CheckEmail from '../components/CheckEmail';
import RobotCheck from '../components/RobotCheck';
import { useToast } from '../components/Toast';
import { login } from '../services/authService';
import { captchaRequired, getErrorMessage, hasStatus } from '../services/api';
import { EMAIL_MAX, PASSWORD_MAX, PASSWORD_MIN } from '../services/config';

export default function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // FR-002: after 3 failed attempts the server requires a captcha with every attempt.
  const [needsCaptcha, setNeedsCaptcha] = useState(false);
  const [captcha, setCaptcha] = useState<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);
  const [unverifiedEmail, setUnverifiedEmail] = useState('');

  const from = (location.state as { from?: string } | null)?.from ?? '/documents';

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError('Please enter your email and password.');
      return;
    }
    if (needsCaptcha && !captcha) {
      setError('Please confirm that you are not a robot.');
      return;
    }
    setError('');
    setLoading(true);
    try {
      const user = await login(email, password, captcha ?? undefined);
      toast.success('Welcome back', user.full_name || user.email);
      navigate(from, { replace: true });
    } catch (err) {
      if (captchaRequired(err)) setNeedsCaptcha(true);
      setCaptchaReset((n) => n + 1); // a captcha solution can only be used once
      if (hasStatus(err, 401)) setError('Invalid email or password.');
      else if (hasStatus(err, 403)) setUnverifiedEmail(email.trim().toLowerCase());
      else if (hasStatus(err, 423)) setError('Too many failed attempts. Sign-in is locked for 15 minutes.');
      else if (hasStatus(err, 429)) setError('Too many requests. Please wait a moment and try again.');
      else if (hasStatus(err, 400) && captchaRequired(err)) setError('Please complete the robot check to continue.');
      else setError(getErrorMessage(err, 'Unable to sign in. Please check your connection and try again.'));
    } finally {
      setLoading(false);
    }
  }

  if (unverifiedEmail) {
    return (
      <CheckEmail
        email={unverifiedEmail}
        title="Verify your email first"
        intro="Your account isn’t active yet. We sent a verification link to"
      />
    );
  }

  return (
    <>
      <div className="auth-card__top">
        <span className="auth-card__icon">
          <LogIn size={24} />
        </span>
        <span className="status-dot">Secure connection</span>
      </div>

      <h2>Welcome back</h2>
      <p className="auth-card__subtitle">Sign in to access your document workspace.</p>

      <form onSubmit={handleSubmit} noValidate>
        <InputField
          label="Email address"
          icon={Mail}
          type="email"
          autoComplete="email"
          placeholder="name@company.com"
          maxLength={EMAIL_MAX}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <InputField
          label="Password"
          icon={LockKeyhole}
          type="password"
          autoComplete="current-password"
          placeholder={`${PASSWORD_MIN}+ characters`}
          maxLength={PASSWORD_MAX}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        <div className="form-options">
          {needsCaptcha && (
            <div className="captcha-reveal">
              <RobotCheck resetKey={captchaReset} onSolved={setCaptcha} />
            </div>
          )}
        </div>
        <Alert message={error} />

        <Button type="submit" wide loading={loading} loadingText="Signing in">
          Sign in <ArrowRight size={18} className="button__arrow" />
        </Button>
      </form>

      <p className="auth-alternate">
        New here? <Link to="/signup">Create an account</Link>
      </p>
    </>
  );
}
