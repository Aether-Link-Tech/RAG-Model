// "I'm not a robot" check (FR-001 / FR-002).
// With a reCAPTCHA site key configured, renders Google's reCAPTCHA v2 widget and passes
// its token to the form. In local development (no key) it is a simple confirmation that
// yields a placeholder token; the backend skips verification there.
// Tokens are single-use: change `resetKey` after every submit to get a fresh one.
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, RefreshCw } from 'lucide-react';
import { DEV_CAPTCHA_TOKEN, loadRecaptcha, recaptchaEnabled } from '../services/captcha';
import { RECAPTCHA_SITE_KEY } from '../services/config';

interface RobotCheckProps {
  /** Receives the captcha token, or null when cleared/expired. */
  onSolved: (token: string | null) => void;
  /** Change this value to clear the check (e.g. after a submit used the token). */
  resetKey?: number;
}

function GoogleRecaptcha({ onSolved, resetKey = 0 }: RobotCheckProps) {
  const container = useRef<HTMLDivElement>(null);
  const widget = useRef<number | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    loadRecaptcha()
      .then((grecaptcha) =>
        grecaptcha.ready(() => {
          if (cancelled || !container.current || widget.current !== null) return;
          widget.current = grecaptcha.render(container.current, {
            sitekey: RECAPTCHA_SITE_KEY,
            callback: (token: string) => onSolved(token),
            'expired-callback': () => onSolved(null),
            'error-callback': () => onSolved(null),
          });
        }),
      )
      .catch((err: Error) => setError(err.message));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    onSolved(null);
    if (widget.current !== null) window.grecaptcha?.reset(widget.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  return (
    <div className="recaptcha">
      <div ref={container} />
      {error && (
        <p className="recaptcha__error" role="alert">
          <AlertTriangle size={14} /> {error}
        </p>
      )}
    </div>
  );
}

function LocalRobotCheck({ onSolved, resetKey = 0 }: RobotCheckProps) {
  const [state, setState] = useState<'idle' | 'loading' | 'verified'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setState('idle');
    onSolved(null);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  function handleClick() {
    if (state === 'loading') return;
    if (state === 'verified') {
      setState('idle');
      onSolved(null);
      return;
    }
    setState('loading');
    timer.current = setTimeout(() => {
      setState('verified');
      onSolved(DEV_CAPTCHA_TOKEN);
    }, 600);
  }

  return (
    <button
      type="button"
      className={`robot-check robot-check--${state}`}
      onClick={handleClick}
      aria-pressed={state === 'verified'}
      aria-busy={state === 'loading'}
      title="Development mode: set VITE_RECAPTCHA_SITE_KEY to use Google reCAPTCHA"
    >
      <span className="robot-check__box">
        {state === 'loading' && <span className="robot-check__spinner" />}
        {state === 'verified' && <Check size={15} strokeWidth={3} />}
      </span>
      <span className="robot-check__text">
        {state === 'loading' ? 'Verifying…' : state === 'verified' ? 'Verified — you’re human' : 'I’m not a robot'}
      </span>
      <span className="robot-check__brand" aria-hidden="true">
        <RefreshCw size={20} />
        <small>Protected</small>
      </span>
    </button>
  );
}

export default function RobotCheck(props: RobotCheckProps) {
  return recaptchaEnabled() ? <GoogleRecaptcha {...props} /> : <LocalRobotCheck {...props} />;
}
