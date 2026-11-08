// Register page (/signup) — AL-119
import { useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Check, Lock, Mail, User, UserPlus } from 'lucide-react';
import InputField from '../components/InputField';
import RobotCheck from '../components/RobotCheck';
import Button from '../components/Button';
import Alert from '../components/Alert';
import CheckEmail from '../components/CheckEmail';
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

// FR-001 password rules: 8+ chars, upper, lower, digit, special
export const PASSWORD_RULES: { label: string; test: (p: string) => boolean }[] = [
  { label: `${PASSWORD_MIN}+ characters`, test: (p) => p.length >= PASSWORD_MIN },
  { label: 'Upper & lower case', test: (p) => /[A-Z]/.test(p) && /[a-z]/.test(p) },
  { label: 'A number', test: (p) => /\d/.test(p) },
  { label: 'A special character', test: (p) => /[^A-Za-z0-9]/.test(p) },
];

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface SignupForm {
  fullName: string;
  email: string;
  password: string;
  confirm: string;
}

// Returns the first validation error, or '' when valid. Limits match backend UserCreate.
export function validate(form: SignupForm, isHuman: boolean): string {
  const email = form.email.trim();

  if (!form.fullName.trim()) return 'Please enter your full name.';
  if (form.fullName.trim().length > NAME_MAX) return `Full name must be at most ${NAME_MAX} characters.`;

  if (email.length < EMAIL_MIN || email.length > EMAIL_MAX) {
    return `Email must be between ${EMAIL_MIN} and ${EMAIL_MAX} characters.`;
  }
  if (!EMAIL_PATTERN.test(email)) return 'Please enter a valid email address.';

  if (form.password.length < PASSWORD_MIN) return `Password must be at least ${PASSWORD_MIN} characters.`;
  if (form.password.length > PASSWORD_MAX) return `Password must be at most ${PASSWORD_MAX} characters.`;
  const missing = PASSWORD_RULES.filter((rule) => !rule.test(form.password));
  if (missing.length) return `Password needs: ${missing.map((r) => r.label.toLowerCase()).join(', ')}.`;
  if (form.password !== form.confirm) return 'Passwords do not match. Please enter them again.';

  if (!isHuman) return 'Please confirm that you are not a robot.';
  return '';
}

export default function SignupPage() {
  const [form, setForm] = useState<SignupForm>({ fullName: '', email: '', password: '', confirm: '' });
  const [captcha, setCaptcha] = useState<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [registeredEmail, setRegisteredEmail] = useState('');

  const strength = passwordStrength(form.password);

  function update(field: keyof SignupForm, value: string) {
    setForm((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();

    const problem = validate(form, Boolean(captcha));
    setError(problem);
    if (problem) return;

    setLoading(true);
    try {
      await signup({
        fullName: form.fullName,
        email: form.email,
        password: form.password,
        captcha: captcha!,
      });
      setRegisteredEmail(form.email.trim().toLowerCase());
    } catch (err) {
      // A captcha solution is single-use: ask for a fresh one after any failed attempt.
      setCaptchaReset((n) => n + 1);
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
    return <CheckEmail email={registeredEmail} />;
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

        {form.password && (
          <ul className="password-rules" aria-label="Password requirements">
            {PASSWORD_RULES.map((rule) => {
              const ok = rule.test(form.password);
              return (
                <li key={rule.label} className={ok ? 'is-met' : ''}>
                  <Check size={12} /> {rule.label}
                </li>
              );
            })}
          </ul>
        )}

        <div className="form-options">
          <RobotCheck
            resetKey={captchaReset}
            onSolved={(solution) => {
              setCaptcha(solution);
              if (solution) setError('');
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
