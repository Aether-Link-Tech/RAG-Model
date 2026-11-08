import api from './api';
import { USE_MOCK } from './config';
import { mockTakenEmails, mockUser, wait } from './mockData';
import { session } from './session';
import type { TokenResponse, User } from '../types';

function axiosLikeError(status: number, detail: string): Error {
  // Same shape as a real axios error so the UI handles mock and real alike
  return Object.assign(new Error(detail), { isAxiosError: true, response: { status, data: { detail } } });
}

// POST /api/v1/users — 201 created, 409 duplicate email, 422 validation error
export async function signup(email: string, password: string): Promise<User> {
  if (USE_MOCK) {
    await wait(900);
    if (mockTakenEmails.includes(email.toLowerCase().trim())) {
      throw axiosLikeError(409, 'A user with this email already exists');
    }
    return { ...mockUser, email };
  }

  const { data } = await api.post<User>('/users', { email, password });
  return data;
}

// POST /api/v1/auth/login — 200 with a Bearer token, 401 generic "Invalid email or password"
export async function login(email: string, password: string): Promise<User> {
  if (USE_MOCK) {
    await wait(700);
    if (password.length < 8) throw axiosLikeError(401, 'Invalid email or password');
    session.start('mock-token', { ...mockUser, email }, 3600);
    return session.user!;
  }

  const { data } = await api.post<TokenResponse>('/auth/login', {
    email: email.trim().toLowerCase(),
    password,
  });
  session.start(data.access_token, data.user, data.expires_in);
  return data.user;
}

// Ends the session in this browser. Server-side token revocation arrives with
// FR-003 (POST /auth/logout + Redis blacklist); the call is attempted and any
// "not implemented yet" response is ignored.
export async function logout(): Promise<void> {
  if (!USE_MOCK && session.isAuthenticated()) {
    try {
      await api.post('/auth/logout');
    } catch {
      /* endpoint not available yet (FR-003) */
    }
  }
  session.clear();
}
