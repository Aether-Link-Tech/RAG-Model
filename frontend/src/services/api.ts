import axios from 'axios';
import { API_URL } from './config';
import { session } from './session';

const api = axios.create({
  baseURL: API_URL,
});

// Attach the Bearer token to every request.
api.interceptors.request.use((config) => {
  const token = session.token;
  if (token) config.headers.Authorization = `Bearer ${token}`;
  return config;
});

// An expired or invalid token ends the session; the app redirects to /login.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const isLogin = String(error?.config?.url ?? '').includes('/auth/login');
    if (axios.isAxiosError(error) && error.response?.status === 401 && !isLogin && session.isAuthenticated()) {
      session.clear();
      window.dispatchEvent(new CustomEvent('auth:expired'));
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
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
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

export default api;
