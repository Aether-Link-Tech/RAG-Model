// Mirrors UserResponse in backend/app/schemas/user.py
export interface User {
  id: string;
  email: string;
  full_name?: string | null;
  role: 'admin' | 'user' | string;
  status?: 'pending' | 'active';
  created_at: string;
  is_deleted: boolean;
}

// Mirrors TokenResponse in backend/app/schemas/auth.py
export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

// Mirrors CaptchaChallenge in backend/app/schemas/auth.py (proof-of-work captcha)
export interface CaptchaChallenge {
  algorithm: 'SHA-256';
  challenge: string;
  maxnumber: number;
  salt: string;
  signature: string;
}

// Mirrors CollectionResponse in backend/app/api/v1/collections.py
export interface Collection {
  id: string;
  name: string;
  document_count: number;
}

// Mirrors AuditLogEntry / AuditLogPage in backend/app/api/v1/admin.py
export type AuditEventType = 'upload_rejected' | 'replace_rejected' | 'access_denied' | 'download_blocked';

export interface AuditLogEntry {
  id: string;
  user_id: string | null;
  event_type: AuditEventType;
  filename: string | null;
  reason: string;
  ip_address: string | null;
  created_at: string;
}

export interface AuditLogPage {
  items: AuditLogEntry[];
  total: number;
  limit: number;
  offset: number;
}

export * from './document';
