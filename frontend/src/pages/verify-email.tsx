// /verify-email?token=… — opened from the verification email (FR-001)
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowRight, CircleCheckBig, CircleX, Mail, MailWarning } from 'lucide-react';
import Button from '../components/Button';
import CheckEmail from '../components/CheckEmail';
import InputField from '../components/InputField';
import { verifyEmail } from '../services/authService';
import { getErrorMessage, statusOf } from '../services/api';

type State = 'verifying' | 'verified' | 'expired' | 'invalid';

export default function VerifyEmailPage() {
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';
  const [state, setState] = useState<State>(token ? 'verifying' : 'invalid');
  const [message, setMessage] = useState('');
  const [email, setEmail] = useState('');
  const [resendFor, setResendFor] = useState('');
  const started = useRef(false);

  useEffect(() => {
    // The link is single-use: guard against React StrictMode's double effect run.
    if (!token || started.current) return;
    started.current = true;
    verifyEmail(token)
      .then((detail) => {
        setMessage(detail);
        setState('verified');
      })
      .catch((err) => {
        setMessage(getErrorMessage(err));
        setState(statusOf(err) === 410 ? 'expired' : 'invalid');
      });
  }, [token]);

  if (resendFor) return <CheckEmail email={resendFor} title="Request a new link" intro="We’ll send a new link to" />;

  if (state === 'verifying') {
    return (
      <div className="signup-success" aria-busy="true">
        <span className="signup-success__icon signup-success__icon--pending">
          <span className="robot-check__spinner" />
        </span>
        <h2>Verifying your email…</h2>
        <p className="auth-card__subtitle">This only takes a moment.</p>
      </div>
    );
  }

  if (state === 'verified') {
    return (
      <div className="signup-success" data-testid="verified">
        <span className="signup-success__icon">
          <CircleCheckBig size={30} />
        </span>
        <h2>Email verified</h2>
        <p className="auth-card__subtitle">{message || 'Your account is active. You can sign in now.'}</p>
        <Link to="/login" className="button button--primary button--wide">
          Sign in <ArrowRight size={18} className="button__arrow" />
        </Link>
      </div>
    );
  }

  function requestNew(event: FormEvent) {
    event.preventDefault();
    if (email.trim()) setResendFor(email.trim().toLowerCase());
  }

  return (
    <div className="signup-success">
      <span className="signup-success__icon signup-success__icon--error">
        {state === 'expired' ? <MailWarning size={30} /> : <CircleX size={30} />}
      </span>
      <h2>{state === 'expired' ? 'This link has expired' : 'This link can’t be used'}</h2>
      <p className="auth-card__subtitle">
        {message || 'The verification link is invalid or has already been used.'} Enter your email to get a new one.
      </p>
      <form onSubmit={requestNew} className="resend-box" noValidate>
        <InputField
          label="Email address"
          icon={Mail}
          type="email"
          autoComplete="email"
          placeholder="name@company.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <Button type="submit" wide>
          Continue <ArrowRight size={18} className="button__arrow" />
        </Button>
      </form>
      <p className="auth-alternate">
        Already verified? <Link to="/login">Sign in</Link>
      </p>
    </div>
  );
}
