// Demo data used when USE_MOCK is true
import type { User } from '../types';

export const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export const mockUser: User = {
  id: '00000000-0000-0000-0000-000000000001',
  email: 'demo@aetherlink.tech',
  role: 'user',
  created_at: new Date().toISOString(),
  is_deleted: false,
};

// Signing up with these emails returns the duplicate-email error
export const mockTakenEmails = ['demo@aetherlink.tech', 'taken@example.com'];
