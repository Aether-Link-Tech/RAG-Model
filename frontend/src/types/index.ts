// Mirrors UserResponse in backend/app/schemas/user.py
export interface User {
  id: string;
  email: string;
  role: string;
  created_at: string;
  is_deleted: boolean;
}

// Mirrors TokenResponse in backend/app/schemas/auth.py
export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export * from './document';
