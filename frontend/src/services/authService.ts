import api from './api';
import { USE_MOCK } from './config';
import { mockTakenEmails, mockUser, wait } from './mockData';
import { session } from './session';
import type { TokenResponse, User } from '../types';

function axiosLikeError(status: number, detail: string): Error {
  // Same shape as a real axios error so the UI handles mock and real alike
  return Object.assign(new Error(detail), { isAxiosError: true, response: { status, data: { detail }, headers: {} } });
}

export interface SignupInput {
  fullName: string;
  email: string;
  password: string;
  captcha: string;
}

// POST /api/v1/auth/register — 201 (account "pending" + verification email), 409 duplicate,
// 400 captcha, 422 validation (FR-001)
export async function signup({ fullName, email, password, captcha }: SignupInput): Promise<User> {
  if (USE_MOCK) {
    await wait(900);
    if (mockTakenEmails.includes(email.toLowerCase().trim())) {
      throw axiosLikeError(409, 'An account with this email already exists.');
    }
    return { ...mockUser, email, full_name: fullName, status: 'pending' };
  }

  const { data } = await api.post<User>('/auth/register', {
    full_name: fullName.trim(),
    email: email.trim().toLowerCase(),
    password,
    captcha,
  });
  return data;
}

// POST /api/v1/auth/login — 200 token pair; 401 generic; 400 + X-Captcha-Required when a
// captcha is needed; 403 email not verified; 423 locked for 15 minutes (FR-002)
export async function login(email: string, password: string, captcha?: string): Promise<User> {
  if (USE_MOCK) {
    await wait(700);
    if (password.length < 8) throw axiosLikeError(401, 'Invalid email or password.');
    session.start('mock-token', { ...mockUser, email }, 3600, 'mock-refresh');
    return session.user!;
  }

  const { data } = await api.post<TokenResponse>('/auth/login', {
    email: email.trim().toLowerCase(),
    password,
    ...(captcha ? { captcha } : {}),
  });
  session.start(data.access_token, data.user, data.expires_in, data.refresh_token);
  return data.user;
}

// POST /api/v1/auth/logout — revokes the access and refresh tokens server-side (FR-003).
// The local session is cleared even if the server cannot be reached.
export async function logout(): Promise<void> {
  if (!USE_MOCK && session.isAuthenticated()) {
    try {
      await api.post('/auth/logout', { refresh_token: session.refreshToken });
    } catch {
      /* already expired or offline: local sign-out still happens */
    }
  }
  session.clear();
}

// POST /api/v1/auth/verify-email — 200 verified, 400 invalid/used, 410 expired
export async function verifyEmail(token: string): Promise<string> {
  if (USE_MOCK) {
    await wait(600);
    return 'Email verified. You can sign in now.';
  }
  const { data } = await api.post<{ detail: string }>('/auth/verify-email', { token });
  return data.detail;
}

// POST /api/v1/auth/resend-verification — always 202 with the same message
export async function resendVerification(email: string, captcha: string): Promise<string> {
  if (USE_MOCK) {
    await wait(600);
    return 'If an unverified account exists for this email, a new link is on its way.';
  }
  const { data } = await api.post<{ detail: string }>('/auth/resend-verification', {
    email: email.trim().toLowerCase(),
    captcha,
  });
  return data.detail;
}
