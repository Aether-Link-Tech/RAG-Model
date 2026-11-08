import axios, { type AxiosRequestConfig } from 'axios';
import { API_URL } from './config';
import { session } from './session';
import type { TokenResponse } from '../types';

const api = axios.create({
  baseURL: API_URL,
});

// Endpoints that must never trigger a silent token refresh.
const AUTH_PATHS = ['/auth/login', '/auth/refresh', '/auth/logout', '/auth/register', '/auth/captcha'];
const isAuthCall = (url: unknown) => AUTH_PATHS.some((path) => String(url ?? '').includes(path));

let refreshing: Promise<string | null> | null = null;

/** Exchange the refresh token for a new pair once, shared by concurrent requests. */
export function refreshSession(): Promise<string | null> {
  const refreshToken = session.refreshToken;
  if (!refreshToken) return Promise.resolve(null);
  refreshing ??= axios
    .post<TokenResponse>(`${API_URL}/auth/refresh`, { refresh_token: refreshToken })
    .then(({ data }) => {
      session.start(data.access_token, data.user, data.expires_in, data.refresh_token);
      return data.access_token;
    })
    .catch(() => null)
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

// Attach the Bearer token to every request; renew it first if it already expired.
api.interceptors.request.use(async (config) => {
  let token = session.token;
  if (!token && session.refreshToken && !isAuthCall(config.url)) token = await refreshSession();
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

type RetriableConfig = AxiosRequestConfig & { _retried?: boolean };

// A 401 first tries one refresh; if that fails the session ends and the app goes to /login.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = (error?.config ?? {}) as RetriableConfig;
    if (axios.isAxiosError(error) && error.response?.status === 401 && !isAuthCall(config.url)) {
      if (!config._retried && session.refreshToken) {
        config._retried = true;
        const token = await refreshSession();
        if (token) {
          config.headers = { ...(config.headers ?? {}), Authorization: `Bearer ${token}` };
          return api.request(config);
        }
      }
      if (session.isAuthenticated()) {
        session.clear();
        window.dispatchEvent(new CustomEvent('auth:expired'));
      }
    }
    return Promise.reject(error);
  },
);

// Converts any error (incl. FastAPI { detail }) into a user-friendly message
export function getErrorMessage(error: unknown, fallback = 'Something went wrong. Please try again.'): string {
  if (axios.isAxiosError(error)) {
    if (!error.response) {
      return 'Cannot reach the server. Make sure the backend is running.';
    }
    const detail = (error.response.data as { detail?: unknown })?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg).replace(/^Value error, /, '');
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export function hasStatus(error: unknown, status: number): boolean {
  return axios.isAxiosError(error) && error.response?.status === status;
}

export function statusOf(error: unknown): number | undefined {
  return axios.isAxiosError(error) ? error.response?.status : undefined;
}

/** True when the backend asks for a captcha before the next login attempt (FR-002). */
export function captchaRequired(error: unknown): boolean {
  if (!axios.isAxiosError(error)) return false;
  const headers = error.response?.headers as Record<string, string> | undefined;
  return headers?.['x-captcha-required'] === 'true';
}

export default api;
