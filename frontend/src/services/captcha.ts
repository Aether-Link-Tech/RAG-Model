// Google reCAPTCHA v2 loader (FR-001 / FR-002). The backend verifies the token
// server-side (services/captcha.py). Without VITE_RECAPTCHA_SITE_KEY (local dev) no
// widget is shown; a placeholder token is sent and the backend, which has no secret
// key in development, skips the check.
import { RECAPTCHA_SITE_KEY } from './config';

export const DEV_CAPTCHA_TOKEN = 'dev-no-recaptcha';

interface GreCaptcha {
  ready(cb: () => void): void;
  render(container: HTMLElement, params: Record<string, unknown>): number;
  reset(widgetId?: number): void;
}

declare global {
  interface Window {
    grecaptcha?: GreCaptcha;
    __onRecaptchaLoad?: () => void;
  }
}

let loader: Promise<GreCaptcha> | null = null;

export const recaptchaEnabled = (): boolean => Boolean(RECAPTCHA_SITE_KEY);

export function loadRecaptcha(): Promise<GreCaptcha> {
  if (window.grecaptcha?.render) return Promise.resolve(window.grecaptcha);
  loader ??= new Promise<GreCaptcha>((resolve, reject) => {
    window.__onRecaptchaLoad = () => resolve(window.grecaptcha!);
    const script = document.createElement('script');
    script.src = 'https://www.google.com/recaptcha/api.js?onload=__onRecaptchaLoad&render=explicit';
    script.async = true;
    script.defer = true;
    script.onerror = () => {
      loader = null;
      reject(new Error('The captcha could not be loaded. Check your connection.'));
    };
    document.head.appendChild(script);
  });
  return loader;
}
