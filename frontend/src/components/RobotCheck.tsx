// "I'm not a robot" check backed by the server's proof-of-work captcha (FR-001 / FR-002).
// Clicking fetches a challenge and solves it in the browser; the solution is passed to
// the form, which sends it with the request. Each solution is single-use, so callers
// reset it (resetKey) after every submit.
import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, Check, RefreshCw } from 'lucide-react';
import { verifyHuman } from '../services/captcha';

type CheckState = 'idle' | 'loading' | 'verified' | 'error';

interface RobotCheckProps {
  /** Receives the solution payload, or null when cleared. */
  onSolved: (solution: string | null) => void;
  /** Change this value to clear the check (e.g. after a submit used the solution). */
  resetKey?: number;
}

export default function RobotCheck({ onSolved, resetKey = 0 }: RobotCheckProps) {
  const [state, setState] = useState<CheckState>('idle');
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setState('idle');
    onSolved(null);
    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  async function handleClick() {
    if (state === 'loading') return;
    if (state === 'verified') {
      setState('idle');
      onSolved(null);
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setState('loading');
    try {
      const solution = await verifyHuman(controller.signal);
      if (controller.signal.aborted) return;
      setState('verified');
      onSolved(solution);
    } catch {
      if (controller.signal.aborted) return;
      setState('error');
      onSolved(null);
    }
  }

  const label =
    state === 'loading'
      ? 'Verifying…'
      : state === 'verified'
        ? 'Verified — you’re human'
        : state === 'error'
          ? 'Verification failed — try again'
          : 'I’m not a robot';

  return (
    <button
      type="button"
      className={`robot-check robot-check--${state === 'error' ? 'idle' : state}`}
      onClick={handleClick}
      aria-pressed={state === 'verified'}
      aria-busy={state === 'loading'}
    >
      <span className="robot-check__box">
        {state === 'loading' && <span className="robot-check__spinner" />}
        {state === 'verified' && <Check size={15} strokeWidth={3} />}
        {state === 'error' && <AlertTriangle size={14} />}
      </span>
      <span className="robot-check__text">{label}</span>
      <span className="robot-check__brand" aria-hidden="true">
        <RefreshCw size={20} />
        <small>Protected</small>
      </span>
    </button>
  );
}
