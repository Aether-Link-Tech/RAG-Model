import axios from 'axios';
import { API_URL } from './config';

const api = axios.create({
  baseURL: API_URL,
});

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

export default api;
