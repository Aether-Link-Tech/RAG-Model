// Register page (/signup) — AL-119
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Lock, Mail, MailCheck, User, UserPlus } from 'lucide-react';
import InputField from '../components/InputField';
import RobotCheck from '../components/RobotCheck';
import Button from '../components/Button';
import Alert from '../components/Alert';
import { signup } from '../services/authService';
import { getErrorMessage, hasStatus } from '../services/api';
import { EMAIL_MAX, EMAIL_MIN, NAME_MAX, PASSWORD_MAX, PASSWORD_MIN } from '../services/config';

// Password strength score 0-4 for the strength bar
function passwordStrength(password: string): number {
  let score = 0;
  if (password.length >= PASSWORD_MIN) score++;
  if (/[A-Z]/.test(password) && /[a-z]/.test(password)) score++;
  if (/\d/.test(password)) score++;
  if (/[^A-Za-z0-9]/.test(password)) score++;
  return score;
}

const strengthLabels = ['Too short', 'Weak', 'Fair', 'Good', 'Strong'];

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface SignupForm {
  fullName: string;
  email: string;
  password: string;
  confirm: string;
}

// Returns the first validation error, or '' when valid. Limits match backend UserCreate.
function validate(form: SignupForm, isHuman: boolean): string {
  const email = form.email.trim();

  if (!form.fullName.trim()) return 'Please enter your full name.';
  if (form.fullName.trim().length > NAME_MAX) return `Full name must be at most ${NAME_MAX} characters.`;

  if (email.length < EMAIL_MIN || email.length > EMAIL_MAX) {
    return `Email must be between ${EMAIL_MIN} and ${EMAIL_MAX} characters.`;
  }
  if (!EMAIL_PATTERN.test(email)) return 'Please enter a valid email address.';

  if (form.password.length < PASSWORD_MIN) return `Password must be at least ${PASSWORD_MIN} characters.`;
  if (form.password.length > PASSWORD_MAX) return `Password must be at most ${PASSWORD_MAX} characters.`;
  if (form.password !== form.confirm) return 'Passwords do not match. Please enter them again.';

  if (!isHuman) return 'Please confirm that you are not a robot.';
  return '';
}

export default function SignupPage() {
  const [form, setForm] = useState<SignupForm>({ fullName: '', email: '', password: '', confirm: '' });
  const [isHuman, setIsHuman] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [registeredEmail, setRegisteredEmail] = useState('');

  const strength = passwordStrength(form.password);

  function update(field: keyof SignupForm, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();

    const problem = validate(form, isHuman);
    setError(problem);
    if (problem) return;

    setLoading(true);
    try {
      // Backend accepts only email + password (full name is not stored yet)
      await signup(form.email.trim(), form.password);
      setRegisteredEmail(form.email.trim());
    } catch (err) {
      if (hasStatus(err, 409)) {
        setError('An account with this email already exists. Try signing in instead.');
      } else {
        setError(getErrorMessage(err, 'We could not create your account. Please try again.'));
      }
    } finally {
      setLoading(false);
    }
  }

  if (registeredEmail) {
    return (
      <div className="signup-success">
        <span className="signup-success__icon">
          <MailCheck size={30} />
        </span>
        <h2>Check your email</h2>
        <p className="auth-card__subtitle">
          Your account was created. We sent a confirmation link to <strong>{registeredEmail}</strong>.
        </p>
        <Link to="/login" className="button button--primary button--wide">
          Go to sign in <ArrowRight size={18} className="button__arrow" />
        </Link>
      </div>
    );
  }

  return (
    <>
      <div className="auth-card__top">
        <span className="auth-card__icon">
          <UserPlus size={24} />
        </span>
        <span className="status-dot">Secure connection</span>
      </div>

      <h2>Create your workspace</h2>
      <p className="auth-card__subtitle">Start building your private knowledge base.</p>

      <form onSubmit={handleSubmit} noValidate>
        <InputField
          label="Full name"
          icon={User}
          autoComplete="name"
          placeholder="Alex Morgan"
          maxLength={NAME_MAX}
          value={form.fullName}
          onChange={(e) => update('fullName', e.target.value)}
        />
        <InputField
          label="Work email"
          icon={Mail}
          type="email"
          autoComplete="email"
          placeholder="name@company.com"
          maxLength={EMAIL_MAX}
          value={form.email}
          onChange={(e) => update('email', e.target.value)}
        />
        <div className="field-row">
          <InputField
            label="Password"
            icon={Lock}
            type="password"
            autoComplete="new-password"
            placeholder="8+ characters"
            maxLength={PASSWORD_MAX}
            value={form.password}
            onChange={(e) => update('password', e.target.value)}
          />
          <InputField
            label="Confirm"
            icon={Lock}
            type="password"
            autoComplete="new-password"
            placeholder="Repeat it"
            maxLength={PASSWORD_MAX}
            value={form.confirm}
            onChange={(e) => update('confirm', e.target.value)}
          />
        </div>

        {form.password && (
          <div className={`strength strength--${strength}`}>
            <div className="strength__bars">
              <span />
              <span />
              <span />
              <span />
            </div>
            <small>{strengthLabels[strength]}</small>
          </div>
        )}

        <div className="form-options">
          <RobotCheck
            verified={isHuman}
            onVerified={(value) => {
              setIsHuman(value);
              if (value) setError('');
            }}
          />
        </div>

        <Alert message={error} />

        <Button type="submit" wide loading={loading} loadingText="Creating account">
          Create account <ArrowRight size={18} className="button__arrow" />
        </Button>
      </form>

      <p className="auth-alternate">
        Already have an account? <Link to="/login">Sign in</Link>
      </p>
    </>
  );
}
