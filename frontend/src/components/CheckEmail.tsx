// "Check your inbox" panel after sign-up (FR-001), with a captcha-protected resend.
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, MailCheck, Send } from 'lucide-react';
import Button from './Button';
import RobotCheck from './RobotCheck';
import { resendVerification } from '../services/authService';
import { getErrorMessage } from '../services/api';

interface CheckEmailProps {
  email: string;
  title?: string;
  intro?: string;
}

export default function CheckEmail({ email, title = 'Check your inbox', intro }: CheckEmailProps) {
  const [captcha, setCaptcha] = useState<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);
  const [showResend, setShowResend] = useState(false);
  const [sending, setSending] = useState(false);
  const [message, setMessage] = useState('');

  async function resend() {
    if (!captcha) return;
    setSending(true);
    try {
      setMessage(await resendVerification(email, captcha));
    } catch (err) {
      setMessage(getErrorMessage(err, 'Could not send a new link. Please try again.'));
    } finally {
      setSending(false);
      setCaptchaReset((n) => n + 1);
    }
  }

  return (
    <div className="signup-success" data-testid="check-email">
      <span className="signup-success__icon signup-success__icon--mail">
        <MailCheck size={30} />
      </span>
      <h2>{title}</h2>
      <p className="auth-card__subtitle">
        {intro ?? 'We sent a verification link to'} <strong>{email}</strong>. Open it within 24 hours to activate
        your account.
      </p>

      {showResend ? (
        <div className="resend-box">
          <RobotCheck resetKey={captchaReset} onSolved={setCaptcha} />
          <Button type="button" variant="ghost" wide disabled={!captcha} loading={sending} loadingText="Sending" onClick={resend}>
            <Send size={16} /> Send a new link
          </Button>
          {message && (
            <p className="resend-box__note" role="status">
              {message}
            </p>
          )}
        </div>
      ) : (
        <button type="button" className="link-button" onClick={() => setShowResend(true)}>
          Didn’t get it? Send a new link
        </button>
      )}

      <Link to="/login" className="button button--primary button--wide">
        Go to sign in <ArrowRight size={18} className="button__arrow" />
      </Link>
    </div>
  );
}
