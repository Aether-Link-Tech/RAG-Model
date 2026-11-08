// Demo data used only when VITE_USE_MOCK=true (no backend needed)
import type { Document, User } from '../types';

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

const now = Date.now();
export const mockDocuments: Document[] = [
  {
    id: 'd1', filename: 'Quarterly report.pdf', mime_type: 'application/pdf', file_size: 482_113,
    file_hash: 'a'.repeat(64), status: 'indexed', visibility: 'private', is_owner: true,
    user_id: mockUser.id, error_message: null, created_at: new Date(now - 86_400_000).toISOString(), updated_at: null,
  },
  {
    id: 'd2', filename: 'Employee handbook.docx', mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    file_size: 1_204_551, file_hash: 'b'.repeat(64), status: 'indexed', visibility: 'public', is_owner: false,
    user_id: null, error_message: null, created_at: new Date(now - 3 * 86_400_000).toISOString(), updated_at: null,
  },
  {
    id: 'd3', filename: 'Meeting notes.txt', mime_type: 'text/plain', file_size: 8_211,
    file_hash: 'c'.repeat(64), status: 'failed', visibility: 'private', is_owner: true,
    user_id: mockUser.id, error_message: 'No extractable text found in document', created_at: new Date(now - 3_600_000).toISOString(), updated_at: null,
  },
];
