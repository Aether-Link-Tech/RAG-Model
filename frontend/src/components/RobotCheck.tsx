// Visual captcha only. Replace the setTimeout with a real CAPTCHA check when the backend supports it.
import { useState } from 'react';
import { Check, RefreshCw } from 'lucide-react';

type CheckState = 'idle' | 'loading' | 'verified';

interface RobotCheckProps {
  verified: boolean;
  onVerified: (value: boolean) => void;
}

export default function RobotCheck({ verified, onVerified }: RobotCheckProps) {
  const [state, setState] = useState<CheckState>(verified ? 'verified' : 'idle');

  function handleClick() {
    if (state === 'loading') return;
    if (state === 'verified') {
      setState('idle');
      onVerified(false);
      return;
    }
    setState('loading');
    setTimeout(() => {
      setState('verified');
      onVerified(true);
    }, 1300);
  }

  return (
    <button
      type="button"
      className={`robot-check robot-check--${state}`}
      onClick={handleClick}
      aria-pressed={state === 'verified'}
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
