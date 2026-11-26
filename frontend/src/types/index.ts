// Mirrors UserResponse in backend/app/schemas/user.py
export interface User {
  id: string;
  email: string;
  full_name?: string | null;
  role: 'admin' | 'user' | string;
  is_verified?: boolean; // FR-001: false until the email link is used
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
export type AuditEventType =
  | 'upload_rejected'
  | 'replace_rejected'
  | 'access_denied'
  | 'download_blocked'
  | 'document_published'
  | 'ocr_image_skipped';

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

// FR-076 admin per-user health (GET /api/v1/admin/users/health, AL-272).
// Metrics are null when the health endpoint isn't deployed and the page falls back to /admin/users.
export type AccountStatus = 'active' | 'quarantined';

export interface UserHealth {
  id: string;
  email: string;
  full_name: string | null;
  role: string;
  status: AccountStatus | null;
  quarantined: boolean;
  document_count: number | null;
  storage_bytes: number | null;
  last_activity: string | null;
}

export interface UserHealthReport {
  users: UserHealth[];
  metricsAvailable: boolean;
}

export * from './document';
export * from './chat';
