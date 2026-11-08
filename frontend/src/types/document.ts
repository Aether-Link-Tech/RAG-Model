// Mirrors DocumentResponse in backend/app/schemas/document.py
export type DocumentStatus = 'pending' | 'processing' | 'indexed' | 'failed';

export type DocumentVisibility = 'private' | 'public';

export interface Document {
  id: string;
  filename: string;
  mime_type: string;
  file_size: number;
  file_hash: string;
  status: DocumentStatus;
  visibility: DocumentVisibility;
  is_owner: boolean;
  user_id: string | null; // null for other users' public documents (privacy)
  error_message: string | null;
  created_at: string;
  updated_at: string | null;
}
