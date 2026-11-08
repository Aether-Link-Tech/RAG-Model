import api from './api';
import { USE_MOCK } from './config';
import { mockTakenEmails, mockUser, wait } from './mockData';
import type { User } from '../types';

// POST /api/v1/users — 201 created, 409 duplicate email, 422 validation error
export async function signup(email: string, password: string): Promise<User> {
  if (USE_MOCK) {
    await wait(900);
    if (mockTakenEmails.includes(email.toLowerCase().trim())) {
      // Same shape as a real axios 409 error
      throw Object.assign(new Error('A user with this email already exists'), {
        isAxiosError: true,
        response: { status: 409, data: { detail: 'A user with this email already exists' } },
      });
    }
    return { ...mockUser, email };
  }

  const { data } = await api.post<User>('/users', { email, password });
  return data;
}
